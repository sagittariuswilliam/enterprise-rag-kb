"""L4 审批钉钉通知 — fire-and-forget 线程, 绝不影响主流程

设计纪律:
  - robot_send_oto 是同步 HTTP, 不能在请求路径里等它; 统一丢 daemon 线程。
  - 一切异常吞掉只 log — 通知失败 ≠ 审批失败。
  - 指定 doc_id 时回写 l4_approvals.notify_ok (1 送达 / 0 失败),
    供管理端观测"审批人到底收没收到提醒"。
"""

from __future__ import annotations

import logging
import threading

from app.services import l4_approvals
from app.config import settings

logger = logging.getLogger(__name__)

# 管理控制台地址 = 配置项 admin_base_url + /admin (部署时在 .env 指定)
ADMIN_URL = settings.admin_base_url.rstrip("/") + "/admin"


def send_async(
    userids: list[str],
    title: str,
    text: str,
    doc_id: str = "",
) -> None:
    """后台线程发 markdown 单聊; 立即返回, 不阻塞调用方。"""
    if not userids:
        return
    t = threading.Thread(
        target=_send, args=(list(userids), title, text, doc_id),
        name="l4-notify", daemon=True,
    )
    t.start()


def _send(userids: list[str], title: str, text: str, doc_id: str) -> None:
    ok = False
    try:
        from app.dingtalk.client import DingTalkClient

        DingTalkClient().robot_send_oto(userids, title, text)
        ok = True
    except Exception as exc:  # noqa: BLE001 — 通知失败只 log
        logger.warning("L4 钉钉通知失败 users=%s doc=%s: %s",
                       userids, doc_id or "-", exc)
    if doc_id:
        l4_approvals.set_notify_ok(doc_id, ok)
