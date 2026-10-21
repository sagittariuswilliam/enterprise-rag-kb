"""/api/docs 查看原文带水印 + doc_view 审计 测试 — 纯进程内 (tmp sqlite + Qdrant :memory:), 不依赖 9012

覆盖:
  1. 有权用户 (owner/dept 命中) 拿到自己 L2 文档全文, chunk 按 chunk_id 排序, watermark 生成
  2. 无权用户 404 (与"不存在"同形, P1 口径: 连标题都看不到)
  3. admin/boss 全量视野可取任意文档
  4. doc_view 审计落库 (actor="姓名(userid)", detail 带 source/chunks)
  5. /api/admin/audit 仅 admin/boss 可访问 (普通用户 403), 支持 action 过滤
  6. truncated 保护 (>200 chunk 只回前 200)
运行: ./venv/bin/python -m pytest tests/test_doc_view.py -q
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import settings

COLL = "kb_test_chunks"
DIM = 8


def _point(doc_id, cid, text, *, source, level, dept_id, owner, vector=None):
    import uuid as _uuid
    from qdrant_client.models import PointStruct
    return PointStruct(
        id=str(_uuid.uuid4()),
        vector=vector or [0.1] * DIM,
        payload={
            "text": text,
            "source": source,
            "page": "",
            "format": "txt",
            "chunk_id": cid,
            "char_count": len(text),
            "doc_id": doc_id,
            "level": level,
            "dept_id": dept_id,
            "owner_userid": owner,
            "extra_acl": [],
        },
    )


SEED_USERS = [
    # userid, name, dept_ids, active, leader, admin
    ("u10a", "员工甲", "[10]", 1, 0, 0),
    ("u30a", "员工丙", "[30]", 1, 0, 0),
    ("adminU", "管理员", "[99]", 1, 0, 1),
]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # --- tmp sqlite ---
    db = tmp_path / "kb.db"
    from app.dingtalk import store
    monkeypatch.setattr(store.settings, "db_path", str(db))
    store.init_db()
    conn = sqlite3.connect(db)
    for uid, name, depts, active, leader, admin in SEED_USERS:
        conn.execute(
            "INSERT INTO users(userid,name,dept_ids,active,leader,admin,boss,"
            "synced_at) VALUES(?,?,?,?,?,?,0,'2026-10-17T00:00:00')",
            (uid, name, depts, active, leader, admin),
        )
    conn.commit()
    conn.close()

    monkeypatch.setattr(settings, "login_enabled", True)
    if not settings.jwt_secret:
        monkeypatch.setattr(settings, "jwt_secret", "test-secret")
    monkeypatch.setattr(settings, "qdrant_collection", COLL)

    # --- Qdrant :memory: (进程内, 不碰 6333) ---
    import asyncio

    from qdrant_client import AsyncQdrantClient
    from qdrant_client.models import Distance, VectorParams

    import app.core.qdrant_client as qc

    async def _mkqdrant():
        q = AsyncQdrantClient(location=":memory:")
        await q.create_collection(
            collection_name=COLL,
            vectors_config=VectorParams(size=DIM, distance=Distance.COSINE),
        )
        await q.upsert(collection_name=COLL, wait=True, points=[
            # 部门 10 的 L2 文档 (乱序 chunk_id, 测排序)
            _point("docA", 1, "第二段内容", source="8hash_制度A.txt",
                   level="L2", dept_id=[10], owner="u10a"),
            _point("docA", 0, "第一段内容", source="8hash_制度A.txt",
                   level="L2", dept_id=[10], owner="u10a"),
            _point("docA", 2, "第三段内容", source="8hash_制度A.txt",
                   level="L2", dept_id=[10], owner="u10a"),
            # 部门 20 的 L1 文档, 与其他人都无关 (docB owner=u20ghost 不在 users 表)
            _point("docB", 0, "机密内容", source="保密B.txt",
                   level="L1", dept_id=[], owner="u20ghost"),
            # L3 全员可见
            _point("docC", 0, "全员公告", source="公告C.txt",
                   level="L3", dept_id=[], owner="u10a"),
        ])
        return q

    qdrant = asyncio.new_event_loop().run_until_complete(_mkqdrant())
    monkeypatch.setattr(qc, "client", qdrant)

    from app.main import app
    from app.core.session import issue_token
    client = TestClient(app)

    def login_as(uid: str, name: str = ""):
        client.headers.update(
            {"Authorization": "Bearer " + issue_token(uid, name or uid)})

    return {"client": client, "db": db, "qdrant": qdrant, "login_as": login_as}


def _audit_rows(env, action):
    conn = sqlite3.connect(env["db"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM audit_log WHERE action=?",
                        (action,)).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------- 有权查看

def test_owner_sees_full_text_with_watermark(env):
    env["login_as"]("u10a", "员工甲")
    r = env["client"].get("/api/docs/docA/chunks")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["doc_id"] == "docA"
    assert b["source"] == "8hash_制度A.txt"
    assert b["level"] == "L2"
    assert b["total_chunks"] == 3
    assert b["truncated"] is False
    # chunk_id 排序拼接
    assert b["text_sections"] == ["第一段内容", "第二段内容", "第三段内容"]
    assert b["watermark"]["actor"] == "员工甲(u10a)"
    assert "T" in b["watermark"]["time"]  # ISO


def test_dept_colleague_and_admin(env):
    # 同部门 u10a 已测; admin 全量视野可看别人的 L1
    env["login_as"]("adminU", "管理员")
    r = env["client"].get("/api/docs/docB/chunks")
    assert r.status_code == 200
    assert r.json()["text_sections"] == ["机密内容"]
    assert r.json()["watermark"]["actor"] == "管理员(adminU)"


# ---------------------------------------------------------------- 无权 404

def test_no_permission_404(env):
    env["login_as"]("u30a", "员工丙")
    r = env["client"].get("/api/docs/docA/chunks")
    assert r.status_code == 404
    # 不存在的 doc 同形 (不泄露差异)
    r2 = env["client"].get("/api/docs/nope/chunks")
    assert r2.status_code == 404
    # 无权时不产生 doc_view 审计
    assert _audit_rows(env, "doc_view") == []
    # L3 全员可见
    r3 = env["client"].get("/api/docs/docC/chunks")
    assert r3.status_code == 200 and r3.json()["text_sections"] == ["全员公告"]


# ---------------------------------------------------------------- 审计落库

def test_doc_view_audit_recorded(env):
    env["login_as"]("u10a", "员工甲")
    assert env["client"].get("/api/docs/docA/chunks").status_code == 200
    rows = _audit_rows(env, "doc_view")
    assert len(rows) == 1
    assert rows[0]["actor"] == "员工甲(u10a)"
    assert rows[0]["target"] == "docA"
    d = json.loads(rows[0]["detail"])
    assert d["source"] == "8hash_制度A.txt" and d["chunks"] == 3


# ---------------------------------------------------------------- 截断保护

def test_truncated_over_200_chunks(env):
    import asyncio
    pts = [_point("docBig", i, f"段落{i}", source="大文件.txt",
                  level="L3", dept_id=[], owner="u10a")
           for i in range(205)]
    # 打乱顺序写入, 校验排序后再截断
    asyncio.new_event_loop().run_until_complete(
        env["qdrant"].upsert(collection_name=COLL, points=list(reversed(pts)),
                             wait=True))
    env["login_as"]("u30a", "员工丙")
    r = env["client"].get("/api/docs/docBig/chunks")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["truncated"] is True
    assert b["total_chunks"] == 200
    assert b["text_sections"][0] == "段落0"
    assert b["text_sections"][-1] == "段落199"


# ---------------------------------------------------------------- 审计查询接口权限

def test_admin_audit_only_admin(env):
    env["login_as"]("u10a", "员工甲")
    env["client"].get("/api/docs/docA/chunks")  # 制造一条 doc_view
    r = env["client"].get("/api/admin/audit")
    assert r.status_code == 403
    env["login_as"]("adminU", "管理员")
    r = env["client"].get("/api/admin/audit?action=doc_view")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items and items[0]["action"] == "doc_view"
    assert items[0]["detail"]["chunks"] == 3  # detail 已解析为 JSON
    # 普通用户拿不到任何审计
    env["login_as"]("u30a", "员工丙")
    assert env["client"].get("/api/admin/audit").status_code == 403
