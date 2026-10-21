"""文档切分模块（chunker）

策略
----
采用 **recursive chunking**(LangChain 风格):

    段落 → 换行 → 中文句号/感叹号/问号 → 英文句末 → 分号 → 逗号 → 空格 → 字符

按分隔符优先级递归切,保证:

- 优先在语义边界(段落 / 句子)断开,而不是在单词中间
- 单个 chunk 长度 ≤ ``chunk_size``(默认 500 字符)
- 相邻 chunk 之间有 ``overlap`` 字符(默认 50)的尾部重叠,保证上下文连续

输出
----
:class:`Chunk` 数据类,包含 ``text`` 与 ``metadata``:

- ``source``   : 来源文件名
- ``chunk_id`` : 在当前文档中的切片序号(从 0 开始)
- ``char_count``: 文本字符数
- ``page``     : 若来自 PDF 页码标记 ``[页码 N]``,会归属对应页码(否则为空)
- ``format``   : 上游文档格式 (pdf / docx / txt / md)

设计
----
- 既支持 ``chunk_text(text, ...)``(纯文本入口,供老代码与测试使用)
- 也支持 ``chunk_document(parsed: ParsedDoc, ...)``(与 parser.py 对齐的主入口)
- PDF 内容里 parser 写入的 ``[页码 N]`` 标记会被识别并切片,保证 chunk 不会跨页
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Sequence

from app.core.logger import get_logger
from app.services.parser import ParsedDoc

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 分隔符优先级(从大到小): 段落 → 换行 → 中文句号 → 英文句末 → 逗号 → 空格 → 字符
# ---------------------------------------------------------------------------
SEPARATORS: Sequence[str] = (
    "\n\n",   # 段落
    "\n",     # 换行
    "。",     # 中文句号
    "！",     # 中文感叹号
    "？",     # 中文问号
    ". ",     # 英文句号(带空格)
    "! ",     # 英文感叹号(带空格)
    "? ",     # 英文问号(带空格)
    "; ",     # 分号
    ";",      # 分号(无空格)
    "，",     # 中文逗号
    ", ",     # 英文逗号
    ",",      # 英文逗号(无空格)
    " ",      # 空格
    "",       # 字符级兜底
)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class Chunk:
    """切分后的文本片段

    Attributes:
        text: 切分后的文本内容
        metadata: 溯源信息,至少包含 source / chunk_id / char_count
    """

    text: str
    metadata: dict = field(default_factory=dict)

    def __len__(self) -> int:  # 便于 len(chunk) 统计
        return len(self.text)

    def __repr__(self) -> str:  # pragma: no cover
        src = self.metadata.get("source", "?")
        cid = self.metadata.get("chunk_id", "?")
        return f"Chunk(source={src!r}, chunk_id={cid}, len={len(self.text)})"


# ---------------------------------------------------------------------------
# 核心:递归切分
# ---------------------------------------------------------------------------
def _split_by_separator(text: str, separator: str) -> List[str]:
    """按分隔符切分,空段自动过滤,空字符串切分回退为字符列表"""
    if separator == "":
        return list(text)
    return text.split(separator)


def recursive_split(
    text: str,
    chunk_size: int = 500,
    overlap: int = 50,
    separators: Sequence[str] | None = None,
) -> List[str]:
    """按段落 → 句子 → 字符递归切分

    Args:
        text: 待切分文本
        chunk_size: 单个 chunk 的目标上限(字符数)
        overlap: 相邻 chunk 之间的尾部重叠字符数
        separators: 自定义分隔符优先级;默认使用中文/英文混合优先级

    Returns:
        切分后的字符串列表,已过滤纯空白 chunk
    """
    if text is None:
        return []
    if chunk_size <= 0:
        raise ValueError(f"chunk_size 必须 > 0,实际 {chunk_size}")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError(
            f"overlap 必须满足 0 <= overlap < chunk_size,实际 overlap={overlap}, chunk_size={chunk_size}"
        )

    text = text.strip()
    if not text:
        return []

    seps: Sequence[str] = separators if separators is not None else SEPARATORS

    chunks: List[str] = _recursive_split_impl(text, chunk_size, seps)

    # 加 overlap:把上一个 chunk 的末尾 overlap 字符拼到下一个 chunk 开头
    if overlap > 0 and len(chunks) > 1:
        overlapped: List[str] = [chunks[0]]
        for i in range(1, len(chunks)):
            prev = chunks[i - 1]
            tail = prev[-overlap:] if len(prev) > overlap else prev
            overlapped.append(tail + chunks[i])
        chunks = overlapped

    # 最终过滤空白
    return [c for c in chunks if c and c.strip()]


def _recursive_split_impl(
    text: str,
    chunk_size: int,
    separators: Sequence[str],
) -> List[str]:
    """实际递归逻辑:尽量在最大语义边界处切,直到所有段 <= chunk_size"""
    if len(text) <= chunk_size:
        return [text]

    # 选第一个能命中的分隔符
    chosen_sep: str | None = None
    for sep in separators:
        if sep == "":
            # 字符级兜底:一定"命中",但只在其他分隔符都失败时才用
            chosen_sep = sep
            continue
        if sep in text:
            chosen_sep = sep
            break

    if chosen_sep is None or chosen_sep == "":
        # 没有任何语义边界可用 → 硬切字符
        return _hard_split(text, chunk_size)

    pieces = _split_by_separator(text, chosen_sep)

    chunks: List[str] = []
    buffer = ""

    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue

        # 试探拼接(把分隔符加回去,字符级除外)
        candidate = (buffer + chosen_sep + piece) if buffer and chosen_sep else piece
        # 当 buffer 为空时,candidate 就是 piece 本身(无前缀分隔符)

        if len(candidate) <= chunk_size:
            buffer = candidate
            continue

        # candidate 超长 → 先 flush 旧 buffer
        if buffer:
            chunks.append(buffer)
            buffer = ""

        if len(piece) > chunk_size:
            # 单个 piece 仍然超长 → 递归(用更细的分隔符)
            sub = _recursive_split_impl(piece, chunk_size, separators)
            chunks.extend(sub)
            buffer = ""
        else:
            buffer = piece

    if buffer:
        chunks.append(buffer)

    return chunks


def _hard_split(text: str, chunk_size: int) -> List[str]:
    """兜底:按 chunk_size 硬切字符串"""
    return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]


# ---------------------------------------------------------------------------
# 简单 semantic chunking(基于段落相似度的合并)
# ---------------------------------------------------------------------------
def semantic_chunk(
    paragraphs: Iterable[str],
    chunk_size: int = 500,
    overlap: int = 50,
) -> List[str]:
    """基于段落的简单 semantic chunking

    把相邻段落拼接到接近 chunk_size 上限,超过则开新 chunk,
    保留 overlap 字符尾部重叠。

    与 recursive_split 的区别:
        - 以段落为最小语义单元,不会再向下钻到句子级
        - 更适合结构化文档(标题/段落分明)

    Args:
        paragraphs: 段落迭代器
        chunk_size: 单个 chunk 的目标上限
        overlap: 相邻 chunk 之间的尾部重叠字符数

    Returns:
        切分后的字符串列表
    """
    paras = [p.strip() for p in paragraphs if p and p.strip()]
    if not paras:
        return []

    chunks: List[str] = []
    buffer = ""

    for p in paras:
        candidate = (buffer + "\n\n" + p) if buffer else p
        if len(candidate) <= chunk_size:
            buffer = candidate
            continue

        if buffer:
            chunks.append(buffer)
            # overlap:把 buffer 末尾 overlap 字符作为下一段开头
            tail = buffer[-overlap:] if overlap > 0 and len(buffer) > overlap else ""
            buffer = (tail + "\n\n" + p) if tail else p
        else:
            buffer = p

    if buffer:
        chunks.append(buffer)

    return [c for c in chunks if c and c.strip()]


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
# PDF 页码标记正则:[页码 N]
_PAGE_MARKER_RE = re.compile(r"\[页码 (\d+)\]")

# D6: docx 图片占位标记 [图片N] / [图片N内容: ...] / [图片N: 装饰图片]
# parser 已保证标记内容单行无换行,故按"到行尾"匹配即为完整原子单元
_IMG_MARKER_RE = re.compile(r"\[图片\d+[^\n]*")
# [图片N内容:...] 必须与其前面最近 N 字上下文同 chunk(公理四:位置即语义)
_IMG_CONTEXT_CHARS = 200


def _split_around_images(
    text: str, ctx_chars: int = _IMG_CONTEXT_CHARS
) -> List[tuple[str, str]]:
    """D6: 把含图片标记的文本拆成 [(kind, seg)],kind ∈ {"text","atomic"}。

    - atomic 段 = 标记本身 + 其前面最近 ctx_chars 字上下文,**绝不再切分**
    - 普通 text 段交给 recursive_split 正常切
    - 落在已消费区域内的后续标记跳过(两标记相距 < ctx_chars 时合并进同一 atomic)
    """
    matches = list(_IMG_MARKER_RE.finditer(text))
    if not matches:
        return [("text", text)]

    segments: List[tuple[str, str]] = []
    cursor = 0
    for m in matches:
        if m.start() < cursor:
            continue  # 已包含在上一个 atomic 的上下文里
        ctx_start = max(cursor, m.start() - ctx_chars)
        end = m.end()
        # 吸收紧随其后的相邻标记(如 [图片N] 与 [图片N内容:...] 只隔换行),
        # 保证占位与识别内容同属一个原子块
        j = matches.index(m) + 1
        while j < len(matches) and matches[j].start() - end <= ctx_chars:
            end = matches[j].end()
            j += 1
        if ctx_start > cursor:
            segments.append(("text", text[cursor:ctx_start]))
        segments.append(("atomic", text[ctx_start:end]))
        cursor = end
    if cursor < len(text):
        segments.append(("text", text[cursor:]))
    return segments


def _split_section_protected(
    section_text: str, chunk_size: int, overlap: int
) -> List[str]:
    """对一个 section(页)做带图片保护的切分。

    无 [图片 标记时行为与 recursive_split 完全一致(零回归);
    有标记时:标记+前文200字为原子单元独占 chunk(可超出 chunk_size),
    其余文本正常递归切分。原子单元处不加 overlap(规格允许放宽)。
    """
    if "[图片" not in section_text:
        return recursive_split(section_text, chunk_size=chunk_size, overlap=overlap)

    out: List[str] = []
    for kind, seg in _split_around_images(section_text):
        if kind == "atomic":
            seg = seg.strip()
            if seg:
                out.append(seg)
        else:
            out.extend(
                recursive_split(seg, chunk_size=chunk_size, overlap=overlap)
            )
    return [c for c in out if c and c.strip()]


def chunk_document(
    parsed: ParsedDoc,
    chunk_size: int = 500,
    overlap: int = 50,
) -> List[Chunk]:
    """对一份已解析文档做 chunking(主入口)

    Args:
        parsed: ``parser.py`` 输出的 ``ParsedDoc``
        chunk_size: 单个 chunk 目标上限
        overlap: 相邻 chunk 之间的尾部重叠

    Returns:
        :class:`Chunk` 列表,带 source / chunk_id / char_count / page / format
    """
    if parsed is None or not parsed.content or not parsed.content.strip():
        logger.warning("chunk_document 收到空文档,返回空列表")
        return []

    filename = parsed.metadata.get("filename", "unknown")
    fmt = parsed.metadata.get("format", "unknown")

    chunks: List[Chunk] = []
    base_meta = {
        "source": filename,
        "format": fmt,
    }

    # 优先识别 PDF 页码标记 → 按页切片,保证 chunk 不跨页
    sections: List[tuple[str, str]] = _split_by_page(parsed.content)

    for page_label, section_text in sections:
        if not section_text.strip():
            continue
        sub_texts = _split_section_protected(section_text, chunk_size, overlap)
        for text in sub_texts:
            chunks.append(
                Chunk(
                    text=text,
                    metadata={
                        **base_meta,
                        "chunk_id": len(chunks),
                        "char_count": len(text),
                        "page": page_label,
                    },
                )
            )

    logger.info(
        "chunk_document 完成: source=%s, format=%s, total_chunks=%d",
        filename,
        fmt,
        len(chunks),
    )
    return chunks


def _split_by_page(content: str) -> List[tuple[str, str]]:
    """按 [页码 N] 标记切分 PDF 内容

    Returns:
        [(page_label, text), ...],page_label 是 "1"/"2"/...;无标记时返回 [("", content)]
    """
    if not _PAGE_MARKER_RE.search(content):
        return [("", content)]

    # 在每个 [页码 N] 之前切片
    parts = _PAGE_MARKER_RE.split(content)
    # re.split 返回 [before, num1, body1, num2, body2, ...]
    result: List[tuple[str, str]] = []
    # 标记前的内容(通常是空,但 PDF 第一页前可能有空白)
    if parts and parts[0].strip():
        result.append(("", parts[0]))

    i = 1
    while i + 1 < len(parts):
        page_num = parts[i]
        body = parts[i + 1]
        # 去掉 body 开头可能残留的换行
        body = body.lstrip("\n")
        result.append((page_num, body))
        i += 2

    return result


# ---------------------------------------------------------------------------
# 兼容旧签名的纯文本入口(供其他模块直接调用)
# ---------------------------------------------------------------------------
def chunk_text(
    text: str,
    chunk_size: int | None = None,
    overlap: int | None = None,
) -> List[str]:
    """纯文本切分入口

    为了与项目骨架已有签名保持兼容,默认从 :mod:`app.config` 读取配置,
    也允许调用方显式传入 ``chunk_size`` / ``overlap`` 覆盖。

    Args:
        text: 待切分文本
        chunk_size: 覆盖默认 chunk_size(可选)
        overlap: 覆盖默认 overlap(可选)

    Returns:
        字符串列表,不含元数据
    """
    # 局部 import 避免循环依赖
    from app.config import settings

    cs = chunk_size if chunk_size is not None else settings.chunk_size
    ov = overlap if overlap is not None else settings.chunk_overlap
    return recursive_split(text, chunk_size=cs, overlap=ov)


def chunk_text_with_meta(
    text: str,
    doc_id: str,
    filename: str,
    chunk_size: int = 500,
    overlap: int = 50,
) -> List[dict]:
    """切分纯文本并附加元数据

    与旧骨架 ``chunk_document(text, doc_id, filename)`` 签名兼容,但额外接受
    ``chunk_size`` / ``overlap`` 参数。

    Returns:
        ``[{"chunk_id", "doc_id", "filename", "content", "index", "char_count"}]``
    """
    pieces = recursive_split(text, chunk_size=chunk_size, overlap=overlap)
    out: List[dict] = []
    for i, p in enumerate(pieces):
        out.append(
            {
                "chunk_id": f"{doc_id}::{i}",
                "doc_id": doc_id,
                "filename": filename,
                "content": p,
                "index": i,
                "char_count": len(p),
            }
        )
    return out


# ---------------------------------------------------------------------------
# 简易自测
# ---------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    demo = (
        "RAG(Retrieval-Augmented Generation)是一种结合检索与生成的技术。"
        "它先从知识库中检索与问题相关的片段,再交给大模型生成答案。\n\n"
        "与传统微调相比,RAG 的优势在于:知识可实时更新、训练成本低、可解释性强。"
        "但 RAG 也面临 chunking 粒度、检索召回率、重排序等挑战。\n\n"
        "本文将系统介绍 RAG 的关键模块:文档解析、文本切分、向量化和检索。"
        "在文本切分部分,我们会详细讨论递归切分与语义切分的取舍。"
    )
    print("=== recursive_split demo ===")
    rs = recursive_split(demo, chunk_size=120, overlap=20)
    for i, c in enumerate(rs):
        print(f"[{i}] len={len(c)} | {c!r}")

    print("\n=== semantic_chunk demo ===")
    sc = semantic_chunk(demo.split("\n\n"), chunk_size=120, overlap=20)
    for i, c in enumerate(sc):
        print(f"[{i}] len={len(c)} | {c!r}")

    print("\n=== chunk_document demo (with PDF page markers) ===")
    pdf_like = (
        "[页码 1]\n第一页内容:介绍 RAG 的基本概念。\n\n"
        "[页码 2]\n第二页内容:chunking 策略对比。\n\n"
        "[页码 3]\n第三页内容:向量检索与重排序。"
    )
    parsed = ParsedDoc(content=pdf_like, metadata={"filename": "demo.pdf", "format": "pdf"})
    chunks = chunk_document(parsed, chunk_size=80, overlap=10)
    for c in chunks:
        print(c, "->", c.text[:40], "...")
