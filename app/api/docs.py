"""/api/docs — #4 点开原文带水印 (引用卡片"查看原文")

纪律:
  - ACL 唯一出口: 非 admin/boss 一律叠加 build_acl_filter,
    无权限文档连 doc_id 是否存在都不暴露 (404, 与 P1 "标题不可见" 口径一致)
  - 查看原文是敏感动作, 命中审计 action="doc_view",
    actor="姓名(userid)", detail 带文件名与 chunk 数
  - 单文档最多返回 MAX_CHUNKS 个 chunk (防大文件打爆浏览器), 超出 truncated=true
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from qdrant_client.models import FieldCondition, Filter, MatchValue

from app.acl.doc_acl import acl_applies
from app.acl.filter import build_acl_filter
from app.acl.identity import Identity
from app.config import settings
from app.core.deps import get_current_identity
from app.core.qdrant_client import get_client
from app.services.audit import record as audit_record
from app.services.retriever import _unpack_scroll_result

logger = logging.getLogger(__name__)
router = APIRouter()

# 单文档返回上限: 超过只回前 N 个 chunk + truncated 标记
MAX_CHUNKS = 200

# 公司口径 UTC+8 (水印时间给前端直接展示)
_TZ_CN = timezone(timedelta(hours=8))


def _chunk_sort_key(item: tuple) -> float:
    """chunk_id 序号排序, 缺失/非数字排最后且稳定"""
    cid = item[0]
    try:
        return float(cid)
    except (TypeError, ValueError):
        return float("inf")


@router.get("/docs/{doc_id}/chunks")
async def doc_chunks(doc_id: str, ident: Identity = Depends(get_current_identity)):
    """按 doc_id 取该文档全部 chunk 原文 (按 chunk_id 排序拼接为段落列表)。

    带查看者本人 ACL: 无权 / 不存在均 404 (不区分, 避免探测 doc_id)。
    """
    if not doc_id or len(doc_id) > 64:
        raise HTTPException(status_code=400, detail="doc_id 无效")

    client = get_client()
    must: list = [FieldCondition(key="doc_id", match=MatchValue(value=doc_id))]
    if acl_applies(ident):
        must.append(build_acl_filter(ident))
    scroll_filter = Filter(must=must)

    collected: list[tuple] = []  # (chunk_id, text, source, level)
    truncated = False
    offset = None
    try:
        while True:
            result = await client.scroll(
                collection_name=settings.qdrant_collection,
                scroll_filter=scroll_filter,
                with_payload=True,
                with_vectors=False,
                limit=100,
                offset=offset,
            )
            points, next_offset = _unpack_scroll_result(result)
            for p in points:
                pl = getattr(p, "payload", None) or {}
                collected.append((
                    pl.get("chunk_id"),
                    pl.get("text") or "",
                    pl.get("source") or "",
                    pl.get("level") or "",
                ))
            if len(collected) > MAX_CHUNKS:
                truncated = True
                break
            if next_offset is None:
                break
            offset = next_offset
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — Qdrant 故障不泄露内部错误
        logger.error("doc_chunks scroll 失败: doc_id=%s err=%s", doc_id, exc)
        raise HTTPException(status_code=502, detail="文档存储暂时不可用")

    if not collected:
        # 与 P1 口径一致: 无权与不存在同形, 连标题都看不到
        raise HTTPException(status_code=404, detail="文档不存在或无权查看")

    collected.sort(key=_chunk_sort_key)
    if truncated:
        collected = collected[:MAX_CHUNKS]

    source = collected[0][2]
    level = collected[0][3]
    sections = [t for (_, t, _, _) in collected if t]

    # 敏感动作审计 (record 内部吞异常, 不阻断返回)
    audit_record(
        actor=f"{ident.name}({ident.userid})",
        action="doc_view",
        target=doc_id,
        detail={"source": source, "chunks": len(collected)},
    )

    return {
        "doc_id": doc_id,
        "source": source,
        "level": level,
        "total_chunks": len(collected),
        "text_sections": sections,
        "truncated": truncated,
        "watermark": {
            "actor": f"{ident.name}({ident.userid})",
            "time": datetime.now(_TZ_CN).isoformat(timespec="seconds"),
        },
    }
