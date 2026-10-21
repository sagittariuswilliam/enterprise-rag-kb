"""P3 灰度开放测试 — 纯本地 (tmp sqlite + fake identity), 不依赖 9012/Qdrant

覆盖:
  1. gray_release 空表 = fail-open 全放行 (同旧行为, 防误清锁死)
  2. 种子后: 放开部门可用, 未放开部门问答/上传 403
  3. admin/boss 恒放行 (控制台必须可用)
  4. /auth/me 带 gray_open 字段
  5. POST /admin/gray/{dept} 开关即时生效 + 审计落库 gray_set
  6. GET /admin/gray 列表结构 (部门/负责人/开关状态)
运行: ./venv/bin/python -m pytest tests/test_gray_release.py -q
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import settings

FIN = [
    # userid, name, dept_ids, active, leader, admin, boss
    ("uOpen", "技术员工", "[10]", 1, 0, 0, 0),
    ("uHold", "FinanceLead", "[20]", 1, 1, 0, 0),
    ("uAdm", "管理员刘", "[99]", 1, 0, 1, 0),
]
GRAY_SEED = [10]  # 部门10 放开, 20 不放


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "kb_test.db"
    conn = sqlite3.connect(db)
    from app.dingtalk.store import SCHEMA
    conn.executescript(SCHEMA)
    for u in FIN:
        conn.execute(
            "INSERT INTO users(userid,name,dept_ids,active,leader,admin,boss,synced_at)"
            " VALUES(?,?,?,?,?,?,?,'2026-10-19')", u)
    conn.execute("INSERT INTO depts(dept_id,name,parent_id,active,synced_at) VALUES(10,'技术部',1,1,'x')")
    conn.execute("INSERT INTO depts(dept_id,name,parent_id,active,synced_at) VALUES(20,'财务部',1,1,'x')")
    conn.execute("INSERT INTO depts(dept_id,name,parent_id,active,synced_at) VALUES(1,'总公司',0,1,'x')")
    for d in GRAY_SEED:
        conn.execute("INSERT INTO gray_release(dept_id) VALUES(?)", (d,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(settings, "db_path", str(db))
    monkeypatch.setattr(settings, "login_enabled", True)
    return db


def _client_login(env):
    from app.main import app
    from app.core.session import issue_token
    client = TestClient(app)

    def login_as(uid: str):
        client.headers.update({"Authorization": "Bearer " + issue_token(uid, uid)})
    return client, login_as


def test_empty_table_fail_open(env, monkeypatch):
    """gray_release 清空 = 全部放行 (fail-open 纪律)"""
    conn = sqlite3.connect(env)
    conn.execute("DELETE FROM gray_release")
    conn.commit()
    conn.close()
    from app.acl.identity import get_identity
    from app.acl.gray import is_open
    assert is_open(get_identity("uHold")) is True


def test_seeded_blocks_closed_dept(env):
    from app.acl.identity import get_identity
    from app.acl.gray import is_open
    assert is_open(get_identity("uOpen")) is True   # 部门10 已放开
    assert is_open(get_identity("uHold")) is False  # 部门20 未放开


def test_admin_boss_always_pass(env):
    from app.acl.identity import get_identity
    from app.acl.gray import is_open
    assert is_open(get_identity("uAdm")) is True  # admin 直通, 部门99不在名单


def test_chat_403_for_closed_dept(env):
    c, login = _client_login(env)
    login("uHold")
    r = c.post("/api/chat", json={"question": "报销制度"})
    assert r.status_code == 403
    assert "灰度" in r.json()["detail"]


def test_chat_stream_403_for_closed_dept(env):
    c, login = _client_login(env)
    login("uHold")
    r = c.post("/api/chat/stream", json={"question": "报销制度"})
    assert r.status_code == 403


def test_upload_403_for_closed_dept(env):
    c, login = _client_login(env)
    login("uHold")
    r = c.post("/api/upload",
               files={"file": ("a.txt", b"hello", "text/plain")},
               data={"level": "L1"})
    assert r.status_code == 403


def test_auth_me_has_gray_open(env):
    c, login = _client_login(env)
    login("uHold")
    r = c.get("/api/auth/me", params={"token": c.headers["Authorization"][7:]})
    assert r.status_code == 200
    assert r.json()["gray_open"] is False
    login("uOpen")
    r2 = c.get("/api/auth/me", params={"token": c.headers["Authorization"][7:]})
    assert r2.json()["gray_open"] is True
    login("uAdm")
    r3 = c.get("/api/auth/me", params={"token": c.headers["Authorization"][7:]})
    assert r3.json()["gray_open"] is True


def test_admin_gray_list_and_toggle(env):
    c, login = _client_login(env)
    login("uAdm")
    r = c.get("/api/admin/gray")
    assert r.status_code == 200
    items = {d["dept_id"]: d for d in r.json()["items"]}
    assert items[10]["open"] is True and items[20]["open"] is False
    # 打开财务部
    r2 = c.post("/api/admin/gray/20", json={"open": True})
    assert r2.status_code == 200 and r2.json()["open"] is True
    # 即时生效: 财务员工可用
    from app.acl.identity import get_identity
    from app.acl.gray import is_open
    assert is_open(get_identity("uHold")) is True
    # 关掉也即时生效
    c.post("/api/admin/gray/20", json={"open": False})
    assert is_open(get_identity("uHold")) is False
    # 审计落库
    conn = sqlite3.connect(env)
    rows = conn.execute("SELECT action, detail FROM audit_log WHERE action='gray_set'").fetchall()
    conn.close()
    assert len(rows) == 2
    assert "财务部" in rows[0][1]


def test_gray_toggle_requires_admin(env):
    c, login = _client_login(env)
    login("uOpen")
    r = c.post("/api/admin/gray/10", json={"open": False})
    assert r.status_code in (401, 403)


def test_gray_toggle_404_unknown_dept(env):
    c, login = _client_login(env)
    login("uAdm")
    r = c.post("/api/admin/gray/12345", json={"open": True})
    assert r.status_code == 404


def test_admin_report_shape(env):
    """报表接口: 空库也要返回完整结构 (除零保护)"""
    c, login = _client_login(env)
    login("uAdm")
    r = c.get("/api/admin/report?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["questions"]["total"] == 0
    assert body["questions"]["has_result_rate"] is None
    assert body["zero_hit_questions"] == []
    assert "l4" in body and body["l4"]["total"] == 0
    # 落一条 chat_search(零命中) + 一条(有命中) 再查
    conn = sqlite3.connect(env)
    conn.execute("INSERT INTO audit_log(actor,action,target,detail) VALUES('x','chat_search','s1','{\"question\":\"报销找谁\",\"hits\":0}')")
    conn.execute("INSERT INTO audit_log(actor,action,target,detail) VALUES('y','chat_search','s2','{\"question\":\"请假制度\",\"hits\":2}')")
    conn.execute("INSERT INTO doc_hits(doc_id,source) VALUES('d1','请假制度.docx')")
    conn.commit()
    conn.close()
    body = c.get("/api/admin/report?days=30").json()
    assert body["questions"]["total"] == 2
    assert body["questions"]["has_result_rate"] == 50.0
    assert body["zero_hit_questions"][0]["question"] == "报销找谁"
    assert body["top_docs"][0]["source"] == "请假制度.docx"
