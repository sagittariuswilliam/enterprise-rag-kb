"""#6 文档命中统计测试 — 纯进程内 (tmp sqlite + Qdrant :memory:, 不打真实服务)

覆盖:
  1. SCHEMA: init_db 建 doc_hits 表幂等 (存量库二次 init 不报错)
  2. record_hits: doc 级去重落行 / 空输入不落 / 写入异常吞掉不抛
  3. aggregate: 按 hits 倒序聚合 + uniq_askers + days 窗口过滤
  4. global_summary: no_hit_docs = total_docs - 全时段 distinct 命中数
  5. GET /api/admin/hits: 非 admin 403, admin 200 结构完整
运行: ./venv/bin/python -m pytest tests/test_hit_stats.py -q
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "kb.db"
    from app.dingtalk import store as dt_store
    monkeypatch.setattr(dt_store.settings, "db_path", str(db))
    dt_store.init_db()
    conn = sqlite3.connect(db)
    for uid, name, depts, active, leader, admin in [
        ("u1", "员工甲", "[10]", 1, 0, 0),
        ("adm", "管理员", "[99]", 1, 0, 1),
    ]:
        conn.execute(
            "INSERT INTO users(userid,name,dept_ids,active,leader,admin,boss,"
            "synced_at) VALUES(?,?,?,?,?,?,0,'2026-10-17T00:00:00')",
            (uid, name, depts, active, leader, admin),
        )
    conn.commit()
    conn.close()

    from app.config import settings
    monkeypatch.setattr(settings, "login_enabled", True)
    if not settings.jwt_secret:
        monkeypatch.setattr(settings, "jwt_secret", "test-secret")

    # Qdrant :memory: (admin/hits 里 list_documents 要走真 client)
    import asyncio
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.models import Distance, VectorParams
    import app.core.qdrant_client as qc
    COLL = "kb_test_hits"

    async def _mkq():
        q = AsyncQdrantClient(location=":memory:")
        await q.create_collection(
            collection_name=COLL,
            vectors_config=VectorParams(size=8, distance=Distance.COSINE),
        )
        return q

    qdrant = asyncio.new_event_loop().run_until_complete(_mkq())
    monkeypatch.setattr(qc, "client", qdrant)
    monkeypatch.setattr(settings, "qdrant_collection", COLL)

    from app.main import app as fastapp
    from app.core.session import issue_token
    client = TestClient(fastapp)

    def login_as(uid, name=""):
        client.headers.update(
            {"Authorization": "Bearer " + issue_token(uid, name or uid)})

    def rows():
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        r = [dict(x) for x in c.execute("SELECT * FROM doc_hits")]
        c.close()
        return r

    return {"client": client, "db": db, "login_as": login_as,
            "rows": rows, "qdrant": qdrant, "coll": COLL}


class Ident:
    def __init__(self, userid, name):
        self.userid, self.name = userid, name
        self.dept_ids, self.leader, self.admin, self.boss = [10], False, False, False


def _results(doc_id="d1", source="考勤制度.pdf"):
    return [
        {"doc_id": doc_id, "source": source, "score": 0.9},
        {"doc_id": doc_id, "source": source, "score": 0.8},   # 同 doc 第二个 chunk
        {"doc_id": "d2", "source": "报销标准.docx", "score": 0.7},
    ]


# ---------- 1. 建表 ----------

def test_doc_hits_table_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "kb2.db"
    from app.dingtalk import store as dt_store
    monkeypatch.setattr(dt_store.settings, "db_path", str(db))
    dt_store.init_db()
    dt_store.init_db()  # 二次幂等不报错
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(doc_hits)")]
    conn.close()
    assert {"doc_id", "source", "hit_at", "query_digest", "actor"} <= set(cols)


# ---------- 2. record_hits ----------

def test_record_hits_doc_dedup(env):
    from app.services.hit_stats import record_hits
    record_hits(_results(), Ident("u1", "员工甲"), "加班怎么调休?")
    rows = env["rows"]()
    assert len(rows) == 2  # d1 两个 chunk 去重成 1 行 + d2 一行
    d1 = next(r for r in rows if r["doc_id"] == "d1")
    assert d1["source"] == "考勤制度.pdf"
    assert d1["actor"] == "员工甲(u1)"
    assert d1["query_digest"] == "加班怎么调休?"
    assert d1["hit_at"]  # 非空


def test_record_hits_empty_and_bad_input(env):
    from app.services.hit_stats import record_hits
    record_hits([], Ident("u1", "甲"), "q")
    record_hits(None, Ident("u1", "甲"), "q")
    assert env["rows"]() == []


def test_record_hits_swallows_db_failure(monkeypatch):
    """表不存在 (模拟写入异常) → 吞掉不抛, 主流程不受影响"""
    import app.dingtalk.store as dt_store
    from app.services import hit_stats

    def _bad_conn():
        raise RuntimeError("db down")
    monkeypatch.setattr(hit_stats, "get_conn", _bad_conn)
    hit_stats.record_hits(_results(), Ident("u1", "甲"), "q")  # 不抛即通过


# ---------- 3/4. aggregate + summary ----------

def _seed_hits(db):
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO doc_hits(doc_id,source,hit_at,query_digest,actor)"
        " VALUES(?,?,?,?,?)",
        [
            ("d1", "考勤制度.pdf", "2026-10-17 10:00:00", "q", "甲(u1)"),
            ("d1", "考勤制度.pdf", "2026-10-17 11:00:00", "q", "乙(u2)"),
            ("d2", "报销标准.docx", "2026-01-01 09:00:00", "q", "甲(u1)"),
        ],
    )
    conn.commit()
    conn.close()


def test_aggregate_orders_and_window(env):
    from app.services.hit_stats import aggregate
    _seed_hits(env["db"])
    items = aggregate(days=365, limit=50)
    assert items[0]["doc_id"] == "d1" and items[0]["hits"] == 2
    assert items[0]["uniq_askers"] == 2
    assert items[0]["last_hit_at"] == "2026-10-17 11:00:00"


def test_global_summary(env):
    from app.services.hit_stats import global_summary
    _seed_hits(env["db"])
    s = global_summary(days=7, total_docs=5)
    # 近 7 天窗口: d2 是 1 月的数据不计入 total_hits, 但全时段 distinct=2 → 零命中 5-2=3
    assert s["total_hits"] == 2
    assert s["no_hit_docs"] == 3
    # total_docs < 命中数不为负
    s2 = global_summary(days=365, total_docs=1)
    assert s2["no_hit_docs"] == 0


# ---------- 5. /api/admin/hits ----------

def test_admin_hits_endpoint(env):
    _seed_hits(env["db"])
    env["login_as"]("u1", "员工甲")
    assert env["client"].get("/api/admin/hits").status_code == 403
    env["login_as"]("adm", "管理员")
    r = env["client"].get("/api/admin/hits?days=365")
    assert r.status_code == 200
    body = r.json()
    assert body["items"][0]["doc_id"] == "d1"
    assert {"total_hits", "total_docs_hit", "no_hit_docs", "total_docs"} <= set(body["summary"])
