"""L4 审批 API — 审批队列 / 预览 / 批准 / 驳回 / 撤回

接口 (前 缀 /api, 全部需登录, JWT 由 auth_middleware 校验):
    GET  /api/l4/queue                审批人队列 (admin 看全部, leader 看指派给自己的)
    GET  /api/l4/{doc_id}/preview     待审文档前 1200 字预览 (仅该单审批人/admin; 审计落库)
    POST /api/l4/{doc_id}/approve     批准入库 (提交后台线程池, 台账回 queued)
    POST /api/l4/{doc_id}/reject      驳回 {reason 必填} (删文件+删台账, 不登记哈希)
    POST /api/l4/{doc_id}/cancel      上传者撤回 (仅 owner 本人; 删文件+删台账, 通知审批人)

设计纪律:
  - 权限判定集中在 can_act(): 该单审批人本人 或 admin/boss。撤回只认 owner。
  - 决策走 l4_approvals.decide() 的原子 UPDATE(WHERE status='pending'),
    并发双击/两人同时批 → 只有一人成功, 另一人 409。
  - 驳回**不 save_hash** → 同一文件改完重传不会被 409 挡住, 再走一遍审批。
  - 通知全部 fire-and-forget (l4_notify), 一切异常吞掉, 绝不影响接口返回。
  - 批准后的入库复用 upload_worker 的线程池任务格式, 与直接上传 L1-L3 完全同链路。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.acl.identity import Identity
from app.core.deps import get_current_identity
from app.services import audit, l4_approvals, l4_notify
from app.services.upload_tasks import (
    STATUS_QUEUED,
    get_task_store,
)
from app.services.upload_worker import get_pool

logger = logging.getLogger(__name__)
router = APIRouter()

# 原生可解析路由(能在线预览); OCR 类只给提示不给文本
_NATIVE_ROUTES = {"direct_pdf", "docx", "text", "tabular", "pptx"}
_OCR_ROUTES = {"scanned_pdf", "image"}

_PREVIEW_CHARS = 1200


class RejectBody(BaseModel):
    """驳回请求体: reason 必填(非空), 会原文通知上传者"""
    reason: str


def can_act(ident: Identity, row: dict[str, Any]) -> bool:
    """审批动作权限: 该单审批人本人, 或 admin/boss(管理视野放行)"""
    return bool(ident.admin or ident.boss
                or ident.userid == row["approver_userid"])


def _load(doc_id: str) -> dict[str, Any]:
    row = l4_approvals.get(doc_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"审批单不存在: {doc_id}")
    return row


def _acl_of(row: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(row.get("acl") or "{}")
    except json.JSONDecodeError:
        return {}


def _notify_owner(row: dict[str, Any], result_text: str) -> None:
    """审批结果通知上传者 (通过/驳回都发, 线程吞错)"""
    l4_notify.send_async(
        [row["owner_userid"]], "L4 审批结果",
        f"你提交的《{row['filename']}》{result_text}",
    )


@router.get("/l4/queue")
async def l4_queue(ident: Identity = Depends(get_current_identity)):
    """审批队列: leader 看指派给自己的单, admin/boss 看全部。含 status 字段。"""
    rows = l4_approvals.list_for_approver(ident.userid,
                                          is_admin=bool(ident.admin or ident.boss))
    out = []
    for r in rows:
        acl = _acl_of(r)
        out.append({
            "doc_id": r["doc_id"],
            "filename": r["filename"],
            "size": r["size"],
            "route": r["route"],
            "status": r["status"],
            "owner_userid": r["owner_userid"],
            "owner_name": l4_approvals.user_name(r["owner_userid"]),
            "approver_userid": r["approver_userid"],
            "approver_name": l4_approvals.user_name(r["approver_userid"]),
            "created_at": r["created_at"],
            "decided_at": r["decided_at"],
            "decision_reason": r["decision_reason"],
            "acl_summary": {
                "level": acl.get("level", "L4"),
                "visible_count": len(acl.get("extra_acl") or []),
            },
        })
    return {"items": out, "count": len(out)}


@router.get("/l4/{doc_id}/preview")
async def l4_preview(doc_id: str, ident: Identity = Depends(get_current_identity)):
    """待审文档预览 (前 1200 字)。仅该单审批人/admin。每次查看落审计。"""
    row = _load(doc_id)
    if not can_act(ident, row):
        raise HTTPException(status_code=403, detail="仅该文档的审批人或管理员可预览")

    audit.record(actor=ident.userid, action="l4_preview", target=doc_id,
                 detail={"filename": row["filename"], "owner": row["owner_userid"]})

    base = {
        "filename": row["filename"],
        "owner_name": l4_approvals.user_name(row["owner_userid"]),
        "size": row["size"],
        "created_at": row["created_at"],
    }
    if row["route"] in _OCR_ROUTES:
        return {**base, "ok": False, "text": "",
                "note": "扫描件暂不支持在线预览, 批准即视为同意入库"}
    if row["route"] not in _NATIVE_ROUTES:
        return {**base, "ok": False, "text": "",
                "note": f"该类型({row['route']})暂不支持在线预览"}

    file_path = Path(row["file_path"])
    if not file_path.is_file():
        return {**base, "ok": False, "text": "", "note": "文件已不存在, 无法预览"}
    try:
        if row["route"] == "tabular":
            from app.services.tabular_parser import parse_tabular
            parsed = parse_tabular(file_path)
        elif row["route"] == "pptx":
            from app.services.pptx_parser import parse_pptx
            parsed = parse_pptx(file_path)
        else:  # direct_pdf / docx / text → parser 统一入口
            from app.services.parser import parse_file
            parsed = parse_file(file_path, doc_id=doc_id)
        text = (parsed.content or "").strip()[:_PREVIEW_CHARS]
    except Exception as exc:  # noqa: BLE001 — 预览失败不阻断审批流
        logger.warning("L4 预览解析失败 doc_id=%s: %s", doc_id, exc)
        return {**base, "ok": False, "text": "", "note": f"预览解析失败: {exc}"}
    return {**base, "ok": True, "text": text}


@router.post("/l4/{doc_id}/approve")
async def l4_approve(doc_id: str, ident: Identity = Depends(get_current_identity)):
    """批准入库: 审批单 pending→approved, 台账 pending_approval→queued, 提交线程池。"""
    row = _load(doc_id)
    if not can_act(ident, row):
        raise HTTPException(status_code=403, detail="仅该文档的审批人或管理员可批准")

    decided = l4_approvals.decide(doc_id, "approved", decided_by=ident.userid)
    if decided is None:
        raise HTTPException(status_code=409, detail="该审批单已被处理, 不能重复决策")

    # ---- 复用上传链路: 先确认台账与文件都还在, 再回 queued 提交线程池 ----
    store = get_task_store()
    task = store.get(doc_id)
    file_path = Path(row["file_path"])
    if task is None or not file_path.is_file():
        # 单据已 consumed 但入库前置条件坏掉 — 保留 approved 决定并如实报错,
        # 上传者需重新上传 (不做半途提交)
        logger.error("L4 批准失败: 台账或文件缺失 doc_id=%s task=%s file=%s",
                     doc_id, task is not None, file_path.is_file())
        raise HTTPException(
            status_code=500,
            detail="批准已记录, 但待入库文件或任务台账缺失, 请让上传者重新上传",
        )

    acl = _acl_of(row)
    store.update(doc_id, status=STATUS_QUEUED, reason="")
    get_pool().submit({
        "doc_id": doc_id,
        "file_path": str(file_path),
        "route": row["route"],
        "file_hash": row["file_hash"],
        "filename": row["filename"],
        "acl": acl,
    })

    audit.record(actor=ident.userid, action="l4_approve", target=doc_id,
                 detail={"filename": row["filename"], "owner": row["owner_userid"]})
    _notify_owner(decided, "已通过审批, 正在入库")
    logger.info("L4 approved: doc_id=%s by=%s", doc_id, ident.userid)
    return {"doc_id": doc_id, "status": "approved", "task_status": STATUS_QUEUED}


@router.post("/l4/{doc_id}/reject")
async def l4_reject(doc_id: str, body: RejectBody,
                    ident: Identity = Depends(get_current_identity)):
    """驳回: pending→rejected; 删落盘文件 + 删台账 + 不登记哈希(重传仍可)。"""
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="驳回必须填写 reason")
    row = _load(doc_id)
    if not can_act(ident, row):
        raise HTTPException(status_code=403, detail="仅该文档的审批人或管理员可驳回")

    decided = l4_approvals.decide(doc_id, "rejected", decided_by=ident.userid,
                                  reason=reason)
    if decided is None:
        raise HTTPException(status_code=409, detail="该审批单已被处理, 不能重复决策")

    _discard_file(row)

    audit.record(actor=ident.userid, action="l4_reject", target=doc_id,
                 detail={"filename": row["filename"], "owner": row["owner_userid"],
                         "reason": reason})
    _notify_owner(decided, f"被驳回: {reason}")
    logger.info("L4 rejected: doc_id=%s by=%s reason=%s", doc_id, ident.userid, reason)
    return {"doc_id": doc_id, "status": "rejected"}


@router.post("/l4/{doc_id}/cancel")
async def l4_cancel(doc_id: str, ident: Identity = Depends(get_current_identity)):
    """上传者撤回: 仅 owner 本人; 删文件 + 删台账 + 通知审批人。"""
    row = _load(doc_id)
    if ident.userid != row["owner_userid"]:
        raise HTTPException(status_code=403, detail="只有文档上传者本人可撤回")

    decided = l4_approvals.decide(doc_id, "cancelled", decided_by=ident.userid)
    if decided is None:
        raise HTTPException(status_code=409, detail="该审批单已被处理, 不能撤回")

    _discard_file(row)

    audit.record(actor=ident.userid, action="l4_cancel", target=doc_id,
                 detail={"filename": row["filename"]})
    l4_notify.send_async(
        [row["approver_userid"]], "L4 审批已撤回",
        f"{row['filename']} 的 L4 审批单已被上传者撤回, 无需处理",
    )
    logger.info("L4 cancelled: doc_id=%s by=%s", doc_id, ident.userid)
    return {"doc_id": doc_id, "status": "cancelled"}


def _discard_file(row: dict[str, Any]) -> None:
    """驳回/撤回的文件清理: 删落盘文件 + 删任务台账。哈希从未登记, 重传不受 409 影响。"""
    try:
        Path(row["file_path"]).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("L4 删除落盘文件失败 doc_id=%s: %s", row["doc_id"], exc)
    get_task_store().remove(row["doc_id"])
