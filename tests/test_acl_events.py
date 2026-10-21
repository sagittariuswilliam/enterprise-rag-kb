"""ACL filter + Stream 事件处理 单测 (不发真实网络)"""

import json
from types import SimpleNamespace

import pytest

from app.acl.filter import build_acl_filter
from app.acl.identity import Identity, UserNotFound, get_identity
from app.dingtalk import events as ev
from app.dingtalk import store
from app.dingtalk.sync import apply_user_snapshot, mark_users_inactive


# ---------- acl filter 结构 ----------

def test_acl_filter_shape():
    user = Identity(userid="u1", name="张三", dept_ids=[1, 23])
    f = build_acl_filter(user)
    assert len(f.should) == 4
    dumped = json.loads(f.model_dump_json(by_alias=True))
    keys = [c["key"] for c in dumped["should"] if "key" in c]
    assert "level" in keys and "owner_userid" in keys
    # 部门命中用 int, 与入库 payload 类型一致
    l2 = [c for c in dumped["should"] if "must" in c][0]["must"][1]
    assert l2["key"] == "dept_id"
    assert set(l2["match"]["any"]) == {1, 23}


# ---------- identity ----------

@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "kb.db"
    monkeypatch.setattr(store.settings, "db_path", str(db))
    store.init_db()
    return db


def _insert_user(db, userid, name="李四", dept_ids="[1,23]", active=1):
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT OR REPLACE INTO users(userid,name,dept_ids,active,leader,synced_at) VALUES(?,?,?,?,?,?)",
        (userid, name, dept_ids, active, 0, "2026-10-15T00:00:00+08:00"))
    conn.commit(); conn.close()


def test_identity_loaded(tmp_db):
    _insert_user(tmp_db, "u9", dept_ids="[1,88]")
    ident = get_identity("u9")
    assert ident.dept_ids == [1, 88]
    assert ident.leader is False


def test_inactive_user_rejected(tmp_db):
    _insert_user(tmp_db, "u9", active=0)
    with pytest.raises(UserNotFound):
        get_identity("u9")


# ---------- stream events ----------

class _FakeClient:
    def __init__(self, users=None, err=None):
        self.users = users or {}
        self.err = err
        self.synced = False

    def get_user(self, uid):
        if self.err:
            raise self.err
        return self.users[uid]

    def list_sub_departments(self, dept_id):
        self.synced = True
        return []

    def iter_all_departments(self):
        self.synced = True
        return [{"dept_id": 1, "name": "根", "parent_id": 0}]

    def list_department_users(self, dept_id):
        return []


def _event(topic, data):
    return SimpleNamespace(headers=SimpleNamespace(topic=topic), data=data)


def test_real_stream_frame_topic_is_star(tmp_db):
    """2026-10-15 实测口径: register_all_event_handler 下 topic='*', 真类型在 event_type."""
    _insert_user(tmp_db, "u1", dept_ids="[1]")
    fake = _FakeClient(users={"u1": {"userid": "u1", "name": "张三", "dept_id_list": [77], "active": True}})
    h = ev.OrgEventHandler.__new__(ev.OrgEventHandler)
    h.client = fake
    e = SimpleNamespace(
        headers=SimpleNamespace(topic="*", event_type="user_modify_org"),
        data={"userId": ["u1"]},
    )
    h.handle_sync(e)
    assert json.loads(store.get_conn().execute("SELECT dept_ids FROM users WHERE userid='u1'").fetchone()[0]) == [77]


def test_user_modify_updates_depts(tmp_db, monkeypatch):
    _insert_user(tmp_db, "u1", dept_ids="[1,23]")
    fake = _FakeClient(users={"u1": {"userid": "u1", "name": "李四", "dept_id_list": [88], "active": True}})
    h = ev.OrgEventHandler.__new__(ev.OrgEventHandler)
    h.client = fake
    h.handle_sync(_event("user_modify_org", {"userId": '["u1"]'}))  # JSON 字符串口径
    assert json.loads(store.get_conn().execute("SELECT dept_ids FROM users WHERE userid='u1'").fetchone()[0]) == [88]


def test_user_leave_marks_inactive(tmp_db):
    _insert_user(tmp_db, "u1")
    h = ev.OrgEventHandler.__new__(ev.OrgEventHandler)
    h.client = _FakeClient()
    h.handle_sync(_event("user_leave_org", {"userId": ["u1"]}))
    assert store.get_conn().execute("SELECT active FROM users WHERE userid='u1'").fetchone()[0] == 0


def test_dept_event_triggers_full_sync(tmp_db, monkeypatch):
    fake = _FakeClient()
    h = ev.OrgEventHandler.__new__(ev.OrgEventHandler)
    h.client = fake
    # full_sync 里会调 get_department, 补上假实现避免真实网络
    fake.get_department = lambda dept_id=1: {"dept_id": 1, "name": "根", "parent_id": 0}
    h.handle_sync(_event("org_dept_modify", {}))
    assert fake.synced


def test_extract_userids_variants():
    assert ev._extract_userids({"userId": '["a","b"]'}) == ["a", "b"]
    assert ev._extract_userids({"userId": ["a"]}) == ["a"]
    assert ev._extract_userids({"userId": "a"}) == ["a"]
    assert ev._extract_userids({}) == []


def test_apply_and_mark_helpers(tmp_db):
    apply_user_snapshot({"userid": "x1", "name": "新来", "dept_id_list": [5]})
    assert get_identity("x1").dept_ids == [5]
    mark_users_inactive(["x1"])
    with pytest.raises(UserNotFound):
        get_identity("x1")
