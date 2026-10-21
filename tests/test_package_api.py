"""P4 打包 API 层测试 — confirm 服务端元数据校验 / 权限 / 并发 / 队列 (tmp db + fake meta)

不依赖 Qdrant: monkeypatch package_proposer.get_docs_meta 返回假元数据。
覆盖:
  1. confirm 拒 L4 / 他人 L1 / 不存在 doc_id (400 或剔除)
  2. confirm 元数据不信客户端 (payload 里 level 由 fake meta 决定)
  3. approve/reject 非审批人 403; 双击 409; 驳回空理由 400
  4. queue 隔离; cancel 权限; mine 视图
  5. preview 非审批人 403 / doc 不在子单 404
运行: ./venv/bin/python -m pytest tests/test_package_api.py -q
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

NOTES: list[tuple] = []

META = {
    "d-own":  {"level": "L2", "dept_id": 10, "owner_userid": "u10", "source": "我的.docx"},
    "d-t20":  {"level": "L2", "dept_id": 20, "owner_userid": "someone", "source": "财务.docx"},
    "d-t30":  {"level": "L2", "dept_id": 30, "owner_userid": "someone", "source": "商务.docx"},
    "d-l3":   {"level": "L3", "dept_id": 1, "owner_userid": "hr", "source": "制度.pdf"},
    "d-l4":   {"level": "L4", "dept_id": 20, "owner_userid": "someone", "source": "指定.docx"},
    "d-l1o":  {"level": "L1", "dept_id": 0, "owner_userid": "someone", "source": "他人私密.docx"},
}


async def fake_meta(doc_ids):
    return {d: META[d] for d in doc_ids if d in META}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    global NOTES
    NOTES = []
    db = tmp_path / "kb.db"
    from app.dingtalk import store
    monkeypatch.setattr(store.settings, "db_path", str(db))
    store.init_db()
    conn = sqlite3.connect(db)
    for did, name in [(10, "技术部"), (20, "财务部"), (30, "商务部")]:
        conn.execute("INSERT INTO depts(dept_id,name,parent_id,synced_at) VALUES(?,?,'1','x')", (did, name))
    for uid, name, depts, active, leader, admin, boss in [
        ("u10", "员工甲", "[10]", 1, 0, 0, 0),
        ("lead10", "技术主管", "[10]", 1, 1, 0, 0),
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
                        lambda users, title, text, sub_id=None, package_id="":
                        NOTES.append((tuple(users), title, text)))

    # 灰度全开 (P3 闸门会查 gray_release 表, tmp 库没数据)
    from app.acl import gray as gray_mod
    monkeypatch.setattr(gray_mod, "is_open", lambda ident: True)

    from app.main import app
    from app.core.session import issue_token
    client = TestClient(app)

    def login_as(uid: str):
        client.headers.update({"Authorization": "Bearer " + issue_token(uid, uid)})

    login_as("u10")
    return {"client": client, "login_as": login_as, "db": db}


def _confirm(env, doc_ids, title="投标包", need="要资质和财务文件"):
    return env["client"].post("/api/package/confirm",
                              json={"title": title, "need_text": need, "doc_ids": doc_ids})


# ---------------------------------------------------------------- confirm


def test_confirm_splits_and_notifies(env):
    r = _confirm(env, ["d-l3", "d-t20", "d-t30"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["exempt_count"] == 1               # d-l3 免审
    assert len(data["suborders"]) == 2             # 20 + 30 (无leader→admin兜底)
    assert data["status"] == "pending_approval"
    # 通知: lead20 一张; 30 无 leader 无 boss → admin 兜底
    targets = {t[0][0] for t in NOTES}
    assert "lead20" in targets and "adminU" in targets


def test_confirm_rejects_l4_and_others_l1(env):
    r = _confirm(env, ["d-l4", "d-l1o"])
    assert r.status_code == 400                    # 剔除后清单为空
    r2 = _confirm(env, ["d-l4", "d-t20"])
    assert r2.status_code == 200
    assert r2.json()["exempt_count"] == 0 and len(r2.json()["suborders"]) == 1


def test_confirm_all_exempt_finalizes(env):
    r = _confirm(env, ["d-own", "d-l3"])
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "approved" and not data["suborders"]
    pid = data["package_id"]
    mine = env["client"].get("/api/package/mine").json()["items"]
    item = next(m for m in mine if m["package_id"] == pid)
    assert item["token"] and len(item["token"]) == 32


# ---------------------------------------------------------------- 审批操作


def _make_pkg(env, ids=("d-t20",)):
    data = _confirm(env, list(ids)).json()
    return data["package_id"], data["suborders"][0]["id"]


def test_approve_permission(env):
    pid, sid = _make_pkg(env)
    env["login_as"]("u10")                          # 发起人自己不能批
    r = env["client"].post(f"/api/pkg-approval/{sid}/approve")
    assert r.status_code == 403
    env["login_as"]("lead10")                       # 别的部门主管也不行
    assert env["client"].post(f"/api/pkg-approval/{sid}/approve").status_code == 403
    env["login_as"]("lead20")
    r = env["client"].post(f"/api/pkg-approval/{sid}/approve")
    assert r.status_code == 200
    # 双击 (同一人) — 已决出 409
    assert env["client"].post(f"/api/pkg-approval/{sid}/approve").status_code == 409


def test_reject_requires_reason(env):
    pid, sid = _make_pkg(env)
    env["login_as"]("lead20")
    r = env["client"].post(f"/api/pkg-approval/{sid}/reject", json={"reason": "  "})
    assert r.status_code == 400
    r = env["client"].post(f"/api/pkg-approval/{sid}/reject", json={"reason": "不适用"})
    assert r.status_code == 200
    # 全部驳回 → 包 rejected + 通知发起人
    mine = env["client"].get("/api/package/mine").json()["items"]  # lead20 视角空
    env["login_as"]("u10")
    mine = env["client"].get("/api/package/mine").json()["items"]
    assert mine[0]["status"] == "rejected"
    assert any("驳回" in t[1] or "驳回" in t[2] for t in NOTES)


def test_queue_isolation(env):
    _make_pkg(env)
    env["login_as"]("lead10")
    assert env["client"].get("/api/pkg-approval/queue").json()["count"] == 0
    env["login_as"]("lead20")
    assert env["client"].get("/api/pkg-approval/queue").json()["count"] == 1


def test_cancel_flow(env):
    pid, sid = _make_pkg(env)
    env["login_as"]("lead20")
    r = env["client"].post(f"/api/package/{pid}/cancel")
    assert r.status_code == 403                    # 只有发起人能撤
    env["login_as"]("u10")
    assert env["client"].post(f"/api/package/{pid}/cancel").status_code == 200
    assert env["client"].post(f"/api/package/{pid}/cancel").status_code == 409
    env["login_as"]("lead20")
    assert env["client"].post(f"/api/pkg-approval/{sid}/approve").status_code == 409


def test_preview_permission(env, monkeypatch):
    pid, sid = _make_pkg(env)
    env["login_as"]("u10")
    r = env["client"].get(f"/api/pkg-approval/{sid}/preview/d-t20")
    assert r.status_code == 403                    # 发起人无权预览正文
    env["login_as"]("lead20")
    r = env["client"].get(f"/api/pkg-approval/{sid}/preview/d-l3")
    assert r.status_code == 404                    # 不在子单内
    # 真 Qdrant 不在测试环境 — 用 fake client 喂 scroll
    import app.api.package as pkg_api
    class FakePoint:
        def __init__(self): self.payload = {"chunk_id": "0", "text": "秘密条款" * 10, "source": "财务.docx"}
    class FakeClient:
        async def scroll(self, **kw): return ([FakePoint()], None)
    monkeypatch.setattr("app.core.qdrant_client.get_client", lambda: FakeClient())
    # API 内部 import get_client from app.core.qdrant_client 在函数体 — patch 模块属性
    import app.core.qdrant_client as qc
    monkeypatch.setattr(qc, "get_client", lambda: FakeClient())
    env["login_as"]("lead20")
    r = env["client"].get(f"/api/pkg-approval/{sid}/preview/d-t20")
    assert r.status_code == 200
    assert "秘密条款" in r.json()["preview"]


def test_admin_packages(env):
    _make_pkg(env)
    env["login_as"]("u10")
    assert env["client"].get("/api/admin/packages").status_code == 403
    env["login_as"]("adminU")
    r = env["client"].get("/api/admin/packages")
    assert r.status_code == 200 and r.json()["items"][0]["doc_count"] == 1
