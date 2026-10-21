"""文档解析模块

支持的格式:
    - PDF  (.pdf)    : pypdf
    - Word (.docx)   : python-docx  (含表格)
    - TXT  (.txt)    : 直接读 utf-8
    - MD   (.md / .markdown) : 直接读 utf-8

输出:
    ParsedDoc — 含 .content (纯文本) 和 .metadata (filename / pages / format 等)

约定:
    - 段落之间使用 "\n\n" 作为分隔
    - PDF 每页文本前面追加 "[页码 N]" 前缀,后续 chunking 可保留页码信息
    - DOCX 的表格以 "[表格 N]" 标记,行内容以 " | " 连接
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from app.core.logger import get_logger

logger = get_logger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".markdown"}


class ParsedDoc:
    """解析结果

    Attributes:
        content: 纯文本内容,段落之间以 "\n\n" 分隔
        metadata: 结构化元信息,至少包含 filename / format
    """

    __slots__ = ("content", "metadata")

    def __init__(self, content: str, metadata: dict):
        self.content = content
        self.metadata = metadata

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ParsedDoc(format={self.metadata.get('format')!r}, "
            f"filename={self.metadata.get('filename')!r}, "
            f"len={len(self.content)})"
        )

    def __str__(self) -> str:  # 便于 print() / 旧代码 str(doc) 使用
        return self.content


# ---------------------------------------------------------------------------
# 公开入口:按扩展名分发
# ---------------------------------------------------------------------------


def parse_file(
    file_path: Union[str, Path],
    *,
    doc_id: str | None = None,
    progress_cb=None,
) -> ParsedDoc:
    """根据文件后缀分发到具体解析器

    Args:
        file_path: 待解析文件路径
        doc_id: D6 新增可选参数,docx 内嵌图片留档目录名
        progress_cb: D4/D6 新增可选参数,OCR 进度回调 (done, total)

    Returns:
        ParsedDoc: 含 content 与 metadata

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: 扩展名不在支持列表中
    """
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    if not path.is_file():
        raise ValueError(f"不是文件: {path}")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"不支持的文件格式: {suffix} "
            f"(支持: {sorted(SUPPORTED_EXTENSIONS)})"
        )

    logger.info("开始解析文件: %s (suffix=%s)", path.name, suffix)

    if suffix == ".pdf":
        result = parse_pdf(path)
    elif suffix == ".docx":
        result = parse_docx(path, doc_id=doc_id, progress_cb=progress_cb)
    else:  # .txt / .md / .markdown
        result = parse_text(path)

    logger.info(
        "解析完成: %s -> format=%s, content_len=%d",
        path.name,
        result.metadata.get("format"),
        len(result.content),
    )
    return result


# 与现有项目骨架保持一致的别名(原骨架签名为 parse(path) -> str,
# 现返回 ParsedDoc,旧代码可直接拿 str(doc) 拿到 content)
def parse(file_path: Union[str, Path]) -> ParsedDoc:
    """parse_file 的别名,保持向后兼容"""
    return parse_file(file_path)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def parse_pdf(file_path: Path) -> ParsedDoc:
    """PDF 解析:逐页提取文本,保留页码标记

    使用 pypdf (>=4.0.0) 的 PdfReader。
    每页文本前追加 "[页码 N]" 前缀,空页会跳过以避免产生空块。
    """
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("缺少依赖 pypdf,请先安装: pip install pypdf>=4.0.0") from e

    reader = PdfReader(str(file_path))
    pages_text: list[str] = []

    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # 单页失败不影响整体
            logger.warning("PDF 第 %d 页解析失败: %s", i + 1, exc)
            text = ""

        if text.strip():
            pages_text.append(f"[页码 {i + 1}]\n{text}")

    return ParsedDoc(
        content="\n\n".join(pages_text),
        metadata={
            "filename": file_path.name,
            "pages": len(reader.pages),
            "format": "pdf",
        },
    )


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

# D6: 内嵌图片扩展名映射(content_type → 留档后缀)
_IMG_EXT_BY_CTYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/bmp": "bmp",
    "image/x-bmp": "bmp",
    "image/tiff": "tiff",
    "image/webp": "webp",
    "image/x-emf": "emf",
    "image/x-wmf": "wmf",
}


class _DocxImageContext:
    """parse_docx 遍历期间的图片收集器。

    遍历只登记 (序号, rId, image_part),OCR 与留档在遍历完成后统一做,
    保证文字流顺序 = 文档 body 原始顺序(公理四:位置即语义)。
    """

    __slots__ = ("slots", "by_rid", "media_dir", "doc_id")

    def __init__(self, media_dir: Path, doc_id: str):
        self.slots: list[dict] = []      # [{idx, rid}] 按出现顺序
        self.by_rid: dict[str, dict] = {}  # rid → {part, idx(首次), ocr_text, saved}
        self.media_dir = media_dir
        self.doc_id = doc_id

    def add(self, rid: str) -> int:
        """登记一次图片引用,返回图片序号(1-based)。同 rId 复用同一序号与识别结果。"""
        if rid in self.by_rid:
            return self.by_rid[rid]["idx"]
        idx = len(self.slots) + 1
        self.slots.append({"idx": idx, "rid": rid})
        self.by_rid[rid] = {"idx": idx, "ocr_text": None, "saved": False}
        return idx


def _extract_para_pieces(para_element, doc, ctx: _DocxImageContext) -> list[str]:
    """按 run 内原始顺序抽取一个段落的 [文字片段/图片占位标记] 列表。"""
    from docx.oxml.ns import qn

    pieces: list[str] = []
    buf: list[str] = []

    def flush_text() -> None:
        if buf:
            t = "".join(buf).strip()
            if t:
                pieces.append(t)
            buf.clear()

    # w:r 含超链接等嵌套结构里的 run,iter 全部覆盖
    for run in para_element.iter(qn("w:r")):
        for node in run.iter():
            tag = node.tag
            if tag == qn("w:t"):
                buf.append(node.text or "")
            elif tag == qn("w:tab"):
                buf.append(" ")
            elif tag == qn("w:br"):
                buf.append("\n")
            elif tag == qn("a:blip"):
                rid = node.get(qn("r:embed")) or node.get(qn("r:link"))
                if rid:
                    try:
                        rel = doc.part.rels[rid]
                    except KeyError:
                        logger.warning("docx 图片关系缺失: rid=%s", rid)
                        continue
                    part = getattr(rel, "target_part", None)
                    if part is None:
                        continue
                    flush_text()
                    idx = ctx.add(rid)
                    entry = ctx.by_rid[rid]
                    if "part" not in entry:
                        entry["part"] = part
                    pieces.append(f"[图片{idx}]")

    flush_text()
    return pieces


def _save_and_ocr_images(ctx: _DocxImageContext, max_images: int,
                         progress_cb=None) -> tuple[int, list[int]]:
    """留档 + OCR 全部已登记图片。

    Returns:
        (ocr_done_count, skipped_idx_list) — skipped 指超出上限未识别的图片序号。

    Raises:
        OcrQuotaError / OcrParseError(与 PDF 扫描路径同语义,由上层处理)
    """
    from app.services.ocr_parser import OCR_LOCK, OcrParseError, ocr_docx_image

    if not ctx.by_rid:
        return 0, []

    ctx.media_dir.mkdir(parents=True, exist_ok=True)
    skipped: list[int] = []
    ocr_done = 0
    need_ocr = [rid for rid, e in ctx.by_rid.items() if e["idx"] <= max_images]

    # 1) 留档(所有图片都存,含超限的)
    for rid, entry in ctx.by_rid.items():
        part = entry.get("part")
        if part is None:
            continue
        try:
            blob = part.blob
            ctype = (getattr(part, "content_type", "") or "").lower()
            ext = _IMG_EXT_BY_CTYPE.get(ctype)
            if not ext:
                ext = Path(str(getattr(part, "partname", ""))).suffix.lstrip(".") or "bin"
            dst = ctx.media_dir / f"img{entry['idx']}.{ext}"
            dst.write_bytes(blob)
            entry["saved"] = True
        except Exception as exc:
            logger.warning("docx 图片留档失败 idx=%s: %s", entry["idx"], exc)

    # 2) OCR(全局串行锁,防并发打 DashScope 限流)
    to_ocr = [(rid, ctx.by_rid[rid]) for rid in need_ocr]
    skipped = [
        e["idx"] for e in ctx.by_rid.values() if e["idx"] > max_images
    ]
    if not to_ocr:
        return 0, sorted(skipped)

    with OCR_LOCK:
        total = len(to_ocr)
        for n, (rid, entry) in enumerate(to_ocr, 1):
            part = entry.get("part")
            if part is None:
                continue
            try:
                text = ocr_docx_image(
                    part.blob, getattr(part, "content_type", "") or ""
                )
                entry["ocr_text"] = text or ""
            except Exception as exc:
                # 额度类错误(OcrQuotaError)必须上抛;单张其他失败只记警告
                from app.services.ocr_parser import OcrQuotaError

                if isinstance(exc, OcrQuotaError):
                    raise
                logger.warning("docx 图片 OCR 失败 idx=%s: %s", entry["idx"], exc)
                entry["ocr_text"] = ""
            ocr_done += 1
            if progress_cb is not None:
                try:
                    progress_cb(n, total)
                except Exception:
                    logger.warning("docx ocr progress_cb 异常(忽略)", exc_info=True)

    return ocr_done, sorted(skipped)


def parse_docx(
    file_path: Path,
    *,
    doc_id: str | None = None,
    progress_cb=None,
) -> ParsedDoc:
    """Word (.docx) 解析 — D6 重写版

    与旧版差异(2026-09-20):
    - **按 body 原始 XML 顺序遍历** paragraph/table 交错,内容与原文顺序一致
      (旧版先全部段落再全部表格,位置信息丢失)
    - **内嵌图片不再丢弃**: 从 run 中检测 a:blip r:embed → doc.part.rels 取
      image part bytes;留档 ``app/data/media/{doc_id}/imgN.{ext}``;
      文字流原位插入 ``[图片N]`` 占位;OCR 识别结果写成
      ``[图片N内容: ...]`` 紧跟占位(公理四:位置即语义)
    - 图片 OCR 张数上限 ``settings.docx_ocr_max_images``,超出只留占位,
      metadata 记 ``skipped_images``
    - 额度错误(OcrQuotaError)上抛,与 PDF 扫描路径同语义

    签名向后兼容: doc_id / progress_cb 均为可选关键字参数。
    """
    try:
        from docx import Document
        from docx.oxml.ns import qn
        from docx.table import Table
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "缺少依赖 python-docx,请先安装: pip install python-docx>=1.1.0"
        ) from e

    from app.config import settings as _settings

    doc = Document(str(file_path))
    _id = doc_id or Path(file_path).name.split("_")[0]
    media_dir = Path(_settings.data_dir) / "media" / _id
    ctx = _DocxImageContext(media_dir, _id)

    parts: list[str] = []          # 输出片段(与 body 顺序一致)
    img_piece_slots: list[int] = []  # parts 中每个 [图片N] 占位的下标
    table_count = 0
    para_count = 0

    body = doc.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            para_count += 1
            pieces = _extract_para_pieces(child, doc, ctx)
            for pc in pieces:
                if pc.startswith("[图片") and pc.endswith("]"):
                    img_piece_slots.append(len(parts))
                parts.append(pc)
        elif child.tag == qn("w:tbl"):
            table_count += 1
            parts.append(f"[表格 {table_count}]")
            table = Table(child, doc)
            for row in table.rows:
                row_cells: list[str] = []
                for cell in row.cells:
                    cell_pieces: list[str] = []
                    for cp in cell._tc.iter(qn("w:p")):
                        cell_pieces.extend(_extract_para_pieces(cp, doc, ctx))
                    row_cells.append(" ".join(cell_pieces).strip())
                row_text = " | ".join(row_cells)
                if row_text.strip(" |"):
                    parts.append(row_text)

    # ---- 图片留档 + OCR(仅当文档确实含内嵌图片) ----
    ocr_done = 0
    skipped: list[int] = []
    if ctx.by_rid:
        ocr_done, skipped = _save_and_ocr_images(
            ctx, _settings.docx_ocr_max_images, progress_cb=progress_cb
        )
        # 把识别结果插到对应占位之后(倒序插入避免下标漂移)
        insertions: list[tuple[int, str]] = []
        for pos in img_piece_slots:
            marker = parts[pos]
            idx = int(marker[3:-1])  # "[图片N]" → N
            entry = next((e for e in ctx.by_rid.values() if e["idx"] == idx), None)
            if entry is None:
                continue
            text = entry.get("ocr_text")
            if text is None:
                if idx in skipped:
                    insertions.append((pos, f"[图片{idx}: 超出识别上限未OCR]"))
                continue
            if text.strip():
                # 压成单行:chunker 的原子保护按"到行尾"匹配,标记内不得含换行
                flat = " ".join(text.split())
                insertions.append((pos, f"[图片{idx}内容: {flat}]"))
            else:
                insertions.append((pos, f"[图片{idx}: 装饰图片]"))
        for pos, seg in sorted(insertions, key=lambda x: -x[0]):
            parts.insert(pos + 1, seg)

    content = "\n\n".join(p for p in parts if p.strip())
    metadata = {
        "filename": file_path.name,
        "paragraphs": para_count,
        "tables": table_count,
        "format": "docx",
        "images_total": len(ctx.by_rid),
        "images_ocr": ocr_done,
        "skipped_images": skipped,
    }
    if ocr_done:
        metadata["parse_method"] = f"native+img_ocr:{_settings.ocr_model}"
    if ctx.by_rid:
        metadata["media_dir"] = str(media_dir)

    return ParsedDoc(content=content, metadata=metadata)


# ---------------------------------------------------------------------------
# TXT / MD
# ---------------------------------------------------------------------------


def parse_text(file_path: Path) -> ParsedDoc:
    """TXT / MD 解析:直接以 utf-8 读取

    对于非 utf-8 文件,会回退到 errors="replace" 以避免崩溃。
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        logger.warning("%s 不是严格 utf-8,使用 replace 模式回退", file_path.name)
        content = file_path.read_text(encoding="utf-8", errors="replace")

    return ParsedDoc(
        content=content,
        metadata={
            "filename": file_path.name,
            "format": file_path.suffix.lstrip(".").lower(),
        },
    )
