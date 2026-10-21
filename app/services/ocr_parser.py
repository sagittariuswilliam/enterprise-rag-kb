"""OCR 解析模块（L2 · 分级解析 — 扫描版路径）

将扫描版 PDF / 图片转成与 parse_pdf 相同风格的文本：
    - pypdfium2 逐页渲染 → JPEG → qwen-vl-ocr（DashScope compatible-mode）
    - 每页文本前加 "[页码 N]" 前缀（与 parser.parse_pdf 约定一致，
      chunker 的页码保留机制可直接复用）
    - 输出 ParsedDoc（format="pdf_scanned" / "image_ocr", parse_method 记录来源）

失败语义：
    - 账号额度类错误（AllocationQuota.* / 403）→ 抛 OcrQuotaError，
      由上层如实报告"待处理"，绝不静默产出空文本
    - 单页 OCR 失败 → 记 warning 跳过该页（与 parse_pdf 单页容错一致）
    - 连续 3 页失败 → 视为整体失败抛异常（避免烧半本书的调用费）
"""

from __future__ import annotations

import base64
import io
import json
import threading
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from app.config import settings
from app.core.logger import get_logger
from app.services.parser import ParsedDoc

logger = get_logger(__name__)

CHAT_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
OCR_PROMPT = "请识别这页文档的全部文字，按原排版顺序输出纯文本（不要HTML标签、不要解释、不要总结、缺失处不要编造）。"  # qwen3.5-ocr 内置任务默认输出HTML，须显式要求纯文本

# D6: docx 内嵌图片双轮识别 prompt
IMAGE_TEXT_PROMPT = "输出图中全部文字，按原排版顺序输出纯文本（不要HTML标签、不要解释、不要总结、缺失处不要编造）。"
IMAGE_DESCRIBE_PROMPT = "用2-3句话描述这张图的内容（若是流程图/架构图，按节点和箭头关系转述）。不要输出任何多余解释。"
# 第一轮识别文字少于此长度 → 视为"图里没什么字"(流程图/架构图)，转第二轮描述
IMAGE_DESCRIBE_THRESHOLD = 10

# D4: OCR 类任务全局串行锁(防并发打 DashScope 限流)。
# 扫描 PDF / 图片上传在 upload_worker 持锁;docx 内嵌图片 OCR 在
# parser.parse_docx 内部持锁(仅当文档确实含内嵌图片时才占锁)。
OCR_LOCK = threading.Lock()

_MAX_CONSECUTIVE_FAILS = 3


class OcrQuotaError(RuntimeError):
    """账号级额度/权限问题（免费额度耗尽、未开通付费等）"""


class OcrParseError(RuntimeError):
    """OCR 解析整体失败（连续多页失败等）"""


