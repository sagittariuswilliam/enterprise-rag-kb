"""管理端只读 API (P1 控制台第一版, 10-方案 §2 管理端)

本批范围 (#2): 概览指标 / 全量文档管理 / 钉钉同步状态 / 成员与部门。
后续批次: L4 审批队列 (#3)、审计日志与命中统计 (#4/#6)。

纪律:
  - 全部接口先过 require_admin (users 表 admin/boss 标志, 实时查库)
  - 管理视图不受 ACL 过滤 (admin 全量视野), 但**只读**——改级别/删除走原有
    /api/upload 接口, 权限口径不变 (can_modify)
  - 不新增任何绕过 retriever 的 Qdrant 向量查询; 计数用 get_collectioninfo
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.acl.identity import Identity
from app.core.deps import get_current_identity
from app.core.qdrant_client import get_client
from app.dingtalk.store import get_conn
from app.services.retriever import list_documents
from app.services.upload_tasks import get_task_store, public_view

logger = logging.getLogger(__name__)
router = APIRouter()


def require_admin(ident: Identity = Depends(get_current_identity)) -> Identity:
    if not (ident.admin or ident.boss):
        raise HTTPException(status_code=403, detail="仅知识库管理员可访问管理端")
    return ident


async def _svc_check(name: str, url: str, timeout: float = 3.0) -> dict:
    """轻量存活探测. 必须丢线程池: 同步 urlopen 自己会阻塞事件循环 -> 必然超时"""
    import asyncio, urllib.request

    def _hit():
        t0 = time.time()
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return {"name": name, "up": True,
                        "ms": round((time.time() - t0) * 1000, 1), "code": resp.status}
        except Exception as e:  # noqa: BLE001
            return {"name": name, "up": False, "ms": None, "code": None,
                    "error": str(e)[:120]}
    return await asyncio.to_thread(_hit)


@router.get("/admin/overview")
async def overview(ident: Identity = Depends(require_admin)):
    """顶部 KPI: 文档数/chunks/用户/部门/待处理任务/服务存活"""
    conn = get_conn()
    try:
        n_users = conn.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
        n_depts = conn.execute("SELECT COUNT(*) FROM depts WHERE active=1").fetchone()[0]
        n_left = conn.execute("SELECT COUNT(*) FROM users WHERE active=0").fetchone()[0]
        last_sync = conn.execute(
            "SELECT kind, detail, dept_count, user_count, created_at "
            "FROM sync_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        # L4 审批: 待处理单数量 (管理概览 KPI)
        l4_pending = conn.execute(
            "SELECT COUNT(*) FROM l4_approvals WHERE status='pending'"
        ).fetchone()[0]
    finally:
        conn.close()

    docs = await list_documents(identity=ident)  # admin 全量视野
    try:
        info = await get_client().get_collection("kb_chunks")
        points = info.points_count
    except Exception as e:  # noqa: BLE001
        logger.warning("qdrant collection info 失败: %s", e)
        points = None

    # 上传任务状态分布
    task_stat = {}
    for t in get_task_store().list():
        task_stat[t.get("status", "?")] = task_stat.get(t.get("status", "?"), 0) + 1

    services = [
        await _svc_check("RAG (self)", "http://127.0.0.1:9012/health"),
        await _svc_check("Qdrant", "http://127.0.0.1:6333/healthz"),
    ]
    return {
        "documents": len(docs),
        "chunks": points,
        "users_active": n_users,
        "users_left": n_left,
        "depts": n_depts,
        "task_status": task_stat,
        "l4_pending": l4_pending,
        "last_sync": dict(last_sync) if last_sync else None,
        "services": services,
    }


@router.get("/admin/documents")
async def admin_documents(ident: Identity = Depends(require_admin)):
    """全量文档管理列表 (不受调用者 ACL 限制, 仅 admin)。
    合并 Qdrant 实况 + 上传任务台账 (级别/时间/失败原因)。"""
    docs = await list_documents(identity=ident)
    raw_tasks = get_task_store().list()  # raw: 带 acl
    by_src: dict[str, dict] = {}
    for t in raw_tasks:
        fname = t.get("filename") or ""
        for key in (t.get("doc_id", ""), fname, f"{t.get('doc_id')}_{fname}"):
            if key:
                by_src[key] = t

    def _row(src, doc_id, d, t):
        acl = t.get("acl") or {}
        return {
            "source": src,
            "doc_id": doc_id,
            "level": d.get("level") or acl.get("level", ""),
            "dept_id": d.get("dept_id") or acl.get("dept_id", []),
            "owner_userid": d.get("owner_userid") or acl.get("owner_userid", ""),
            "extra_acl": d.get("extra_acl") or acl.get("extra_acl", []),
            "chunks": t.get("chunks"),
            "format": t.get("format") or t.get("route", ""),
            "status": t.get("status", "ingested"),
            "reason": t.get("reason", ""),
            "created_at": t.get("created_at"),
            "parse_method": t.get("parse_method", ""),
            "ocr_progress": t.get("ocr_progress"),
        }

    out = []
    seen_ids = set()
    for d in docs:
        src = d.get("source") or ""
        doc_id = d.get("doc_id") or ""
        t = by_src.get(src) or by_src.get(doc_id) or {}
        seen_ids.add(doc_id)
        out.append(_row(src, doc_id, d, t))
    # 台账里 failed / 尚未出现在 Qdrant 的文档也列出 (管理要看全)
    for t in raw_tasks:
        doc_id = t.get("doc_id", "")
        if doc_id in seen_ids:
            continue
        seen_ids.add(doc_id)
        out.append(_row(f"{doc_id}_{t.get('filename')}", doc_id, {}, t))
    out.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return {"documents": out, "count": len(out)}


@router.get("/admin/sync")
async def admin_sync(ident: Identity = Depends(require_admin),
                     limit: int = Query(20, ge=1, le=100)):
    """钉钉同步状态: 最近 N 条 sync_log + 部门/人员快照计数"""
    conn = get_conn()
    try:
        logs = conn.execute(
            "SELECT id, kind, detail, dept_count, user_count, created_at "
            "FROM sync_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        dept_stat = conn.execute(
            "SELECT COUNT(*) total, SUM(active) active FROM depts"
        ).fetchone()
        user_stat = conn.execute(
            "SELECT COUNT(*) total, SUM(active) active FROM users"
        ).fetchone()
        last_dept_sync = conn.execute(
            "SELECT MAX(synced_at) m FROM depts").fetchone()["m"]
    finally:
        conn.close()
    return {
        "logs": [dict(r) for r in logs],
        "depts": {"total": dept_stat["total"], "active": dept_stat["active"]},
        "users": {"total": user_stat["total"], "active": user_stat["active"]},
        "last_dept_synced_at": last_dept_sync,
    }


@router.get("/admin/members")
async def admin_members(ident: Identity = Depends(require_admin),
                        q: str = Query("", max_length=50),
                        dept_id: int = Query(0, ge=0),
                        include_left: bool = Query(False)):
    """成员列表 (姓名/userid/部门/标志位); q 前缀搜索, dept_id 过滤"""
    conn = get_conn()
    try:
        sql = ("SELECT userid, name, title, dept_ids, leader, admin, boss, active, "
               "job_number, synced_at FROM users WHERE (active=1 OR (?=1))")
        params: list = [1 if include_left else 0]
        if q.strip():
            sql += " AND name LIKE ?"
            params.append(f"{q.strip()}%")
        if dept_id:
            sql += " AND dept_ids LIKE ?"
            params.append(f"%{dept_id}%")
        rows = conn.execute(sql + " ORDER BY active DESC, name LIMIT 200", params).fetchall()
        depts = {r["dept_id"]: r["name"] for r in conn.execute(
            "SELECT dept_id, name FROM depts").fetchall()}
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            ids = json.loads(r["dept_ids"] or "[]")
        except json.JSONDecodeError:
            ids = []
        out.append({**dict(r), "dept_ids": ids,
                    "dept_names": [depts.get(d, f"#{d}") for d in ids]})
    return {"members": out, "count": len(out)}


@router.get("/admin/depts")
async def admin_depts_tree(ident: Identity = Depends(require_admin)):
    """部门树 (钉钉镜像): 含停用标记, 供概览展示组织全貌"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT dept_id, name, parent_id, member_count, active, synced_at "
            "FROM depts ORDER BY dept_id").fetchall()
    finally:
        conn.close()
    nodes = {r["dept_id"]: {**dict(r), "children": []} for r in rows}
    roots = []
    for n in nodes.values():
        p = n["parent_id"]
        if p and p in nodes:
            nodes[p]["children"].append(n)
        else:
            roots.append(n)
    return {"tree": roots, "count": len(nodes)}


