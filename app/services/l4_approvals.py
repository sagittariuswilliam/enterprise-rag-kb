"""L4 审批单 CRUD — l4_approvals 表的唯一读写入口 (纯 SQLite, 无业务副作用)

设计纪律:
  - 审批人不可自选: pick_approver() 按"上传者部门负责人(leader=1 且 dept_ids
    交集) → 兜底 admin 标记者"规则自动指派; 找不到 raise LookupError,
    由 API 层转 500 (通讯录缺负责人属于数据问题, 不放行)。
  - decide() 原子防并发: UPDATE ... WHERE status='pending', rowcount=0 → None,
    API 层转 409 (双击/两个 admin 同时操作只有一个能成功)。
  - 本模块不做通知/不提交线程池 — 那些是 API 层编排, 保持可单测。
  - created_at/decided_at 用本地时间字符串 (与 audit_log 口径一致)。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Optional

from app.dingtalk.store import get_conn

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"

_DECISION_STATUSES = {STATUS_APPROVED, STATUS_REJECTED, STATUS_CANCELLED}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _user_name(conn: sqlite3.Connection, userid: str) -> str:
    row = conn.execute("SELECT name FROM users WHERE userid=?", (userid,)).fetchone()
    return row["name"] if row else userid


def user_name(userid: str) -> str:
    """userid → 姓名 (查不到回退为 userid 本身, 通知文案不因此失败)"""
    conn = get_conn()
    try:
        return _user_name(conn, userid)
    finally:
        conn.close()


# ---------------------------------------------------------- 审批人指派


def pick_approver(owner_userid: str) -> tuple[str, str]:
    """自动指派审批人 → (userid, name)

    规则(已拍板):
      1. 上传者所在部门内 leader=1 AND active=1 的人 (dept_ids 与上传者有交集),
         多个取 userid 排序最小者(稳定);
      2. 找不到 → admin=1 AND active=1 兜底 (同样按 userid 排序取第一个);
      3. 都没有 → LookupError (通讯录缺负责人, 不允许 L4 入库)。

    排除上传者本人 — 自己给自己审批无意义。
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT dept_ids FROM users WHERE userid=? AND active=1",
            (owner_userid,),
        ).fetchone()
        if row is None:
            raise LookupError(f"上传者不在通讯录或已停用: {owner_userid}")
        try:
            owner_depts = set(json.loads(row["dept_ids"] or "[]"))
        except json.JSONDecodeError:
            owner_depts = set()

        approver = None
        # 1) 部门负责人: 逐条取 leader, 在 Python 侧做部门交集(dept_ids 是 JSON 文本)
        if owner_depts:
            for r in conn.execute(
                "SELECT userid, name, dept_ids FROM users "
                "WHERE leader=1 AND active=1 AND userid<>? ORDER BY userid",
                (owner_userid,),
            ).fetchall():
                try:
                    d = set(json.loads(r["dept_ids"] or "[]"))
                except json.JSONDecodeError:
                    d = set()
                if d & owner_depts:
                    approver = r
                    break
        # 2) 兜底 admin
        if approver is None:
            approver = conn.execute(
                "SELECT userid, name FROM users "
                "WHERE admin=1 AND active=1 AND userid<>? ORDER BY userid LIMIT 1",
                (owner_userid,),
            ).fetchone()
        if approver is None:
            raise LookupError(
                f"找不到 L4 审批人: 上传者 {owner_userid} 所在部门无在职负责人, "
                "且通讯录无在职 admin 兜底, 请先补齐组织数据"
            )
        return approver["userid"], approver["name"]
    finally:
        conn.close()


# ---------------------------------------------------------- 建单 / 查单


def create_pending(
    doc_id: str,
    *,
    filename: str,
    owner_userid: str,
    approver_userid: str,
    acl: dict[str, Any],
    file_path: str,
    file_hash: str,
    route: str,
    size: int,
) -> dict[str, Any]:
    """落一张 pending 审批单(acl 存 JSON 文本, 批准时反序列化提交 worker)"""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO l4_approvals(doc_id, filename, owner_userid, "
            "approver_userid, status, acl, file_path, file_hash, route, size, "
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                doc_id, filename, owner_userid, approver_userid, STATUS_PENDING,
                json.dumps(acl, ensure_ascii=False), file_path, file_hash,
                route, size, _now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get(doc_id) or {}


def get(doc_id: str) -> Optional[dict[str, Any]]:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM l4_approvals WHERE doc_id=?", (doc_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_for_approver(userid: str, is_admin: bool) -> list[dict[str, Any]]:
    """审批队列: 非 admin 只看指派给自己的; admin 看全部(管理视野)。"""
    conn = get_conn()
    try:
        if is_admin:
            rows = conn.execute(
                "SELECT * FROM l4_approvals ORDER BY created_at DESC, doc_id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM l4_approvals WHERE approver_userid=? "
                "ORDER BY created_at DESC, doc_id",
                (userid,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_pending_by_owner(owner_userid: str) -> list[dict[str, Any]]:
    """上传者视角: 自己提交且仍待审的单"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM l4_approvals WHERE owner_userid=? AND status=? "
            "ORDER BY created_at DESC, doc_id",
            (owner_userid, STATUS_PENDING),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_pending() -> int:
    """待审批总数 (admin overview 用)"""
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM l4_approvals WHERE status=?", (STATUS_PENDING,)
        ).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------- 决策 / 回写


def decide(
    doc_id: str,
    to_status: str,
    decided_by: str,
    reason: str = "",
) -> Optional[dict[str, Any]]:
    """原子状态迁移: 仅 pending → approved/rejected/cancelled。

    返回迁移后的行; 单不存在或已被决策过 → None (API 层转 404/409)。
    """
    if to_status not in _DECISION_STATUSES:
        raise ValueError(f"非法目标状态: {to_status}")
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE l4_approvals SET status=?, decided_by=?, decision_reason=?, "
            "decided_at=? WHERE doc_id=? AND status=?",
            (to_status, decided_by, reason, _now(), doc_id, STATUS_PENDING),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM l4_approvals WHERE doc_id=?", (doc_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_notify_ok(doc_id: str, ok: bool) -> None:
    """回写钉钉通知送达标记 (1 送达 / 0 失败); 失败静默——只是观测字段"""
    try:
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE l4_approvals SET notify_ok=? WHERE doc_id=?",
                (1 if ok else 0, doc_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass
