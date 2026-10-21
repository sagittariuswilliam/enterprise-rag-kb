"""P4 打包 M1 测试 — 拆单/免审矩阵/定稿/驳回/撤回/防双击/离职兜底 (纯 tmp sqlite)

覆盖 (16 号 §7 第 1 条):
  1. classify_docs 免审矩阵: L3 免 / 自己文档免 / leader 本部门免 / boss-admin 全免 /
     L4 剔除 / 他人 L1 剔除
  2. create_package 按部门拆单 + 审批人选取 (leader→boss→admin 兜底链)
  3. 全免审 → 确认即定稿 (token+expires)
  4. decide 原子防双击 (第二次 None); 非 pending 409 语义
  5. 部分驳回 → 剔除被拒文档继续 approved; 全部驳回 → rejected
  6. cancel 仅 creator + 仅 pending_approval
  7. 离职兜底 reassign_dead_approvers → 转 boss/admin
  8. record_view / notify_ok 回写
运行: ./venv/bin/python -m pytest tests/test_package_m1.py -q
"""

from __future__ import annotations

import json
import sqlite3

import pytest

DEPTS = [(10, "技术部"), (20, "财务部"), (30, "商务部")]


def _seed(conn):
    for did, name in DEPTS:
        conn.execute("INSERT INTO depts(dept_id,name,parent_id,synced_at) "
                     "VALUES(?,?,'1','2026-10-21')", (did, name))
    users = [
        ("u10", "员工甲", "[10]", 1, 0, 0, 0),
        ("lead10", "技术主管", "[10]", 1, 1, 0, 0),
        ("lead20", "财务主管", "[20]", 1, 1, 0, 0),
        ("ghost20", "离职财务主管", "[20]", 0, 1, 0, 0),
        ("bossU", "老板", "[1]", 1, 0, 0, 1),
        ("adminU", "管理员", "[1]", 1, 0, 1, 0),
    ]
    for uid, name, depts, active, leader, admin, boss in users:
        conn.execute(
            "INSERT INTO users(userid,name,dept_ids,active,leader,admin,boss,"
            "synced_at) VALUES(?,?,?,?,?,?,?,'2026-10-21T00:00:00')",
            (uid, name, depts, active, leader, admin, boss),
        )


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db = tmp_path / "kb.db"
    from app.dingtalk import store
    monkeypatch.setattr(store.settings, "db_path", str(db))
    store.init_db()
    conn = sqlite3.connect(db)
    _seed(conn)
    conn.commit()
    conn.close()
    return {"db": db}


def _doc(did, level, dept, owner=""):
    return {"doc_id": did, "level": level, "dept_id": dept, "owner_userid": owner}


EMP = {"userid": "u10", "dept_ids": [10], "leader": False, "admin": False, "boss": False}
LEAD10 = {"userid": "lead10", "dept_ids": [10], "leader": True, "admin": False, "boss": False}
BOSS = {"userid": "bossU", "dept_ids": [1], "leader": False, "admin": False, "boss": True}
ADMIN = {"userid": "adminU", "dept_ids": [1], "leader": False, "admin": True, "boss": False}


# ------------------------------------------------------- classify_docs


def test_classify_exempt_matrix(env):
    from app.services.packages import classify_docs
    docs = [
        _doc("d1", "L3", 10, "x"),          # L3 免
        _doc("d2", "L2", 20, "u10"),        # 自己的 L2 免
        _doc("d3", "L2", 20, "y"),          # 他人 L2 技术部外 → 拆
        _doc("d4", "L2", 10, "z"),          # 他人 L2 本部门但非发起人 非 leader → 拆
        _doc("d5", "L4", 20, "y"),          # L4 剔除
        _doc("d6", "L1", 0, "other"),       # 他人 L1 剔除
    ]
    exempt, need = classify_docs(docs, EMP)
    assert set(exempt) == {"d1", "d2"}
    assert need == {20: ["d3"], 10: ["d4"]}


def test_classify_leader_dept_exempt(env):
    from app.services.packages import classify_docs
    docs = [_doc("a", "L2", 10, "z"), _doc("b", "L2", 20, "z")]
    exempt, need = classify_docs(docs, LEAD10)
    assert exempt == ["a"]
    assert need == {20: ["b"]}


def test_classify_boss_admin_all_exempt(env):
    from app.services.packages import classify_docs
    docs = [_doc("a", "L2", 10, "z"), _doc("b", "L2", 20, "z")]
    for ident in (BOSS, ADMIN):
        exempt, need = classify_docs(docs, ident)
        assert set(exempt) == {"a", "b"} and need == {}


# ------------------------------------------------------- 拆单 + 定稿


