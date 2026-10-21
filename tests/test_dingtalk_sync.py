"""app/dingtalk 单元测试 — 不发真实网络请求, 用 FakeClient 驱动"""

import json
import sqlite3

import pytest

from app.dingtalk import store, sync as sync_mod
from app.dingtalk.client import DingTalkClient


class FakeClient(DingTalkClient):
    def __init__(self, depts, users_by_dept):
        super().__init__(app_key="x", app_secret="***")
        self._depts = depts
        self._users = users_by_dept

    def get_department(self, dept_id=1):
        return self._depts[0]

    def list_sub_departments(self, dept_id):
        return [d for d in self._depts if d.get("parent_id") == dept_id]

    def list_department_users(self, dept_id):
        return self._users.get(dept_id, [])


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "kb.db"
    monkeypatch.setattr(store.settings, "db_path", str(db))
    return db


def _rows(db, sql):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


DEPTS = [
    {"dept_id": 1, "name": "总公司", "parent_id": 0, "member_count": 3},
    {"dept_id": 2, "name": "人事部", "parent_id": 1},
]
USERS = {
    1: [  # 根部门 3 人, 其中 alice 同时在人事部
        {"userid": "a", "name": "Alice", "active": True, "leader": True,
         "dept_id_list": [1], "unionid": "ua"},
        {"userid": "b", "name": "Bob", "active": True, "dept_id_list": [1]},
        {"userid": "c", "name": "Carol", "active": True, "dept_id_list": [1]},
    ],
    2: [
        {"userid": "a", "name": "Alice", "active": True, "dept_id_list": [2]},
    ],
}


def test_full_sync_first_run(tmp_db):
    result = sync_mod.full_sync(FakeClient(DEPTS, USERS))
    assert result == {"departments": 2, "users": 3, "marked_inactive": 0}
    users = {u["name"]: json.loads(u["dept_ids"]) for u in _rows(tmp_db, "SELECT * FROM users")}
    assert users["Alice"] == [1, 2]          # 多部门合并
    assert users["Bob"] == [1]
    alice = [u for u in _rows(tmp_db, "SELECT * FROM users WHERE name='Alice'")][0]
    assert alice["leader"] == 1
    depts = {d["name"]: d["member_count"] for d in _rows(tmp_db, "SELECT * FROM depts")}
    assert depts["人事部"] == 1               # 真实在人数, 不是钉钉直挂数


def test_second_run_is_idempotent(tmp_db):
    fake = lambda: sync_mod.full_sync(FakeClient(DEPTS, USERS))
    fake(); fake()
    assert len(_rows(tmp_db, "SELECT * FROM users")) == 3


def test_leave_marks_inactive(tmp_db):
    sync_mod.full_sync(FakeClient(DEPTS, USERS))
    users2 = {1: USERS[1][:2], 2: []}        # Carol 离职
    result = sync_mod.full_sync(FakeClient(DEPTS, users2))
    assert result["marked_inactive"] == 1
    carol = _rows(tmp_db, "SELECT * FROM users WHERE name='Carol'")[0]
    assert carol["active"] == 0              # 停用不删档


def test_transfer_updates_depts(tmp_db):
    sync_mod.full_sync(FakeClient(DEPTS, USERS))
    # Alice 从根部门+人事部 调到 只挂人事部; Bob/Carol 保持在根部门
    users2 = {
        1: [USERS[1][1], USERS[1][2]],
        2: [dict(USERS[1][0], dept_id_list=[2])],
    }
    sync_mod.full_sync(FakeClient(DEPTS, users2))
    alice = _rows(tmp_db, "SELECT * FROM users WHERE userid='a'")[0]
    assert json.loads(alice["dept_ids"]) == [2]
    assert len(_rows(tmp_db, "SELECT * FROM users WHERE active=1")) == 3


def test_token_cache_second_call_no_network(monkeypatch):
    c = DingTalkClient(app_key="x", app_secret="***")
    calls = []

    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"accessToken": "tok123", "expireIn": 7200}
    def fake_post(*a, **k):
        calls.append(1); return Resp()
    monkeypatch.setattr(c._http, "post", fake_post)
    assert c.get_token() == "tok123"
    assert c.get_token() == "tok123"
    assert len(calls) == 1                   # 第二次走缓存
