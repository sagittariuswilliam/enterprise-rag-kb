"""钉钉扫码登录路由 — P0 (2026-10-16)

流程 (官方: open.dingtalk.com/document/orgapp/tutorial-obtaining-user-personal-information):
1. GET /api/auth/login-url          -> 前端拿授权页 URL (含 state)
2. 用户在钉钉登录页授权 -> 302 回 GET /api/auth/callback?code=&state=
3. callback: authCode -> 用户token -> /contact/users/me 拿 unionId
             -> getbyunionid 拿企业 userid -> users 表校验 active
             -> 签发会话 JWT (sub=userid, 8h)
4. 前端 GET /api/auth/me 用 JWT 拿身份展示。

state 用 HMAC 短时效签名防 CSRF (无 Redis, 单实例够用)。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from app.acl.identity import UserNotFound, get_identity
from app.config import settings
from app.core.logger import get_logger
from app.core.session import SessionInvalid, issue_token, verify_token
from app.dingtalk.client import DingTalkClient, DingTalkError

logger = get_logger(__name__)

router = APIRouter()

_AUTH_BASE = "https://login.dingtalk.com/oauth2/auth"
_STATE_TTL = 600  # 10 分钟内必须完成授权

_client: DingTalkClient | None = None


def get_client() -> DingTalkClient:
    global _client
    if _client is None:
        _client = DingTalkClient()
    return _client


# ---------- state: HMAC(userid无关) 时效签名 ----------
# M3: 支持第 4 段 next 回跳路径 (受控链接页 /p/{token} 登录后回原页),
# next 参与 HMAC 签名防篡改; 缺省为空 = 回首页。

def _make_state(next_path: str = "") -> str:
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    payload = f"{ts}.{nonce}.{next_path}"
    sig = hmac.new(
        settings.jwt_secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:16]
    return f"{ts}.{nonce}.{sig}" + (f".{next_path}" if next_path else "")


def _check_state(state: str) -> bool:
    parts = state.split(".")
    if len(parts) < 3:
        return False
    ts, nonce, sig = parts[0], parts[1], parts[2]
    next_path = ".".join(parts[3:]) if len(parts) > 3 else ""
    if not ts.isdigit() or time.time() - int(ts) > _STATE_TTL:
        return False
    payload = f"{ts}.{nonce}.{next_path}"
    expect = hmac.new(
        settings.jwt_secret.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()[:16]
    return hmac.compare_digest(sig, expect)


def _state_next(state: str) -> str:
    """从 state 提取回跳路径 (先过 _check_state); 无/非法 → '/'。"""
    parts = state.split(".")
    if len(parts) <= 3:
        return "/"
    nxt = ".".join(parts[3:])
    if nxt.startswith("/p/") or nxt.startswith("/package"):
        return nxt
    return "/"


def _redirect_uri(request_base: str) -> str:
    """回调地址 = 本站 /api/auth/callback.

    后台「重定向URL」需填完全一致的值 (含端口)。
    通过 X-Forwarded-* (nginx 反代) 还原公网 https 地址。
    """
    return f"{request_base}/api/auth/callback"


# ---------- 1. 前端获取授权链接 ----------

@router.get("/auth/login-url")
async def login_url(base: str = Query(..., description="站点公网前缀, 如 https://kb.example.com:9443"),
                    next: str = Query("", description="M3: 登录成功后回跳路径, 仅允许 /p/… 或 /package…")):
    if not settings.login_enabled:
        raise HTTPException(404, "登录未启用")
    if not settings.dingtalk_app_key:
        raise HTTPException(500, "缺少 DINGTALK_APP_KEY")
    nxt = next if (next.startswith("/p/") or next.startswith("/package")) else ""
    uri = _redirect_uri(base.rstrip("/"))
    url = (
        f"{_AUTH_BASE}?redirect_uri={quote(uri, safe='')}"
        f"&response_type=code&client_id={settings.dingtalk_app_key}"
        f"&scope={'openid'}&prompt=consent&state={_make_state(nxt)}"
    )
    return {"login_url": url, "redirect_uri": uri}


# ---------- 2. 钉钉回调 ----------

_FAIL_HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>body{{font-family:sans-serif;display:flex;justify-content:center;
align-items:center;height:100vh;margin:0;color:#334155}}</style></head>
<body><div><h3>登录失败</h3><p>{msg}</p><p><a href="/">返回重试</a></p></div></body></html>"""


@router.get("/auth/callback")
async def callback(
    code: str = Query("", alias="code"),
    authCode: str = Query("", alias="authCode"),
    state: str = Query("", alias="state"),
    error: str = Query("", alias="error"),
):
    if error:
        return HTMLResponse(_FAIL_HTML.format(msg=f"钉钉返回错误: {error}"), status_code=400)
    # 文档: code 与 authCode 一致, 取任一
    auth_code = code or authCode
    if not auth_code:
        return HTMLResponse(_FAIL_HTML.format(msg="缺少授权码"), status_code=400)
    if not _check_state(state):
        return HTMLResponse(_FAIL_HTML.format(msg="state 校验失败, 请重新发起登录"), status_code=400)

    cli = get_client()
    try:
        info = cli.get_user_info_by_code(auth_code)
        unionid = info.get("unionId") or info.get("unionid")
        if not unionid:
            raise DingTalkError(f"未取到 unionId: {info}")
        userid = cli.get_userid_by_unionid(unionid)
    except DingTalkError as e:
        logger.warning(f"扫码登录钉钉侧失败: {e}")
        return HTMLResponse(_FAIL_HTML.format(msg="授权验证失败, 请稍后重试"), status_code=502)

    # 本地库校验 (离职/未同步的人直接拒绝)
    try:
        ident = get_identity(userid)
    except UserNotFound:
        logger.warning(f"扫码登录被拒: userid={userid} 不在 users 表或已停用")
        return HTMLResponse(_FAIL_HTML.format(msg="你的账号不在知识库授权名单内（或已停用）"), status_code=403)

    token = issue_token(ident.userid, ident.name)
    logger.info(f"扫码登录成功: {ident.name}({ident.userid})")
    # 用同源页面把 token 写 localStorage 后跳转 (前端自动识别 ?token=)
    # M3: state 第4段携带 next 回跳 (受控链接页), 非法/缺省 → 首页
    nxt = _state_next(state)
    html = f"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<p style="font-family:sans-serif;color:#334155">登录成功，正在进入知识库…</p>
<script>localStorage.setItem('rag_token','{token}');location.replace('{nxt}');</script>
</body></html>"""
    return HTMLResponse(html)


# ---------- 3. 前端用 JWT 换身份 ----------

@router.get("/auth/me")
async def me(token: str = Query(...)):
    try:
        userid = verify_token(token)
    except SessionInvalid:
        return JSONResponse({"detail": "invalid token"}, status_code=401)
    try:
        ident = get_identity(userid)
    except UserNotFound:
        return JSONResponse({"detail": "user inactive"}, status_code=403)
    from app.acl.gray import is_open  # P3: 前端据此渲染"即将开放"页
    return {
        "userid": ident.userid,
        "name": ident.name,
        "dept_ids": ident.dept_ids,
        "leader": ident.leader,
        "admin": ident.admin,
        "boss": ident.boss,
        "gray_open": is_open(ident),
    }
