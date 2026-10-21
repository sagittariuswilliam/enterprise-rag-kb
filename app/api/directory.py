"""通讯录目录接口 — L4 选人组件的数据源 (P1)

纪律: userid/部门 id 一律禁止手填 (2026-10-16 拍板), 前端只从本接口返回的
候选里点选, 避免填错导致越权/失效静默。

路由:
    GET /api/directory/search?q=<姓名前缀/全名>   搜在职成员 (limit 20)
    GET /api/directory/depts                       部门列表 (同步自钉钉)

数据来源: 本地 users/depts 表 (钉钉镜像, 事件订阅保活)。
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Query

from app.dingtalk.store import get_conn

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/directory/search")
async def search_users(q: str = Query(..., min_length=1, max_length=50),
                       limit: int = Query(20, ge=1, le=50)):
    """按姓名 LIKE 搜在职成员 → [{userid, name, dept_ids, dept_names}]"""
    kw = q.strip()
    if not kw:
        raise HTTPException(status_code=400, detail="q 不能为空")
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT userid, name, dept_ids FROM users "
            "WHERE active=1 AND name LIKE ? ORDER BY name LIMIT ?",
            (f"{kw}%", limit),
        ).fetchall()
        dept_rows = conn.execute(
            "SELECT dept_id, name FROM depts WHERE active=1"
        ).fetchall()
    finally:
        conn.close()

    dept_name = {r["dept_id"]: r["name"] for r in dept_rows}
    out = []
    for r in rows:
        try:
            ids = json.loads(r["dept_ids"] or "[]")
        except json.JSONDecodeError:
            ids = []
        out.append({
            "userid": r["userid"],
            "name": r["name"],
            "dept_ids": ids,
            "dept_names": [dept_name.get(d, f"#{d}") for d in ids],
        })
    return {"users": out, "count": len(out)}


@router.get("/directory/depts")
async def list_depts():
    """全部在职部门 → [{dept_id, name, parent_id, member_count}]"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT dept_id, name, parent_id, member_count FROM depts "
            "WHERE active=1 ORDER BY dept_id"
        ).fetchall()
    finally:
        conn.close()
    return {
        "depts": [dict(r) for r in rows],
        "count": len(rows),
    }
