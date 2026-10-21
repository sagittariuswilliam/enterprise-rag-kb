"""#5 扫描件校对队列 — review_queue 表的唯一读写入口 (纯 SQLite, 无业务副作用)

口径完全对齐 l4_approvals.py:
  - create_pending(): worker 低置信分支落一行 pending(issues/suggested_text 存原文)
  - decide(): 原子 UPDATE ... WHERE status='pending', rowcount=0 → None
    (API 层转 409, 双击/两人同时批只有一个成功)
  - created_at/decided_at 用本地时间字符串 (与 audit_log 口径一致)
  - 本模块不做入库/不删文件 — 那些是 API 层编排, 保持可单测。
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

_DECISION_STATUSES = {STATUS_APPROVED, STATUS_REJECTED}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def create_pending(
    doc_id: str,
    *,
    filename: str,
    owner_userid: str,
    acl: dict[str, Any],
    file_path: str,
    file_hash: str,
    route: str,
    size: int,
    pages: int,
    issues: list[dict[str, Any]],
    suggested_text: str,
) -> dict[str, Any]:
    """落一条 pending 校对单(acl/issues 存 JSON 文本, 批准时用)"""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO review_queue(doc_id, filename, owner_userid,"
            " acl, file_path, file_hash, route, size, pages, issues,"
            " suggested_text, status, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",  # 13 列
            (
                doc_id, filename, owner_userid,
                json.dumps(acl or {}, ensure_ascii=False), file_path, file_hash,
                route, int(size or 0), int(pages or 0),
                json.dumps(issues or [], ensure_ascii=False),
                suggested_text or "", STATUS_PENDING, _now(),
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
            "SELECT * FROM review_queue WHERE doc_id=?", (doc_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_all() -> list[dict[str, Any]]:
    """全量列表(API 层做部门视野裁剪后返回)"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM review_queue ORDER BY created_at DESC, doc_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_pending() -> int:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM review_queue WHERE status=?", (STATUS_PENDING,)
        ).fetchone()[0]
    finally:
        conn.close()


def user_name(userid: str) -> str:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT name FROM users WHERE userid=?", (userid,)
        ).fetchone()
        return row["name"] if row else userid
    finally:
        conn.close()


def decide(
    doc_id: str,
    to_status: str,
    decided_by: str,
    note: str = "",
) -> Optional[dict[str, Any]]:
    """原子状态迁移: 仅 pending → approved/rejected。

    返回迁移后的行; 单不存在或已被决策过 → None (API 层转 404/409)。
    """
    if to_status not in _DECISION_STATUSES:
        raise ValueError(f"非法目标状态: {to_status}")
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE review_queue SET status=?, decided_by=?, decision_note=?,"
            " decided_at=? WHERE doc_id=? AND status=?",
            (to_status, decided_by, note, _now(), doc_id, STATUS_PENDING),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT * FROM review_queue WHERE doc_id=?", (doc_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
