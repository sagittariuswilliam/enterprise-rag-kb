"""#5 扫描件校对队列 API — 待校对列表 / 识别稿查看 / 批准入库 / 驳回

接口 (前缀 /api, 全部需登录, JWT 由 auth_middleware 校验):
    GET  /api/review/queue              校对队列 (admin/boss 全部; leader 本部门; 普通员工 403)
    GET  /api/review/overview           {pending: N} 概览 KPI (权限同上)
    GET  /api/review/{doc_id}           详情含 OCR 识别稿全文 (有权者可看; 审计 review_view)
    POST /api/review/{doc_id}/approve   批准入库 {text 可选编辑稿} (同步切块入库+登记哈希+删文件)
    POST /api/review/{doc_id}/reject    驳回 {reason 必填} (删文件+台账移除, 不登记哈希)

设计纪律 (口径抄 app/api/l4.py):
  - 权限: 谁能看谁能批 (admin/boss ∪ owner 本人 ∪ owner 同部门 leader);
    reject 收紧为 admin/boss ∪ owner。queue/overview 普通员工 403。
  - 决策走 review_queue.decide() 原子 UPDATE(WHERE status='pending'),
    并发双击 → 只有一人成功, 另一人 409。
  - 批准时才正式入库: chunk_document(ParsedDoc(编辑稿)) → index_chunks(原 acl)
    → save_hash(此后重传 409 生效) → 台账置 ingested → 删落盘文件
    (与 L4 批准后入库链路一致: 入库成功即由流程消耗文件)。
  - 驳回不 save_hash → 同一文件重传不会被 409 挡住, 可再走一遍 OCR。
    台账处理照 l4 reject (_discard: 删文件 + remove 台账)。
  - 审计 actor 用 "姓名(userid)" 格式 (口径同 docs.py doc_view)。
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
from app.dingtalk.store import get_conn
from app.services import audit
from app.services import review_queue
from app.services.chunker import chunk_document

logger = logging.getLogger(__name__)
router = APIRouter()


class ApproveBody(BaseModel):
    """批准请求体: text 可选(人工编辑后的全文), 缺省用 OCR 识别稿原文"""
    text: Optional[str] = None


class RejectBody(BaseModel):
    """驳回请求体: reason 必填(非空)"""
    reason: str


# ---------------------------------------------------------------- 权限

def _user_depts(conn, userid: str) -> set[int]:
    row = conn.execute(
        "SELECT dept_ids FROM users WHERE userid=?", (userid,)
    ).fetchone()
    if not row:
        return set()
    try:
        return set(json.loads(row["dept_ids"] or "[]"))
    except json.JSONDecodeError:
        return set()


def _owner_depts(conn, owner_userid: str) -> set[int]:
    return _user_depts(conn, owner_userid)


def visible_rows(ident: Identity, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """队列视野裁剪: admin/boss 全部; leader 只看本部门上传者; 其他空。
    返回 None 语义由调用方用 is_manager/is_leader 先行判定。"""
    conn = get_conn()
    try:
        my_depts = set(ident.dept_ids or [])
        out = []
        owner_dept_cache: dict[str, set[int]] = {}
        for r in rows:
            owner = r.get("owner_userid") or ""
            if owner == ident.userid:
                out.append(r)
                continue
            if owner not in owner_dept_cache:
                owner_dept_cache[owner] = _owner_depts(conn, owner)
            if owner_dept_cache[owner] & my_depts:
                out.append(r)
        return out
    finally:
        conn.close()


def can_view(ident: Identity, row: dict[str, Any]) -> bool:
    """谁能看这份识别稿: admin/boss / owner 本人 / owner 同部门 leader。
    (口径: 谁有权看谁有权批 — approve 复用; reject 另收紧)"""
    if ident.admin or ident.boss or ident.userid == row["owner_userid"]:
        return True
    if not ident.leader:
        return False
    conn = get_conn()
    try:
        return bool(_owner_depts(conn, row["owner_userid"])
                    & set(ident.dept_ids or []))
    finally:
        conn.close()


def _load(doc_id: str) -> dict[str, Any]:
    row = review_queue.get(doc_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"校对单不存在: {doc_id}")
    return row


def _acl_of(row: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(row.get("acl") or "{}")
    except json.JSONDecodeError:
        return {}


def _issues_of(row: dict[str, Any]) -> list[Any]:
    try:
        v = json.loads(row.get("issues") or "[]")
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------- 查询

@router.get("/review/queue")
async def review_queue_list(ident: Identity = Depends(get_current_identity)):
    """校对队列: admin/boss 全部, leader 本部门上传者; 普通员工 403"""
    if not (ident.admin or ident.boss or ident.leader):
        raise HTTPException(status_code=403, detail="仅管理员或部门负责人可查看校对队列")
    rows = review_queue.list_all()
    if not (ident.admin or ident.boss):
        rows = visible_rows(ident, rows)
    out = []
    for r in rows:
        out.append({
            "doc_id": r["doc_id"],
            "filename": r["filename"],
            "owner_userid": r["owner_userid"],
            "owner_name": review_queue.user_name(r["owner_userid"]),
            "pages": r["pages"],
            "route": r["route"],
            "size": r["size"],
            "issues": _issues_of(r),
            "status": r["status"],
            "created_at": r["created_at"],
            "decided_at": r["decided_at"],
            "decision_note": r["decision_note"],
        })
    return {"items": out, "count": len(out)}


@router.get("/review/overview")
async def review_overview(ident: Identity = Depends(get_current_identity)):
    """待校对数量 (管理概览 KPI)。leader 只数自己视野内的。"""
    if not (ident.admin or ident.boss or ident.leader):
        raise HTTPException(status_code=403, detail="仅管理员或部门负责人可查看校对概览")
    rows = [r for r in review_queue.list_all()
            if r["status"] == review_queue.STATUS_PENDING]
    if not (ident.admin or ident.boss):
        rows = visible_rows(ident, rows)
    return {"pending": len(rows)}


@router.get("/review/{doc_id}")
async def review_detail(doc_id: str,
                        ident: Identity = Depends(get_current_identity)):
    """详情: OCR 识别稿全文 + 信号。有权查看者可看; 每次查看落审计。"""
    row = _load(doc_id)
    if not can_view(ident, row):
        raise HTTPException(status_code=403, detail="仅上传者本人、本部门负责人或管理员可查看")
    audit.record(actor=f"{ident.name}({ident.userid})",
                 action="review_view", target=doc_id,
                 detail={"filename": row["filename"], "owner": row["owner_userid"]})
    return {
        "doc_id": row["doc_id"],
        "filename": row["filename"],
        "owner_userid": row["owner_userid"],
        "owner_name": review_queue.user_name(row["owner_userid"]),
        "pages": row["pages"],
        "route": row["route"],
        "size": row["size"],
        "status": row["status"],
        "issues": _issues_of(row),
        "suggested_text": row["suggested_text"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
        "decision_note": row["decision_note"],
        "acl": _acl_of(row),
    }


# ---------------------------------------------------------------- 决策

@router.post("/review/{doc_id}/approve")
async def review_approve(doc_id: str, body: ApproveBody | None = None,
                         ident: Identity = Depends(get_current_identity)):
    """批准入库: pending→approved(原子); 用编辑稿/识别稿重切入库; 登记哈希; 删文件。

    返回 {status:"approved", chunks:N}
    """
    row = _load(doc_id)
    if not can_view(ident, row):
        raise HTTPException(status_code=403, detail="仅上传者本人、本部门负责人或管理员可批准")

    text = ((body.text if body else None) or row["suggested_text"] or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="入库文本为空, 无法批准")

    decided = review_queue.decide(doc_id, "approved",
                                  decided_by=ident.userid, note="")
    if decided is None:
        raise HTTPException(status_code=409, detail="该校对单已被处理, 不能重复决策")

    # ---- 入库 (chunk_document 走 ParsedDoc 主入口, 与自动链路同切块口径) ----
    from app.config import settings
    from app.services.parser import ParsedDoc
    from app.services.retriever import index_chunks
    from app.services.hash_registry import save_hash
    from app.services.upload_tasks import STATUS_INGESTED, get_task_store

    parsed = ParsedDoc(content=text, metadata={
        "filename": row["filename"],
        "format": "reviewed",
        "parse_method": "review",
    })
    chunks = chunk_document(parsed, chunk_size=settings.chunk_size,
                            overlap=settings.chunk_overlap)
    if not chunks:
        # 决策已落但没能切出块 — 如实报错, 单据保持 approved(不重复入库),
        # 文件保留, 由人工改用文本或走驳回重传
        logger.error("review approve 切块为 0: doc_id=%s", doc_id)
        raise HTTPException(status_code=500, detail="批准已记录, 但文本未能切块, 请检查内容")

    acl = _acl_of(row)
    # 与 worker 同 loop 语义: 本接口是 async handler, 直接 await
    try:
        indexed = await index_chunks(chunks, doc_id, acl=acl)
    except Exception as exc:  # noqa: BLE001
        logger.exception("review approve 入库失败: doc_id=%s", doc_id)
        raise HTTPException(status_code=500, detail=f"入库失败: {exc}")
    if indexed == 0:
        raise HTTPException(status_code=500, detail="入库返回 0 条, 请检查 Qdrant 连接")

    # ---- 正式入库完成: 登记哈希(重传 409 从此刻生效) + 台账 + 删文件 ----
    save_hash(row["file_hash"], row["filename"])
    store = get_task_store()
    if store.get(doc_id) is not None:
        store.update(doc_id, status=STATUS_INGESTED, chunks=indexed, reason="",
                     format="reviewed")
    # 删法与 L4 链路一致: 入库成功后落盘文件即被消耗 (worker 成功路径同样不删,
    # 由台账/清理链路处理); 校对批准在 API 内即时 unlink, 失败不影响入库结果。
    try:
        Path(row["file_path"]).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("review approve 删除落盘文件失败 doc_id=%s: %s", doc_id, exc)

    audit.record(actor=f"{ident.name}({ident.userid})",
                 action="review_approve", target=doc_id,
                 detail={"filename": row["filename"], "owner": row["owner_userid"],
                         "chunks": indexed, "edited": bool((body.text if body else None))})
    logger.info("review approved+ingested: doc_id=%s by=%s chunks=%d",
                doc_id, ident.userid, indexed)
    return {"doc_id": doc_id, "status": "approved", "chunks": indexed}


@router.post("/review/{doc_id}/reject")
async def review_reject(doc_id: str, body: RejectBody,
                        ident: Identity = Depends(get_current_identity)):
    """驳回: pending→rejected; 删落盘文件 + 台账移除 + 不登记哈希(重传仍可)。

    权限收紧: 仅 admin/boss 或 owner 本人 (leader 可看可批不可驳)。
    """
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="驳回必须填写 reason")
    row = _load(doc_id)
    if not (ident.admin or ident.boss or ident.userid == row["owner_userid"]):
        raise HTTPException(status_code=403, detail="仅上传者本人或管理员可驳回")

    decided = review_queue.decide(doc_id, "rejected",
                                  decided_by=ident.userid, note=reason)
    if decided is None:
        raise HTTPException(status_code=409, detail="该校对单已被处理, 不能重复决策")

    _discard_file(row)

    audit.record(actor=f"{ident.name}({ident.userid})",
                 action="review_reject", target=doc_id,
                 detail={"filename": row["filename"], "owner": row["owner_userid"],
                         "reason": reason})
    logger.info("review rejected: doc_id=%s by=%s reason=%s", doc_id, ident.userid, reason)
    return {"doc_id": doc_id, "status": "rejected"}


def _discard_file(row: dict[str, Any]) -> None:
    """驳回的文件清理: 删落盘文件 + 移除任务台账(照 l4 reject 口径)。
    哈希从未登记, 重传不受 409 影响。"""
    try:
        Path(row["file_path"]).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("review 删除落盘文件失败 doc_id=%s: %s", row["doc_id"], exc)
    from app.services.upload_tasks import get_task_store
    get_task_store().remove(row["doc_id"])
