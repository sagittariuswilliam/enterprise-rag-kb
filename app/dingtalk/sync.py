"""通讯录全量同步 — 钉钉 -> 本地 users/depts 表

用法:
    venv/bin/python -m app.dingtalk.sync            # 全量同步一次
定时: 每日凌晨跑一次 + Stream 事件实时增量 (P0 后段) + 管理员手动触发

diff 语义 (07 方案 §3.2 / 10-14 拍板):
- 新增的人  -> 建档, 默认最低权限 (权限表 P1 建, 这里只进 users 镜像)
- 离职的人  -> active=0 停用, 不删档 (审计需要)
- 调岗的人  -> dept_ids 覆盖更新, 下一次检索自然生效
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.dingtalk.client import DingTalkClient
from app.dingtalk.store import get_conn, init_db


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _user_row(u: dict, now: str) -> tuple:
    return (
        u["userid"], u["name"], u.get("mobile") or "", u.get("email") or "",
        u.get("title") or "",
        json.dumps(sorted(set(u.get("dept_id_list") or []))),
        1 if u.get("leader") else 0,
        1 if u.get("admin") else 0,
        1 if u.get("boss") else 0,
        1 if u.get("active", True) else 0,
        u.get("unionid") or "", u.get("job_number") or "", now,
    )


_UPSERT_USER = """INSERT INTO users(userid,name,mobile,email,title,dept_ids,leader,admin,boss,active,unionid,job_number,synced_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(userid) DO UPDATE SET
                 name=excluded.name, mobile=excluded.mobile, email=excluded.email,
                 title=excluded.title, dept_ids=excluded.dept_ids, leader=excluded.leader,
                 admin=excluded.admin, boss=excluded.boss, active=excluded.active,
                 unionid=excluded.unionid, job_number=excluded.job_number, synced_at=excluded.synced_at"""


def apply_user_snapshot(snap: dict) -> None:
    """Stream 增量: 单个用户最新快照 upsert (调岗后 dept_ids 自然覆盖).

    注意 user/get 返回的 dept_id_list 是部门 id 数组, 与 user/list 口径一致。
    """
    init_db()
    conn = get_conn()
    try:
        conn.execute(_UPSERT_USER, _user_row(dict(snap), _now()))
        conn.execute(
            "INSERT INTO sync_log(kind,detail,user_count,created_at) VALUES(?,?,?,?)",
            ("stream_event", f"user upsert {snap.get('userid')}", 1, _now()))
        conn.commit()
    finally:
        conn.close()


def mark_users_inactive(userids: list[str]) -> None:
    """离职停用 (不删档, 审计保留)."""
    if not userids:
        return
    init_db()
    conn = get_conn()
    try:
        conn.executemany(
            "UPDATE users SET active=0, synced_at=? WHERE userid=?",
            [(_now(), uid) for uid in userids])
        conn.execute(
            "INSERT INTO sync_log(kind,detail,user_count,created_at) VALUES(?,?,?,?)",
            ("stream_event", f"离职停用: {','.join(userids)}", len(userids), _now()))
        conn.commit()
    finally:
        conn.close()


def full_sync(client: DingTalkClient | None = None) -> dict:
    client = client or DingTalkClient()
    init_db()
    now = _now()

    depts = client.iter_all_departments()
    dept_rows = [
        (d["dept_id"], d["name"], d.get("parent_id", 0),
         d.get("member_count") or d.get("actual_member_count") or 0, 1, now)
        for d in depts
    ]

    # 按部门拉人, userid 去重合并 (一人多部门)
    people: dict[str, dict] = {}
    per_dept_count: dict[int, int] = {}
    for d in depts:
        dept_users = client.list_department_users(d["dept_id"])
        per_dept_count[d["dept_id"]] = len(dept_users)
        for u in dept_users:
            uid = u["userid"]
            if uid not in people:
                u = dict(u)
                u["dept_id_list"] = []
                people[uid] = u
            people[uid]["dept_id_list"].append(d["dept_id"])

    user_rows = [_user_row(u, now) for u in people.values()]

    conn = get_conn()
    try:
        conn.execute("BEGIN")
        # upsert + 本期消失的标记停用
        old_uids = {r["userid"] for r in conn.execute("SELECT userid FROM users")}
        new_uids = {r[0] for r in user_rows}
        left = old_uids - new_uids
        conn.executemany(_UPSERT_USER, user_rows)
        if left:
            conn.executemany("UPDATE users SET active=0, synced_at=? WHERE userid=?",
                             [(now, uid) for uid in left])
        conn.executemany(
            """INSERT INTO depts(dept_id,name,parent_id,member_count,active,synced_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(dept_id) DO UPDATE SET
                 name=excluded.name, parent_id=excluded.parent_id,
                 member_count=excluded.member_count, active=excluded.active,
                 synced_at=excluded.synced_at""",
            [(did, nm, pid, per_dept_count.get(did, mc), a, s)
             for did, nm, pid, mc, a, s in dept_rows])
        conn.execute(
            "INSERT INTO sync_log(kind,detail,dept_count,user_count,created_at) VALUES(?,?,?,?,?)",
            ("full_sync", f"离职停用 {len(left)} 人", len(dept_rows), len(user_rows), now))
        conn.commit()
    finally:
        conn.close()

    return {"departments": len(dept_rows), "users": len(user_rows), "marked_inactive": len(left)}


if __name__ == "__main__":
    result = full_sync()
    print("✅ 同步完成:", json.dumps(result, ensure_ascii=False))
