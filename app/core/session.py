"""会话 JWT — P0 扫码登录 (2026-10-16)

纪律 (07 方案 §3.1): JWT 只放 userid, 不放部门/权限 —
权限每次查询实时从 users 表取 (identity.py), 调岗/离职即时生效。
"""

from __future__ import annotations

import time

import jwt

from app.config import settings

_ALG = "HS256"


def issue_token(userid: str, name: str) -> str:
    now = int(time.time())
    payload = {
        "sub": userid,
        "name": name,
        "iat": now,
        "exp": now + settings.jwt_ttl_hours * 3600,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALG)


class SessionInvalid(RuntimeError):
    pass


def verify_token(token: str) -> str:
    """返回 userid; 无效/过期抛 SessionInvalid."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[_ALG])
    except jwt.PyJWTError as e:
        raise SessionInvalid(str(e)) from e
    userid = payload.get("sub")
    if not userid:
        raise SessionInvalid("missing sub")
    return userid
