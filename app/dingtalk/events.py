"""Stream 事件订阅 — 入离职/调岗实时增量 (07 方案 §3.2)

WebSocket 出站长连接, 无需公网回调地址/域名。
事件类型 (钉钉官方):
  user_add_org     新员工入职     -> 重拉该用户建档, 默认最低权限
  user_modify_org  资料变更/调岗  -> 重拉该用户, dept_ids 覆盖更新
  user_leave_org   离职           -> active=0 停用, 不删档
  dept_add/update/delete 组织变更 -> 触发一次全量同步 (部门数少, 全量更稳)

运行方式:
  venv/bin/python -m app.dingtalk.events          # 前台跑 (开发验证)
生产: systemd user service 守护 (P1 部署时配)。
"""

from __future__ import annotations

import asyncio
import json
import logging

import dingtalk_stream as ds

from app.config import settings
from app.dingtalk.client import DingTalkClient, DingTalkError
from app.dingtalk.store import get_conn
from app.dingtalk.sync import apply_user_snapshot, full_sync, mark_users_inactive

logger = logging.getLogger("rag_cs.dingtalk.events")

_USER_EVENTS = {"user_add_org", "user_modify_org", "user_active_org"}
_LEAVE_EVENTS = {"user_leave_org"}
_DEPT_EVENTS = {"org_dept_create", "org_dept_modify", "org_dept_remove"}


class OrgEventHandler(ds.EventHandler):
    def __init__(self, client: DingTalkClient | None = None):
        super().__init__()
        self.client = client or DingTalkClient()

    async def process(self, event: ds.EventMessage):
        try:
            await asyncio.to_thread(self.handle_sync, event)
        except Exception:
            logger.exception("事件处理失败 type=%s data=%s", event.headers.topic, event.data)
        return ds.AckMessage.STATUS_OK, "OK"

    def handle_sync(self, event: ds.EventMessage) -> None:
        """同步核心, 与 asyncio 解耦便于单测直接调用.

        事件类型口径 (2026-10-15 实测): register_all_event_handler 模式下
        headers.topic 恒为 "*", 真实类型在 headers.event_type
        (来自 Frame 的 extensions['type'])。双口径兼容。
        """
        h = event.headers
        etype = getattr(h, "event_type", None) or ""
        if not etype or etype == "*":
            etype = getattr(h, "topic", "") or ""
        if not etype or etype == "*":
            etype = (event.data or {}).get("EventType", "") or (event.data or {}).get("type", "")
        payload = event.data or {}
        logger.info("收到事件: %s %s", etype, json.dumps(payload, ensure_ascii=False)[:200])
        if not etype:
            return  # 识别不出类型, 只留日志

        if etype in _USER_EVENTS:
            for uid in _extract_userids(payload):
                try:
                    snap = self.client.get_user(uid)
                except DingTalkError as exc:
                    if exc.errcode == 60121:  # 用户不存在
                        mark_users_inactive([uid])
                        continue
                    raise
                apply_user_snapshot(snap)
        elif etype in _LEAVE_EVENTS:
            uids = _extract_userids(payload)
            mark_users_inactive(uids)
        elif etype in _DEPT_EVENTS:
            full_sync(self.client)


def _extract_userids(payload: dict) -> list[str]:
    """事件体里 userid 列表的字段是 list 或 JSON 字符串 (钉钉口径不一, 双兼容)."""
    raw = payload.get("userId") or payload.get("userid") or payload.get("list") or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = [raw]
    return [str(u) for u in raw] if isinstance(raw, list) else [str(raw)]


def build_client() -> ds.DingTalkStreamClient:
    cred = ds.Credential(settings.dingtalk_app_key, settings.dingtalk_app_secret)
    client = ds.DingTalkStreamClient(cred)
    client.register_all_event_handler(OrgEventHandler())
    return client


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logger.info("启动 Stream 事件订阅 (app_key=%s...)", settings.dingtalk_app_key[:8])
    build_client().start_forever()


if __name__ == "__main__":
    main()
