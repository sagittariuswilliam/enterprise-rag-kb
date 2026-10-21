"""接口测试 - 健康检查 / 根页面 / 上传列表与状态(真实结构断言)

2026-09-21 清理: 删除 v1 时期的 "status=TODO" 占位断言
(/api/upload/list 与 /api/history 旧版占位接口已随 D4 改造演进/移除)。
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    """P1: /api/* 依赖真实身份(users 表) — 用临时库种 test-user, 不碰生产 db"""
    from app.config import settings
    import sqlite3

    db = tmp_path_factory.mktemp("apitest") / "kb_v2_test.db"
    old_db = settings.db_path
    settings.db_path = str(db)
    try:
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users(userid TEXT PRIMARY KEY, name TEXT,"
            " mobile TEXT, email TEXT, title TEXT, dept_ids TEXT DEFAULT '[]',"
            " leader INTEGER DEFAULT 0, admin INTEGER DEFAULT 0,"
            " boss INTEGER DEFAULT 0, active INTEGER DEFAULT 1, unionid TEXT,"
            " job_number TEXT, synced_at TEXT)")
        conn.execute(
            "INSERT OR REPLACE INTO users(userid,name,dept_ids,active,synced_at) "
            "VALUES('test-user','测试','[1]',1,'2026-10-16T00:00:00')")
        conn.commit(); conn.close()
        with TestClient(app) as c:
            yield c
    finally:
        settings.db_path = old_db


def test_health(client):
    """健康检查"""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "enterprise-rag-kb"
    assert data["version"] == "0.1.0"


def test_root(client):
    """根路由返回 HTML"""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "RAG 智能客服" in resp.text


def test_docs(client):
    """OpenAPI 文档可访问"""
    resp = client.get("/openapi.json")
    assert resp.status_code == 200


def test_upload_list(client):
    """上传列表返回真实文档结构 (documents/count) — 需带 JWT"""
    from app.core.session import issue_token
    token = issue_token("test-user", "测试")
    resp = client.get("/api/upload/list", params={"token": token})
    assert resp.status_code == 200
    data = resp.json()
    assert "documents" in data and "count" in data
    assert isinstance(data["documents"], list)
    assert data["count"] == len(data["documents"])


def test_upload_status_unknown(client):
    """未知 doc_id 的状态查询应 404"""
    from app.core.session import issue_token
    token = issue_token("test-user", "测试")
    resp = client.get("/api/upload/status/deadbeef", params={"token": token})
    assert resp.status_code == 404


def test_api_requires_token(client):
    """P0 扫码登录: /api/* 无 token -> 401"""
    resp = client.get("/api/upload/list")
    assert resp.status_code == 401


def test_api_rejects_bad_token(client):
    """伪造 token -> 401"""
    resp = client.get("/api/upload/list", params={"token": "garbage.token.here"})
    assert resp.status_code == 401


def test_auth_me_roundtrip(client):
    """签发->校验往返: 合法 JWT 在 /api/auth/me 返回 200/401 分支正确"""
    from app.core.session import issue_token
    resp = client.get("/api/auth/me", params={"token": "bad"})
    assert resp.status_code == 401
    # 合法签名但 userid 不在库 -> 403 (user inactive)
    token = issue_token("not-in-db-user", "幽灵")
    resp = client.get("/api/auth/me", params={"token": token})
    assert resp.status_code == 403
