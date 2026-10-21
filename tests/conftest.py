"""测试全局夹具 — 未配置 .env 时注入测试专用密钥, 保证 clone 后开箱即跑"""
from app.config import settings

if not settings.jwt_secret:
    settings.jwt_secret = "test-onl…cret"
