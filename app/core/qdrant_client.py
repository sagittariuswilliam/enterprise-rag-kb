"""Qdrant 连接封装（端口 6333）"""

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams

from app.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

client: AsyncQdrantClient | None = None


async def init_qdrant() -> AsyncQdrantClient:
    """初始化 Qdrant 客户端，并确保 collection 存在"""
    global client
    client = AsyncQdrantClient(url=settings.qdrant_url)

    collections = await client.get_collections()
    names = [c.name for c in collections.collections]

    if settings.qdrant_collection not in names:
        await client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=VectorParams(
                size=settings.embedding_dim,
                distance=Distance.COSINE,
            ),
        )
        logger.info(f"创建 collection: {settings.qdrant_collection}")
    else:
        logger.info(f"collection 已存在: {settings.qdrant_collection}")

    return client


def get_client() -> AsyncQdrantClient:
    """获取全局 Qdrant 客户端"""
    if client is None:
        raise RuntimeError("Qdrant 未初始化，请先调用 init_qdrant()")
    return client


async def close_qdrant() -> None:
    """关闭连接"""
    global client
    if client is not None:
        await client.close()
        client = None
        logger.info("Qdrant 连接已关闭")
