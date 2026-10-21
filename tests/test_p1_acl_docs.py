"""P1 测试 — 文档级 ACL 写入/校验/过滤

层 1: build_doc_acl 参数校验 (纯单测)
层 2: 真 Qdrant 临时 collection 验证 build_acl_filter 过滤语义 (端到端,
      手动插向量, 不走 embedder)
层 3: upload 接口密级参数 400 校验 + directory 接口 (TestClient)
"""

import json
import uuid

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

from app.acl.doc_acl import (
    AclError,
    build_doc_acl,
    can_modify,
    normalize_extra_acl,
)
from app.acl.filter import build_acl_filter
from app.acl.identity import Identity

QDRANT_URL = "http://localhost:6333"
DIM = 4  # 测试用迷你向量维度


# ============ 层 1: 写侧构造 ============

def test_build_doc_acl_l2_requires_dept():
    with pytest.raises(AclError):
        build_doc_acl("L2", owner_userid="u1")
    acl = build_doc_acl("L2", owner_userid="u1", dept_ids=["5"])
    assert acl == {"level": "L2", "dept_id": [5],
                   "owner_userid": "u1", "extra_acl": []}


def test_build_doc_acl_l4_requires_target():
    with pytest.raises(AclError):
        build_doc_acl("L4", owner_userid="u1")
    acl = build_doc_acl("L4", owner_userid="u1", l4_users=["zhang"],
                        l4_depts=["7"])
    assert acl["extra_acl"] == ["zhang", "dept:7"]


def test_build_doc_acl_bad_level():
    with pytest.raises(AclError):
        build_doc_acl("L5", owner_userid="u1")
    with pytest.raises(AclError):
        build_doc_acl("L2", owner_userid="u1", dept_ids=["abc"])


def test_normalize_extra_acl_injection_guard():
    with pytest.raises(AclError):
        normalize_extra_acl(["dept:999"], [])  # userid 冒充部门前缀 → 拒绝


def test_can_modify():
    owner = Identity(userid="u1", name="A", dept_ids=[1])
    other = Identity(userid="u2", name="B", dept_ids=[1])
    admin = Identity(userid="u3", name="C", dept_ids=[1], admin=True)
    acl = {"level": "L2", "dept_id": [1], "owner_userid": "u1", "extra_acl": []}
    assert can_modify(owner, acl) is True
    assert can_modify(other, acl) is False   # 同部门也不能删别人的
    assert can_modify(admin, acl) is True


# ============ 层 2: 真 Qdrant 过滤语义 ============

@pytest.fixture(scope="module")
def qdrant():
    c = QdrantClient(url=QDRANT_URL)
    name = f"p1_test_{uuid.uuid4().hex[:8]}"
    c.create_collection(name, vectors_config={"size": DIM, "distance": "Cosine"})
    yield c, name
    c.delete_collection(name)


def _seed(qdrant):
    client, coll = qdrant
    points = [
        # (id, level, dept_ids, owner, extra_acl)
        ("doc-L3",  "L3", [],          "boss1", []),
        ("doc-L2a", "L2", [10],        "u10a",  []),
        ("doc-L2b", "L2", [20],        "u20a",  []),
        ("doc-L1",  "L1", [],          "u10a",  []),
        ("doc-L4",  "L4", [],          "boss1", ["u20a", "dept:30"]),
    ]
    pts = []
    for i, (doc_id, level, depts, owner, extra) in enumerate(points):
        vec = [1.0 if j == i % DIM else 0.1 for j in range(DIM)]
        pts.append(PointStruct(
            id=str(uuid.uuid4()), vector=vec,
            payload={"text": doc_id, "source": doc_id, "doc_id": doc_id,
                     "level": level, "dept_id": depts,
                     "owner_userid": owner, "extra_acl": extra}))
    client.upsert(collection_name=coll, points=pts, wait=True)