@router.get("/admin/audit")
async def admin_audit(
    ident: Identity = Depends(require_admin),
    limit: int = Query(100, ge=1, le=500),
    action: str = Query("", max_length=40),
):
    """审计日志查询 (仅 admin/boss): 倒序, 可按动作过滤。detail 解析成 JSON。"""
    from app.services import audit as audit_svc
    rows = audit_svc.recent(limit=limit, action=action.strip())
    for r in rows:
        try:
            r["detail"] = json.loads(r.get("detail") or "{}")
        except (TypeError, ValueError):
            pass
    return {"items": rows, "count": len(rows)}


@router.get("/admin/hits")
async def admin_hits(
    ident: Identity = Depends(require_admin),
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(50, ge=1, le=500),
):
    """#6 文档命中统计 (仅 admin/boss): 哪些文档被检索命中过多少次。

    零命中口径 (no_hit_docs): 知识库现存文档总数 - doc_hits 全时段 distinct doc 数。
    现存文档总数取 Qdrant kb_chunks distinct doc_id (list_documents 全量视野后
    去重 doc_id) — Qdrant 是入库实况唯一权威, 台账 upload_tasks.json 可能含
    failed/pending_approval 等未入库状态, 用它会把没进向量库的文档算成零命中。
    Qdrant 不可达时退回台账 ingested 计数 (降级不报错, summary 里标 source)。
    """
    from app.services import hit_stats

    items = hit_stats.aggregate(days=days, limit=limit)

    total_docs = 0
    total_docs_from = "qdrant"
    try:
        docs = await list_documents(identity=ident)  # admin 全量视野
        doc_ids = {d.get("doc_id") for d in docs if d.get("doc_id")}
        total_docs = len(doc_ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("admin/hits Qdrant 统计失败, 退回台账口径: %s", exc)
        from app.services.upload_tasks import get_task_store
        total_docs = sum(
            1 for t in get_task_store().list() if t.get("status") == "ingested"
        )
        total_docs_from = "upload_tasks(降级)"

    summary = hit_stats.global_summary(days=days, total_docs=total_docs)
    summary["total_docs"] = total_docs
    summary["total_docs_from"] = total_docs_from
    return {"items": items, "days": days, "summary": summary}


# ----------------------------------------------------------------------------
# P3 灰度开放配置 (2026-10-19)
# ----------------------------------------------------------------------------

@router.get("/admin/gray")
async def admin_gray_list(ident: Identity = Depends(require_admin)):
    """部门开放状态列表: 部门/在职人数/负责人/是否放开/最近变更"""
    from app.acl.gray import released_dept_ids

    conn = get_conn()
    try:
        depts = conn.execute(
            "SELECT dept_id, name, parent_id, active FROM depts ORDER BY dept_id"
        ).fetchall()
        users = conn.execute(
            "SELECT userid, name, dept_ids, leader, admin, boss, active FROM users"
        ).fetchall()
        opened_at = {r["dept_id"]: r["opened_at"] for r in conn.execute(
            "SELECT dept_id, opened_at FROM gray_release").fetchall()}
    finally:
        conn.close()

    # 一人多部门: 负责人取"挂该部门且 leader=1"的第一人 (钉钉同步口径)
    leaders: dict[int, str] = {}
    counts: dict[int, int] = {}
    for u in users:
        if not u["active"]:
            continue
        try:
            ids = json.loads(u["dept_ids"] or "[]")
        except json.JSONDecodeError:
            ids = []
        for d in ids:
            counts[d] = counts.get(d, 0) + 1
            if u["leader"] and d not in leaders:
                leaders[d] = u["name"]
    released = released_dept_ids()
    items = []
    for d in depts:
        if not d["active"]:
            continue
        is_root = d["dept_id"] == 1 or d["parent_id"] == 0
        items.append({
            "dept_id": d["dept_id"],
            "name": d["name"],
            "is_root": is_root,
            "member_count": counts.get(d["dept_id"], 0),
            "leader_name": leaders.get(d["dept_id"], ""),
            "open": d["dept_id"] in released,
            "opened_at": opened_at.get(d["dept_id"], ""),
        })
    return {"items": items, "configured": bool(released)}


@router.post("/admin/gray/{dept_id}")
async def admin_gray_set(dept_id: int, payload: dict = Body(...),
                        ident: Identity = Depends(require_admin)):
    """开关某部门灰度 body={"open": true|false} (即时生效)。根部门也可关。"""
    from app.acl.gray import set_open

    open_ = bool(payload.get("open"))

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT dept_id, name FROM depts WHERE dept_id=? AND active=1",
            (dept_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="部门不存在或已停用")
    set_open(dept_id, open_)
    from app.services import audit as audit_svc
    audit_svc.record(actor=ident.userid, action="gray_set",
                     target=str(dept_id),
                     detail={"dept": row["name"], "open": open_})
    return {"dept_id": dept_id, "name": row["name"], "open": open_}


# ----------------------------------------------------------------------------
# P3 审计报表 (给老板的一页汇总)
# ----------------------------------------------------------------------------

@router.get("/admin/report")
async def admin_report(ident: Identity = Depends(require_admin),
                       days: int = Query(30, ge=1, le=365)):
    """问答量/有结果率/零命中问题/热门文档 Top5/L4 审批统计。

    口径全部来自既有落库 (audit_log/chat_search, doc_hits, l4_approvals),
    不新增埋点。零命中 = chat_search 且 hits=0 的问题聚类。
    """
    conn = get_conn()
    try:
        win = "datetime('now','localtime', ?)"
        p = f"-{days} day"

        q_total = conn.execute(
            f"SELECT COUNT(*) c FROM audit_log WHERE action='chat_search' "
            f"AND created_at >= {win}", (p,)).fetchone()["c"]
        users_rows = conn.execute(
            f"SELECT actor FROM audit_log WHERE action='chat_search' "
            f"AND created_at >= {win}", (p,)).fetchall()
        active_users = len({r["actor"] for r in users_rows})
        has_result = 0
        # 逐条解析 detail 统计 hits (量级小: 窗口内问答条数 < 万)
        det_rows = conn.execute(
            f"SELECT detail FROM audit_log WHERE action='chat_search' "
            f"AND created_at >= {win}", (p,)).fetchall()
        zero_questions: dict[str, int] = {}
        for r in det_rows:
            try:
                d = json.loads(r["detail"] or "{}")
            except json.JSONDecodeError:
                d = {}
            if (d.get("hits") or 0) > 0:
                has_result += 1
            else:
                q = (d.get("question") or "").strip()[:100]
                if q:
                    zero_questions[q] = zero_questions.get(q, 0) + 1

        top_docs = conn.execute(
            f"SELECT source, COUNT(*) n FROM doc_hits "
            f"WHERE hit_at >= {win} GROUP BY doc_id ORDER BY n DESC LIMIT 5",
            (p,)).fetchall()

        l4_total = conn.execute(
            f"SELECT COUNT(*) c FROM l4_approvals WHERE created_at >= {win}",
            (p,)).fetchone()["c"]
        l4_approved = conn.execute(
            f"SELECT COUNT(*) c FROM l4_approvals WHERE status='approved' "
            f"AND created_at >= {win}", (p,)).fetchone()["c"]
        l4_rows = conn.execute(
            f"SELECT doc_id, filename, owner_userid, approver_userid, status, "
            f"created_at, decided_at FROM l4_approvals "
            f"WHERE created_at >= {win} ORDER BY created_at DESC LIMIT 20",
            (p,)).fetchall()
        name_map = {u["userid"]: u["name"] for u in conn.execute(
            "SELECT userid, name FROM users").fetchall()}
    finally:
        conn.close()

    zeros = sorted(zero_questions.items(), key=lambda x: -x[1])[:20]
    return {
        "days": days,
        "questions": {"total": q_total, "active_users": active_users,
                      "has_result_rate": round(has_result / q_total * 100, 1) if q_total else None},
        "zero_hit_questions": [{"question": q, "count": n} for q, n in zeros],
        "top_docs": [{"source": d["source"] or "未知", "hits": d["n"]} for d in top_docs],
        "l4": {"total": l4_total, "approved": l4_approved,
               "items": [{**dict(r), "owner_name": name_map.get(r["owner_userid"], r["owner_userid"]),
                          "approver_name": name_map.get(r["approver_userid"], r["approver_userid"])}
                         for r in l4_rows]},
    }


# ----------------------------------------------------------------------------
# P5* 关系驾驶舱 — 只读图数据 2026-10-20
# ----------------------------------------------------------------------------

@router.get("/admin/graph")
async def admin_graph(ident: Identity = Depends(require_admin)):
    """公司→部门→人 关系图数据 (纯只读, SQLite 实时查库, 零 Qdrant 依赖).

    口径见 app/services/graph.py 模块注释: 负责人来自 users.leader;
    人只连最具体部门 (去根后子部门), 防 25 条边糊根节点;
    部门文档/命中数按上传台账 acl 归属聚合."""
    from app.services.graph import assemble_graph
    return assemble_graph()
