"""P4 组清单 propose — LLM 拆查询 → 组包池召回 → 按文档聚合去重 (16 号 V1.1 §5)

纪律:
  - 召回必过 build_package_pool_filter (唯一出口, 绕开=违规)。
  - 输出只允许元数据: doc_id / source(标题) / level / dept_id / 命中理由(查询词, 非正文)。
    chunk 正文绝不进返回 (Q3 标题级例外的边界)。
  - 不落库, 可反复试探; 审计由 API 层记录。
  - LLM 拆 3-6 个检索词, JSON 输出; 解析失败降级为原文单查询 (propose 不能因 LLM 挂)。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from qdrant_client.models import FieldCondition, Filter, MatchAny

from app.acl.package_filter import build_package_pool_filter
from app.config import settings
from app.core.qdrant_client import get_client
from app.services.llm import get_llm
from app.services.retriever import Retriever

logger = logging.getLogger(__name__)

MAX_SUB_QUERIES = 6
PER_QUERY_TOP = 12        # 每个子查询召回 chunk 数
MAX_DOCS = 40             # 清单上限 (UI 可翻页的极限, 再多没意义)


def _primary_dept(raw: Any) -> int:
    """payload dept_id 归一化 — 入库侧多部门文档存的是【数组】([1001]),
    老数据/单部门可能是标量。拆单/展示按主归属部门(第一个)口径。
    (2026-10-21 P4 演练发现: int(list) TypeError → propose 500)"""
    if isinstance(raw, (list, tuple)):
        for x in raw:
            try:
                v = int(x)
                if v:
                    return v
            except (TypeError, ValueError):
                continue
        return 0
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0

_SPLIT_PROMPT = (
    "你在帮公司招投标员工从知识库找资料。把需求拆成 3-{n} 个检索查询词，"
    "覆盖需求提到的不同资料类别。只输出 JSON 数组，如 [\"查询1\",\"查询2\"]，"
    "不要输出其他文字。\n需求：{need}"
)


def _parse_queries(raw: str, need_text: str) -> list[str]:
    """LLM 输出 → 查询列表; 解析失败降级单查询"""
    m = re.search(r"\[.*\]", raw, re.S)
    if m:
        try:
            arr = json.loads(m.group(0))
            qs = [str(q).strip() for q in arr if str(q).strip()][:MAX_SUB_QUERIES]
            if qs:
                return qs
        except (json.JSONDecodeError, TypeError):
            pass
    logger.warning("propose LLM 拆查询解析失败, 降级原文单查询: %r", raw[:120])
    return [need_text]


async def propose_docs(need_text: str, creator_userid: str) -> list[dict[str, Any]]:
    """返回草稿清单 [{doc_id, source, level, dept_id, hit_reason, score}], 按 score 降序.

    hit_reason = 命中该文档的子查询词 (口径: 为什么觉得这份有用), 不是正文摘录。
    """
    # 1. LLM 拆查询
    try:
        raw = await get_llm().achat(
            _SPLIT_PROMPT.format(n=MAX_SUB_QUERIES, need=need_text.strip()[:500]),
            temperature=0.1,
        )
    except Exception as exc:  # noqa: BLE001 — LLM 挂了不能拖死组清单
        logger.warning("propose LLM 失败, 降级单查询: %s", exc)
        raw = ""
    queries = _parse_queries(raw, need_text)

    # 2. 向量化 + 池内召回 (embedder 复用 Retriever 单例的本地 bge-m3)
    embedder = Retriever().embedder
    pool = build_package_pool_filter(creator_userid)
    client = get_client()

    agg: dict[str, dict[str, Any]] = {}
    for q in queries:
        try:
            vec = embedder.embed_query(q).tolist()
            resp = await client.query_points(
                collection_name=settings.qdrant_collection,
                query=vec,
                limit=PER_QUERY_TOP,
                query_filter=pool,
                with_payload=True,
                with_vectors=False,
            )
            points = resp.points
        except Exception as exc:  # noqa: BLE001
            logger.error("propose 召回失败 query=%r: %s", q, exc)
            continue
        for p in points:
            pl = p.payload or {}
            doc_id = pl.get("doc_id") or ""
            level = (pl.get("level") or "").upper()
            if not doc_id or level not in ("L1", "L2", "L3"):
                # L4 / 脏数据防御 (池理论上已排除, 再挡一层)
                continue
            score = float(getattr(p, "score", 0.0) or 0.0)
            cur = agg.get(doc_id)
            if cur is None:
                agg[doc_id] = {
                    "doc_id": doc_id,
                    "source": pl.get("source", "unknown"),
                    "level": level,
                    "dept_id": _primary_dept(pl.get("dept_id")),
                    "owner_userid": pl.get("owner_userid") or "",
                    "hit_reason": q,
                    "score": score,
                }
            else:
                cur["score"] = max(cur["score"], score)
                if q not in cur["hit_reason"]:
                    cur["hit_reason"] += f"、{q}"

    docs = sorted(agg.values(), key=lambda d: -d["score"])[:MAX_DOCS]
    return docs


async def get_docs_meta(doc_ids: list[str]) -> dict[str, dict[str, Any]]:
    """confirm 侧服务端补齐元数据 (level/dept/owner/source) — 不信客户端传入。

    scroll by doc_id (不带池过滤: 用户可能手动补选了 propose 没返回的文档,
    但 L4/他人 L1 仍会被池口径拒绝 — 通过 payload 校验实现, 不通过 ACL 查询实现)。
    """
    if not doc_ids:
        return {}
    client = get_client()
    out: dict[str, dict[str, Any]] = {}
    offset = None
    want = set(doc_ids)
    try:
        while True:
            result = await client.scroll(
                collection_name=settings.qdrant_collection,
                scroll_filter=Filter(must=[
                    FieldCondition(key="doc_id", match=MatchAny(any=list(want))),
                ]),
                with_payload=True,
                with_vectors=False,
                limit=64,
                offset=offset,
            )
            points, next_offset = result
            for p in points:
                pl = p.payload or {}
                did = pl.get("doc_id")
                if not did:
                    continue
                cur = out.get(did)
                if cur is None:
                    out[did] = {
                        "level": (pl.get("level") or "").upper(),
                        "dept_id": _primary_dept(pl.get("dept_id")),
                        "owner_userid": pl.get("owner_userid") or "",
                        "source": pl.get("source", ""),
                    }
            if next_offset is None:
                break
            offset = next_offset
    except Exception as exc:  # noqa: BLE001
        logger.error("get_docs_meta scroll 失败: %s", exc)
        raise
    return out
