"""P4 打包导出 — packages/pkg_approvals 读写 + 拆单/免审/定稿引擎 (16 号 V1.1 §2/§4)

设计纪律 (对齐 l4_approvals 模式):
  - 纯 SQLite, 业务编排(通知/审计)在 API 层, 保持可单测。
  - 拆单按"文档归属部门"(payload dept_id), 一张部门一张子单, 责任到人。
  - 免审矩阵 (§2 Q2 拍板):
      L3 全员可见 → 免审
      L1 仅 owner 本人 → 免审 (他人 L1 根本不进池, propose 已排除)
      L4 → 不进池 (confirm 侧再挡一次, 双保险)
      发起人 admin/boss → 全免审
      发起人 = 文档 owner 或 文档部门 leader → 该文档免审
      其余 → 拆给文档部门审批链: 部门负责人 → boss → admin (排除发起人自己)
  - 定稿: 全部子单 status != pending 时触发; approved_ids = 免审集合 ∪ 通过子单文档;
    全驳回 → rejected; 部分驳回 → 剔除被拒文件继续。
  - decide() 原子防并发 (UPDATE WHERE status='pending'), rowcount=0 → None → API 转 409。
  - 审批人离职兜底 (§4): 子单 approver 离职 → 改 boss 审, boss 无/离职 → admin 兜底。
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Optional

from app.dingtalk.store import get_conn

PKG_PENDING = "pending_approval"
PKG_APPROVED = "approved"
PKG_REJECTED = "rejected"
PKG_CANCELLED = "cancelled"
PKG_EXPIRED = "expired"

SUB_PENDING = "pending"
SUB_APPROVED = "approved"
SUB_REJECTED = "rejected"

LINK_TTL_DAYS = 7
MAX_DOCS_LIMIT = 60      # confirm 单次清单上限 (防御)

# 文档密级合法值 (chunk payload level)
_LEVELS = {"L1", "L2", "L3", "L4"}
# 组包召回池允许的密级 (§3: L4 默认不进池; L1 仅本人, 由 owner 过滤另行处理)
POOL_LEVELS = {"L1", "L2", "L3"}


def _now() -> datetime:
    return datetime.now()


def _now_s() -> str:
    return _now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------- 身份辅助 (实时查库)


def _dept_name(conn, dept_id: int) -> str:
    row = conn.execute("SELECT name FROM depts WHERE dept_id=?", (dept_id,)).fetchone()
    return row["name"] if row else f"部门{dept_id}"


def _user_name(conn, userid: str) -> str:
    row = conn.execute("SELECT name FROM users WHERE userid=?", (userid,)).fetchone()
    return row["name"] if row else (userid or "")


def _user_active(conn, userid: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM users WHERE userid=? AND active=1", (userid,)
    ).fetchone()
    return row is not None


def _find_dept_leader(conn, dept_id: int, exclude_userid: str) -> Optional[str]:
    """部门负责人: leader=1、在职、dept_ids 含该部门、排除发起人自己"""
    for r in conn.execute(
        "SELECT userid, dept_ids FROM users "
        "WHERE leader=1 AND active=1 AND userid<>?",
        (exclude_userid,),
    ).fetchall():
        try:
            d = set(json.loads(r["dept_ids"] or "[]"))
        except json.JSONDecodeError:
            d = set()
        if dept_id in d:
            return r["userid"]
    return None


def _find_fallback_approver(conn, exclude_userid: str, prefer_boss: bool = True) -> Optional[str]:
    """兜底审批链: boss → admin (均在职、排除发起人)"""
    if prefer_boss:
        row = conn.execute(
            "SELECT userid FROM users WHERE boss=1 AND active=1 AND userid<>? "
            "ORDER BY userid LIMIT 1", (exclude_userid,),
        ).fetchone()
        if row:
            return row["userid"]
    row = conn.execute(
        "SELECT userid FROM users WHERE admin=1 AND active=1 AND userid<>? "
        "ORDER BY userid LIMIT 1", (exclude_userid,),
    ).fetchone()
    return row["userid"] if row else None


def pick_sub_approver(conn, dept_id: int, creator: str) -> str:
    """子单审批人: 部门负责人 → boss → admin。全找不到 raise LookupError。"""
    who = _find_dept_leader(conn, dept_id, creator)
    if who is None:
        who = _find_fallback_approver(conn, creator)
    if who is None:
        raise LookupError(
            f"部门 {dept_id} 无在职负责人, 且通讯录无在职 boss/admin 可兜底, "
            "请先补齐组织数据"
        )
    return who


# ---------------------------------------------------------- 免审判定 + 拆单


def classify_docs(docs: list[dict[str, Any]], creator_ident: dict[str, Any]) -> tuple[list[str], dict[int, list[str]]]:
    """免审矩阵核心函数 (纯计算, 不查库; creator_ident 由 API 层传入).

    docs 每条: {doc_id, level, dept_id, owner_userid}
    creator_ident: {userid, dept_ids(list), leader, admin, boss}

    返回 (exempt_doc_ids, {dept_id: [doc_id,...]}) — exempt 直接进授权集,
    其余按部门拆子单。
    """
    uid = creator_ident["userid"]
    my_depts = set(creator_ident.get("dept_ids") or [])
    full_exempt = bool(creator_ident.get("admin") or creator_ident.get("boss"))

    exempt: list[str] = []
    need: dict[int, list[str]] = {}
    for d in docs:
        level = (d.get("level") or "").upper()
        owner = d.get("owner_userid") or ""
        dept_id = int(d.get("dept_id") or 0)
        if full_exempt or level == "L3" or owner == uid:
            # admin/boss 全免; L3 全员可见免审; 自己写的文档免审 (含 L1/L2)
            exempt.append(d["doc_id"])
            continue
        if level == "L1":
            # 他人 L1: 理论上不进池, 这里再挡一次; 无权且不拆单, 直接剔除
            continue
        if level == "L4":
            # 不进包 (§2); 若前端塞了 L4 doc_id, confirm 层已 400, 这里防御剔除
            continue
        # L2: 发起人恰为该文档部门负责人 → 免审 (自己部门文件自己说了算)
        if creator_ident.get("leader") and dept_id in my_depts:
            exempt.append(d["doc_id"])
            continue
        need.setdefault(dept_id, []).append(d["doc_id"])
    return exempt, need


def _doc_ids_of_sub(conn, sub_id: int) -> list[str]:
    row = conn.execute("SELECT doc_ids FROM pkg_approvals WHERE id=?", (sub_id,)).fetchone()
    try:
        return json.loads(row["doc_ids"]) if row and row["doc_ids"] else []
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------- 建包 / 拆单落库


def create_package(
    *,
    title: str,
    need_text: str,
    creator: str,
    creator_ident: dict[str, Any],
    docs: list[dict[str, Any]],
) -> dict[str, Any]:
    """确认提交 → 建包 + 拆子单 (同事务). docs 元数据必须来自服务端补齐 (Q3),
    不接受客户端提供的 level/dept。

    返回 {"package": row, "suborders": [...], "exempt": [...], "no_approval": bool}
    子单审批人离职时按 §4 兜底链换 boss/admin, 由 pick_sub_approver 内部完成。
    """
    package_id = secrets.token_hex(6)
    exempt, need = classify_docs(docs, creator_ident)
    now = _now_s()
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO packages(package_id, title, need_text, creator, doc_ids, "
            "status, created_at, confirmed_at) VALUES(?,?,?,?,?,?,?,?)",
            (package_id, title, need_text, creator,
             json.dumps([d["doc_id"] for d in docs], ensure_ascii=False),
             PKG_PENDING, now, now),
        )
        subs: list[dict[str, Any]] = []
        for dept_id in sorted(need):
            approver = pick_sub_approver(conn, dept_id, creator)
            cur = conn.execute(
                "INSERT INTO pkg_approvals(package_id, dept_id, approver, doc_ids, "
                "status) VALUES(?,?,?,?,?)",
                (package_id, dept_id, approver,
                 json.dumps(need[dept_id], ensure_ascii=False), SUB_PENDING),
            )
            subs.append({
                "id": cur.lastrowid, "dept_id": dept_id,
                "dept_name": _dept_name(conn, dept_id),
                "approver": approver, "doc_ids": need[dept_id],
            })
        if not subs:
            # 全免审 → 确认即定稿
            _finalize_locked(conn, package_id)
        conn.commit()
        pkg = get_package(package_id) or {}
        return {
            "package": pkg, "suborders": subs, "exempt": exempt,
            "no_approval": not subs,
        }
    finally:
        conn.close()


# ---------------------------------------------------------- 查询


def get_package(package_id: str) -> Optional[dict[str, Any]]:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM packages WHERE package_id=?", (package_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_package_by_token(token: str) -> Optional[dict[str, Any]]:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM packages WHERE token=?", (token,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_suborders(package_id: str) -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM pkg_approvals WHERE package_id=? ORDER BY dept_id",
            (package_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["dept_name"] = _dept_name(conn, d["dept_id"])
            out.append(d)
        return out
    finally:
        conn.close()


def list_queue(userid: str, is_privileged: bool) -> list[dict[str, Any]]:
    """审批队列: 负责人看指派给自己的 pending 子单; boss/admin 看全部 pending."""
    conn = get_conn()
    try:
        if is_privileged:
            rows = conn.execute(
                "SELECT * FROM pkg_approvals WHERE status=? "
                "ORDER BY id DESC", (SUB_PENDING,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM pkg_approvals WHERE status=? AND approver=? "
                "ORDER BY id DESC", (SUB_PENDING, userid),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["dept_name"] = _dept_name(conn, d["dept_id"])
            pkg = conn.execute(
                "SELECT title, creator, need_text FROM packages WHERE package_id=?",
                (d["package_id"],),
            ).fetchone()
            d["package_title"] = pkg["title"] if pkg else ""
            d["need_text"] = (pkg["need_text"] if pkg and "need_text" in pkg.keys() else "") or ""
            d["creator_name"] = _user_name(conn, pkg["creator"]) if pkg else ""
            d["approver_name"] = _user_name(conn, d["approver"])
            out.append(d)
        return out
    finally:
        conn.close()


def list_mine(creator: str) -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM packages WHERE creator=? ORDER BY created_at DESC, package_id",
            (creator,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_all() -> list[dict[str, Any]]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM packages ORDER BY created_at DESC, package_id"
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["view_count"] = conn.execute(
                "SELECT COUNT(*) FROM package_views WHERE package_id=?",
                (d["package_id"],),
            ).fetchone()[0]
            d["creator_name"] = _user_name(conn, d.get("creator") or "")
            out.append(d)
        return out
    finally:
        conn.close()


# ---------------------------------------------------------- 决策 / 定稿


def decide_suborder(
    sub_id: int, to_status: str, decided_by: str, reason: str = ""
) -> Optional[dict[str, Any]]:
    """原子迁移 pending→approved/rejected. 返回迁移后的子单行;
    已被决策/不存在 → None (API 转 409/404). 迁移成功后如包全部决出则定稿。
    """
    if to_status not in (SUB_APPROVED, SUB_REJECTED):
        raise ValueError(f"非法子单目标状态: {to_status}")
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE pkg_approvals SET status=?, decided_at=?, reason=? "
            "WHERE id=? AND status=?",
            (to_status, _now_s(), reason, sub_id, SUB_PENDING),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM pkg_approvals WHERE id=?", (sub_id,)).fetchone()
        package_id = row["package_id"]
        remaining = conn.execute(
            "SELECT COUNT(*) FROM pkg_approvals WHERE package_id=? AND status=?",
            (package_id, SUB_PENDING),
        ).fetchone()[0]
        if remaining == 0:
            _finalize_locked(conn, package_id)
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM pkg_approvals WHERE id=?", (sub_id,)
        ).fetchone())
    finally:
        conn.close()


def _finalize_locked(conn, package_id: str) -> dict[str, Any]:
    """包内已无 pending 子单时调用 — 计算授权集并定稿. 调用方持有同一 conn(事务内).

    approved_ids = 免审文档(不属于任何子单的) ∪ approved 子单文档.
    空授权集 → rejected (全部被拒); 否则 approved + 生成 token + 7 天有效期。
    """
    pkg = conn.execute(
        "SELECT * FROM packages WHERE package_id=?", (package_id,)
    ).fetchone()
    if pkg is None or pkg["status"] != PKG_PENDING:
        return dict(pkg) if pkg else {}
    all_docs = json.loads(pkg["doc_ids"] or "[]")
    approved: set[str] = set()
    sub_rows = conn.execute(
        "SELECT status, doc_ids FROM pkg_approvals WHERE package_id=?", (package_id,)
    ).fetchall()
    in_sub: set[str] = set()
    for s in sub_rows:
        ids = json.loads(s["doc_ids"] or "[]")
        in_sub.update(ids)
        if s["status"] == SUB_APPROVED:
            approved.update(ids)
    exempt = [d for d in all_docs if d not in in_sub]
    approved_ids = sorted(set(exempt) | approved)
    now = _now()
    if not approved_ids:
        conn.execute(
            "UPDATE packages SET status=?, finalized_at=? WHERE package_id=?",
            (PKG_REJECTED, now.strftime("%Y-%m-%d %H:%M:%S"), package_id),
        )
        return {"status": PKG_REJECTED, "approved_ids": []}
    token = secrets.token_hex(16)
    expires = now + timedelta(days=LINK_TTL_DAYS)
    conn.execute(
        "UPDATE packages SET status=?, approved_ids=?, token=?, expires_at=?, "
        "finalized_at=? WHERE package_id=?",
        (PKG_APPROVED, json.dumps(approved_ids), token,
         expires.strftime("%Y-%m-%d %H:%M:%S"),
         now.strftime("%Y-%m-%d %H:%M:%S"), package_id),
    )
    return {"status": PKG_APPROVED, "approved_ids": approved_ids,
            "token": token, "expires_at": expires.strftime("%Y-%m-%d %H:%M:%S")}


def finalize_if_done(package_id: str) -> Optional[dict[str, Any]]:
    """幂等定稿检查 (cancel/定时口径外的兜底): 无 pending 子单且仍 pending_approval → 定稿。"""
    conn = get_conn()
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM pkg_approvals WHERE package_id=? AND status=?",
            (package_id, SUB_PENDING),
        ).fetchone()[0]
        if pending:
            return None
        r = _finalize_locked(conn, package_id)
        conn.commit()
        return r if r else None
    finally:
        conn.close()


def cancel_package(package_id: str, userid: str) -> Optional[dict[str, Any]]:
    """发起人撤回 (仅 pending_approval 期间). 成功返回包行, 状态不符 → None."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE packages SET status=?, finalized_at=? WHERE package_id=? "
            "AND creator=? AND status=?",
            (PKG_CANCELLED, _now_s(), package_id, userid, PKG_PENDING),
        )
        if cur.rowcount == 0:
            return None
        conn.execute(
            "UPDATE pkg_approvals SET status=?, decided_at=? WHERE package_id=? AND status=?",
            ("cancelled", _now_s(), package_id, SUB_PENDING),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM packages WHERE package_id=?", (package_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def record_view(package_id: str, viewer: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO package_views(package_id, viewer, opened_at) VALUES(?,?,?)",
            (package_id, viewer, _now_s()),
        )
        conn.commit()
    finally:
        conn.close()


