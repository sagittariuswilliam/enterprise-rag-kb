"""P5* 关系驾驶舱 /admin/graph 测试 — 纯本地 (tmp sqlite + 台账 monkeypatch)

覆盖 (docs/15 §4 验收标准):
  1. 节点/边装配: 部门节点含灰度/负责人/文档数/命中数 meta
  2. 负责人口径: users.leader=1 → dept meta.leaders + kind=leads 边
  3. 去根规则: dept_ids=[1,x] 的人只连 x; 只有 [1] 的连根; 真跨部门 [x,y] 双连
  4. 部门文档归属: L2→acl.dept_id, L4→extra_acl "dept:<id>", 其他状态不计
  5. 部门命中数: doc_hits × 台账归属摊派
  6. 权限: 非管理员 403; admin 200
运行: ./venv/bin/python -m pytest tests/test_graph_dashboard.py -q
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.config import settings

USERS = [
    # userid, name, dept_ids, active, leader, admin, boss
    ("uRoot", "老板刘", "[1]", 1, 0, 1, 1),
    ("uTech", "技术员工", "[1, 10]", 1, 0, 0, 0),
    ("uFinLeader", "FinanceLead", "[1, 20]", 1, 1, 0, 0),
    ("uMulti", "跨部门张", "[10, 20]", 1, 0, 0, 0),
    ("uGone", "离职者", "[10]", 0, 0, 0, 0),
]


class FakeStore:
    def __init__(self, tasks):
        self._tasks = tasks

    def list(self):
        return list(self._tasks)


TASKS = [
    {"doc_id": "d1", "status": "ingested",
     "acl": {"level": "L2", "dept_id": [10]}},
    {"doc_id": "d2", "status": "ingested",
     "acl": {"level": "L4", "dept_id": [], "extra_acl": ["dept:20", "uFinLeader"]}},
    {"doc_id": "d3", "status": "pending_approval",   # 未入库不计
     "acl": {"level": "L2", "dept_id": [10]}},
    {"doc_id": "d4", "status": "ingested",           # L3 不属部门
     "acl": {"level": "L3", "dept_id": [1]}},
    {"doc_id": "dX", "status": "ingested",           # 无部门归属
     "acl": {"level": "L1", "dept_id": []}},
]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "kb_test.db"
    conn = sqlite3.connect(db)
    from app.dingtalk.store import SCHEMA
    conn.executescript(SCHEMA)
    for u in USERS:
        conn.execute(
            "INSERT INTO users(userid,name,dept_ids,active,leader,admin,boss,synced_at)"
            " VALUES(?,?,?,?,?,?,?,'2026-10-20')", u)
    conn.execute("INSERT INTO depts(dept_id,name,parent_id,active,synced_at) VALUES(1,'总公司',0,1,'x')")
    conn.execute("INSERT INTO depts(dept_id,name,parent_id,active,synced_at) VALUES(10,'技术部',1,1,'x')")
    conn.execute("INSERT INTO depts(dept_id,name,parent_id,active,synced_at) VALUES(20,'财务部',1,1,'x')")
    conn.execute("INSERT INTO depts(dept_id,name,parent_id,active,synced_at) VALUES(30,'停用部',1,0,'x')")
    conn.execute("INSERT INTO gray_release(dept_id) VALUES(10)")
    # d1 命中 2 次, d2 命中 1 次, d9 孤儿命中 (台账无此 doc → 丢弃)
    conn.executemany(
        "INSERT INTO doc_hits(doc_id,source,hit_at,query_digest,actor) VALUES(?,?,?,?,?)",
        [("d1", "s", "2026-10-20 10:00:00", "q", "a"),
         ("d1", "s", "2026-10-20 11:00:00", "q", "a"),
         ("d2", "s", "2026-10-20 12:00:00", "q", "a"),
         ("d9", "s", "2026-10-20 12:00:00", "q", "a")])
    conn.commit()
    conn.close()
    monkeypatch.setattr(settings, "db_path", str(db))
    monkeypatch.setattr(settings, "login_enabled", True)
    import app.services.upload_tasks as ut
    monkeypatch.setattr(ut, "get_task_store", lambda: FakeStore(TASKS))
    import app.services.graph as g
    monkeypatch.setattr(g, "get_task_store", lambda: FakeStore(TASKS))
    return db


def _payload():
    from app.services.graph import assemble_graph
    return assemble_graph()


def _node(payload, typ, id_):
    return next(n for n in payload["nodes"] if n["type"] == typ and n["id"] == id_)


def test_dept_meta(env):
    p = _payload()
    tech = _node(p, "dept", 10)
    assert tech["meta"]["gray_open"] is True
    assert tech["meta"]["doc_count"] == 1        # d1 only (d3 pending 不计)
    assert tech["meta"]["hit_count"] == 2        # d1 × 2
    fin = _node(p, "dept", 20)
    assert fin["meta"]["gray_open"] is False
    assert fin["meta"]["doc_count"] == 1         # d2 via extra_acl dept:20
    assert fin["meta"]["hit_count"] == 1
    assert fin["meta"]["leaders"] == ["FinanceLead"]
    stopped = _node(p, "dept", 30)
    assert stopped["meta"]["active"] is False


def test_person_edges_drop_root(env):
    p = _payload()
    edges = {(e["from"], e["to"], e["kind"]) for e in p["edges"]}
    assert ("uTech", 10, "belongs") in edges
    assert ("uTech", 1, "belongs") not in edges      # [1,10] 不连根
    assert ("uRoot", 1, "belongs") in edges          # 纯 [1] 连根
    assert ("uMulti", 10, "belongs") in edges
    assert ("uMulti", 20, "belongs") in edges
    assert ("uFinLeader", 20, "leads") in edges      # leader → leads 边
    assert ("uGone", 10, "belongs") not in edges     # 离职不入图
    assert _node(p, "user", "uMulti")["meta"]["multi_dept"] is True
    assert _node(p, "user", "uTech")["meta"]["root_only"] is False
    assert _node(p, "user", "uRoot")["meta"]["root_only"] is True


def test_summary(env):
    p = _payload()
    assert p["summary"] == {"depts": 4, "users_active": 4,
                            "leaders": 1, "multi_dept_users": 1}
    assert p["root_dept_id"] == 1


def test_api_permission(env):
    from app.main import app
    from app.core.session import issue_token
    from fastapi.testclient import TestClient
    client = TestClient(app)
    # 非管理员 (uTech 无 admin/boss) → 403
    client.headers.update({"Authorization": "Bearer " + issue_token("uTech", "uTech")})
    assert client.get("/api/admin/graph").status_code == 403
    # admin → 200
    client.headers.update({"Authorization": "Bearer " + issue_token("uRoot", "uRoot")})
    r = client.get("/api/admin/graph")
    assert r.status_code == 200
    body = r.json()
    assert "nodes" in body and "edges" in body and "summary" in body
