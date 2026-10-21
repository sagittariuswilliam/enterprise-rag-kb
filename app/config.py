"""应用配置 - 从 .env 读取"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ===== 服务 =====
    host: str = "0.0.0.0"
    port: int = 9012

    # ===== Qdrant =====
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "kb_chunks"

    # ===== Embedding (阿里云百炼 API) =====
    dashscope_api_key: str = ""
    embedding_model: str = "qwen3.7-text-embedding"
    embedding_fallback_model: str = "qwen3.7-text-embedding-flash"
    embedding_dim: int = 1024

    # ===== Reranker (阿里云百炼 API) =====
    rerank_model: str = "qwen3.7-text-rerank"

    # ===== OCR (L2 分级解析 - 扫描版路径) =====
    ocr_model: str = "qwen3.5-ocr"  # 2026-09-19: qwen-vl-ocr 系列 2026-10-10 下线(公告118434)，切官方推荐继任 qwen3.5-ocr(免费额度100万tokens未用)
    ocr_model_fallback: str = "qwen-vl-ocr-2025-11-20"
    ocr_render_scale: float = 1.6      # 页面渲染倍率(1.6≈115dpi 足够 OCR)
    ocr_jpeg_quality: int = 82
    ocr_max_pages: int = 0             # 0=不限;单次 OCR 页数硬上限(费用保险丝)
    docx_ocr_max_images: int = 30      # D6: 单个 docx 内嵌图片 OCR 张数上限(超出只留占位)

    # ===== 上传 (D4 异步化) =====
    max_file_size: int = 500 * 1024 * 1024   # 单文件上限 500MB(流式落盘,不占内存)
    upload_workers: int = 2                  # 后台解析工作线程数(OCR 任务另有全局锁串行)

    # 旧本地模型路径 (保留兼容, 已不再使用)
    embedding_model_path: str = "./models/bge-m3"
    reranker_model_path: str = "./models/bge-reranker-v2-m3"

    # ===== LLM (兼容 OpenAI Chat Completions 协议的服务均可) =====
    llm_api_key: str = ""
    llm_base_url: str = "https://api.example.com/v1"
    llm_model: str = "your-llm-model-name"

    # ===== 分块 =====
    chunk_size: int = 500
    chunk_overlap: int = 50

    # ===== 检索 =====
    top_k_dense: int = 20
    top_k_bm25: int = 20
    top_k_rerank: int = 8

    # ===== 存储 =====
    data_dir: str = "app/data"
    db_path: str = "app/data/kb_v2.db"   # V2 业务库 (users/depts/sync_log)

    # ===== 钉钉 (P0: 组织同步 / 扫码登录 / Stream 事件) =====
    dingtalk_app_key: str = ""
    dingtalk_app_secret: str = ""
    dingtalk_robot_code: str = ""
    # 管理控制台公网前缀 (钉钉通知里的链接), 部署时按实际域名配置
    admin_base_url: str = "https://your-domain.example.com"
    # 2026-10-16 P0 扫码登录: 正式员工入口已开, /api/* 强制 JWT
    login_enabled: bool = True

    # ===== 会话 JWT (P0 扫码登录) =====
    jwt_secret: str = ""
    jwt_ttl_hours: int = 8

    # ===== 日志 =====
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
