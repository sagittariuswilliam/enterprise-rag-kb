"""/api/package* — P4 招投标打包导出 API (16 号 V1.1 §5)

接口 (前缀 /api, 全部需登录):
  POST /api/package/propose          {need_text} → AI 草稿清单 (不落库, 可反复试探)
  POST /api/package/confirm          {title, need_text, doc_ids[]} → 落库+拆单+通知
  POST /api/package/{id}/cancel      发起人撤回
  GET  /api/package/mine             我的包列表
  GET  /api/pkg-approval/queue       我作为负责人的待审子单
  GET  /api/pkg-approval/{sub_id}/preview   子单内某文档预览 (仅该子单审批人/admin)
  POST /api/pkg-approval/{sub_id}/approve    批准子单
  POST /api/pkg-approval/{sub_id}/reject     驳回子单 {reason 必填}
  GET  /api/admin/packages           管理端全量

设计纪律:
  - propose 是 Q3 标题级例外的唯一出口: 只回元数据+命中词, 绝不回正文;
    每次调用落审计 (action=package_propose)。
  - confirm 元数据服务端补齐 (get_docs_meta), 不信客户端 level/dept;
    L4 / 他人 L1 / 不存在 → 400 拒绝 (审计记录被拒 doc_id)。
  - approve/reject 权限 = 子单 approver 本人或 boss/admin (同 L4 can_act);
    decide 原子防双击 → 409。
  - 定稿后通知发起人链接 (含 token); 包全驳回也通知。
  - 通知 fire-and-forget, 不影响返回。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.acl.identity import Identity
from app.core.deps import get_current_identity, require_gray_open
from app.services import audit
from app.services import package_proposer
from app.services import packages as pkg_store
from app.services import pkg_notify

logger = logging.getLogger(__name__)
router = APIRouter()

# 灰度闸门用 chat.py/upload.py 同款【函数体内显式调用】require_gray_open(ident) 模式。
# 禁用 router dependencies=[Depends(...)] 参数级注入: 本环境 FastAPI 会把依赖函数
# 参数并进 body schema, 导致 POST 请求体解析 422 (2026-10-21 M1 实测踩坑)。


class ProposeBody(BaseModel):
    need_text: str


class ConfirmBody(BaseModel):
    title: str = ""
    need_text: str
    doc_ids: list[str]


class RejectBody(BaseModel):
    reason: str


def _ident_dict(ident: Identity) -> dict[str, Any]:
    return {
        "userid": ident.userid, "dept_ids": list(ident.dept_ids),
        "leader": ident.leader, "admin": ident.admin, "boss": ident.boss,
    }


# ---------------------------------------------------------------- 员工端


@router.post("/package/propose")
async def package_propose(body: ProposeBody, ident: Identity = Depends(get_current_identity)):
    require_gray_open(ident)
    need = (body.need_text or "").strip()
    if not need:
        raise HTTPException(status_code=400, detail="请先描述需求")
    if len(need) > 1000:
        raise HTTPException(status_code=400, detail="需求描述过长(≤1000字)")
    docs = await package_proposer.propose_docs(need, ident.userid)
    # 敏感动作审计: 谁通过组清单看过哪些标题 (Q3 补偿防线)
    audit.record(
        actor=f"{ident.name}({ident.userid})",
        action="package_propose",
        target="-",
        detail={"need_text": need[:200],
                "returned": len(docs),
                "doc_ids": [d["doc_id"] for d in docs][:50]},
    )
    return {
        "items": [
            {k: d[k] for k in ("doc_id", "source", "level", "dept_id", "hit_reason")}
            for d in docs
        ]
    }


@router.post("/package/confirm")
async def package_confirm(body: ConfirmBody, ident: Identity = Depends(get_current_identity)):
    require_gray_open(ident)
    need = (body.need_text or "").strip()
    doc_ids = list(dict.fromkeys(body.doc_ids or []))
    if not doc_ids:
        raise HTTPException(status_code=400, detail="清单为空")
    if len(doc_ids) > pkg_store.MAX_DOCS_LIMIT:
        raise HTTPException(status_code=400, detail="单次打包文档数超限")
    # 元数据服务端补齐 — 客户端传的 level/dept 一律不信 (Q3 边界)
    try:
        meta = await package_proposer.get_docs_meta(doc_ids)
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=502, detail="文档存储暂时不可用")
    docs: list[dict[str, Any]] = []
    rejected: list[str] = []
    for did in doc_ids:
        m = meta.get(did)
        if m is None or m["level"] not in ("L1", "L2", "L3"):
            # 不存在 / L4 / 脏数据 → 拒绝
            rejected.append(did)
            continue
        if m["level"] == "L1" and m["owner_userid"] != ident.userid:
            rejected.append(did)
            continue
        docs.append({"doc_id": did, "level": m["level"],
                     "dept_id": m["dept_id"], "owner_userid": m["owner_userid"]})
    if rejected:
        audit.record(actor=f"{ident.name}({ident.userid})",
                     action="package_confirm_rejected_ids",
                     target="-", detail={"doc_ids": rejected})
    if not docs:
        raise HTTPException(status_code=400, detail="清单内没有可打包的文档")

    title = (body.title or "").strip() or need[:30]
    try:
        result = pkg_store.create_package(
            title=title, need_text=need[:1000], creator=ident.userid,
            creator_ident=_ident_dict(ident), docs=docs,
        )
    except LookupError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    audit.record(actor=f"{ident.name}({ident.userid})",
                 action="package_confirm", target=result["package"]["package_id"],
                 detail={"title": title, "docs": len(docs),
                         "suborders": len(result["suborders"])})

    # 通知: 子单 → 各审批人; 全免审直接定稿 → 发起人链接
    for sub in result["suborders"]:
        pkg_notify.send_async(
            [sub["approver"]], "打包审批",
            f"「{title}」需要你审批 {sub['dept_name']} 的 {len(sub['doc_ids'])} 份文档。"
            f"请打开管理控制台「打包审批」处理。",
            sub_id=sub["id"],
        )
    if result["no_approval"]:
        pkg = result["package"]
        _notify_creator_finalized(pkg)

    return {
        "package_id": result["package"]["package_id"],
        "status": result["package"]["status"],
        "suborders": [
            {"id": s["id"], "dept_name": s["dept_name"],
             "approver": s["approver"], "doc_count": len(s["doc_ids"])}
            for s in result["suborders"]
        ],
        "exempt_count": len(result["exempt"]),
    }


@router.post("/package/{package_id}/cancel")
async def package_cancel(package_id: str, ident: Identity = Depends(get_current_identity)):
    require_gray_open(ident)
    row = pkg_store.cancel_package(package_id, ident.userid)
    if row is None:
        pkg = pkg_store.get_package(package_id)
        if pkg is None:
            raise HTTPException(status_code=404, detail="包不存在")
        if pkg["creator"] != ident.userid:
            raise HTTPException(status_code=403, detail="只能撤回自己发起的包")
        raise HTTPException(status_code=409, detail="该包已定稿或已撤回, 无法撤回")
    audit.record(actor=f"{ident.name}({ident.userid})",
                 action="package_cancel", target=package_id, detail={})
    return {"ok": True}


@router.get("/package/mine")
async def package_mine(ident: Identity = Depends(get_current_identity)):
    require_gray_open(ident)
    rows = pkg_store.list_mine(ident.userid)
    out = []
    for r in rows:
        subs = pkg_store.list_suborders(r["package_id"])
        pending = [s for s in subs if s["status"] == pkg_store.SUB_PENDING]
        out.append({
            "package_id": r["package_id"],
            "title": r["title"],
            "status": r["status"],
            "doc_count": len(json.loads(r["doc_ids"] or "[]")),
            "pending_depts": [s["dept_name"] for s in pending],
            "token": r["token"] if r["status"] == pkg_store.PKG_APPROVED else None,
            "expires_at": r["expires_at"],
            "created_at": r["created_at"],
        })
    return {"items": out}


# ---------------------------------------------------------------- 审批端


def _load_sub(sub_id: int) -> dict[str, Any]:
    conn_row = _sub_raw(sub_id)
    if conn_row is None:
        raise HTTPException(status_code=404, detail=f"子单不存在: {sub_id}")
    return conn_row


def _sub_raw(sub_id: int) -> dict[str, Any] | None:
    from app.dingtalk.store import get_conn
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM pkg_approvals WHERE id=?", (sub_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _can_act_sub(ident: Identity, sub: dict[str, Any]) -> bool:
    return bool(ident.admin or ident.boss or ident.userid == sub["approver"])


@router.get("/pkg-approval/queue")
async def pkg_approval_queue(ident: Identity = Depends(get_current_identity)):
    n = pkg_store.reassign_dead_approvers()
    if n:
        logger.info("打包子单离职改派 %d 张", n)
    rows = pkg_store.list_queue(ident.userid, bool(ident.admin or ident.boss))
    return {"items": rows, "count": len(rows)}


@router.post("/pkg-approval/{sub_id}/approve")
async def pkg_approval_approve(sub_id: int, ident: Identity = Depends(get_current_identity)):
    sub = _load_sub(sub_id)
    if not _can_act_sub(ident, sub):
        raise HTTPException(status_code=403, detail="只有该子单审批人可操作")
    if sub["status"] != pkg_store.SUB_PENDING:
        raise HTTPException(status_code=409, detail="该子单已决出")
    row = pkg_store.decide_suborder(sub_id, pkg_store.SUB_APPROVED, ident.userid)
    if row is None:
        raise HTTPException(status_code=409, detail="该子单已被决出(并发)")
    audit.record(actor=f"{ident.name}({ident.userid})",
                 action="pkg_approval_approve", target=f"sub:{sub_id}",
                 detail={"package_id": sub["package_id"]})
    pkg = pkg_store.get_package(sub["package_id"])
    _maybe_notify_finalized(pkg, sub["package_id"])
    return {"ok": True, "package_status": pkg["status"] if pkg else ""}


@router.post("/pkg-approval/{sub_id}/reject")
async def pkg_approval_reject(sub_id: int, body: RejectBody,
                              ident: Identity = Depends(get_current_identity)):
    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="驳回必须填写理由")
    sub = _load_sub(sub_id)
    if not _can_act_sub(ident, sub):
        raise HTTPException(status_code=403, detail="只有该子单审批人可操作")
    if sub["status"] != pkg_store.SUB_PENDING:
        raise HTTPException(status_code=409, detail="该子单已决出")
    row = pkg_store.decide_suborder(sub_id, pkg_store.SUB_REJECTED, ident.userid, reason)
    if row is None:
        raise HTTPException(status_code=409, detail="该子单已被决出(并发)")
    audit.record(actor=f"{ident.name}({ident.userid})",
                 action="pkg_approval_reject", target=f"sub:{sub_id}",
                 detail={"package_id": sub["package_id"], "reason": reason[:200]})
    pkg = pkg_store.get_package(sub["package_id"])
    _maybe_notify_finalized(pkg, sub["package_id"])
    return {"ok": True, "package_status": pkg["status"] if pkg else ""}


@router.get("/pkg-approval/{sub_id}/preview/{doc_id}")
async def pkg_approval_preview(sub_id: int, doc_id: str,
                               ident: Identity = Depends(get_current_identity)):
    """子单内文档预览 (前 1200 字) — 仅该子单审批人/admin/boss; doc 必须属于该子单.

    Q3 边界: 正文只有审批链可见, 发起人本人无权限时看不到 (除非也是审批人)。
    """
    sub = _load_sub(sub_id)
    if not _can_act_sub(ident, sub):
        raise HTTPException(status_code=403, detail="只有该子单审批人可预览")
    doc_ids = json.loads(sub["doc_ids"] or "[]")
    if doc_id not in doc_ids:
        raise HTTPException(status_code=404, detail="文档不在该子单内")
    from app.core.qdrant_client import get_client
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    from app.config import settings
    client = get_client()
    collected: list[tuple] = []
    offset = None
    try:
        while True:
            result = await client.scroll(
                collection_name=settings.qdrant_collection,
                scroll_filter=Filter(must=[
                    FieldCondition(key="doc_id", match=MatchValue(value=doc_id))
                ]),
                with_payload=True, with_vectors=False, limit=100, offset=offset,
            )
            points, next_offset = result
            for p in points:
                pl = p.payload or {}
                collected.append((pl.get("chunk_id"), pl.get("text") or "",
                                  pl.get("source") or ""))
            if next_offset is None or len(collected) > 40:
                break
            offset = next_offset
    except Exception as exc:  # noqa: BLE001
        logger.error("pkg preview scroll 失败 doc=%s: %s", doc_id, exc)
        raise HTTPException(status_code=502, detail="文档存储暂时不可用")
    if not collected:
        raise HTTPException(status_code=404, detail="文档不存在")
    try:
        collected.sort(key=lambda x: float(x[0]))
    except (TypeError, ValueError):
        pass
    text = "\n".join(t for (_, t, _s) in collected if t)[:1200]
    audit.record(actor=f"{ident.name}({ident.userid})",
                 action="pkg_preview", target=doc_id,
                 detail={"sub_id": sub_id, "package_id": sub["package_id"]})
    return {"doc_id": doc_id, "source": collected[0][2], "preview": text}


# ---------------------------------------------------------------- 管理端


@router.get("/admin/packages")
async def admin_packages(ident: Identity = Depends(get_current_identity)):
    if not (ident.admin or ident.boss):
        raise HTTPException(status_code=403, detail="仅管理员")
    rows = pkg_store.list_all()
    for r in rows:
        r["doc_count"] = len(json.loads(r.get("doc_ids") or "[]"))
    return {"items": rows}


# ---------------------------------------------------------------- 内部编排


# ---------------------------------------------------------------- 受控链接页 (M3)
#
# 安全模型 (16 号 V1.1 "审批即授权"):
#   - 打开者 userid 必须 == packages.creator, 其他人拿 token 也 403 (零信息)
#   - 过 expires_at → 410
#   - 文档正文绕过本人 ACL (审批链已授权), 但只限 approved_ids 集合内
#   - 每次打开写 package_views (管理端点开次数 + 审计)


def _pkg_by_token_checked(token: str, ident: Identity) -> dict[str, Any]:
    """token → 包行 + 三重校验 (仅 creator / 有效期)。返回 code 供前端分支。"""
    pkg = pkg_store.get_package_by_token(token)
    if pkg is None:
        # 与 P1 口径一致: 不存在与无权同形, 不暴露 token 是否存在
        raise HTTPException(status_code=403, detail="链接无效或无权查看")
    if pkg["creator"] != ident.userid:
        audit.record(actor=f"{ident.name}({ident.userid})",
                     action="package_open_denied", target=pkg["package_id"],
                     detail={"token_tail": token[-6:]})
        # 他人拿链接: 零信息, 与不存在同形
        raise HTTPException(status_code=403, detail="链接无效或无权查看")
    return pkg


@router.get("/package/open/{token}")
async def package_open(token: str, ident: Identity = Depends(get_current_identity)):
    require_gray_open(ident)
    pkg = _pkg_by_token_checked(token, ident)
    status = pkg["status"]
    if status != pkg_store.PKG_APPROVED:
        # 审批中 / 已驳回 / 已撤回 → 无内容, 返回状态供前端渲染对应页
        subs = pkg_store.list_suborders(pkg["package_id"])
        pending = [s for s in subs if s["status"] == pkg_store.SUB_PENDING]
        return {
            "package_id": pkg["package_id"],
            "title": pkg["title"],
            "status": status,
            "pending_depts": [s["dept_name"] for s in pending],
            "docs": [],
        }
    from datetime import datetime
    try:
        exp = datetime.strptime(pkg["expires_at"], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        exp = None
    if exp is not None and datetime.now() > exp:
        # 16 号 §5: 过 7 天有效期 → 410
        raise HTTPException(status_code=410, detail="链接已过期, 请重新发起打包")
    # 记打开 (每次)
    pkg_store.record_view(pkg["package_id"], ident.userid)
    audit.record(actor=f"{ident.name}({ident.userid})",
                 action="package_open", target=pkg["package_id"], detail={})
    approved_ids = json.loads(pkg["approved_ids"] or "[]")
    metas: dict[str, Any] = {}
    if approved_ids:
        try:
            metas = await package_proposer.get_docs_meta(approved_ids)
        except Exception as exc:  # noqa: BLE001
            logger.error("package_open meta 失败 pkg=%s: %s", pkg["package_id"], exc)
    docs = [
        {"doc_id": did,
         "source": (metas.get(did) or {}).get("source") or did,
         "level": (metas.get(did) or {}).get("level") or ""}
        for did in approved_ids
    ]
    return {
        "package_id": pkg["package_id"],
        "title": pkg["title"],
        "status": "approved",
        "expires_at": pkg["expires_at"],
        "docs": docs,
        "watermark_actor": f"{ident.name}({ident.userid})",
    }


@router.get("/package/open/{token}/doc/{doc_id}")
async def package_open_doc(token: str, doc_id: str,
                           ident: Identity = Depends(get_current_identity)):
    """链接页取文档正文 — 审批即授权: 只校验 (1)token 归属本人 (2)doc 在 approved_ids。

    不叠加本人 ACL (发起人本无权看的跨部门 L2, 审批通过后在此可见 — 核心用例)。
    """
    require_gray_open(ident)
    pkg = _pkg_by_token_checked(token, ident)
    if pkg["status"] != pkg_store.PKG_APPROVED:
        raise HTTPException(status_code=403, detail="链接无效或无权查看")
    approved_ids = set(json.loads(pkg["approved_ids"] or "[]"))
    if doc_id not in approved_ids:
        raise HTTPException(status_code=404, detail="文档不存在或不在本包内")
    # 复用 pkg preview 同款 scroll (不带 ACL), 但链接页给全量 chunk (≤200)
    from datetime import datetime, timedelta, timezone
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    from app.config import settings
    from app.core.qdrant_client import get_client
    client = get_client()
    collected: list[tuple] = []
    offset = None
    try:
        while True:
            result = await client.scroll(
                collection_name=settings.qdrant_collection,
                scroll_filter=Filter(must=[
                    FieldCondition(key="doc_id", match=MatchValue(value=doc_id))
                ]),
                with_payload=True, with_vectors=False, limit=100, offset=offset,
            )
            points, next_offset = result
            for p in points:
                pl = p.payload or {}
                collected.append((pl.get("chunk_id"), pl.get("text") or "",
                                  pl.get("source") or "", pl.get("level") or ""))
            if len(collected) > 200 or next_offset is None:
                break
            offset = next_offset
    except Exception as exc:  # noqa: BLE001
        logger.error("package_open_doc scroll 失败 doc=%s: %s", doc_id, exc)
        raise HTTPException(status_code=502, detail="文档存储暂时不可用")
    if not collected:
        raise HTTPException(status_code=404, detail="文档不存在或不在本包内")
    try:
        collected.sort(key=lambda x: float(x[0]))
    except (TypeError, ValueError):
        pass
    collected = collected[:200]
    _TZ_CN = timezone(timedelta(hours=8))
    audit.record(actor=f"{ident.name}({ident.userid})",
                 action="package_doc_view", target=doc_id,
                 detail={"package_id": pkg["package_id"],
                         "source": collected[0][2]})
    return {
        "doc_id": doc_id,
        "source": collected[0][2],
        "level": collected[0][3],
        "text_sections": [t for (_, t, _s, _l) in collected if t],
        "watermark": {
            "actor": f"{ident.name}({ident.userid})",
            "time": datetime.now(_TZ_CN).isoformat(timespec="seconds"),
        },
    }


def _notify_creator_finalized(pkg: dict[str, Any]) -> None:
    status = pkg.get("status")
    if status == pkg_store.PKG_APPROVED and pkg.get("token"):
        pkg_notify.send_async(
            [pkg["creator"]], "打包完成",
            f"《{pkg['title']}》已审批通过。受控链接 7 天内有效 ({pkg['expires_at']} 过期),"
            f"登录知识库在「我的打包」打开。",
            package_id=pkg["package_id"],
        )
    elif status == pkg_store.PKG_REJECTED:
        pkg_notify.send_async(
            [pkg["creator"]], "打包被驳回",
            f"《{pkg['title']}》的文档全部被驳回, 未生成包。可按驳回理由调整后重新发起。",
            package_id=pkg["package_id"],
        )


def _maybe_notify_finalized(pkg: dict[str, Any] | None, package_id: str) -> None:
    """子单决出后: 若包已定稿(无剩余 pending) → 通知发起人结果"""
    if pkg is None or pkg["status"] == pkg_store.PKG_PENDING:
        return
    _notify_creator_finalized(pkg)
