"""混合检索模块（retriever）

职责
----
1. ``Retriever.search``: dense(Qdrant 向量召回) → rerank(bge-reranker-v2-m3)
3. ``index_chunks``: 把切分好的 :class:`~app.services.chunker.Chunk` 列表入库
4. ``list_documents``: 列出 collection 中已入库的全部文档（按 ``source`` 去重）
5. ``delete_document``: 按 ``source`` 删除某个文档的所有 chunk

设计要点
--------
- Qdrant 客户端是 :class:`AsyncQdrantClient`（见 ``app/core/qdrant_client.py``），
  因此所有与 Qdrant 交互的函数均为 ``async``，由 FastAPI 路由直接 ``await``。
- 检索采用 dense → rerank 二阶段流水线，不引入 BM25 中词库（与现有
  ``embedder`` + ``reranker`` 单例配套，保持依赖最小）。
- ``search`` 支持 ``doc_filter``，仅在指定 ``source`` 下做召回。
- ``delete_document`` 先 count 一次再 delete，得以返回实际删除数量
  （Qdrant 的 delete API 本身只返回 ``UpdateResult``）。
- **表格邻居扩展 (2026-10-19)**: rerank 命中的块若来自表格类文档
  (format ∈ {xlsx, xlsm, csv})，自动补齐同文档 chunk_id 相邻区间
  (±TAB_NEIGHBOR_WINDOW) 并向内填洞 — 修复"一整年合同数据恰好横跨
  切块边界, 检索只捞回 1/4 块导致计数不全"的问题。邻居块仍走同一
  ACL filter, 不放大权限; 邻居块不再触发二跳扩展 (防全表扩散)。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

import numpy as np
from qdrant_client.models import (FieldCondition, Filter, MatchAny, MatchValue,
                                  PointStruct, Range)

from app.acl.filter import build_acl_filter
from app.acl.doc_acl import acl_applies
from app.config import settings
from app.core.logger import get_logger
from app.core.qdrant_client import get_client
from app.services.chunker import Chunk
from app.services.embedder import get_embedder
from app.services.reranker import get_reranker

logger = get_logger(__name__)


# ============================================================
# Retriever 类（dense → rerank 二阶段检索）
# ============================================================
class Retriever:
    """混合检索器：dense (Qdrant 向量) + rerank (bge-reranker-v2-m3)

    使用方式::

        retriever = Retriever()
        hits = await retriever.search("什么是 RAG？", top_k=5)

    返回的每条 hit 形如::

        {
            "text":    "...",    # chunk 原文
            "score":   0.87,     # reranker 归一化分数 (0~1)
            "source":  "...",    # 文件名
            "page":    "3",      # 页码标记（PDF 有，非 PDF 为 "" 或 "?"）
            "format":  "pdf",    # 文档格式
            "chunk_id": 7,       # chunk 在原文档内的切片序号
            "doc_id":  "...",    # 文档 ID（入库时生成）
        }
    """

    def __init__(self) -> None:
        self.embedder = get_embedder()
        self.reranker = get_reranker()
        self.qdrant = get_client()
        self.collection = settings.qdrant_collection

    # --------------------------------------------------------
    # 主入口：search
    # --------------------------------------------------------
    async def search(
        self,
        query: str,
        top_k: int | None = None,
        doc_filter: Optional[str] = None,
        identity=None,
    ) -> List[Dict[str, Any]]:
        """完整检索流程

        Args:
            query: 用户查询文本
            top_k: 最终返回的条数；为 ``None`` 时取 ``settings.top_k_rerank``
            doc_filter: 可选，只检索某个 ``source`` 的文档
            identity: :class:`app.acl.identity.Identity` — 必传。非 admin/boss
                时召回强制叠加 build_acl_filter (P1, 07 方案 §3.4 唯一出口);
                admin/boss 全量视野。None 视为匿名 → 无权限, 返回 []

        Returns:
            命中列表，按 rerank 分数降序，最多 ``top_k`` 条
        """
        if not query or not query.strip():
            logger.warning("search() 收到空 query，直接返回 []")
            return []

        # ---- P1: 权限过滤在 Qdrant filter 层做, 检索后砍 = 违规 ----
        must_parts: List[Any] = []
        if identity is None:
            logger.warning("search() 未传 identity, 按无权限处理返回 []")
            return []
        if acl_applies(identity):
            must_parts.append(build_acl_filter(identity))
        if doc_filter:
            must_parts.append(
                FieldCondition(key="source", match=MatchValue(value=doc_filter))
            )
        search_filter = Filter(must=must_parts) if must_parts else None

        k_final = top_k if top_k is not None else settings.top_k_rerank
        if k_final <= 0:
            return []

        # 1. embed query（np.ndarray → list[float]）
        query_vec: np.ndarray = self.embedder.embed_query(query)
        query_list = query_vec.tolist()

        # 2. Qdrant dense 检索（取 top_k_dense 作为候选）

        # qdrant-client 1.7+: AsyncQdrantClient.search() 改名为 query_points()
        try:
            response = await self.qdrant.query_points(
                collection_name=self.collection,
                query=query_list,
                limit=settings.top_k_dense,
                query_filter=search_filter,
                with_payload=True,
                with_vectors=False,
            )
            results = response.points  # query_points 返回 QueryResponse, .points 是命中列表
        except Exception as exc:  # pragma: no cover - 运行时依赖 Qdrant
            logger.error("Qdrant query_points 失败: %s", exc)
            return []

        if not results:
            logger.info("Qdrant 无命中: query=%r filter=%s", query, doc_filter)
            return []

        # 3. 准备 rerank 输入（取每个命中的原文）
        passages: List[str] = [self._extract_text(r.payload) for r in results]

        # 4. rerank（CPU 重排，取 top_k_final）
        # reranker.rerank 返回 List[(orig_index, score, text)]
        try:
            reranked = self.reranker.rerank(query, passages, top_k=k_final)
        except Exception as exc:  # pragma: no cover - 运行时依赖模型
            logger.error("rerank 失败，回退到 dense 顺序: %s", exc)
            reranked = [
                (i, float(getattr(r, "score", 0.0) or 0.0), passages[i])
                for i in range(min(k_final, len(results)))
            ]

        # 5. 组装输出（带 payload 溯源字段）
        output: List[Dict[str, Any]] = []
        for orig_idx, score, text in reranked:
            if orig_idx < 0 or orig_idx >= len(results):
                continue
            payload = results[orig_idx].payload or {}
            output.append(
                {
                    "text": text or self._extract_text(payload),
                    "score": float(score),
                    "source": payload.get("source", "unknown"),
                    "page": payload.get("page", "?"),
                    "format": payload.get("format", "?"),
                    "chunk_id": payload.get("chunk_id", orig_idx),
                    "doc_id": payload.get("doc_id", ""),
                    "level": payload.get("level", ""),
                }
            )

        logger.info(
            "search done: dense=%d -> final=%d (query=%r)",
            len(results),
            len(output),
            query[:60],
        )
        output = await self._expand_tabular_neighbors(
            output, search_filter, k_final
        )
        return output

    # --------------------------------------------------------
    # 表格邻居扩展 (2026-10-19): 命中的 xlsx/csv 块自动补齐
    # 同文档相邻 chunk — 修"整年数据横跨切块边界只捞回 1/4"问题
    # --------------------------------------------------------
    TAB_FORMATS = {"xlsx", "xlsm", "csv"}
    TAB_NEIGHBOR_WINDOW = 4   # 命中块 chunk_id ±4 (一整年数据跨度实测可达 4+)

    async def _expand_tabular_neighbors(
        self,
        hits: List[Dict[str, Any]],
        search_filter: Optional[Filter],
        k_final: int,
    ) -> List[Dict[str, Any]]:
        """对命中的表格块, 拉同文档 chunk_id 相邻区间的块补进结果。

        规则:
        - 只扩 format ∈ TAB_FORMATS 的命中块; 邻居不再二跳(防全表扩散)
        - 每个命中表格块扩 [cid-W, cid+W] 并向内填洞(同文档命中块间的 gap)
        - 邻居带进 ACL search_filter, 权限口径与主检索一致
        - 邻居分数标 0(未经 rerank), 排序时置于原命中之后
        - 总扩容量上限 = max(4, k_final) 块, 控上下文成本
        """
        tab_hits = [h for h in hits if h.get("format") in self.TAB_FORMATS]
        if not tab_hits:
            return hits

        existing = {(h.get("doc_id"), h.get("chunk_id")) for h in hits}
        # 每个命中表格块独立扩 ±W, 按 (命中块 score 降序, 距离升序) 排候选。
        # 不按 min/max 合并区间 — 表头块(常被命中)会把区间撑满全表浪费 budget。
        candidates: List[tuple] = []  # (neg_score, dist, doc_id, cid)
        for h in tab_hits:
            cid = h.get("chunk_id")
            if not isinstance(cid, int):
                continue
            sc = float(h.get("score", 0.0))
            for d in list(range(1, self.TAB_NEIGHBOR_WINDOW + 1)):
                for nb in (cid - d, cid + d):
                    if nb >= 0:
                        candidates.append((-sc, d, h.get("doc_id"), nb))

        # 按 doc 分组批量 scroll, 减少请求数
        by_doc_want: Dict[str, Dict[int, tuple]] = {}
        for neg_sc, d, doc_id, nb in sorted(candidates):
            if (doc_id, nb) in existing:
                continue
            existing.add((doc_id, nb))
            by_doc_want.setdefault(doc_id, {})[nb] = (neg_sc, d)

        client = get_client()
        got: Dict[tuple, Dict[str, Any]] = {}  # (doc_id,cid) -> hit dict
        for doc_id, nbs in by_doc_want.items():
            cid_list = sorted(nbs)
            must_parts: List[Any] = [
                FieldCondition(key="doc_id", match=MatchValue(value=doc_id)),
                FieldCondition(key="chunk_id", match=MatchAny(any=cid_list)),
            ]
            if search_filter is not None:
                must_parts.extend(search_filter.must or [])
            try:
                pts, _ = await client.scroll(
                    collection_name=self.collection,
                    scroll_filter=Filter(must=must_parts),
                    limit=len(cid_list),
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception as exc:  # pragma: no cover
                logger.error("表格邻居扩展 scroll 失败 doc=%s: %s", doc_id, exc)
                continue
            for p in pts:
                pl = p.payload or {}
                nb_cid = pl.get("chunk_id")
                if nb_cid not in nbs:
                    continue
                got[(doc_id, nb_cid)] = {
                    "text": self._extract_text(pl),
                    "score": 0.0,
                    "source": pl.get("source", "unknown"),
                    "page": pl.get("page", "?"),
                    "format": pl.get("format", "?"),
                    "chunk_id": pl.get("chunk_id", -1),
                    "doc_id": pl.get("doc_id", ""),
                    "level": pl.get("level", ""),
                }

        # 按候选优先级 (score desc, dist asc) 截 budget, 再按 chunk_id 排好
        # 预算 = 2 倍 k_final: 单年数据可横跨 4+ 块且多个年份块竞争预算,
        # 实测 budget=8 会挤掉低优先级年的边缘块 (2026-10-19 定标)
        budget = 2 * max(4, k_final)
        added: List[Dict[str, Any]] = []
        for doc_id, nbs in sorted(
            by_doc_want.items(),
            key=lambda kv: min(nbs.values()),
        ):
            for nb in sorted(nbs, key=lambda c: nbs[c]):
                h = got.get((doc_id, nb))
                if h is None:
                    continue
                added.append(h)
                if len(added) >= budget:
                    break
            if len(added) >= budget:
                break
        if added:
            logger.info(
                "表格邻居扩展: +%d 块 (来自 %s)",
                len(added), ",".join(sorted(by_doc_want.keys())),
            )
        return hits + added

    # --------------------------------------------------------
    # 内部 helper
    # --------------------------------------------------------
    @staticmethod
    def _build_source_filter(doc_filter: Optional[str]) -> Optional[Filter]:
        """根据 doc_filter 构造 Qdrant Filter（仅按 source 过滤）"""
        if not doc_filter:
            return None
        return Filter(
            must=[
                FieldCondition(
                    key="source",
                    match=MatchValue(value=doc_filter),
                )
            ]
        )

    @staticmethod
    def _extract_text(payload: Optional[Dict[str, Any]]) -> str:
        """从 payload 中提取文本字段（兼容多种 key）"""
        if not payload:
            return ""
        return (
            payload.get("text")
            or payload.get("content")
            or payload.get("chunk")
            or ""
        )


# ============================================================
# 入库：index_chunks（供 upload.py / 离线脚本调用）
# ============================================================
async def index_chunks(chunks: Iterable[Any], doc_id: str,
                       acl: Optional[Dict[str, Any]] = None) -> int:
    """把 chunk 列表入库到 Qdrant

    Args:
        chunks: :class:`~app.services.chunker.Chunk` 列表（或任何带
                ``.text`` / ``.metadata`` 属性的对象）
        doc_id: 文档唯一 ID，会作为 Qdrant point id 的前缀
        acl: build_doc_acl() 产出的 {level, dept_id, owner_userid, extra_acl}。
             **必传** — 缺失直接拒绝入库 (P1: 无密级文档不允许进 kb_chunks)。

    Returns:
        成功入库的 chunk 数量（0 表示空输入或全部失败）
    """
    if not acl or "level" not in (acl or {}):
        raise ValueError("index_chunks 缺少 acl — 文档必须带密级入库 (P1)")
    chunks_list: List[Any] = list(chunks)
    if not chunks_list:
        logger.warning("index_chunks 收到空列表，doc_id=%s", doc_id)
        return 0

    embedder = get_embedder()
    client = get_client()
    collection = settings.qdrant_collection

    # 1. 批量 embedding
    texts = [getattr(c, "text", "") or "" for c in chunks_list]
    vectors = embedder.embed(texts)  # np.ndarray, shape=(N, dim)

    # 2. 组装 PointStruct
    import uuid as _uuid
    points: List[PointStruct] = []
    for i, (chunk, vec) in enumerate(zip(chunks_list, vectors)):
        meta = getattr(chunk, "metadata", None) or {}
        # Qdrant point ID 必须是 unsigned int 或完整 UUID,不能是字符串前缀拼接
        point_id = str(_uuid.uuid4())
        points.append(
            PointStruct(
                id=point_id,
                vector=vec.tolist(),
                payload={
                    "text": getattr(chunk, "text", ""),
                    "source": meta.get("source", "unknown"),
                    "page": meta.get("page", ""),
                    "format": meta.get("format", "unknown"),
                    "chunk_id": meta.get("chunk_id", i),
                    "char_count": meta.get("char_count", len(texts[i])),
                    "doc_id": doc_id,
                    # ---- P1 文档级 ACL (payload 契约见 acl/doc_acl.py) ----
                    "level": acl["level"],
                    "dept_id": acl.get("dept_id", []),
                    "owner_userid": acl.get("owner_userid", ""),
                    "extra_acl": acl.get("extra_acl", []),
                },
            )
        )

    # 3. 分批 upsert（Qdrant 单次请求 body 上限 32MB，大文档必须分批）
    batch_size = 200
    done = 0
    try:
        for start in range(0, len(points), batch_size):
            batch = points[start : start + batch_size]
            await client.upsert(collection_name=collection, points=batch, wait=True)
            done += len(batch)
            logger.info(
                "index_chunks batch upsert: doc_id=%s %d/%d",
                doc_id, done, len(points),
            )
    except Exception as exc:  # pragma: no cover - 运行时依赖 Qdrant
        logger.error(
            "Qdrant upsert 失败: doc_id=%s err=%s (已成功 %d/%d)",
            doc_id, exc, done, len(points),
        )
        # 回滚：删掉本 doc 已入库的残留 chunks，避免半截数据被检索到
        if done > 0:
            try:
                await client.delete(
                    collection_name=collection,
                    points_selector=Filter(
                        must=[FieldCondition(
                            key="doc_id", match=MatchValue(value=doc_id)
                        )]
                    ),
                    wait=True,
                )
                logger.info("已回滚部分入库 chunks: doc_id=%s", doc_id)
            except Exception as clean_exc:  # noqa: BLE001
                logger.warning("回滚失败: doc_id=%s err=%s", doc_id, clean_exc)
        if done == 0:
            return 0
        raise RuntimeError(
            f"入库中途失败：已成功 {done}/{len(points)} 块，"
            f"错误：{type(exc).__name__}: {exc}"
        ) from exc

    logger.info("index_chunks ok: doc_id=%s count=%d", doc_id, done)
    return done


# ============================================================
# 文档列表：list_documents
# ============================================================
async def list_documents(identity=None) -> List[Dict[str, Any]]:
    """列出 collection 中已入库的全部文档（按 ``source`` 去重）

    P1: 传 identity 时只列该用户有权看到的文档 (scroll 带 ACL filter);
    admin/boss 全量。identity=None 视为匿名 → 返回 []。

    Returns:
        ``[{"source", "doc_id", "level", "dept_id", "owner_userid", "extra_acl"}, ...]``
    """
    if identity is None:
        logger.warning("list_documents 未传 identity, 按无权限处理返回 []")
        return []

    client = get_client()
    collection = settings.qdrant_collection

    scroll_filter = None
    if acl_applies(identity):
        scroll_filter = build_acl_filter(identity)

    docs: dict[str, dict] = {}
    offset = None
    page = 0

    try:
        while True:
            result = await client.scroll(
                collection_name=collection,
                limit=100,
                offset=offset,
                with_payload=True,
                with_vectors=False,
                scroll_filter=scroll_filter,
            )
            # 兼容两种返回：tuple(points, next_offset) 或 dataclass
            points, next_offset = _unpack_scroll_result(result)

            for p in points:
                payload = getattr(p, "payload", None) or {}
                src = payload.get("source")
                if src:
                    # 同 source 多 chunk → 合并 doc_id (理论上 doc_id 一致)
                    entry = docs.setdefault(src, {
                        "source": src,
                        "doc_id": payload.get("doc_id", ""),
                        "level": payload.get("level", ""),
                        "dept_id": payload.get("dept_id", []),
                        "owner_userid": payload.get("owner_userid", ""),
                        "extra_acl": payload.get("extra_acl", []),
                    })
                    if not entry["doc_id"]:
                        entry["doc_id"] = payload.get("doc_id", "")

            page += 1
            if next_offset is None:
                break
            offset = next_offset
    except Exception as exc:  # pragma: no cover - 运行时依赖 Qdrant
        logger.error("list_documents 失败: %s", exc)
        return []

    logger.info(
        "list_documents: scanned %d pages, %d unique sources (user=%s)",
        page, len(docs), getattr(identity, "userid", "?"),
    )
    return sorted(docs.values(), key=lambda d: d["source"])


# ============================================================
# 删除文档：delete_document
# ============================================================
async def delete_document(source: str) -> int:
    """删除某个文档（按 ``source``）的全部 chunk

    Args:
        source: 文档文件名（即入库时 payload["source"]）

    Returns:
        实际删除的 chunk 数量（Qdrant delete API 不直接返回数量，
        这里先 ``count`` 再 ``delete``）
    """
    if not source:
        logger.warning("delete_document 收到空 source")
        return 0

    client = get_client()
    collection = settings.qdrant_collection

    delete_filter = Filter(
        must=[
            FieldCondition(
                key="source",
                match=MatchValue(value=source),
            )
        ]
    )

    # 1. 先 count（精确返回删除数量）
    deleted_count = 0
    try:
        count_result = await client.count(
            collection_name=collection,
            count_filter=delete_filter,
            exact=True,
        )
        deleted_count = int(getattr(count_result, "count", 0) or 0)
    except Exception as exc:  # pragma: no cover
        logger.warning("count 失败（将继续 delete）: %s", exc)
        deleted_count = 0

    # 2. delete
    try:
        await client.delete(
            collection_name=collection,
            points_selector=delete_filter,
            wait=True,
        )
    except Exception as exc:  # pragma: no cover - 运行时依赖 Qdrant
        logger.error("delete_document 失败: source=%s err=%s", source, exc)
        return 0

    logger.info(
        "delete_document ok: source=%s deleted=%d", source, deleted_count
    )
    return deleted_count


# ============================================================
# scroll 返回值兼容（不同版本 qdrant-client 返回形状略有差异）
# ============================================================
def _unpack_scroll_result(result: Any) -> tuple[list, Any]:
    """把 scroll() 的返回值统一成 (points, next_offset) 形式

    qdrant-client 较新版本可能返回 ``ScrollResponse`` 之类的具名元组，
    老版本直接返回 ``(points, next_offset)``，这里都兼容。
    """
    if isinstance(result, tuple) and len(result) == 2:
        return result[0], result[1]

    # ScrollResponse / dataclass 形式
    points = getattr(result, "points", None)
    next_offset = getattr(result, "next_page_offset", None)
    if next_offset is None:
        next_offset = getattr(result, "offset", None)
    return list(points or []), next_offset


# ============================================================
# 简易自测（仅 import / 函数签名检查）
# ============================================================
if __name__ == "__main__":  # pragma: no cover
    # 不实际连 Qdrant，只验证模块导入与符号
    print("Retriever ok:", Retriever)
    print("index_chunks ok:", index_chunks)
    print("list_documents ok:", list_documents)
    print("delete_document ok:", delete_document)