def set_notify_ok(package_id: str, ok: bool) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE packages SET notify_ok=? WHERE package_id=?",
            (1 if ok else 0, package_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_sub_notify_ok(sub_id: int, ok: bool) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE pkg_approvals SET notify_ok=? WHERE id=?",
            (1 if ok else 0, sub_id),
        )
        conn.commit()
    finally:
        conn.close()


def reassign_dead_approvers() -> int:
    """离职兜底 (§4): pending 子单的 approver 已停用 → 转 boss (无则 admin)。

    返回改派条数。由管理端队列查询前/同步任务后调用 (API 层编排)。
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, package_id, dept_id, approver FROM pkg_approvals "
            "WHERE status=?", (SUB_PENDING,),
        ).fetchall()
        n = 0
        for r in rows:
            if _user_active(conn, r["approver"]):
                continue
            pkg = conn.execute(
                "SELECT creator FROM packages WHERE package_id=?", (r["package_id"],)
            ).fetchone()
            if pkg is None:
                continue
            who = _find_fallback_approver(conn, pkg["creator"]) or _find_dept_leader(
                conn, r["dept_id"], pkg["creator"]
            )
            # 优先 boss→admin (对齐 §4 "改由 boss 审"); boss 缺失时再试部门 leader
            if who is None:
                continue
            conn.execute(
                "UPDATE pkg_approvals SET approver=? WHERE id=?", (who, r["id"])
            )
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()