def test_create_split_and_approvers(env):
    from app.services import packages as ps
    docs = [_doc("d1", "L3", 10, "x"), _doc("d3", "L2", 20, "y"),
            _doc("d4", "L2", 10, "z")]
    r = ps.create_package(title="投标包", need_text="要资质", creator="u10",
                          creator_ident=EMP, docs=docs)
    assert not r["no_approval"]
    subs = r["suborders"]
    assert len(subs) == 2
    by_dept = {s["dept_id"]: s for s in subs}
    assert by_dept[20]["approver"] == "lead20"   # 财务负责人
    assert by_dept[10]["approver"] == "lead10"   # 技术负责人 (排除发起人 u10 无影响)
    assert r["exempt"] == ["d1"]
    pkg = r["package"]
    assert pkg["status"] == ps.PKG_PENDING


def test_dept_no_leader_fallback_boss_admin(env):
    """商务部(30) 无 leader → 兜底链 boss→admin 选中 bossU (在职 boss)"""
    from app.services import packages as ps
    docs = [_doc("d30", "L2", 30, "z")]
    r = ps.create_package(title="T", need_text="N", creator="u10",
                          creator_ident=EMP, docs=docs)
    sub = r["suborders"][0]
    assert sub["approver"] == "bossU"


def test_all_exempt_finalizes_immediately(env):
    from app.services import packages as ps
    docs = [_doc("d1", "L3", 10, "x"), _doc("d2", "L2", 10, "u10")]
    r = ps.create_package(title="T", need_text="N", creator="u10",
                          creator_ident=EMP, docs=docs)
    assert r["no_approval"]
    pkg = r["package"]
    assert pkg["status"] == ps.PKG_APPROVED
    assert pkg["token"] and len(pkg["token"]) == 32
    assert json.loads(pkg["approved_ids"]) == ["d1", "d2"]
    assert pkg["expires_at"]  # 7 天后


def test_partial_reject_drops_docs(env):
    from app.services import packages as ps
    docs = [_doc("d1", "L3", 10, "x"), _doc("d3", "L2", 20, "y"),
            _doc("d4", "L2", 10, "z")]
    r = ps.create_package(title="T", need_text="N", creator="u10",
                          creator_ident=EMP, docs=docs)
    subs = {s["dept_id"]: s for s in r["suborders"]}
    # 财务驳回, 技术批准
    out = ps.decide_suborder(subs[20]["id"], ps.SUB_REJECTED, "lead20", "不适用本项目")
    assert out is not None
    assert subs[20]["id"] != subs[10]["id"]
    pkg = ps.get_package(r["package"]["package_id"])
    assert pkg["status"] == ps.PKG_PENDING          # 还有 pending → 未定稿
    ps.decide_suborder(subs[10]["id"], ps.SUB_APPROVED, "lead10")
    pkg = ps.get_package(r["package"]["package_id"])
    assert pkg["status"] == ps.PKG_APPROVED
    assert set(json.loads(pkg["approved_ids"])) == {"d1", "d4"}   # d3 被剔除


def test_all_rejected(env):
    from app.services import packages as ps
    docs = [_doc("d3", "L2", 20, "y"), _doc("d4", "L2", 10, "z")]
    r = ps.create_package(title="T", need_text="N", creator="u10",
                          creator_ident=EMP, docs=docs)
    for s in r["suborders"]:
        ps.decide_suborder(s["id"], ps.SUB_REJECTED, s["approver"], "不行")
    pkg = ps.get_package(r["package"]["package_id"])
    assert pkg["status"] == ps.PKG_REJECTED
    assert not pkg["token"]


def test_double_decide_atomic(env):
    from app.services import packages as ps
    docs = [_doc("d4", "L2", 10, "z")]
    r = ps.create_package(title="T", need_text="N", creator="u10",
                          creator_ident=EMP, docs=docs)
    sid = r["suborders"][0]["id"]
    assert ps.decide_suborder(sid, ps.SUB_APPROVED, "lead10") is not None
    assert ps.decide_suborder(sid, ps.SUB_REJECTED, "lead10") is None       # 防双击
    assert ps.decide_suborder(sid, ps.SUB_APPROVED, "adminU") is None


# ------------------------------------------------------- 撤回


def test_cancel(env):
    from app.services import packages as ps
    docs = [_doc("d4", "L2", 10, "z")]
    r = ps.create_package(title="T", need_text="N", creator="u10",
                          creator_ident=EMP, docs=docs)
    pid = r["package"]["package_id"]
    assert ps.cancel_package(pid, "u99") is None       # 非 creator
    assert ps.cancel_package(pid, "u10") is not None
    assert ps.cancel_package(pid, "u10") is None       # 已撤不能再撤
    subs = ps.list_suborders(pid)
    assert all(s["status"] == "cancelled" for s in subs)


def test_cancel_after_finalized(env):
    from app.services import packages as ps
    docs = [_doc("d4", "L2", 10, "z")]
    r = ps.create_package(title="T", need_text="N", creator="u10",
                          creator_ident=EMP, docs=docs)
    pid = r["package"]["package_id"]
    ps.decide_suborder(r["suborders"][0]["id"], ps.SUB_APPROVED, "lead10")
    assert ps.cancel_package(pid, "u10") is None       # 已定稿不可撤


