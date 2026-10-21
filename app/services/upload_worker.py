"""D4 · 上传后台工作队列(threading + queue,不引入 Celery/Redis)

职责:
    - ``process_document()``: 单文件"解析→门禁→切块→入库"的可复用编排函数
      (从 upload.py 同步流程抽出,worker 与任何离线脚本都可调用)
    - ``UploadWorkerPool``: 常驻工作线程池 + FIFO 队列;
      OCR 类任务(scanned_pdf / image / 含图 docx)全局串行(一把锁),
      普通解析/embedding 不受限
    - 任务状态经 upload_tasks.TaskStore 持久化到 upload_tasks.json

线程里的 async 调用(index_chunks 走 AsyncQdrantClient)通过
``asyncio.run_coroutine_threadsafe(coro, main_loop)`` 提交回主事件循环。
"""

from __future__ import annotations

import asyncio
import queue
import threading
from pathlib import Path
from typing import Any, Optional

from app.config import settings
from app.core.logger import get_logger
from app.services.chunker import chunk_document
from app.services.gate import check_content
from app.services.hash_registry import save_hash
from app.services.ocr_parser import OCR_LOCK, OcrParseError, OcrQuotaError, parse_image_ocr, parse_pdf_scanned
from app.services.ocr_quality import low_confidence_signals
from app.services.parser import parse_file
from app.services.retriever import index_chunks
from app.services.review_queue import create_pending as create_review
from app.services.upload_tasks import (
    STATUS_FAILED,
    STATUS_INGESTED,
    STATUS_NEEDS_REVIEW,
    STATUS_PARSING,
    TaskStore,
    get_task_store,
)

logger = get_logger(__name__)

# 需要持有 OCR 全局锁的路由(防并发打 DashScope 限流)
_OCR_ROUTES = {"scanned_pdf", "image"}


def _run_coro(coro):
    """在工作线程里把协程提交回主事件循环并等待结果。"""
    loop = get_main_loop()
    if loop is None or loop.is_closed():
        raise RuntimeError("主事件循环不可用,无法执行异步入库")
    fut = asyncio.run_coroutine_threadsafe(coro, loop)
    return fut.result()


# 主事件循环引用(main.py lifespan 里注入)
_main_loop: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


def get_main_loop() -> Optional[asyncio.AbstractEventLoop]:
    return _main_loop


class ParseFailure(Exception):
    """解析/门禁/切块阶段的可预期失败(带人类可读 reason)。"""


