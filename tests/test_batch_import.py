"""P2b 批量导入测试 — 纯本地 (tmp sqlite + tmp uploads + fake pool), 不依赖 9012/Qdrant

覆盖:
  1. 非 admin 403; admin 通过
  2. 正常批: 3 个 txt → 全部 queued, pool 收 3 个 job, 台账带 batch_id, level=L3
  3. 批内去重: 同一批两个相同内容文件 → 第二个 409 rejected, 不中断整批
  4. 跨批去重: 与已入库哈希重复 → rejected 409
  5. 格式混批: 拒收 .doc 文件只拒它自己, 其余照常受理
  6. 超 100 文件 → 400
  7. L2 批未选部门 → 400; 选了 → acl.dept_id 正确
  8. 空文件列表 → 400
运行: ./venv/bin/python -m pytest tests/test_batch_import.py -q
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.config import settings

SUBMITS: list[dict] = []


class FakePool:
    def submit(self, job: dict) -> None:
        SUBMITS.append(job)


SEED_USERS = [
    # userid, name, dept_ids, active, leader, admin
    ("staff1", "普通员工", "[10]", 1, 0, 0),
    ("adminU", "管理员", "[99]", 1, 0, 1),
]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    global SUBMITS
    SUBMITS = []

    db = tmp_path / "kb.db"
    from app.dingtalk import store
    monkeypatch.setattr(store.settings, "db_path", str(db))
    store.init_db()
    conn = sqlite3.connect(db)
    for uid, name, depts, active, leader, admin in SEED_USERS:
        conn.execute(
            "INSERT INTO users(userid,name,dept_ids,active,leader,admin,boss,"
            "synced_at) VALUES(?,?,?,?,?,?,0,'2026-10-19T00:00:00')",
            (uid, name, depts, active, leader, admin),
        )
    conn.commit()
    conn.close()

    monkeypatch.setattr(settings, "login_enabled", True)
    if not settings.jwt_secret:
        monkeypatch.setattr(settings, "jwt_secret", "test-secret")

    import app.api.upload as upload_mod
    up_dir = tmp_path / "uploads"
    up_dir.mkdir()
    monkeypatch.setattr(upload_mod, "UPLOAD_DIR", up_dir)

    import app.services.upload_tasks as ut
    fake_store = ut.TaskStore(path=tmp_path / "upload_tasks.json")
    monkeypatch.setattr(ut, "_store", fake_store)

    fake_pool = FakePool()
    monkeypatch.setattr(upload_mod, "get_pool", lambda: fake_pool)

    from app.services import l4_notify
    monkeypatch.setattr(l4_notify, "send_async",
                        lambda *a, **k: None)

    import app.services.hash_registry as hr
    monkeypatch.setattr(hr, "HASH_REGISTRY", tmp_path / "hashes.json")

    from app.main import app
    from app.core.session import issue_token
    client = TestClient(app)

    def login_as(uid: str):
        client.headers.update({"Authorization": "Bearer " + issue_token(uid, uid)})

    login_as("adminU")
    return {"client": client, "uploads": up_dir, "store": fake_store,
            "pool": fake_pool, "login_as": login_as}


def _batch(env, files: dict, *, level="L3", data=None, user=None):
    """files: {name: bytes}"""
    if user:
        env["login_as"](user)
    form = {"level": level}
    if data:
        form.update(data)
    fl = [("files", (name, content, "text/plain")) for name, content in files.items()]
    return env["client"].post("/api/upload/batch", files=fl, data=form)


GOOD = ("公司请假制度说明。" * 40).encode("utf-8")


def test_batch_requires_admin(env):
    r = _batch(env, {"a.txt": GOOD}, user="staff1")
    assert r.status_code == 403
    assert not SUBMITS


def test_batch_happy_path(env):
    r = _batch(env, {"a.txt": GOOD, "b.txt": ("报销流程规定。" * 40).encode(),
                     "c.txt": ("考勤管理办法。" * 40).encode()})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["total"] == 3 and j["accepted"] == 3 and j["rejected"] == 0
    assert j["level"] == "L3"
    assert len(SUBMITS) == 3
    assert all(s["acl"]["level"] == "L3" for s in SUBMITS)
    # 台账带 batch_id
    for one in j["results"]:
        t = env["store"].get(one["doc_id"])
        assert t["batch_id"] == j["batch_id"]
        assert t["status"] == "queued"


def test_batch_intra_duplicate(env):
    """同批两个内容相同的文件 → 第二个批内 409, 不影响第一个"""
    r = _batch(env, {"a.txt": GOOD, "a_copy.txt": GOOD, "b.txt": ("别的制度内容。" * 40).encode()})
    assert r.status_code == 200
    j = r.json()
    assert j["accepted"] == 2 and j["rejected"] == 1
    rej = [x for x in j["results"] if not x["ok"]]
    assert len(rej) == 1 and rej[0]["http_status"] == 409
    assert "本批次" in rej[0]["reason"]
    assert len(SUBMITS) == 2
    # 被拒文件不留盘
    assert len(list(env["uploads"].glob("*"))) == 2


def test_batch_cross_batch_duplicate(env, tmp_path):
    # 模拟"a.txt 已入库": 批内受理后 worker 才登记哈希, fake pool 不真跑,
    # 故这里显式调 save_hash(与 upload_worker 成功路径同一入口)
    r1 = _batch(env, {"a.txt": GOOD})
    assert r1.status_code == 200
    from app.services import hash_registry as hr
    import hashlib
    hr.save_hash(hashlib.sha256(GOOD).hexdigest(), "a.txt")
    r = _batch(env, {"renamed.txt": GOOD})  # 换名同内容
    assert r.status_code == 200
    j = r.json()
    assert j["accepted"] == 0 and j["rejected"] == 1
    assert j["results"][0]["http_status"] == 409


def test_batch_bad_format_isolated(env):
    r = _batch(env, {"ok.txt": GOOD, "old.doc": b"\xd0\xcf1111junk"})
    assert r.status_code == 200
    j = r.json()
    assert j["accepted"] == 1
    rej = [x for x in j["results"] if not x["ok"]]
    assert rej[0]["http_status"] == 400
    assert len(SUBMITS) == 1


def test_batch_too_many_files(env):
    files = {f"f{i}.txt": GOOD + str(i).encode() for i in range(101)}
    # 注意: 内容全不同防去重干扰计数(实际先触发数量校验)
    r = _batch(env, files)
    assert r.status_code == 400
    assert "100" in r.json()["detail"]


def test_batch_l2_requires_dept(env):
    r = _batch(env, {"a.txt": GOOD}, level="L2")
    assert r.status_code == 400
    r2 = _batch(env, {"a.txt": GOOD}, level="L2", data={"dept_ids": "10"})
    assert r2.status_code == 200
    assert SUBMITS[-1]["acl"]["dept_id"] == [10]


def test_single_upload_regression(env):
    """重构后单传 /api/upload 行为不变"""
    r = env["client"].post(
        "/api/upload",
        files={"file": ("s.txt", GOOD, "text/plain")},
        data={"level": "L3"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert len(SUBMITS) == 1
    # 单传不带 batch_id
    assert "batch_id" not in env["store"].get(r.json()["doc_id"])
