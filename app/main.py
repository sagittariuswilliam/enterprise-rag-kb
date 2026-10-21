"""RAG 智能客服 - FastAPI 入口（端口 9012）"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from app.api import admin, auth, chat, directory, docs, history, l4, package, review, upload
from app.config import settings
from app.core.logger import get_logger, setup_logging
from app.core.qdrant_client import close_qdrant, init_qdrant

setup_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ===== 启动 =====
    logger.info("🚀 RAG 客服启动中...")
    # V2 业务库建表 (幂等 CREATE IF NOT EXISTS) — 原先只靠钉钉同步入口触发,
    # 新表(l4_approvals/audit_log)必须在服务启动时就位, 不等同步
    from app.dingtalk.store import init_db
    init_db()
    try:
        await init_qdrant()
        logger.info("✅ Qdrant 连接成功")
    except Exception as e:
        logger.error(f"❌ Qdrant 连接失败: {e}")

    # D4: 注入主事件循环 + 启动上传后台工作线程池
    import asyncio

    from app.services.upload_worker import get_pool, set_main_loop
    set_main_loop(asyncio.get_running_loop())
    get_pool().start()

    yield

    # ===== 关闭 =====
    try:
        get_pool().shutdown()
    except Exception as e:
        logger.warning(f"关闭上传工作池出错: {e}")
    try:
        await close_qdrant()
    except Exception as e:
        logger.warning(f"关闭 Qdrant 出错: {e}")
    logger.info("👋 RAG 客服关闭")


app = FastAPI(
    title="RAG 智能客服",
    description="企业级权限感知 RAG 知识库 (FastAPI + Qdrant + OpenAI 兼容 LLM)",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS - 允许前端跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== API 鉴权中间件 =====
# 2026-09-18: 用户拍板去掉鉴权(仅自用)。
# 2026-10-16 P0: 扫码登录上线, /api/* 重新强制 JWT (?token= 或 Authorization Bearer)。
# 豁免: /api/auth/* (登录入口本身)。
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if settings.login_enabled and path.startswith("/api/") and not path.startswith("/api/auth/"):
        from app.core.session import SessionInvalid, verify_token

        token = request.query_params.get("token") or ""
        if not token:
            hdr = request.headers.get("Authorization", "")
            if hdr.startswith("Bearer "):
                token = hdr[7:]
        try:
            userid = verify_token(token)
        except SessionInvalid:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        request.state.userid = userid
    return await call_next(request)


# 健康检查
@app.get("/health")
async def health():
    return {"status": "ok", "service": "enterprise-rag-kb", "version": "0.1.0"}


# 根路由 - 返回前端 SPA
from fastapi.staticfiles import StaticFiles
import os
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 根路径优先返回 index.html
INDEX_PATH = os.path.join(STATIC_DIR, "index.html")

@app.get("/", response_class=HTMLResponse)
async def root():
    if os.path.isfile(INDEX_PATH):
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            return f.read()
    return """<html><body>RAG 智能客服 - 前端未找到</body></html>"""


# 挂载 API 路由
app.include_router(auth.router, prefix="/api", tags=["auth"])
app.include_router(directory.router, prefix="/api", tags=["directory"])
app.include_router(upload.router, prefix="/api", tags=["upload"])
app.include_router(chat.router, prefix="/api", tags=["chat"])
app.include_router(history.router, prefix="/api", tags=["history"])
app.include_router(admin.router, prefix="/api", tags=["admin"])
app.include_router(l4.router, prefix="/api", tags=["l4"])
app.include_router(review.router, prefix="/api", tags=["review"])
app.include_router(docs.router, prefix="/api", tags=["docs"])
app.include_router(package.router, prefix="/api", tags=["package"])

# 管理端控制台 SPA
ADMIN_PATH = os.path.join(STATIC_DIR, "admin.html")


@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    if os.path.isfile(ADMIN_PATH):
        with open(ADMIN_PATH, "r", encoding="utf-8") as f:
            return f.read()
    return "<html><body>管理端未部署</body></html>"


# M3 受控链接页 (P4 打包): /p/{token} — 页面本身不含数据,
# 数据走 /api/package/open/{token} (权限在 API 层: 仅 creator + 有效期)。
# 未登录时前端引导钉钉扫码, state 携带 next 回跳本页。
PACKAGE_PATH = os.path.join(STATIC_DIR, "package.html")


@app.get("/p/{token}", response_class=HTMLResponse)
async def package_page(token: str):
    import re as _re
    if not _re.fullmatch(r"[0-9a-f]{32}", token or ""):
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "链接无效</body></html>", status_code=404)
    if os.path.isfile(PACKAGE_PATH):
        with open(PACKAGE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    return "<html><body>受控链接页未部署</body></html>"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