# ------------------------------------------------------- 离职兜底


def test_reassign_dead_approvers(env):
    from app.services import packages as ps
    # 造一张 approver=ghost 的子单 (直接改库模拟"审批后该 leader 离职")
    docs = [_doc("d3", "L2", 20, "y")]
    r = ps.create_package(title="T", need_text="N", creator="u10",
                          creator_ident=EMP, docs=docs)
    sid = r["suborders"][0]["id"]
    from app.dingtalk.store import get_conn
    conn = get_conn()
    conn.execute("UPDATE users SET active=0 WHERE userid='lead20'")
    conn.execute("UPDATE pkg_approvals SET approver='lead20' WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    n = ps.reassign_dead_approvers()
    assert n == 1
    q = ps.list_queue("bossU", True)
    assert q[0]["approver"] == "bossU"     # 离职 → 转 boss


# ------------------------------------------------------- 队列隔离 / 视图 / notify_ok


def test_queue_isolation(env):
    from app.services import packages as ps
    docs = [_doc("d3", "L2", 20, "y"), _doc("d4", "L2", 10, "z")]
    r = ps.create_package(title="T", need_text="N", creator="u10",
                          creator_ident=EMP, docs=docs)
    q10 = ps.list_queue("lead10", False)
    q20 = ps.list_queue("lead20", False)
    assert len(q10) == 1 and q10[0]["dept_id"] == 10
    assert len(q20) == 1 and q20[0]["dept_id"] == 20
    qadmin = ps.list_queue("adminU", True)
    assert len(qadmin) == 2


def test_record_view_and_notify_ok(env):
    from app.services import packages as ps
    docs = [_doc("d1", "L3", 10, "x")]
    r = ps.create_package(title="T", need_text="N", creator="u10",
                          creator_ident=EMP, docs=docs)
    pid = r["package"]["package_id"]
    ps.record_view(pid, "u10")
    ps.set_notify_ok(pid, False)
    from app.dingtalk.store import get_conn
    conn = get_conn()
    c = conn.execute("SELECT COUNT(*) FROM package_views WHERE package_id=?", (pid,)).fetchone()[0]
    ok = conn.execute("SELECT notify_ok FROM packages WHERE package_id=?", (pid,)).fetchone()[0]
    conn.close()
    assert c == 1 and ok == 0


def test_list_mine_and_all(env):
    from app.services import packages as ps
    docs = [_doc("d1", "L3", 10, "x")]
    r = ps.create_package(title="T", need_text="N", creator="u10",
                          creator_ident=EMP, docs=docs)
    assert len(ps.list_mine("u10")) == 1
    assert len(ps.list_mine("u99")) == 0
    rows = ps.list_all()
    assert rows[0]["view_count"] == 0


# ---------- 2026-10-21 P4 演练回归: payload dept_id 为数组时 propose/meta 不崩 ----------

def test_primary_dept_normalizes_list_and_scalar():
    """真实 Qdrant payload 的 dept_id 多部门存数组 —— int(list) 曾致 propose 500"""
    from app.services.package_proposer import _primary_dept
    assert _primary_dept([1001]) == 1001
    assert _primary_dept([1001, 1002]) == 1001   # 主归属=第一个
    assert _primary_dept(1001) == 1001                 # 标量兼容
    assert _primary_dept("1001") == 1001
    assert _primary_dept(None) == 0
    assert _primary_dept([]) == 0
    assert _primary_dept([None, "x", 7]) == 7                    # 脏元素跳过


def test_propose_survives_array_dept(monkeypatch):
    """propose_docs 聚合路径: 数组 dept payload 不再抛 TypeError"""
    import asyncio
    from app.services import package_proposer as ppx

    class FakePt:
        score = 0.9
        payload = {"doc_id": "dA", "level": "L2",
                   "dept_id": [1001], "owner_userid": "u9", "source": "sA"}

    class FakeResp:
        points = [FakePt()]

    class FakeClient:
        async def query_points(self, **kw):
            return FakeResp()

    class FakeEmb:
        def embed_query(self, q):
            class V:
                def tolist(self):
                    return [0.1] * 4
            return V()

    monkeypatch.setattr(ppx, "get_client", lambda: FakeClient())
    monkeypatch.setattr(ppx, "Retriever", lambda: type("R", (), {"embedder": FakeEmb()})())
    monkeypatch.setattr(ppx, "_parse_queries", lambda raw, need: ["q1"])
    from app.acl import package_filter as pfx
    monkeypatch.setattr(ppx, "build_package_pool_filter", lambda u: pfx.build_package_pool_filter(u))
    docs = asyncio.run(ppx.propose_docs("需要商务部资质", "u10"))
    assert docs and docs[0]["dept_id"] == 1001
