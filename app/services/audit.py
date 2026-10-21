"""审计日志 — 谁在什么时间干了什么 (P1 #3 首批落库, #4 补命中统计)

表 audit_log 在 kb_v2.db (dingtalk/store.py SCHEMA 维护)。
纪律:
  - 只记录"敏感动作": L4 审批决定 / 查看待审文档内容 / 管理员全量文档视野
  - 普通员工的检索问答审计在 #4 与命中统计一起接 (chat.py 一处 hook)
  - 写入失败绝不影响主流程 (吞异常 + warning)
"""

from __future__ import annotations

import json
from typing import Any, Optional

from app.core.logger import get_logger
from app.dingtalk.store import get_conn

logger = get_logger(__name__)


def record(
    actor: str,
    action: str,
    target: str = "",
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """落一条审计. actor=操作者 userid; action=事件名; target=对象标识."""
    try:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO audit_log(actor, action, target, detail) "
                "VALUES(?,?,?,?)",
                (
                    str(actor),
                    action,
                    str(target),
                    json.dumps(detail or {}, ensure_ascii=False),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — 审计失败不能阻断业务
        logger.warning("audit 写入失败 actor=%s action=%s: %s", actor, action, exc)


def recent(limit: int = 50, action: str = "") -> list[dict[str, Any]]:
    """倒序读取 (仅管理端)."""
    limit = max(1, min(int(limit), 500))
    conn = get_conn()
    try:
        if action:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE action=? "
                "ORDER BY id DESC LIMIT ?",
                (action, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
