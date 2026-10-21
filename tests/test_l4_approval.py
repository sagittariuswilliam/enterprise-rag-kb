"""L4 审批状态机测试 — 纯本地 (tmp sqlite + tmp uploads + fake pool), 不依赖 9012/Qdrant

覆盖:
  1. pick_approver: 部门负责人 / admin 兜底 / 离职主管跳过 / 全无负责人 500
  2. 上传 L4 → pending_approval (审批单落库, 台账 pending_approval, 线程池没收到任务)
  3. 上传 L2 → 行为不变 (queued + pool 收到)
  4. approve → approved + 台账 queued + pool 收到; 重复 409; 无权 403
  5. reject → rejected + 文件删 + 台账 remove + 通知上传者; 空 reason 400
  6. cancel 仅 owner; preview 仅审批人/admin; 审计有 l4_preview
  7. 队列隔离: leader A 队列不含 leader B 的单
  8. TaskStore._recover_interrupted 保护 pending_approval
运行: ./venv/bin/python -m pytest tests/test_l4_approval.py -q
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings


# ---------------------------------------------------------------- fixtures

NOTES: list[tuple] = []   # 记录 l4_notify.send_async 调用
SUBMITS: list[dict] = []  # 记录 fake pool.submit 调用


class FakePool:
    def submit(self, job: dict) -> None:
        SUBMITS.append(job)


SEED_USERS = [
    # userid, name, dept_ids, active, leader, admin
    ("u10a", "员工甲", "[10]", 1, 0, 0),
    ("leadA", "主管A", "[10]", 1, 1, 0),
    ("u20a", "员工乙", "[20]", 1, 0, 0),
    ("leadB", "主管B", "[20,10]", 1, 1, 0),
    ("ghost", "离职主管", "[10]", 0, 1, 0),
    ("u30a", "员工丙", "[30]", 1, 0, 0),
    ("adminU", "管理员", "[99]", 1, 0, 1),
]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """tmp db + tmp uploads + tmp 任务台账 + fake pool + fake 钉钉通知"""
    global NOTES, SUBMITS
    NOTES, SUBMITS = [], []

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

    # 上传目录 → tmp
    import app.api.upload as upload_mod
    up_dir = tmp_path / "uploads"
    up_dir.mkdir()
    monkeypatch.setattr(upload_mod, "UPLOAD_DIR", up_dir)

    # 任务台账 → tmp (重建单例)
    import app.services.upload_tasks as ut
    store_path = tmp_path / "upload_tasks.json"
    fake_store = ut.TaskStore(path=store_path)
    monkeypatch.setattr(ut, "_store", fake_store)

    # 线程池 → fake (不真起 worker)
    fake_pool = FakePool()
    import app.api.l4 as l4_mod
    monkeypatch.setattr(upload_mod, "get_pool", lambda: fake_pool)
    monkeypatch.setattr(l4_mod, "get_pool", lambda: fake_pool)

    # 钉钉通知 → 同步记录
    from app.services import l4_notify
    monkeypatch.setattr(l4_notify, "send_async",
                        lambda users, title, text, doc_id="":
                        NOTES.append((tuple(users), title, text)))

    # 哈希注册表 → tmp (不碰生产)
    import app.services.hash_registry as hr
    monkeypatch.setattr(hr, "HASH_REGISTRY", tmp_path / "hashes.json")

    from app.main import app
    from app.core.session import issue_token
    client = TestClient(app)
    client.userid = "u10a"
    client.headers.update({"Authorization": "Bearer " + issue_token("u10a", "员工甲")})

    def login_as(uid: str):
        from app.core.session import issue_token
        client.headers.update({"Authorization": "Bearer " + issue_token(uid, uid)})

    env_obj = {
        "client": client, "db": db, "uploads": up_dir,
        "store": fake_store, "pool": fake_pool, "login_as": login_as,
    }
    return env_obj


def _upload(env, *, level, filename="合同.txt", content=None, data=None):
    content = content or ("保密条款内容。" * 20).encode("utf-8")
    form = {"level": level}
    if data:
        form.update(data)
    r = env["client"].post(
        "/api/upload",
        files={"file": (filename, content, "text/plain")},
        data=form,
    )
    return r


# ---------------------------------------------------------------- pick_approver

def test_pick_approver_leader(env):
    from app.services.l4_approvals import pick_approver
    uid, name = pick_approver("u10a")
    # dept10 的在职 leader: leadA (ghost 已离职被跳过; leadB 部门含10 但 userid 排序在后)
    assert uid == "leadA"
    assert name == "主管A"


def test_pick_approver_skip_left_leader(env):
    from app.services.l4_approvals import pick_approver
    # ghost 是 dept10 leader 但 active=0 → 不会返回 ghost
    uid, _ = pick_approver("u10a")
    assert uid != "ghost"


def test_pick_approver_admin_fallback(env):
    from app.services.l4_approvals import pick_approver
    # u30a 所在部门 30 没有任何 leader → 兜底 admin
    uid, name = pick_approver("u30a")
    assert uid == "adminU"


def test_pick_approver_none_raises(env):
    from app.services.l4_approvals import pick_approver
    conn = sqlite3.connect(env["db"])
    conn.execute("UPDATE users SET active=0 WHERE userid='adminU'")
    conn.commit()
    conn.close()
    with pytest.raises(LookupError):
        pick_approver("u30a")


# ---------------------------------------------------------------- 上传分支

def test_upload_l4_creates_pending_and_no_pool(env):
    r = _upload(env, level="L4", data={"l4_userids": "u20a"})
    assert r.status_code == 200, r.text
    body = r.json()
    doc_id = body["doc_id"]
    assert body["status"] == "pending_approval"
    assert body["approver"] == "主管A"

    # 审批单落库
    conn = sqlite3.connect(env["db"])
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM l4_approvals WHERE doc_id=?",
                       (doc_id,)).fetchone()
    conn.close()
    assert row and row["status"] == "pending"
    assert row["approver_userid"] == "leadA"
    assert json.loads(row["acl"])["level"] == "L4"

    # 台账 pending_approval
    task = env["store"].get(doc_id)
    assert task["status"] == "pending_approval"

    # 文件在 uploads 目录
    assert Path(task["file_path"]).is_file()

    # 线程池没收到任务
    assert SUBMITS == []

    # 通知了审批人
    assert NOTES and NOTES[0][0] == ("leadA",)


def test_upload_l2_unchanged(env):
    r = _upload(env, level="L2", data={"dept_ids": "10"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert env["store"].get(body["doc_id"])["status"] == "queued"
    assert len(SUBMITS) == 1 and SUBMITS[0]["doc_id"] == body["doc_id"]
    assert NOTES == []


def test_upload_l4_no_approver_500(env):
    conn = sqlite3.connect(env["db"])
    conn.execute("UPDATE users SET active=0 WHERE userid='adminU'")
    conn.commit()
    conn.close()
    # u30a: 部门30无 leader, admin 兜底也被停用 → 500
    env["login_as"]("u30a")
    r = _upload(env, level="L4", data={"l4_userids": "u20a"})
    assert r.status_code == 500
    # 没有残留: 文件被删, 无审批单, 无台账
    assert list(env["uploads"].iterdir()) == []
    conn = sqlite3.connect(env["db"])
    assert conn.execute("SELECT COUNT(*) FROM l4_approvals").fetchone()[0] == 0
    conn.close()
    assert env["store"].list() == []


# ---------------------------------------------------------------- queue / 隔离

def _make_pending(env, owner="u10a", approver_login="leadA"):
    env["login_as"](owner)
    r = _upload(env, level="L4", data={"l4_userids": "u20a"})
    assert r.status_code == 200, r.text
    env["login_as"](approver_login)
    return r.json()["doc_id"]


def test_queue_isolation(env):
    doc1 = _make_pending(env, owner="u10a")   # → leadA
    doc2 = _make_pending(env, owner="u20a")   # leadB 部门含10+20, u20a dept20;
    # 注意 leadB userid 排序在 leadA 之后 → u20a 的单审批人是 leadB
    env["login_as"]("leadA")
    q = env["client"].get("/api/l4/queue").json()["items"]
    assert {i["doc_id"] for i in q} == {doc1}
    env["login_as"]("leadB")
    q2 = env["client"].get("/api/l4/queue").json()["items"]
    assert {i["doc_id"] for i in q2} == {doc2}


def test_queue_admin_sees_all(env):
    doc1 = _make_pending(env, owner="u10a")
    doc2 = _make_pending(env, owner="u20a")
    env["login_as"]("adminU")
    q = env["client"].get("/api/l4/queue").json()["items"]
    assert {doc1, doc2} <= {i["doc_id"] for i in q}


# ---------------------------------------------------------------- approve

def test_approve_flow(env):
    doc_id = _make_pending(env)
    r = env["client"].post(f"/api/l4/{doc_id}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert env["store"].get(doc_id)["status"] == "queued"
    assert len(SUBMITS) == 1
    job = SUBMITS[0]
    assert job["doc_id"] == doc_id and job["acl"]["level"] == "L4"
    assert job["file_path"]  # 原任务格式
    # 通知上传者 (approve 也发)
    assert any(n[0] == ("u10a",) and "已通过" in n[2] for n in NOTES)
    # 审计
    conn = sqlite3.connect(env["db"])
    conn.row_factory = sqlite3.Row
    a = conn.execute("SELECT * FROM audit_log WHERE action='l4_approve'").fetchall()
    conn.close()
    assert len(a) == 1 and a[0]["actor"] == "leadA"


def test_approve_twice_409(env):
    doc_id = _make_pending(env)
    assert env["client"].post(f"/api/l4/{doc_id}/approve").status_code == 200
    r = env["client"].post(f"/api/l4/{doc_id}/approve")
    assert r.status_code == 409


def test_approve_by_stranger_403(env):
    doc_id = _make_pending(env)
    env["login_as"]("u30a")  # 既非审批人也非 admin 也非 owner
    r = env["client"].post(f"/api/l4/{doc_id}/approve")
    assert r.status_code == 403


def test_approve_missing_file_500(env):
    doc_id = _make_pending(env)
    task = env["store"].get(doc_id)
    Path(task["file_path"]).unlink()
    r = env["client"].post(f"/api/l4/{doc_id}/approve")
    assert r.status_code == 500
    assert SUBMITS == []


# ---------------------------------------------------------------- reject

def test_reject_flow(env):
    doc_id = _make_pending(env)
    file_before = Path(env["store"].get(doc_id)["file_path"])
    assert file_before.is_file()
    r = env["client"].post(f"/api/l4/{doc_id}/reject", json={})
    assert r.status_code == 422  # pydantic 必填校验
    r = env["client"].post(f"/api/l4/{doc_id}/reject", json={"reason": "  "})
    assert r.status_code == 400
    r = env["client"].post(f"/api/l4/{doc_id}/reject", json={"reason": "密级过高"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"
    assert not file_before.exists()          # 文件已删
    assert env["store"].get(doc_id) is None  # 台账已 remove
    assert any(n[0] == ("u10a",) and "被驳回: 密级过高" in n[2] for n in NOTES)
    # reject 后不能重复决策
    r = env["client"].post(f"/api/l4/{doc_id}/approve")
    assert r.status_code == 409


# ---------------------------------------------------------------- cancel

def test_cancel_only_owner(env):
    doc_id = _make_pending(env)
    # 审批人不能撤
    r = env["client"].post(f"/api/l4/{doc_id}/cancel")
    assert r.status_code == 403
    # owner 本人可撤
    env["login_as"]("u10a")
    r = env["client"].post(f"/api/l4/{doc_id}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert env["store"].get(doc_id) is None
    assert list(env["uploads"].iterdir()) == []
    # 通知了审批人
    assert any("撤回" in n[2] and "leadA" in n[0] for n in NOTES)


# ---------------------------------------------------------------- preview

def test_preview_permissions(env):
    doc_id = _make_pending(env)
    # 普通无关用户 403
    env["login_as"]("u30a")
    assert env["client"].get(f"/api/l4/{doc_id}/preview").status_code == 403
    # 审批人本人 OK, 返回前 1200 字
    env["login_as"]("leadA")
    r = env["client"].get(f"/api/l4/{doc_id}/preview")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "保密条款" in body["text"]
    assert body["owner_name"] == "员工甲"
    # 审计有 l4_preview
    conn = sqlite3.connect(env["db"])
    conn.row_factory = sqlite3.Row
    a = conn.execute("SELECT actor FROM audit_log WHERE action='l4_preview' "
                     "AND target=?", (doc_id,)).fetchall()
    conn.close()
    assert a and a[0]["actor"] == "leadA"
    # admin 放行; 404 单不存在
    env["login_as"]("adminU")
    assert env["client"].get(f"/api/l4/{doc_id}/preview").status_code == 200
    assert env["client"].get("/api/l4/nope0000/preview").status_code == 404


# ---------------------------------------------------------------- 重启恢复保护

def test_recover_keeps_pending_approval(tmp_path):
    from app.services.upload_tasks import TaskStore
    p = tmp_path / "tasks.json"
    s1 = TaskStore(path=p)
    s1.create("d1", filename="a.txt", size=1, route="text",
              file_path="/x", acl={"level": "L4"},
              status="pending_approval")
    s1.create("d2", filename="b.txt", size=1, route="text", file_path="/y")
    s1.update("d2", status="parsing")
    # 模拟重启: 重新加载
    s2 = TaskStore(path=p)
    assert s2.get("d1")["status"] == "pending_approval"   # 审批单不受重启影响
    assert s2.get("d2")["status"] == "failed"              # 普通中断照旧标失败
