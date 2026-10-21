"""身份模块 — 权限引擎只认本地 users 表 (钉钉是镜像, 不是权限本体)

纪律 (07 方案 §3.1): JWT 里只放 userid, 部门信息每次查询实时从库里取,
保证调岗即时生效。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.dingtalk.store import get_conn


class UserNotFound(RuntimeError):
    """用户不存在或已停用 (离职) — 登录入口即应拒绝."""


@dataclass
class Identity:
    userid: str
    name: str
    dept_ids: list[int] = field(default_factory=list)
    leader: bool = False
    admin: bool = False
    boss: bool = False


def get_identity(userid: str) -> Identity:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE userid=? AND active=1", (userid,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise UserNotFound(f"用户不存在或已停用: {userid}")
    return Identity(
        userid=row["userid"],
        name=row["name"],
        dept_ids=json.loads(row["dept_ids"] or "[]"),
        leader=bool(row["leader"]),
        admin=bool(row["admin"]),
        boss=bool(row["boss"]),
    )
