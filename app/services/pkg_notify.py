"""P4 打包钉钉通知 — 复用 l4_notify 的 fire-and-forget 纪律 (16 号 §5)

  - 后台 daemon 线程发送, 一切异常吞掉只 log — 通知失败 ≠ 审批失败。
  - 指定 sub_id 回写 pkg_approvals.notify_ok; 指定 package_id 回写 packages.notify_ok。
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from app.services import packages as pkg_store

logger = logging.getLogger(__name__)


def send_async(
    userids: list[str],
    title: str,
    text: str,
    sub_id: Optional[int] = None,
    package_id: str = "",
) -> None:
    if not userids:
        return
    t = threading.Thread(
        target=_send, args=(list(userids), title, text, sub_id, package_id),
        name="pkg-notify", daemon=True,
    )
    t.start()


def _send(userids: list[str], title: str, text: str,
          sub_id: Optional[int], package_id: str) -> None:
    ok = False
    try:
        from app.dingtalk.client import DingTalkClient

        DingTalkClient().robot_send_oto(userids, title, text)
        ok = True
    except Exception as exc:  # noqa: BLE001 — 通知失败只 log
        logger.warning("P4 钉钉通知失败 users=%s pkg=%s sub=%s: %s",
                       userids, package_id or "-", sub_id or "-", exc)
    if sub_id is not None:
        pkg_store.set_sub_notify_ok(sub_id, ok)
    if package_id:
        pkg_store.set_notify_ok(package_id, ok)
