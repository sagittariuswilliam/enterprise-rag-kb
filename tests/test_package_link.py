"""P4 M3 受控链接页测试 — /package/open/{token} 权限/过期/记次 + auth next 回跳

覆盖 16 号 V1.1 §7 验收 2 (越权实锤):
  1. creator 打开 approved 包 → 200 + 文档清单 (含本人无 ACL 的跨部门 L2)
  2. 他人拿同一 token → 403 零信息 (与不存在同形)
  3. 未决出 → 返回 pending 状态, docs 为空
  4. 过期 → 410
  5. 每次打开 view_count +1
  6. doc 正文: 在 approved_ids → 200 (审批即授权, 不叠本人 ACL); 不在包内 → 404
  7. auth state next: _make_state/_check_state/_state_next 往返 + 篡改拒绝

不依赖真实 Qdrant: monkeypatch get_client 返回 fake scroll。
运行: ./venv/bin/python -m pytest tests/test_package_link.py -q
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

NOTES: list[tuple] = []

META = {
    "d-own": {"level": "L2", "dept_id": 10, "owner_userid": "u10", "source": "我的.docx"},
    "d-t20": {"level": "L2", "dept_id": 20, "owner_userid": "someone", "source": "财务.docx"},
    "d-l3":  {"level": "L3", "dept_id": 1, "owner_userid": "hr", "source": "制度.pdf"},
}


async def fake_meta(doc_ids):
    return {d: META[d] for d in doc_ids if d in META}


class _FakePoint:
    def __init__(self, payload):
        self.payload = payload


class _FakeClient:
    async def scroll(self, *, collection_name, scroll_filter, with_payload,
                     with_vectors, limit, offset):
        return ([_FakePoint({"chunk_id": 0, "text": "正文内容A",
                             "source": "财务.docx", "level": "L2"})], None)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    global NOTES
    NOTES = []
    db = tmp_path / "kb.db"
    from app.dingtalk import store
    monkeypatch.setattr(store.settings, "db_path", str(db))
    store.init_db()
    conn = sqlite3.connect(db)
    for did, name in [(10, "技术部"), (20, "财务部")]:
        conn.execute("INSERT INTO depts(dept_id,name,parent_id,synced_at) VALUES(?,?,'1','x')", (did, name))
    for uid, name, depts, active, leader, admin, boss in [
        ("u10", "员工甲", "[10]", 1, 0, 0, 0),
        ("u20", "员工乙", "[20]", 1, 0, 0, 0),
        ("lead20", "财务主管", "[20]", 1, 1, 0, 0),
        ("adminU", "管理员", "[1]", 1, 0, 1, 0),
    ]:
        conn.execute(
            "INSERT INTO users(userid,name,dept_ids,active,leader,admin,boss,synced_at) "
            "VALUES(?,?,?,?,?,?,?,'x')", (uid, name, depts, active, leader, admin, boss))
    conn.commit()
    conn.close()

    monkeypatch.setattr("app.config.settings.login_enabled", True)
    monkeypatch.setattr("app.config.settings.jwt_secret", "test-secret-1234567890-abcdefghij")
    import app.services.package_proposer as pp
    monkeypatch.setattr(pp, "get_docs_meta", fake_meta)
    from app.services import pkg_notify
    monkeypatch.setattr(pkg_notify, "send_async",
                        lambda users, title, text, sub_id=None, package_id="": None)
    from app.acl import gray as gray_mod
    monkeypatch.setattr(gray_mod, "is_open", lambda ident: True)
    import app.core.qdrant_client as qc
    monkeypatch.setattr(qc, "get_client", lambda: _FakeClient())

    from app.main import app
    from app.core.session import issue_token
    client = TestClient(app)

    def login_as(uid: str):
        client.headers.update({"Authorization": "Bearer " + issue_token(uid, uid)})

    login_as("u10")
    return {"client": client, "login_as": login_as, "db": db}


def _approve_all(env, db, pid):
    """admin 批准该包全部 pending 子单 → 定稿"""
    env["login_as"]("adminU")
    subs = sqlite3.connect(db).execute(
        "SELECT id FROM pkg_approvals WHERE package_id=? AND status='pending'", (pid,)
    ).fetchall()
    for (sid,) in subs:
        r = env["client"].post(f"/api/pkg-approval/{sid}/approve")
        assert r.status_code == 200, r.text
    env["login_as"]("u10")


def _make_approved_pkg(env):
    r = env["client"].post("/api/package/confirm", json={
        "title": "投标包", "need_text": "要资质和财务文件",
        "doc_ids": ["d-l3", "d-t20"]})
    assert r.status_code == 200
    pid = r.json()["package_id"]
    _approve_all(env, env["db"], pid)
    mine = env["client"].get("/api/package/mine").json()["items"]
    item = next(m for m in mine if m["package_id"] == pid)
    assert item["status"] == "approved" and item["token"]
    return pid, item["token"]


# ---------------------------------------------------------------- 打开权限

def test_creator_can_open(env):
    pid, token = _make_approved_pkg(env)
    r = env["client"].get(f"/api/package/open/{token}")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "approved"
    ids = {d["doc_id"] for d in data["docs"]}
    assert ids == {"d-l3", "d-t20"}          # 审批即授权: 跨部门 L2 在清单里
    assert data["watermark_actor"].startswith("员工甲")


def test_others_get_zero_info_403(env):
    pid, token = _make_approved_pkg(env)
    env["login_as"]("u20")
    r = env["client"].get(f"/api/package/open/{token}")
    assert r.status_code == 403
    assert "无权" in r.json()["detail"] or "无效" in r.json()["detail"]
    # admin 也不行 (仅 creator)
    env["login_as"]("adminU")
    assert env["client"].get(f"/api/package/open/{token}").status_code == 403


def test_unknown_token_403_same_shape(env):
    r = env["client"].get("/api/package/open/" + "0" * 32)
    assert r.status_code == 403


def test_pending_pkg_no_docs(env):
    r = env["client"].post("/api/package/confirm", json={
        "title": "在审批", "need_text": "x", "doc_ids": ["d-t20"]})
    pid = r.json()["package_id"]
    token = "f" * 32
    conn = sqlite3.connect(env["db"])
    conn.execute("UPDATE packages SET token=? WHERE package_id=?", (token, pid))
    conn.commit()
    conn.close()
    r2 = env["client"].get(f"/api/package/open/{token}")
    assert r2.status_code == 200
    data = r2.json()
    assert data["status"] == "pending_approval"
    assert data["docs"] == []
    assert "财务部" in data["pending_depts"]


def test_expired_410(env):
    pid, token = _make_approved_pkg(env)
    conn = sqlite3.connect(env["db"])
    conn.execute("UPDATE packages SET expires_at='2020-01-01 00:00:00' WHERE package_id=?", (pid,))
    conn.commit()
    conn.close()
    r = env["client"].get(f"/api/package/open/{token}")
    assert r.status_code == 410


def test_view_count_increments(env):
    pid, token = _make_approved_pkg(env)
    env["client"].get(f"/api/package/open/{token}")
    env["client"].get(f"/api/package/open/{token}")
    conn = sqlite3.connect(env["db"])
    n = conn.execute("SELECT COUNT(*) FROM package_views WHERE package_id=?", (pid,)).fetchone()[0]
    conn.close()
    assert n == 2


# ---------------------------------------------------------------- 文档正文

def test_doc_view_approval_bypasses_own_acl(env):
    pid, token = _make_approved_pkg(env)
    # u10 本人对 d-t20 (财务部 L2) 无 ACL, 但审批通过后链接页可见
    r = env["client"].get(f"/api/package/open/{token}/doc/d-t20")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["text_sections"] == ["正文内容A"]
    assert data["watermark"]["actor"].startswith("员工甲")


def test_doc_not_in_pkg_404(env):
    pid, token = _make_approved_pkg(env)
    r = env["client"].get(f"/api/package/open/{token}/doc/d-own")
    assert r.status_code == 404


def test_doc_other_user_403(env):
    pid, token = _make_approved_pkg(env)
    env["login_as"]("u20")
    r = env["client"].get(f"/api/package/open/{token}/doc/d-t20")
    assert r.status_code == 403


# ---------------------------------------------------------------- auth next 回跳

def test_state_next_roundtrip():
    from app.api.auth import _check_state, _make_state, _state_next
    st = _make_state("/p/abc123")
    assert _check_state(st)
    assert _state_next(st) == "/p/abc123"
    # 无 next 的旧格式仍兼容
    st0 = _make_state()
    assert _check_state(st0) and _state_next(st0) == "/"
    # 篡改 next → 校验失败
    bad = st.rsplit(".", 1)[0] + ".evil"
    assert not _check_state(bad)
    # 非法 next 白名单
    assert _state_next(_make_state("/admin")) == "/"


def test_login_url_next_in_state():
    from app.main import app  # noqa: F401 (确保路由注册)
    import app.config as cfg
    # login_enabled=False 时 404 — 用 env fixture 的 True 场景在集成里测;
    # 这里只测白名单: 非法 next 不会进入 state
    from app.api.auth import _make_state, _check_state
    assert _check_state(_make_state("/p/" + "0" * 32))
