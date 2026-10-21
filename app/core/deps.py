"""FastAPI 依赖 — JWT → Identity (实时查 users 表, 调岗即时生效)

前提: main.py 的 auth_middleware 已验签 JWT 并把 userid 挂到 request.state。
本依赖只负责把 userid 换成完整 Identity (部门/admin 标记实时从库取)。

login_enabled=False 时 (本地开发/测试) 返回一个 admin 身份, 不查库。
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from app.acl.identity import Identity, UserNotFound, get_identity
from app.config import settings


def get_current_identity(request: Request) -> Identity:
    if not settings.login_enabled:
        return Identity(userid="dev", name="开发者", dept_ids=[], leader=False,
                        admin=True, boss=True)
    userid = getattr(request.state, "userid", None)
    if not userid:
        # 中间件未挂 (login_enabled 打开后不应发生) — 按未登录处理
        raise HTTPException(status_code=401, detail="unauthorized")
    try:
        return get_identity(userid)
    except UserNotFound:
        raise HTTPException(
            status_code=403,
            detail="账号不在通讯录或已停用, 请联系管理员",
        )


def require_gray_open(ident: Identity) -> Identity:
    """P3 灰度闸门 — 问答/上传等使用入口统一挂此依赖 (admin/boss 直通)。

    纪律: 新增使用类入口必挂本依赖 (同 build_acl_filter 唯一出口逻辑, F1 教训)。
    """
    from app.acl.gray import is_open  # 局部 import 避免启动期查库

    if not is_open(ident):
        raise HTTPException(
            status_code=403,
            detail="本部门尚在灰度开放批次安排中，开放后将通过钉钉通知",
        )
    return ident
