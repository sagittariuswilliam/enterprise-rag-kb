"""重排 - 阿里云百炼 Rerank API

模型: qwen3.7-text-rerank (2026-09-18 实测免费额度可用, 排序质量好)
端点: DashScope 原生 /api/v1/services/rerank/text-rerank/text-rerank

接口保持与旧版完全兼容:
- Reranker.rerank(query, passages, top_k) -> List[Tuple[int, float, str]]
- 模块级 rerank(query, candidates, top_k) -> list[dict]
"""
from __future__ import annotations

from typing import List, Tuple

import httpx

from app.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

RERANK_ENDPOINT = (
    "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
)


class Reranker:
    """qwen3.7-text-rerank 封装 (DashScope 原生 API)"""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key or settings.dashscope_api_key
        self.model = model or settings.rerank_model
        self.timeout = timeout

    # --------------------------------------------------------
    # 加载 (兼容旧接口: 云端无需加载)
    # --------------------------------------------------------

    def load(self):
        return self

    # --------------------------------------------------------
    # 推理
    # --------------------------------------------------------

    def rerank(
        self,
        query: str,
        passages: List[str],
        top_k: int = 5,
    ) -> List[Tuple[int, float, str]]:
        """重排序

        Returns:
            List[Tuple[原始下标, 分数, 文本]] 按分数从高到低, 最多 top_k 个
        """
        if not passages:
            return []

        k = min(top_k, len(passages))
        if k <= 0:
            return []

        payload = {
            "model": self.model,
            "input": {"query": query, "documents": passages},
            "parameters": {"return_documents": False, "top_n": k},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(RERANK_ENDPOINT, json=payload, headers=headers)
            data = r.json()

        if "output" not in data:
            code = data.get("code", "")
            msg = data.get("message", "")
            logger.error("Rerank API 失败: [%s] %s", code, msg)
            raise RuntimeError(f"Rerank API 失败: [{code}] {msg}")

        # output.results: [{index, relevance_score}, ...] 已按分数降序
        results = data["output"].get("results", [])
        out: List[Tuple[int, float, str]] = []
        for item in results[:k]:
            idx = item["index"]
            score = float(item.get("relevance_score", 0.0))
            out.append((idx, score, passages[idx]))
        return out


# ============================================================
# 单例
# ============================================================

_reranker: Reranker | None = None


def get_reranker() -> Reranker:
    global _reranker
    if _reranker is None:
        _reranker = Reranker()
    return _reranker


# ============================================================
# 模块级便捷函数 (与项目原有 API 兼容)
# ============================================================

def _candidate_text(c: dict) -> str:
    """从候选 dict 提取文本字段 (兼容多种 key)"""
    return c.get("text") or c.get("content") or c.get("chunk") or ""


def load_model():
    return get_reranker().load()


def rerank(query: str, candidates: list[dict], top_k: int | None = None) -> list[dict]:
    """对候选文档重排, 返回 Top-K (附带 'score' 字段)"""
    if not candidates:
        return []

    k = top_k or settings.top_k_rerank
    passages = [_candidate_text(c) for c in candidates]
    valid_idx = [i for i, t in enumerate(passages) if t]
    if not valid_idx:
        return []

    valid_passages = [passages[i] for i in valid_idx]
    ranked = get_reranker().rerank(query, valid_passages, top_k=k)

    out: list[dict] = []
    for orig_i, score, _text in ranked:
        item = dict(candidates[valid_idx[orig_i]])
        item["score"] = score
        out.append(item)
    return out