def process_document(
    *,
    doc_id: str,
    file_path: Path,
    route: str,
    file_hash: str,
    filename: str,
    store: Optional[TaskStore] = None,
    on_delete_file: bool = True,
    acl: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """单文件 解析→门禁→切块→入库 全流程(同步函数,供 worker 线程调用)。

    与旧同步上传语义对齐:
        - 质量门禁不过(空/短/乱码) → 失败,删文件
        - OcrQuotaError → 失败,**文件保留**待处理
        - 解析失败(ValueError/OcrParseError/其他) → 失败,删文件
        - 入库 0 条 → 失败,删文件

    Returns:
        {"status": ingested|failed, "chunks": int, "reason": str,
         "format": str, "parse_method": str, "metadata": dict}
    """
    store = store or get_task_store()
    file_path = Path(file_path)

    def _progress(done: int, total: int) -> None:
        store.update(doc_id, ocr_progress={"done": done, "total": total})

    def _fail(reason: str, *, keep_file: bool = False) -> dict[str, Any]:
        if not keep_file and on_delete_file:
            file_path.unlink(missing_ok=True)
        store.update(doc_id, status=STATUS_FAILED, reason=reason)
        logger.warning("任务失败 doc_id=%s keep_file=%s: %s", doc_id, keep_file, reason)
        return {"status": STATUS_FAILED, "chunks": 0, "reason": reason,
                "format": "", "parse_method": "", "metadata": {}}

    # ---- 解析(L2 分级) ----
    store.update(doc_id, status=STATUS_PARSING, reason="",
                 ocr_progress={"done": 0, "total": 0})
    route_effective = route  # direct_pdf 降级后改为 pdf_ocr_fallback (#5 低置信判定用)
    try:
        if route in _OCR_ROUTES:
            with OCR_LOCK:  # OCR 类任务全局串行
                store.update(doc_id, status=STATUS_PARSING)
                if route == "scanned_pdf":
                    parsed = parse_pdf_scanned(file_path, progress_cb=_progress)
                else:
                    parsed = parse_image_ocr(file_path)
        else:
            if route == "tabular":
                from app.services.tabular_parser import parse_tabular
                parsed = parse_tabular(file_path)
            elif route == "pptx":
                from app.services.pptx_parser import parse_pptx
                parsed = parse_pptx(file_path)
            else:
                # docx(可能含内嵌图片 OCR,parse_docx 内部自持锁)/ direct_pdf / text
                parsed = parse_file(
                    file_path, doc_id=doc_id,
                    progress_cb=_progress if route == "docx" else None,
                )
    except OcrQuotaError as exc:
        return _fail(f"OCR 云账号额度不可用,文件已保留待处理: {exc}", keep_file=True)
    except OcrParseError as exc:
        return _fail(f"OCR 解析失败: {exc}")
    except FileNotFoundError as exc:
        return _fail(f"文件不存在: {exc}", keep_file=True)
    except ValueError as exc:
        return _fail(f"解析失败: {exc}")
    except Exception as exc:
        logger.exception("解析异常: %s", file_path)
        return _fail(f"解析失败: {exc}")

    # ---- 门禁(L4 后置:空/短/乱码) ----
    # 单页扫描件(身份证/营业执照/证书照片): 天然字少, 用 60 字低线, 否则误杀
    # (2026-10-16 法人身份证 136 字被 300 门禁拒收事故; 营业执照恰好 300+ 侥幸过线)
    _n_pages = parsed.metadata.get("pages") or 1
    allow_short = (_n_pages <= 1 and route in ("scanned_pdf", "image")) or (
        route == "image"  # 图片天然 1 页
    )
    gate = check_content(parsed.content, pages=parsed.metadata.get("pages"),
                         allow_short=allow_short)

    # direct_pdf 假阳性兜底(2026-10-16): 图文混排 PDF 只有零星文字密页,
    # 路由抽样可能恰好抽中密页判成原生, 但全量直读产出仍不达标。
    # 此时不直接拒收, 自动降级走 OCR 重解析一次(用户无感知, 界面看到进度页)。
    if not gate.ok and route == "direct_pdf":
        logger.warning("direct_pdf 门禁未过 [%s], 自动降级 OCR 重解析: %s",
                       gate.rule, filename)
        store.update(doc_id, reason="文字层不足,转 OCR 识别中…")
        try:
            with OCR_LOCK:
                store.update(doc_id, status=STATUS_PARSING,
                             ocr_progress={"done": 0, "total": 0})
                parsed = parse_pdf_scanned(file_path, progress_cb=_progress)
            route_effective = "pdf_ocr_fallback"  # #5: 降级后按 OCR 路线判低置信
            _np = parsed.metadata.get("pages") or 1
            gate = check_content(parsed.content, pages=parsed.metadata.get("pages"),
                                 allow_short=allow_short or _np <= 1)
        except OcrQuotaError as exc:
            return _fail(f"OCR 兜底不可用,文件已保留待处理: {exc}", keep_file=True)
        except OcrParseError as exc:
            return _fail(f"质量门禁拦截 [{gate.rule}]: {gate.reason}"
                         f"(OCR 兜底亦失败: {exc})")
        except Exception as exc:
            return _fail(f"质量门禁拦截 [{gate.rule}]: {gate.reason}"
                         f"(OCR 兜底异常: {exc})")

    if not gate.ok:
        return _fail(f"质量门禁拦截 [{gate.rule}]: {gate.reason}")

    # ---- #5 低置信分支: OCR 类路线且启发式有信号 → 进人工校对队列 ----
    # 位置: gate 已过(垃圾内容仍走 fail 删文件), 切块/入库/登记哈希之前。
    # 不入库、不登记哈希(重传仍可 409 之前放行)、**文件必须保留**待批准时重切。
    # low_confidence_signals 内部有 route 白名单, 非 OCR 路线直接返回 []。
    if route_effective in ("scanned_pdf", "image", "pdf_ocr_fallback"):
        signals = low_confidence_signals(
            parsed.content, parsed.metadata.get("pages"), route_effective,
        )
        if signals:
            acl = acl or {}
            try:
                create_review(
                    doc_id,
                    filename=filename,
                    owner_userid=acl.get("owner_userid", ""),
                    acl=acl,
                    file_path=str(file_path),
                    file_hash=file_hash,
                    route=route_effective,
                    size=file_path.stat().st_size if file_path.is_file() else 0,
                    pages=int(parsed.metadata.get("pages") or 1),
                    issues=signals,
                    suggested_text=parsed.content,
                )
            except Exception:  # noqa: BLE001 — 建单失败不能吞掉文件, 退回失败态(文件保留)
                logger.exception("review_queue 建单失败: doc_id=%s", doc_id)
                return _fail("低置信待校对, 但校对队列写入失败", keep_file=True)
            store.update(doc_id, status=STATUS_NEEDS_REVIEW,
                         reason="低置信, 待人工校对")
            logger.info(
                "任务待校对 doc_id=%s filename=%s route=%s signals=%s",
                doc_id, filename, route_effective,
                [s["rule"] for s in signals],
            )
            return {"status": STATUS_NEEDS_REVIEW, "chunks": 0,
                    "reason": "低置信, 待人工校对",
                    "format": parsed.metadata.get("format", "unknown"),
                    "parse_method": parsed.metadata.get("parse_method", ""),
                    "signals": signals, "metadata": parsed.metadata}

    # ---- 切块 ----
    try:
        chunks = chunk_document(
            parsed,
            chunk_size=settings.chunk_size,
            overlap=settings.chunk_overlap,
        )
    except ValueError as exc:
        return _fail(f"切分参数错误: {exc}")
    if not chunks:
        return _fail("文档未能切出任何 chunk")

    # ---- 入库(async → 提交回主循环) ----
    try:
        indexed = _run_coro(index_chunks(chunks, doc_id, acl=acl))
    except Exception as exc:
        logger.exception("入库失败: doc_id=%s", doc_id)
        return _fail(f"入库失败: {exc}")
    if indexed == 0:
        return _fail("入库返回 0 条,请检查 Qdrant 连接")

    # ---- 成功:登记哈希(此后重传同文件 → 409) ----
    save_hash(file_hash, filename)
    store.update(
        doc_id,
        status=STATUS_INGESTED,
        reason="",
        chunks=indexed,
        format=parsed.metadata.get("format", "unknown"),
        parse_method=parsed.metadata.get("parse_method", "native"),
    )
    logger.info(
        "任务完成 doc_id=%s filename=%s route=%s chunks=%d/%d",
        doc_id, filename, route, indexed, len(chunks),
    )
    return {
        "status": STATUS_INGESTED,
        "chunks": indexed,
        "reason": "",
        "format": parsed.metadata.get("format", "unknown"),
        "parse_method": parsed.metadata.get("parse_method", "native"),
        "metadata": parsed.metadata,
    }


class UploadWorkerPool:
    """threading 工作线程池 + FIFO 队列(依赖最小,不用 Celery/Redis)"""

    def __init__(self, n_workers: int = 2) -> None:
        self._q: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._n = max(1, n_workers)
        self._started = False
        self._stop = threading.Event()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for i in range(self._n):
            t = threading.Thread(
                target=self._loop, name=f"upload-worker-{i}", daemon=True
            )
            t.start()
            self._threads.append(t)
        logger.info("上传工作线程池已启动: %d workers", self._n)

    def submit(self, job: dict[str, Any]) -> None:
        self._q.put(job)

    def shutdown(self) -> None:
        self._stop.set()
        for _ in self._threads:
            self._q.put(None)

    def _loop(self) -> None:
        store = get_task_store()
        while not self._stop.is_set():
            job = self._q.get()
            if job is None:
                break
            doc_id = job.get("doc_id", "?")
            try:
                process_document(
                    doc_id=doc_id,
                    file_path=Path(job["file_path"]),
                    route=job["route"],
                    file_hash=job.get("file_hash", ""),
                    filename=job.get("filename", ""),
                    store=store,
                    acl=job.get("acl"),
                )
            except Exception:
                logger.exception("worker 未捕获异常: doc_id=%s", doc_id)
                store.update(doc_id, status=STATUS_FAILED,
                             reason="worker 内部异常,详见服务日志")
            finally:
                self._q.task_done()


_pool: Optional[UploadWorkerPool] = None
_pool_lock = threading.Lock()


def get_pool() -> UploadWorkerPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = UploadWorkerPool(n_workers=settings.upload_workers)
    return _pool
