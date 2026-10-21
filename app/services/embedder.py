"""向量化 - 阿里云百炼 Embedding API

主模型: qwen3.7-text-embedding (免费额度耗尽自动降级 flash)
备用:   qwen3.7-text-embedding-flash
维度:   1024 (与 Qdrant collection 兼容)

2026-09-18: 从本地 bge-m3 切换到云端 API, 内存 2.2GB -> ~0
接口保持与旧版完全兼容 (embed / embed_query / embed_texts / dim)
"""
from __future__ import annotations

from typing import List

import httpx
import numpy as np

from app.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

# DashScope 原生 embedding 端点
EMBEDDING_ENDPOINT = (
    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
    "text-embedding/text-embedding"
)
# 官方单批最大 10 条
BATCH_LIMIT = 10
# 免费额度耗尽错误码 -> 触发降级
QUOTA_EXHAUSTED_CODE = "AllocationQuota.FreeTierOnly"


class Embedder:
    """云端 embedding 封装 (DashScope 原生 API)

    - 主模型免费额度耗尽 -> 自动切换备用模型 (进程内记住, 不再重试主模型)
    - 返回向量做 L2 归一化, 与旧 bge-m3 normalize_embeddings=True 行为一致
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key or settings.dashscope_api_key
        self.model = model or settings.embedding_model
        self.fallback_model = fallback_model or settings.embedding_fallback_model
        self.timeout = timeout
        self._active_model = self.model  # 当前生效模型 (降级后指向 fallback)

    # --------------------------------------------------------
    # 内部: 单批 API 调用
    # --------------------------------------------------------

    def _call_api(self, texts: List[str], model: str) -> List[List[float]]:
        """调一次 DashScope embedding API, 返回向量列表 (与 texts 等长)"""
        payload = {
            "model": model,
            "input": {"texts": texts},
            "parameters": {"dimension": settings.embedding_dim},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(EMBEDDING_ENDPOINT, json=payload, headers=headers)
            data = r.json()

        if "output" not in data:
            code = data.get("code", "")
            msg = data.get("message", "")
            raise EmbeddingAPIError(code, msg, r.status_code)

        embeddings = data["output"]["embeddings"]
        # API 按 text_index 排序返回, 保险起见排一下
        embeddings = sorted(embeddings, key=lambda e: e.get("text_index", 0))
        return [e["embedding"] for e in embeddings]

    def _embed_batch(self, texts: List[str]) -> np.ndarray:
        """单批 (<=10 条) embedding, 含降级逻辑"""
        try:
            vecs = self._call_api(texts, self._active_model)
        except EmbeddingAPIError as e:
            if (
                e.code == QUOTA_EXHAUSTED_CODE
                and self._active_model != self.fallback_model
                and self.fallback_model
            ):
                logger.warning(
                    "主模型 %s 免费额度耗尽, 自动降级到 %s",
                    self._active_model,
                    self.fallback_model,
                )
                self._active_model = self.fallback_model
                vecs = self._call_api(texts, self._active_model)
            else:
                logger.error("Embedding API 失败: [%s] %s", e.code, e.message)
                raise RuntimeError(f"Embedding API 失败: [{e.code}] {e.message}") from e

        arr = np.asarray(vecs, dtype=np.float32)
        # L2 归一化 (与旧版 normalize_embeddings=True 一致)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms

    # --------------------------------------------------------
    # 对外接口 (与旧版兼容)
    # --------------------------------------------------------

    def load(self):
        """兼容旧接口: 云端模型无需加载, 返回 self"""
        return self

    def embed(
        self,
        texts: List[str],
        batch_size: int = BATCH_LIMIT,
        show_progress_bar: bool = False,
    ) -> np.ndarray:
        """批量 embedding, 返回 (N, dim) float32, 已归一化"""
        if not texts:
            return np.empty((0, settings.embedding_dim), dtype=np.float32)

        batch_size = min(batch_size, BATCH_LIMIT)
        chunks = [
            list(texts[i : i + batch_size]) for i in range(0, len(texts), batch_size)
        ]
        results = []
        for chunk in chunks:
            results.append(self._embed_batch(chunk))
        return np.vstack(results)

    def embed_query(self, query: str) -> np.ndarray:
        """单条 query embedding"""
        return self.embed([query])[0]

    @property
    def dim(self) -> int:
        return settings.embedding_dim


class EmbeddingAPIError(Exception):
    """DashScope API 业务错误"""

    def __init__(self, code: str, message: str, status: int = 0) -> None:
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"[{code}] {message} (http {status})")


# ============================================================
# 单例
# ============================================================

_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
    return _embedder


# ============================================================
# 模块级便捷函数 (与项目原有 API 兼容)
# ============================================================

def _to_python_list(vecs: np.ndarray) -> list[list[float]]:
    return vecs.tolist()


def load_model():
    return get_embedder().load()


def embed_texts(texts: list[str]) -> list[list[float]]:
    vecs = get_embedder().embed(texts)
    return _to_python_list(vecs)


def embed_query(query: str) -> list[float]:
    vec = get_embedder().embed_query(query)
    return vec.tolist()
