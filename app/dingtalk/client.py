"""钉钉 API 客户端 — access_token 缓存 + 通讯录读取

设计纪律 (07 方案 §3.2):
- token 内存缓存, 提前 5 分钟刷新, 严禁逐请求取 token
- 同步(读钉钉)和存储(写本地库)解耦: 本模块只负责拿到钉钉原始数据
- 新版 API (api.dingtalk.com) 与旧版 (oapi.dingtalk.com) 共用同一个 token
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import httpx

from app.config import settings

_NEW_API = "https://api.dingtalk.com"
_OLD_API = "https://oapi.dingtalk.com"


class DingTalkError(RuntimeError):
    def __init__(self, message: str, errcode: int | str | None = None):
        super().__init__(message)
        self.errcode = errcode


class DingTalkClient:
    """线程安全的极简钉钉客户端 (仅 P0 需要的只读接口)."""

    def __init__(self, app_key: str | None = None, app_secret: str | None = None):
        self.app_key = app_key or settings.dingtalk_app_key
        self.app_secret = app_secret or settings.dingtalk_app_secret
        self._token: str = ""
        self._expires_at: float = 0.0
        self._lock = threading.Lock()
        self._http = httpx.Client(timeout=15)

    # ---------- token ----------

    def get_token(self) -> str:
        """返回有效 access_token, 过期前 300s 自动刷新."""
        with self._lock:
            if self._token and time.time() < self._expires_at - 300:
                return self._token
            resp = self._http.post(
                f"{_NEW_API}/v1.0/oauth2/accessToken",
                json={"appKey": self.app_key, "appSecret": self.app_secret},
            )
            resp.raise_for_status()
            data = resp.json()
            token = data.get("accessToken")
            if not token:
                raise DingTalkError(f"取 token 失败: {data}", errcode=data.get("code"))
            self._token = token
            self._expires_at = time.time() + int(data.get("expireIn", 7200))
            return self._token

    # ---------- 旧版 topapi 通用 POST ----------

    def _topapi(self, path: str, body: dict[str, Any]) -> Any:
        resp = self._http.post(
            f"{_OLD_API}{path}",
            params={"access_token": self.get_token()},
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode") != 0:
            raise DingTalkError(
                f"{path} 失败: errcode={data.get('errcode')} errmsg={data.get('errmsg')}",
                errcode=data.get("errcode"),
            )
        # 兼容两种返回: 带 result 包装 (/topapi/v2/*) 与平铺字段 (/topapi/user/getbyunionid)
        if "result" in data:
            return data["result"]
        return data

    # ---------- 扫码登录 (P0, 2026-10-16) ----------

    def exchange_user_token(self, auth_code: str) -> dict:
        """authCode -> 用户个人 accessToken.

        官方文档 (open.dingtalk.com/document/development/obtain-user-token):
        POST /v1.0/oauth2/userAccessToken  {clientId, clientSecret, code, grantType}
        grantType 固定 authorization_code.
        """
        resp = self._http.post(
            f"{_NEW_API}/v1.0/oauth2/userAccessToken",
            json={
                "clientId": self.app_key,
                "clientSecret": self.app_secret,
                "code": auth_code,
                "grantType": "authorization_code",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("accessToken"):
            raise DingTalkError(f"换取用户token失败: {data}", errcode=data.get("code"))
        return data

    def get_user_info_by_code(self, auth_code: str) -> dict:
        """authCode -> {unionId, nick, mobile?}.

        两步: 换个人 token -> GET /v1.0/contact/users/me (unionId 传字面量 me).
        官方文档: open.dingtalk.com/document/development/dingtalk-retrieve-user-information
        """
        user_token = self.exchange_user_token(auth_code)["accessToken"]
        resp = self._http.get(
            f"{_NEW_API}/v1.0/contact/users/me",
            headers={"x-acs-dingtalk-access-token": user_token},
        )
        if resp.status_code != 200:
            raise DingTalkError(
                f"获取用户信息失败: {resp.status_code} {resp.text[:200]}",
            )
        return resp.json()

    def get_userid_by_unionid(self, unionid: str) -> str:
        """unionId -> 企业内部 userid.

        官方文档 (open.dingtalk.com/document/orgapp-server/query-a-user-by-the-union-id):
        POST /topapi/user/getbyunionid  body {"unionid": ...}, result.userid
        """
        result = self._topapi("/topapi/user/getbyunionid", {"unionid": unionid})
        userid = result.get("userid")
        if not userid:
            raise DingTalkError(f"unionid 不在本组织: {unionid}")
        return userid

    # ---------- 消息通知 ----------

    def robot_send_oto(self, userids: list[str], title: str, text: str) -> dict:
        """机器人批量单聊通知 (markdown).

        官方文档 (open.dingtalk.com/document/orgapp/chatbots-send-one-on-one-chat-messages-in-batches):
        POST /v1.0/robot/oToMessages/batchSend
          body {robotCode, userIds(<=20), msgKey: "sampleMarkdown", msgParam: JSON串}
          header x-acs-dingtalk-access-token: <应用 token>
        权限要求: 企业内机器人发送消息权限。
        返回 {processQueryKey, invalidStaffIdList, flowControlledStaffIdList}
        """
        if not settings.dingtalk_robot_code:
            raise DingTalkError("未配置 DINGTALK_ROBOT_CODE, 无法发通知")
        resp = self._http.post(
            f"{_NEW_API}/v1.0/robot/oToMessages/batchSend",
            headers={"x-acs-dingtalk-access-token": self.get_token()},
            json={
                "robotCode": settings.dingtalk_robot_code,
                "userIds": userids[:20],
                "msgKey": "sampleMarkdown",
                "msgParam": json.dumps(
                    {"title": title, "text": text}, ensure_ascii=False
                ),
            },
        )
        data = resp.json() if resp.content else {}
        if resp.status_code != 200:
            raise DingTalkError(
                f"robot_send_oto 失败: {resp.status_code} {str(data)[:200]}",
                errcode=data.get("code"),
            )
        return data

    # ---------- 通讯录 ----------

    def get_department(self, dept_id: int = 1) -> dict:
        """单个部门信息 (含 member_count / 负责人)."""
        return self._topapi("/topapi/v2/department/get", {"dept_id": dept_id})

    def list_sub_departments(self, dept_id: int) -> list[dict]:
        """直接子部门列表."""
        return self._topapi("/topapi/v2/department/listsub", {"dept_id": dept_id}) or []

    def iter_all_departments(self) -> list[dict]:
        """BFS 拉全部门树, 返回扁平列表 (含根部门 id=1)."""
        root = self.get_department(1)
        root["parent_id"] = 0
        all_depts = [root]
        queue = [1]
        seen = {1}
        while queue:
            cur = queue.pop(0)
            for sub in self.list_sub_departments(cur):
                if sub["dept_id"] in seen:
                    continue
                seen.add(sub["dept_id"])
                all_depts.append(sub)
                queue.append(sub["dept_id"])
        return all_depts

    def get_user(self, userid: str) -> dict:
        """单个成员详情 (含 dept_id_list, Stream 事件增量重拉用)."""
        return self._topapi("/topapi/v2/user/get", {"userid": userid})

    def list_department_users(self, dept_id: int) -> list[dict]:
        """按部门分页拉全部成员 (size 上限 100, 循环 cursor)."""
        users: list[dict] = []
        cursor = 0
        while True:
            result = self._topapi(
                "/topapi/v2/user/list",
                {"dept_id": dept_id, "cursor": cursor, "size": 100},
            )
            users.extend(result.get("list") or [])
            if not result.get("has_more"):
                break
            next_cursor = result.get("next_cursor")
            if next_cursor is None or next_cursor == cursor:  # 防御: cursor 不前进则退出
                break
            cursor = next_cursor
        return users