def _render_page_jpeg(pdfium_page, scale: float, quality: int) -> bytes:
    img = pdfium_page.render(scale=scale).to_pil().convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _ocr_image_bytes(jpeg: bytes, *, timeout: int = 90, prompt: str | None = None) -> str:
    """调 qwen-vl-ocr 识别一张 JPEG。403 额度错误转 OcrQuotaError。

    Args:
        prompt: 覆盖默认 OCR_PROMPT(D6 docx 图片识别用不同 prompt);
                签名向后兼容,不传时行为与改造前完全一致。
    """
    payload = {
        "model": settings.ocr_model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64,"
                            + base64.b64encode(jpeg).decode()
                        },
                    },
                    {"type": "text", "text": prompt or OCR_PROMPT},
                ],
            }
        ],
        "max_tokens": 2048,
    }
    req = urllib.request.Request(
        CHAT_ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {settings.dashscope_api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode(errors="replace")
        except Exception:  # pragma: no cover
            pass
        if exc.code in (401, 403) or "FreeTierOnly" in detail or "quota" in detail.lower():
            raise OcrQuotaError(
                f"OCR 账号额度/权限被拒 (HTTP {exc.code}): {detail[:200]}"
            ) from exc
        raise OcrParseError(f"OCR API HTTP {exc.code}: {detail[:200]}") from exc
    except Exception as exc:
        raise OcrParseError(f"OCR API 调用失败: {exc}") from exc

    try:
        return (body["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError) as exc:
        raise OcrParseError(f"OCR 响应格式异常: {str(body)[:200]}") from exc


def parse_pdf_scanned(
    file_path: Path,
    *,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> ParsedDoc:
    """扫描版 PDF：逐页渲染 + OCR，产出与 parse_pdf 同风格的 ParsedDoc。

    Args:
        progress_cb: D4 新增,可选。每页 OCR 完成后回调 (done, total),
                     供后台任务更新 ocr_progress。签名向后兼容。
    """
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(file_path))
    total = len(pdf)
    limit = settings.ocr_max_pages or total
    n = min(total, limit)

    pages_text: list[str] = []
    fails = 0
    failed_pages: list[int] = []

    for i in range(n):
        try:
            jpeg = _render_page_jpeg(
                pdf[i], settings.ocr_render_scale, settings.ocr_jpeg_quality
            )
            text = _ocr_image_bytes(jpeg)
            fails = 0
            if text:
                pages_text.append(f"[页码 {i + 1}]\n{text}")
        except OcrQuotaError:
            raise  # 额度问题立刻上抛，不烧后面的页
        except OcrParseError as exc:
            fails += 1
            failed_pages.append(i + 1)
            logger.warning("OCR 第 %d 页失败(连续%d): %s", i + 1, fails, exc)
            if fails >= _MAX_CONSECUTIVE_FAILS:
                raise OcrParseError(
                    f"连续 {fails} 页 OCR 失败，中止（已失败页: {failed_pages}）"
                )
            continue
        finally:
            if progress_cb is not None:
                try:
                    progress_cb(i + 1, n)
                except Exception:  # 进度回调故障不影响解析
                    logger.warning("ocr progress_cb 异常(忽略)", exc_info=True)

    if not pages_text:
        raise OcrParseError(f"OCR 完成但零文字产出: {file_path.name} (共尝试 {n} 页)")

    return ParsedDoc(
        content="\n\n".join(pages_text),
        metadata={
            "filename": file_path.name,
            "pages": total,
            "pages_parsed": n,
            "format": "pdf_scanned",
            "parse_method": f"ocr:{settings.ocr_model}",
            "failed_pages": failed_pages,
        },
    )


def parse_image_ocr(file_path: Path) -> ParsedDoc:
    """图片文件（jpg/png 等）直接 OCR，单页语义。"""
    from PIL import Image

    img = Image.open(file_path).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=settings.ocr_jpeg_quality)
    text = _ocr_image_bytes(buf.getvalue())
    if not text:
        raise OcrParseError(f"图片 OCR 零文字产出: {file_path.name}")
    return ParsedDoc(
        content=text,
        metadata={
            "filename": file_path.name,
            "pages": 1,
            "format": "image_ocr",
            "parse_method": f"ocr:{settings.ocr_model}",
        },
    )


# ---------------------------------------------------------------------------
# D6 · docx 内嵌图片识别(双轮 prompt)
# ---------------------------------------------------------------------------
def _to_jpeg_bytes(img_bytes: bytes, content_type: str = "") -> bytes:
    """任意格式图片 bytes → JPEG bytes(OCR API 输入)。

    无法用 PIL 解码时(如 wmf/emf 老矢量图)返回原始 bytes,
    由调用方决定跳过。
    """
    fmt = (content_type or "").lower()
    if "jpeg" in fmt or "jpg" in fmt:
        return img_bytes
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(img_bytes))
        img.load()
        rgb = img.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, "JPEG", quality=settings.ocr_jpeg_quality)
        return buf.getvalue()
    except Exception as exc:
        logger.debug("图片转 JPEG 失败(按原始 bytes 尝试): %s", exc)
        return img_bytes


def ocr_docx_image(img_bytes: bytes, content_type: str = "") -> str:
    """D6: 识别 docx 内嵌图片,双轮 prompt。

    第一轮要求输出图中全部文字;若识别文字 < IMAGE_DESCRIBE_THRESHOLD(10字),
    判定为流程图/架构图/装饰图,第二轮要求用 2-3 句话转述图内容
    (流程图必须按节点和箭头关系转述)。

    Returns:
        识别/描述文本;两轮都无产出时返回 ""(调用方标"装饰图片")。

    Raises:
        OcrQuotaError: 账号额度问题(与 PDF 扫描路径同一语义,上层 507/failed)
        OcrParseError: API 调用/响应异常
    """
    jpeg = _to_jpeg_bytes(img_bytes, content_type)

    text = _ocr_image_bytes(jpeg, prompt=IMAGE_TEXT_PROMPT)
    if len(text.strip()) >= IMAGE_DESCRIBE_THRESHOLD:
        return text.strip()

    # 第二轮:转述图内容(流程图/架构图按节点和箭头关系)
    try:
        desc = _ocr_image_bytes(jpeg, prompt=IMAGE_DESCRIBE_PROMPT)
    except OcrQuotaError:
        raise
    except OcrParseError as exc:
        logger.warning("图片第二轮描述失败(保留第一轮结果): %s", exc)
        desc = ""

    if text.strip() and desc.strip():
        return f"{text.strip()} {desc.strip()}"
    return (desc or text).strip()
