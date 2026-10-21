"""#5 扫描件校对队列测试 — 纯进程内 (tmp sqlite + Qdrant :memory: + fake embedder)

覆盖:
  1. worker: 低置信 OCR → needs_review (队列落行, 未入库, 未登记哈希, 文件保留)
  2. worker: 高质量 OCR → 照常自动 ingested (防误杀卡死回归)
  3. approve: 入库 chunks>0 + 登记哈希 + 台账 ingested + 删文件 + 审计; 双击 409
  4. approve 带编辑稿 text → 入库内容是编辑稿
  5. reject: 删文件 + 台账移除 + 不登记哈希; 空 reason 400
  6. 权限: 普通员工 queue 403; leader 只见本部门
  7. 重启豁免: needs_review 台账不被标 failed
运行: ./venv/bin/python -m pytest tests/test_review_queue.py -q
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import settings

COLL = "kb_test_review"
DIM = 8

# 单页 ~92 字的 4 页文本: 过门禁(369>=300 且页均 92>=80) 但触发 sparse(页均<120)
LOWCONF_TEXT = ("本条款规定员工加班后应当在次月内安排调休，"
                "逾期未休的按小时折算补贴发放到工资卡里。") * 9  # 369 字
GOOD_TEXT = ("本公司信息系统运维管理办法依据国家有关法律法规制定，"
             "适用于总部及下属各单位的信息系统运行维护工作。"
             "运维值班人员应当每两小时巡检一次机房温湿度与设备状态，"
             "发现告警立即按流程上报并记录处置台账。") * 16  # ~1600 字


class FakeEmbedder:
    def embed(self, texts, **kw):
        return np.tile(np.full(DIM, 0.1, dtype=np.float32), (len(texts), 1))

    def embed_query(self, q):
        return np.full(DIM, 0.1, dtype=np.float32)

    @property
    def dim(self):
        return DIM


SEED_USERS = [
    ("u10a", "员工甲", "[10]", 1, 0, 0),
    ("leadA", "主管A", "[10]", 1, 1, 0),
    ("u30a", "员工丙", "[30]", 1, 0, 0),
    ("leadB", "主管B", "[30]", 1, 1, 0),
    ("adminU", "管理员", "[99]", 1, 0, 1),
]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # --- tmp sqlite ---
    db = tmp_path / "kb.db"
    from app.dingtalk import store as dt_store
    monkeypatch.setattr(dt_store.settings, "db_path", str(db))
    dt_store.init_db()
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

    # --- Qdrant :memory: ---
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.models import Distance, VectorParams
    import app.core.qdrant_client as qc

    async def _mkq():
        q = AsyncQdrantClient(location=":memory:")
        await q.create_collection(
            collection_name=COLL,
            vectors_config=VectorParams(size=DIM, distance=Distance.COSINE),
        )
        return q

    qdrant = asyncio.new_event_loop().run_until_complete(_mkq())
    monkeypatch.setattr(qc, "client", qdrant)

    # --- fake embedder (retriever 内部 get_embedder()) ---
    import app.services.retriever as ret
    monkeypatch.setattr(ret, "get_embedder", lambda *a, **k: FakeEmbedder())

    # --- tmp 任务台账 ---
    import app.services.upload_tasks as ut
    fake_ts = ut.TaskStore(path=tmp_path / "upload_tasks.json")
    monkeypatch.setattr(ut, "_store", fake_ts)

    # --- tmp 哈希注册表 ---
    import app.services.hash_registry as hr
    monkeypatch.setattr(hr, "HASH_REGISTRY", tmp_path / "hashes.json")

    # --- worker 的 _run_coro → 直接 asyncio.run ---
    import app.services.upload_worker as uw
    monkeypatch.setattr(uw, "_run_coro", lambda coro: asyncio.run(coro))

    from app.main import app as fastapp
    from app.core.session import issue_token
    client = TestClient(fastapp)

    def login_as(uid, name=""):
        client.headers.update(
            {"Authorization": "Bearer " + issue_token(uid, name or uid)})

    def make_file(doc_id, content=b"fake scanned pdf"):
        p = tmp_path / "uploads"
        p.mkdir(exist_ok=True)
        f = p / f"{doc_id}_扫描件.pdf"
        f.write_bytes(content)
        return f

    def seed_review(doc_id, *, owner="u10a", filename="扫描件.pdf",
                    text=LOWCONF_TEXT, file_path=None, route="scanned_pdf"):
        from app.services.review_queue import create_pending
        from app.services.gate import sha256_of_file
        f = file_path or make_file(doc_id)
        create_pending(
            doc_id, filename=filename, owner_userid=owner,
            acl={"level": "L2", "dept_id": [10], "owner_userid": owner,
                 "extra_acl": []},
            file_path=str(f), file_hash=sha256_of_file(f), route=route,
            size=f.stat().st_size, pages=4,
            issues=[{"rule": "sparse", "msg": "test", "value": 88}],
            suggested_text=text,
        )
        fake_ts.create(doc_id, filename=filename, size=f.stat().st_size,
                       route=route, file_path=str(f), status="needs_review")
        return f

    return {"client": client, "db": db, "qdrant": qdrant, "login_as": login_as,
            "tmp": tmp_path, "make_file": make_file, "seed_review": seed_review,
            "ts": fake_ts}


def _audit(env, action):
    conn = sqlite3.connect(env["db"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM audit_log WHERE action=?",
                        (action,)).fetchall()
    conn.close()
    return rows


def _qdrant_points(env, doc_id):
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    res = asyncio.new_event_loop().run_until_complete(
        env["qdrant"].scroll(
            collection_name=COLL,
            scroll_filter=Filter(must=[FieldCondition(
                key="doc_id", match=MatchValue(value=doc_id))]),
            with_payload=True, with_vectors=False, limit=50))
    pts, _ = res if isinstance(res, tuple) else (res.points, None)
    return pts


# ============================================================ worker 分支

def test_worker_lowconf_goes_to_review(env, monkeypatch):
    """低置信 OCR → needs_review: 队列落行/未入库/未登记哈希/文件保留"""
    import app.services.upload_worker as uw
    from app.services.parser import ParsedDoc

    monkeypatch.setattr(uw, "parse_pdf_scanned",
                        lambda p, progress_cb=None: ParsedDoc(
                            LOWCONF_TEXT, {"pages": 4, "format": "pdf_scanned",
                                           "parse_method": "ocr"}))
    f = env["make_file"]("r1")
    # 真实链路里台账行由 upload API create, worker 只 update — 测试预建
    env["ts"].create("r1", filename="扫描件.pdf", size=1, route="scanned_pdf",
                     file_path=str(f), status="queued")
    res = uw.process_document(
        doc_id="r1", file_path=f, route="scanned_pdf",
        file_hash="hash-r1", filename="扫描件.pdf",
        acl={"level": "L2", "dept_id": [10], "owner_userid": "u10a",
             "extra_acl": []})
    assert res["status"] == "needs_review", res
    assert res["signals"] and res["signals"][0]["rule"] == "sparse"
    # 队列有行且 pending, issues/suggested_text 持久
    from app.services import review_queue
    row = review_queue.get("r1")
    assert row and row["status"] == "pending" and row["pages"] == 4
    assert row["suggested_text"] == LOWCONF_TEXT
    assert json.loads(row["issues"])[0]["rule"] == "sparse"
    # 未入库 / 未登记哈希 / 文件保留
    assert _qdrant_points(env, "r1") == []
    from app.services.hash_registry import load_hash_registry
    assert "hash-r1" not in load_hash_registry()
    assert f.exists()
    # 台账 needs_review
    assert env["ts"].get("r1")["status"] == "needs_review"


def test_worker_good_ocr_still_ingests(env, monkeypatch):
    """高质量 OCR → 自动入库 (防误杀回归)"""
    import app.services.upload_worker as uw
    from app.services.parser import ParsedDoc

    monkeypatch.setattr(uw, "parse_pdf_scanned",
                        lambda p, progress_cb=None: ParsedDoc(
                            GOOD_TEXT, {"pages": 4, "format": "pdf_scanned",
                                        "parse_method": "ocr"}))
    f = env["make_file"]("r2")
    res = uw.process_document(
        doc_id="r2", file_path=f, route="scanned_pdf",
        file_hash="hash-r2", filename="好扫描件.pdf",
        acl={"level": "L3", "dept_id": [], "owner_userid": "u10a",
             "extra_acl": []})
    assert res["status"] == "ingested", res
    assert res["chunks"] > 0
    from app.services import review_queue
    assert review_queue.get("r2") is None


# ============================================================ approve

def test_approve_ingests_marks_and_doubles_409(env):
    f = env["seed_review"]("a1")
    env["login_as"]("adminU", "管理员")
    r = env["client"].post("/api/review/a1/approve", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved" and body["chunks"] > 0

    pts = _qdrant_points(env, "a1")
    assert len(pts) == body["chunks"]
    assert pts[0].payload["level"] == "L2"          # 原 acl 生效
    assert pts[0].payload["owner_userid"] == "u10a"

    # 哈希此时才登记 (重传 409 生效口径)
    from app.services.hash_registry import load_hash_registry
    reg = load_hash_registry()
    assert reg and "扫描件.pdf" in json.dumps(reg, ensure_ascii=False)

    assert env["ts"].get("a1")["status"] == "ingested"
    assert not f.exists()                            # 入库即消耗文件
    rows = _audit(env, "review_approve")
    assert len(rows) == 1 and "管理员(adminU)" == rows[0]["actor"]

    # 双击 → 409
    r2 = env["client"].post("/api/review/a1/approve", json={})
    assert r2.status_code == 409


def test_approve_with_edited_text(env):
    env["seed_review"]("a2")
    env["login_as"]("adminU", "管理员")
    edited = GOOD_TEXT + "人工勘误补充条款。"
    r = env["client"].post("/api/review/a2/approve", json={"text": edited})
    assert r.status_code == 200, r.text
    pts = _qdrant_points(env, "a2")
    joined = "".join(p.payload["text"] for p in pts)
    assert "人工勘误补充条款" in joined


def test_approve_empty_text_400(env):
    env["seed_review"]("a3", text="   ")
    env["login_as"]("adminU", "管理员")
    r = env["client"].post("/api/review/a3/approve", json={})
    assert r.status_code == 400


# ============================================================ reject

def test_reject_keeps_reupload_possible(env):
    f = env["seed_review"]("j1")
    env["login_as"]("adminU", "管理员")
    r = env["client"].post("/api/review/j1/reject",
                           json={"reason": "识别质量太差"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    assert not f.exists()
    assert env["ts"].get("j1") is None               # 台账移除
    from app.services.hash_registry import load_hash_registry
    assert load_hash_registry() == {}                # 未登记哈希 → 可重传
    assert _audit(env, "review_reject")
    # 重复决策 409
    assert env["client"].post("/api/review/j1/reject",
                              json={"reason": "再驳"}).status_code == 409


def test_reject_requires_reason(env):
    env["seed_review"]("j2")
    env["login_as"]("adminU", "管理员")
    assert env["client"].post("/api/review/j2/reject",
                              json={"reason": "  "}).status_code == 400


# ============================================================ 权限

def test_queue_permissions(env):
    env["seed_review"]("p1", owner="u10a")
    env["seed_review"]("p2", owner="u30a")
    # 普通员工 403
    env["login_as"]("u30a", "员工丙")
    assert env["client"].get("/api/review/queue").status_code == 403
    # leader A 只见本部门 (u10a 的单)
    env["login_as"]("leadA", "主管A")
    items = env["client"].get("/api/review/queue").json()["items"]
    assert {x["doc_id"] for x in items} == {"p1"}
    ov = env["client"].get("/api/review/overview").json()
    assert ov["pending"] == 1
    # admin 全部
    env["login_as"]("adminU", "管理员")
    assert env["client"].get("/api/review/queue").json()["count"] == 2
    # leader 可批 p1, 不可看 p2
    env["login_as"]("leadA", "主管A")
    assert env["client"].get("/api/review/p2").status_code == 403
    assert env["client"].get("/api/review/p1").status_code == 200
    assert _audit(env, "review_view")


def test_owner_can_approve_own(env):
    env["seed_review"]("o1", owner="u10a")
    env["login_as"]("u10a", "员工甲")
    # owner 本人可看可批自己上传的
    assert env["client"].get("/api/review/o1").status_code == 200
    assert env["client"].post("/api/review/o1/approve",
                              json={}).status_code == 200


# ============================================================ 重启豁免

def test_needs_review_survives_restart(env, tmp_path, monkeypatch):
    from app.services.upload_tasks import TaskStore, STATUS_NEEDS_REVIEW
    p = tmp_path / "restart.json"
    s1 = TaskStore(path=p)
    s1.create("rr1", filename="x.pdf", size=1, route="scanned_pdf",
              file_path="/x", status=STATUS_NEEDS_REVIEW)
    s2 = TaskStore(path=p)   # 模拟服务重启重载
    assert s2.get("rr1")["status"] == STATUS_NEEDS_REVIEW