def test_qdrant_acl_filter_visible_sets(qdrant):
    """核心验收: 同库不同人看到不同集合, 越权文档连标题都不召回"""
    _seed(qdrant)
    client, coll = qdrant

    def visible_docs(userid, dept_ids):
        ident = Identity(userid=userid, name="x", dept_ids=dept_ids)
        res = client.query_points(
            collection_name=coll,
            query=[1.0] * DIM,
            limit=10,
            query_filter=build_acl_filter(ident),
            with_payload=True,
        )
        return {p.payload["doc_id"] for p in res.points}

    # u10a 部门10: 自己的 L3 + L2a + 自己的 L1, 看不到 L2b/L4
    assert visible_docs("u10a", [10]) == {"doc-L3", "doc-L2a", "doc-L1"}
    # u20a 部门20: L2b + 被 L4 指名 → 4 个, 看不到 L2a/L1
    assert visible_docs("u20a", [20]) == {"doc-L3", "doc-L2b", "doc-L4"}
    # u30a 部门30 (仅部门被 L4 指名)
    assert visible_docs("u30a", [30]) == {"doc-L3", "doc-L4"}
    # 多部门并集: 部门 10+20 — L4 文档指名的是 u20a 本人, 不是部门20, 所以看不到
    assert visible_docs("u-mix", [10, 20]) == {
        "doc-L3", "doc-L2a", "doc-L2b"}


def test_qdrant_scroll_with_acl_filter(qdrant):
    """list_documents 同款 scroll filter — 无权限 source 不出现"""
    _seed(qdrant)
    client, coll = qdrant
    ident = Identity(userid="u20a", name="x", dept_ids=[20])
    points, _ = client.scroll(
        collection_name=coll, limit=50, with_payload=True,
        scroll_filter=build_acl_filter(ident))
    sources = {p.payload["source"] for p in points}
    assert sources == {"doc-L3", "doc-L2b", "doc-L4"}


# ============ 层 3: API 校验 ============

@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app.dingtalk import store
    db = tmp_path / "kb.db"
    monkeypatch.setattr(store.settings, "db_path", str(db))
    store.init_db()
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO users(userid,name,dept_ids,active,leader,admin,boss,synced_at) "
        "VALUES('u10a','测试员','[10]',1,0,0,0,'2026-10-16T00:00:00')")
    conn.execute(
        "INSERT INTO depts(dept_id,name,parent_id,active,synced_at) "
        "VALUES(10,'测试部',0,1,'2026-10-16T00:00:00')")
    conn.commit(); conn.close()

    # 开登录 + 发真 JWT
    from app.config import settings
    monkeypatch.setattr(settings, "login_enabled", True)
    if not settings.jwt_secret:
        monkeypatch.setattr(settings, "jwt_secret", "test-secret")

    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.session import issue_token
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer " + issue_token("u10a", "测试员")})
    return c


def test_upload_missing_level_rejected(client):
    resp = client.post("/api/upload",
                       files={"file": ("a.txt", b"content", "text/plain")})
    assert resp.status_code == 422  # level 必传


def test_upload_l2_without_dept_rejected(client):
    resp = client.post(
        "/api/upload",
        files={"file": ("a.txt", b"content", "text/plain")},
        data={"level": "L2"},
    )
    assert resp.status_code == 400
    assert "部门" in resp.json()["detail"]


def test_upload_l4_empty_target_rejected(client):
    resp = client.post(
        "/api/upload",
        files={"file": ("a.txt", b"content", "text/plain")},
        data={"level": "L4"},
    )
    assert resp.status_code == 400


def test_directory_search_from_mirror(client):
    resp = client.get("/api/directory/search?q=测试")
    assert resp.status_code == 200
    users = resp.json()["users"]
    assert any(u["userid"] == "u10a" for u in users)
    assert users[0]["dept_names"] == ["测试部"]

    resp2 = client.get("/api/directory/depts")
    assert resp2.status_code == 200
    assert resp2.json()["count"] == 1
