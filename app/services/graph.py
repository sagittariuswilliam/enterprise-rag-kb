"""P5* 关系驾驶舱 — /admin/graph 数据装配 (纯 SQLite + 上传台账, 零 Qdrant 依赖)

口径 (P5 实测结论):
  - 负责人 = users.leader=1 (depts 表没有 leader 字段, 勿凭直觉反推)
  - 人→部门边只连"最具体部门": dept_ids 去掉根部门后还有子部门 → 只连子部门
    (钉钉把全员自动挂根部门, 直接画会 25 条边糊在根节点)
  - 跨部门 = 去根后仍有多于 1 个部门 (真跨部门任职)
  - 部门文档数 = 台账 ingested 任务按 acl 归属聚合:
      L2 → acl.dept_id 数组; L4 → acl.extra_acl 里的 "dept:<id>"
      (L1/L3 不属任何部门, 只进 root 之外的口径不重复计)
  - 部门命中数 = doc_hits 近 30 天, 按同归属口径聚合 (hit_stats 表无部门列,
    doc_hits.doc_id 对不上台账的存量数据自然丢弃, 宁缺毋滥)
  - 停用部门 active=0 与离职用户 active=0 均返回, 前端灰色呈现
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from app.dingtalk.store import get_conn
from app.services.upload_tasks import get_task_store

ROOT_DEPT_ID = 1  # 钉钉根部门
HIT_DAYS = 30     # 命中热度窗口, 与 /admin/hits 默认一致


def _dept_doc_owners() -> Dict[str, List[int]]:
    """doc_id -> 归属部门列表 (从上传台账 acl 聚合, 只算 ingested)."""
    owners: Dict[str, List[int]] = {}
    for t in get_task_store().list():
        if t.get("status") != "ingested":
            continue
        acl = t.get("acl") or {}
        dids: List[int] = []
        level = acl.get("level") or ""
        if level == "L2":
            dids = [d for d in (acl.get("dept_id") or []) if isinstance(d, int)]
        elif level == "L4":
            dids = [int(x.split(":", 1)[1]) for x in (acl.get("extra_acl") or [])
                    if isinstance(x, str) and x.startswith("dept:")]
        if dids:
            owners[t.get("doc_id", "")] = dids
    return owners


def _dept_hit_counts(conn) -> Dict[int, int]:
    """近 HIT_DAYS 天各归属部门命中次数 (doc → 部门按台账 acl 摊派)."""
    owners = _dept_doc_owners()
    counts: Dict[int, int] = {}
    for doc_id, n in conn.execute(
        "SELECT doc_id, COUNT(*) FROM doc_hits"
        " WHERE hit_at >= datetime('now','localtime', ?) GROUP BY doc_id",
        (f"-{HIT_DAYS} days",),
    ).fetchall():
        for d in owners.get(doc_id, []):
            counts[d] = counts.get(d, 0) + n
    return counts


def assemble_graph() -> Dict[str, Any]:
    conn = get_conn()
    try:
        depts = conn.execute(
            "SELECT dept_id, name, parent_id, member_count, active FROM depts"
        ).fetchall()
        users = conn.execute(
            "SELECT userid, name, title, dept_ids, leader, admin, boss, active"
            " FROM users WHERE active=1"
        ).fetchall()
        gray = {r["dept_id"]: r["opened_at"] for r in
                conn.execute("SELECT dept_id, opened_at FROM gray_release").fetchall()}
        hit_by_dept = _dept_hit_counts(conn)
    finally:
        conn.close()

    dept_ids = {d["dept_id"] for d in depts}
    doc_count: Dict[int, int] = {}
    for dids in _dept_doc_owners().values():
        for d in dids:
            doc_count[d] = doc_count.get(d, 0) + 1

    nodes: List[dict] = []
    edges: List[dict] = []
    leader_names: Dict[int, List[str]] = {}

    for d in depts:
        nodes.append({
            "type": "dept", "id": d["dept_id"], "name": d["name"],
            "parent": d["parent_id"] if d["parent_id"] in dept_ids else 0,
            "meta": {
                "active": bool(d["active"]),
                "member_count": d["member_count"],
                "gray_open": d["dept_id"] in gray,
                "gray_opened_at": gray.get(d["dept_id"]),
                "doc_count": doc_count.get(d["dept_id"], 0),
                "hit_count": hit_by_dept.get(d["dept_id"], 0),
                "is_root": d["dept_id"] == ROOT_DEPT_ID,
            },
        })

    for u in users:
        try:
            raw_ids = [d for d in json.loads(u["dept_ids"] or "[]")
                       if d in dept_ids]
        except ValueError:
            raw_ids = []
        # 最具体部门: 去根后取子部门; 只剩根 (或空) 才连根
        specific = [d for d in raw_ids if d != ROOT_DEPT_ID] or [ROOT_DEPT_ID]
        multi = len(specific) > 1
        if u["leader"]:
            for d in (specific if specific != [ROOT_DEPT_ID] else raw_ids):
                leader_names.setdefault(d, []).append(u["name"])
        nodes.append({
            "type": "user", "id": u["userid"], "name": u["name"],
            "meta": {
                "title": u["title"] or "",
                "leader": bool(u["leader"]),
                "admin": bool(u["admin"]),
                "boss": bool(u["boss"]),
                "dept_ids": raw_ids,
                "multi_dept": multi,
                "root_only": specific == [ROOT_DEPT_ID],
            },
        })
        for d in specific:
            edges.append({"from": u["userid"], "to": d,
                          "kind": "leads" if u["leader"] else "belongs"})

    # 部门树边 (驾驶舱骨架: 子部门 → 父部门)
    for d in depts:
        if d["parent_id"] in dept_ids and d["dept_id"] != ROOT_DEPT_ID:
            edges.append({"from": d["dept_id"], "to": d["parent_id"], "kind": "child"})

    for node in nodes:
        if node["type"] == "dept":
            node["meta"]["leaders"] = leader_names.get(node["id"], [])

    return {
        "nodes": nodes,
        "edges": edges,
        "root_dept_id": ROOT_DEPT_ID,
        "hit_days": HIT_DAYS,
        "summary": {
            "depts": len(depts),
            "users_active": len(users),
            "leaders": sum(1 for u in users if u["leader"]),
            "multi_dept_users": sum(
                1 for n in nodes
                if n["type"] == "user" and n["meta"]["multi_dept"]),
        },
    }
