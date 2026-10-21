"""L4 哈希去重注册表(共享读写)

原先内联在 app/api/upload.py;D4 异步化后 upload 路由与后台 worker
都要读写,抽成独立模块避免循环依赖。

注册表文件: app/data/doc_hashes.json,格式 {sha256: filename}。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from app.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

HASH_REGISTRY = Path(settings.data_dir) / "doc_hashes.json"

_lock = threading.Lock()


def load_hash_registry() -> dict[str, str]:
    """读取注册表;损坏时返回空表,不阻塞上传。"""
    try:
        with _lock:
            if HASH_REGISTRY.exists():
                return json.loads(HASH_REGISTRY.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("哈希注册表读取失败(忽略): %s", exc)
    return {}


def save_hash(file_hash: str, filename: str) -> None:
    """登记一个已成功入库文件的哈希。"""
    if not file_hash:
        return
    try:
        with _lock:
            known = {}
            if HASH_REGISTRY.exists():
                try:
                    known = json.loads(HASH_REGISTRY.read_text(encoding="utf-8"))
                except Exception:
                    known = {}
            known[file_hash] = filename
            HASH_REGISTRY.write_text(
                json.dumps(known, ensure_ascii=False, indent=0), encoding="utf-8"
            )
    except OSError as exc:
        logger.warning("哈希注册表写入失败: %s", exc)


def remove_hash(file_hash: str) -> None:
    """从注册表删除一个哈希(清理测试数据/删除文档时用)。"""
    if not file_hash:
        return
    try:
        with _lock:
            if not HASH_REGISTRY.exists():
                return
            known = json.loads(HASH_REGISTRY.read_text(encoding="utf-8"))
            if known.pop(file_hash, None) is not None:
                HASH_REGISTRY.write_text(
                    json.dumps(known, ensure_ascii=False, indent=0), encoding="utf-8"
                )
    except Exception as exc:
        logger.warning("哈希注册表删除失败: %s", exc)
