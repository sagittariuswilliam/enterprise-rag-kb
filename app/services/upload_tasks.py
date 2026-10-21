"""D4 · 上传任务状态机(持久化)

状态流转:
    queued → parsing(可带 ocr_progress: {done, total}) → ingested / failed(reason)
    L4 审批门禁 (#L4): pending_approval → (批准后改 queued 提交线程池) / rejected / cancelled
    — pending_approval 任务重启不标 failed(审批单在 l4_approvals 表, 文件保留)

持久化在 app/data/upload_tasks.json:
    {doc_id: task_dict, ...}
服务重启时已 ingested/failed 的记录不丢;重启瞬间仍处于 queued/parsing 的任务
被标记为 failed(reason=服务重启中断),文件保留可重传(哈希未登记,不会 409)。

线程安全:所有读写走一把 threading.Lock,原子写(tmp+rename)。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

from app.config import settings
from app.core.logger import get_logger

logger = get_logger(__name__)

TASKS_FILE = Path(settings.data_dir) / "upload_tasks.json"

STATUS_QUEUED = "queued"
STATUS_PARSING = "parsing"
STATUS_INGESTED = "ingested"
STATUS_FAILED = "failed"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_NEEDS_REVIEW = "needs_review"   # #5 低置信扫描件待人工校对(等人状态, 重启不标 failed)

# 终态集合(重启恢复时不动)
_TERMINAL = {STATUS_INGESTED, STATUS_FAILED}


class TaskStore:
    """upload_tasks.json 的内存镜像 + 持久化"""

    def __init__(self, path: Path = TASKS_FILE) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._load()
        self._recover_interrupted()

    # -------------------------------------------------- 持久化
    def _load(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._tasks = data
        except Exception as exc:
            logger.warning("upload_tasks.json 读取失败(从空表开始): %s", exc)
            self._tasks = {}

    def _persist(self) -> None:
        """原子写:先写 tmp 再 rename。调用方必须已持锁。"""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(self._path.parent), prefix=".upload_tasks.", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._tasks, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._path)
        except Exception as exc:  # 持久化失败不阻塞任务本身
            logger.warning("upload_tasks.json 写入失败: %s", exc)

    def _recover_interrupted(self) -> None:
        """重启后把非终态任务如实标记为失败(文件保留可重传)。

        L4: pending_approval 是"等人"状态不是"在跑"状态 — 审批单持久在
        l4_approvals 表, 重启后审批人仍可继续处理, 故不参与中断恢复标记。
        """
        dirty = False
        with self._lock:
            for doc_id, task in self._tasks.items():
                if task.get("status") in (STATUS_PENDING_APPROVAL,
                                          STATUS_NEEDS_REVIEW):
                    # #5: needs_review 同样是"等人"状态 — OCR 稿持久在
                    # review_queue 表, 重启后校对人仍可处理, 文件保留
                    continue
                if task.get("status") not in _TERMINAL:
                    task["status"] = STATUS_FAILED
                    task["reason"] = "服务重启导致任务中断,文件已保留,请重新上传"
                    task["updated_at"] = time.time()
                    dirty = True
            if dirty:
                self._persist()
        if dirty:
            logger.warning("重启恢复:非终态上传任务已标记为 failed")

    # -------------------------------------------------- 对外 API
    def create(
        self,
        doc_id: str,
        *,
        filename: str,
        size: int,
        route: str,
        file_path: str,
        file_hash: str = "",
        acl: dict[str, Any] | None = None,
        status: str = STATUS_QUEUED,
    ) -> dict[str, Any]:
        now = time.time()
        task = {
            "doc_id": doc_id,
            "filename": filename,
            "size": size,
            "route": route,
            "file_path": file_path,
            "file_hash": file_hash,
            "acl": acl or {},
            "status": status,
            "reason": "",
            "chunks": 0,
            "format": "",
            "parse_method": "",
            "ocr_progress": {"done": 0, "total": 0},
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._tasks[doc_id] = task
            self._persist()
        return dict(task)

    def update(self, doc_id: str, **fields: Any) -> Optional[dict[str, Any]]:
        with self._lock:
            task = self._tasks.get(doc_id)
            if task is None:
                return None
            task.update(fields)
            task["updated_at"] = time.time()
            self._persist()
            return dict(task)

    def get(self, doc_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            task = self._tasks.get(doc_id)
            return dict(task) if task else None

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            tasks = [dict(t) for t in self._tasks.values()]
        tasks.sort(key=lambda t: t.get("created_at") or 0, reverse=True)
        return tasks

    def remove(self, doc_id: str) -> bool:
        with self._lock:
            if self._tasks.pop(doc_id, None) is not None:
                self._persist()
                return True
            return False


def public_view(task: dict[str, Any]) -> dict[str, Any]:
    """对外暴露的任务视图(去掉 file_path 等内部字段)。"""
    if not task:
        return {}
    return {
        "doc_id": task.get("doc_id"),
        "filename": task.get("filename"),
        "size": task.get("size"),
        "route": task.get("route"),
        "status": task.get("status"),
        "reason": task.get("reason") or "",
        "chunks": task.get("chunks", 0),
        "format": task.get("format") or "",
        "parse_method": task.get("parse_method") or "",
        "ocr_progress": task.get("ocr_progress") or {"done": 0, "total": 0},
        "level": (task.get("acl") or {}).get("level", ""),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }


# 进程级单例
_store: TaskStore | None = None
_store_lock = threading.Lock()


def get_task_store() -> TaskStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = TaskStore()
    return _store
