"""V2 业务库 schema + 连接管理 (SQLite)

表结构对齐 07 方案 §3.3:
- depts  : 钉钉部门镜像 (只读, 同步任务维护)
- users  : 钉钉成员镜像 (dept_ids 为 JSON 数组, 一人多部门)
- sync_log: 每次同步的快照记录 (可回滚排查)

纪律: 权限引擎只认本库, 不知道数据来自钉钉 — 换源=换适配器。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS depts (
    dept_id     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    parent_id   INTEGER NOT NULL DEFAULT 0,
    member_count INTEGER DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1,
    synced_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    userid      TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    mobile      TEXT,
    email       TEXT,
    title       TEXT,
    dept_ids    TEXT NOT NULL DEFAULT '[]',   -- JSON 数组, 一人多部门
    leader      INTEGER NOT NULL DEFAULT 0,
    admin       INTEGER NOT NULL DEFAULT 0,
    boss        INTEGER NOT NULL DEFAULT 0,
    active      INTEGER NOT NULL DEFAULT 1,
    unionid     TEXT,
    job_number  TEXT,
    synced_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,                -- full_sync / stream_event
    detail      TEXT NOT NULL DEFAULT '',
    dept_count  INTEGER DEFAULT 0,
    user_count  INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL
);

-- ===== P4 招投标打包导出 (16 号方案 V1.1 §4) =====
CREATE TABLE IF NOT EXISTS packages (
    package_id   TEXT PRIMARY KEY,          -- 随机 12 hex
    token        TEXT,                      -- 定稿后生成的受控链接 token (32 hex)
    title        TEXT NOT NULL,
    need_text    TEXT NOT NULL,             -- 原始需求 (审计回放)
    creator      TEXT NOT NULL,             -- 发起人 userid
    doc_ids      TEXT NOT NULL DEFAULT '[]',-- 确认提交的清单 JSON
    approved_ids TEXT,                      -- 定稿后的授权集合 JSON
    status       TEXT NOT NULL DEFAULT 'pending_approval',
               -- pending_approval→approved(已发链接)→expired; rejected=全部驳回; cancelled=撤回
    expires_at   TEXT,
    created_at   TEXT NOT NULL,
    confirmed_at TEXT,
    finalized_at TEXT,
    notify_ok    INTEGER
);

CREATE TABLE IF NOT EXISTS pkg_approvals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id   TEXT NOT NULL,
    dept_id      INTEGER NOT NULL,          -- 文档归属部门
    approver     TEXT NOT NULL,             -- 子单审批人 userid
    doc_ids      TEXT NOT NULL,             -- 该部门需要审的文件 JSON
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected
    decided_at   TEXT,
    reason       TEXT,
    notify_ok    INTEGER
);

CREATE TABLE IF NOT EXISTS package_views (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id TEXT NOT NULL,
    viewer     TEXT NOT NULL,
    opened_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pkgapprovals_status ON pkg_approvals(status);
CREATE INDEX IF NOT EXISTS idx_pkgapprovals_pkg ON pkg_approvals(package_id);
CREATE INDEX IF NOT EXISTS idx_packages_creator ON packages(creator);

CREATE INDEX IF NOT EXISTS idx_users_active ON users(active);
CREATE INDEX IF NOT EXISTS idx_depts_parent ON depts(parent_id);

CREATE TABLE IF NOT EXISTS l4_approvals (
    doc_id          TEXT PRIMARY KEY,
    filename        TEXT NOT NULL DEFAULT '',
    owner_userid    TEXT NOT NULL DEFAULT '',
    approver_userid TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending/approved/rejected/cancelled
    acl             TEXT NOT NULL DEFAULT '{}',        -- build_doc_acl 产出, 批准时用
    file_path       TEXT NOT NULL DEFAULT '',
    file_hash       TEXT NOT NULL DEFAULT '',
    route           TEXT NOT NULL DEFAULT '',
    size            INTEGER NOT NULL DEFAULT 0,
    decided_by      TEXT NOT NULL DEFAULT '',
    decision_reason TEXT NOT NULL DEFAULT '',
    notify_ok       INTEGER,                           -- 1 送达 / 0 失败 / NULL 未发
    created_at      TEXT NOT NULL,
    decided_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_l4_status ON l4_approvals(status);

CREATE TABLE IF NOT EXISTS review_queue (
    doc_id         TEXT PRIMARY KEY,
    filename       TEXT NOT NULL DEFAULT '',
    owner_userid   TEXT NOT NULL DEFAULT '',
    acl            TEXT NOT NULL DEFAULT '{}',
    file_path      TEXT NOT NULL DEFAULT '',
    file_hash      TEXT NOT NULL DEFAULT '',
    route          TEXT NOT NULL DEFAULT '',
    size           INTEGER NOT NULL DEFAULT 0,
    pages          INTEGER NOT NULL DEFAULT 0,
    issues         TEXT NOT NULL DEFAULT '[]',      -- 低置信信号 JSON 列表
    suggested_text TEXT NOT NULL DEFAULT '',          -- OCR 识别稿(可人工编辑)
    status         TEXT NOT NULL DEFAULT 'pending',   -- pending/approved/rejected
    decided_by     TEXT NOT NULL DEFAULT '',
    decision_note  TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    decided_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_status ON review_queue(status);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    target     TEXT NOT NULL DEFAULT '',
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);

-- #6 文档命中统计: 检索命中的文档落一行 (chunk 级去重到 doc 级, 见 hit_stats.py)
CREATE TABLE IF NOT EXISTS doc_hits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id       TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT '',             -- 文件名冗余, 展示免联查
    hit_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    query_digest TEXT NOT NULL DEFAULT '',             -- 命中时问题前 100 字
    actor        TEXT NOT NULL DEFAULT ''              -- 谁问的 name(userid)
);
CREATE INDEX IF NOT EXISTS idx_doc_hits_doc ON doc_hits(doc_id);

-- P3 灰度开放: 行存在=该部门已放开问答/上传; 空表=全放行(fail-open)。
-- 与钉钉同步解耦: 同步永不读写本表。
CREATE TABLE IF NOT EXISTS gray_release (
    dept_id    INTEGER PRIMARY KEY,
    opened_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def get_conn() -> sqlite3.Connection:
    path = Path(settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
