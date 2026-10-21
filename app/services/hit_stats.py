"""#6 文档命中统计 — doc_hits 表的唯一读写入口 (纯 SQLite, 纪律对齐 audit.py)

口径:
  - 一次问答对每个**唯一 doc_id** 落一行 (同 doc 多 chunk 命中不去重到 chunk 级,
    "命中次数" = 该 doc 的行数 ≈ 被检索命中过的问答次数, 够用且简单)
  - hit_at 本地时间字符串, 与 l4_approvals/review_queue/audit_log 口径一致
  - 写入失败绝不影响问答主流程 (吞异常 + warning, 同 audit.record)
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from app.core.logger import get_logger
from app.dingtalk.store import get_conn

logger = get_logger(__name__)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def record_hits(
    results: List[Dict[str, Any]],
    ident,
    question: str,
) -> None:
    """检索命中落账. results=retriever.search 返回列表, ident=Identity, question=原问题"""
    try:
        # doc 级去重 (保序, 取每个 doc 首个 chunk 的 source 作展示名)
        seen: Dict[str, str] = {}
        for r in results or []:
            doc_id = r.get("doc_id") or ""
            if not doc_id or doc_id in seen:
                continue
            seen[doc_id] = r.get("source") or "unknown"
        if not seen:
            return
        actor = f"{getattr(ident, 'name', '')}({getattr(ident, 'userid', '')})"
        digest = (question or "")[:100]
        now = _now()
        conn = get_conn()
        try:
            conn.executemany(
                "INSERT INTO doc_hits(doc_id, source, hit_at, query_digest, actor)"
                " VALUES(?,?,?,?,?)",
                [(doc_id, src, now, digest, actor) for doc_id, src in seen.items()],
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — 统计失败绝不能影响问答
        logger.warning("hit_stats 写入失败: %s", exc)


def aggregate(days: int = 30, limit: int = 50) -> List[Dict[str, Any]]:
    """按 doc_id 聚合近 N 天命中, 倒序 hits; 仅管理端读取."""
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 500))
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT doc_id,"
            "       MAX(source) AS source,"
            "       COUNT(*) AS hits,"
            "       MAX(hit_at) AS last_hit_at,"
            "       COUNT(DISTINCT actor) AS uniq_askers"
            " FROM doc_hits"
            " WHERE hit_at >= datetime('now','localtime', ?)"
            " GROUP BY doc_id ORDER BY hits DESC, last_hit_at DESC LIMIT ?",
            (f"-{days} days", limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def global_summary(days: int = 30, total_docs: int = 0) -> Dict[str, Any]:
    """全局摘要 KPI. total_docs=知识库现存文档总数 (调用方给, 见 admin.py 口径注释)."""
    days = max(1, min(int(days), 365))
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total_hits,"
            "       COUNT(DISTINCT doc_id) AS total_docs_hit"
            " FROM doc_hits WHERE hit_at >= datetime('now','localtime', ?)",
            (f"-{days} days",),
        ).fetchone()
        # 从未命中口径: 全时段有命中的 doc 数 (不受 days 过滤, 否则近期窗口会虚高零命中)
        hit_all = conn.execute(
            "SELECT COUNT(DISTINCT doc_id) FROM doc_hits"
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "total_hits": row["total_hits"],
        "total_docs_hit": row["total_docs_hit"],
        "no_hit_docs": max(total_docs - hit_all, 0),
    }
