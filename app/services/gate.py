"""质量门禁模块（L4 · Quality Gate）

入库前的最后一道闸：垃圾进 = 垃圾出，不合格的资料不进知识库。

三条硬规则：
    1. 最小文字量  — 解析产出 < MIN_CHARS（或平均每页 < MIN_CHARS_PER_PAGE）
                     → 拦截（典型：扫描版被旧代码直读成空文本、
                       352 字测试摘要 docx）
    2. 哈希去重    — 同一文件重复上传 → 拒绝（uploads 里现存 2 份重复教材）
    3. 乱码率      — 替换符/控制字符占比 > MAX_GARBLED_RATIO → 拦截
                     （编码错误的文档 chunk 化后全是噪声）

返回 GateResult(ok, rule, reason)：ok=False 时上层拒绝入库并如实报告原因，
不再出现"上传成功但 0 有效内容"的静默假成功。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import NamedTuple

from app.core.logger import get_logger

logger = get_logger(__name__)

# 整文档最低字数（低于此值几乎必然没有检索价值）
MIN_CHARS = 300
# 单页扫描件(证件/证书照片等)最低字数: 天然文字就少, 300 会误杀(2026-10-16 身份证事故)
MIN_CHARS_SINGLE_PAGE = 60
# 平均每页最低字数（PDF 多页文档用，防止"每页只认出一行"的劣质 OCR 混入）
MIN_CHARS_PER_PAGE = 80
# 乱码占比阈值（U+FFFD 替换符 + 不可见控制字符）
MAX_GARBLED_RATIO = 0.05


class GateResult(NamedTuple):
    ok: bool
    rule: str      # 命中的规则名（passed/empty/short/garbled/duplicate）
    reason: str


def sha256_of_file(file_path: Path) -> str:
    """流式哈希，417MB 大文件也不占内存。"""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def check_duplicate(file_hash: str, known_hashes: dict[str, str]) -> GateResult:
    """known_hashes: {sha256: filename}。命中即判重复。"""
    dup_of = known_hashes.get(file_hash)
    if dup_of:
        return GateResult(False, "duplicate", f"与已入库文档内容完全相同: {dup_of}")
    return GateResult(True, "passed", "")


def check_content(parsed_text: str, pages: int | None = None,
                  allow_short: bool = False) -> GateResult:
    """对解析产出的全文做三道检查。

    allow_short=True: 单页扫描件(证件/证书照片)专用低线 60 字。
    """
    text = (parsed_text or "").strip()

    if not text:
        return GateResult(False, "empty", "解析后零文字产出（疑似扫描版/空文档）")

    min_chars = MIN_CHARS_SINGLE_PAGE if allow_short else MIN_CHARS
    if len(text) < min_chars:
        return GateResult(
            False, "short", f"文字量 {len(text)} 字 < 门禁 {min_chars} 字，无检索价值"
        )

    if pages and pages > 1:
        per_page = len(text) / pages
        if per_page < MIN_CHARS_PER_PAGE:
            return GateResult(
                False,
                "short",
                f"平均每页 {per_page:.0f} 字 < 门禁 {MIN_CHARS_PER_PAGE}（解析质量差）",
            )

    garbled = text.count("\ufffd") + sum(
        1 for ch in text if ord(ch) < 9 or 11 <= ord(ch) <= 12 or 14 <= ord(ch) <= 31
    )
    ratio = garbled / max(len(text), 1)
    if ratio > MAX_GARBLED_RATIO:
        return GateResult(
            False, "garbled", f"乱码占比 {ratio:.1%} > {MAX_GARBLED_RATIO:.0%}"
        )

    return GateResult(True, "passed", "")
