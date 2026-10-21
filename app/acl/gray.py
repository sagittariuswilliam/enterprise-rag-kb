"""灰度开放控制 — P3 (2026-10-19)

灰度规则:
  - 未放开部门员工: 问答/上传入口后端 403; 前端据 /auth/me 的 gray_open=false 显示"即将开放"页
  - admin/boss 恒放行 (控制台必须可用, 含子管理员豁免)
  - 表为空 = 全部放行 (fail-open, 防误清表锁死全员; 首次部署由迁移脚本灌种子)

存储: gray_release(dept_id) 表, 行存在=已放开。与钉钉同步完全解耦 (同步永不写本表)。
"""

from __future__ import annotations

from app.acl.identity import Identity
from app.dingtalk.store import get_conn


def released_dept_ids() -> set[int]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT dept_id FROM gray_release").fetchall()
    finally:
        conn.close()
    return {r["dept_id"] for r in rows}


def is_open(ident: Identity) -> bool:
    """当前身份是否可用问答/上传。admin/boss 直通。"""
    if ident.admin or ident.boss:
        return True
    released = released_dept_ids()
    if not released:  # 未配置 → fail-open (同旧行为)
        return True
    return any(d in released for d in ident.dept_ids)


def set_open(dept_id: int, open_: bool) -> None:
    conn = get_conn()
    try:
        if open_:
            conn.execute(
                "INSERT OR IGNORE INTO gray_release(dept_id) VALUES(?)", (dept_id,))
        else:
            conn.execute("DELETE FROM gray_release WHERE dept_id=?", (dept_id,))
        conn.commit()
    finally:
        conn.close()
