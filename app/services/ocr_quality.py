"""OCR 低置信启发式判定 (#5 扫描件校对队列)

背景: qwen-vl-ocr 不返回置信度分数, 只能靠文本信号近似判断"这份识别稿
大概率有问题"。设计口径: 宁可人工过目, 但不误杀 ——
只有明确可疑才进校对队列, 无信号照常自动入库。

三个信号 (命中任意一个 → 待人工校对):
  sparse   : 多页扫描件平均每页字数 < 120 (正常教材/制度页均 300+ 字;
             单页证件/证书天然字少, 不适用此信号)
  noise    : 噪声字符占比 > 6% (OCR 误识别产生的符号串:
             非中文/非字母数字/非常用标点的字符, 含连续 3 个以上同字符)
  broken   : 存在连续 >=6 个相同非空白字符, 或 '□'/'?' 密集 (每 500 字 >=8 个)

阈值参考 2026-10-17 库里真实样本定标 (教材 pdf_scanned 页均 ~400 字,
身份证 image_ocr 136 字单页不触发 sparse)。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

# ---- 阈值 ----
SPARSE_MIN_CHARS_PER_PAGE = 120   # 多页扫描件页均字数下限
NOISE_RATIO_MAX = 0.06             # 噪声字符占比上限
RUN_CHAR_MIN = 6                   # 连续相同字符达到此数判为识别乱码
MARK_CHARS = "□◻▯"                 # 疑似占位符(缺字形)字符集
MARK_DENSITY_PER_500 = 8           # 每 500 字允许的可疑字符数

# 噪声 = Unicode 类别为 S*(符号) 或 Cc(控制字符), 且排除常见排版符号。
# 2026-10-17 定标教训: 库里真实资料是 markdown 转的 PDF, 合法内容里就是会有
# 成对的 < > 标签、` 代码标记、= 分隔线、| 表格线、\n —— 按 Unicode 类别一刀切
# 必然误杀。宁可漏卡(还有 sparse/run/mark 三个信号兜底), 不可误杀(全员扫描件
# 都卡队列 = 自动化报废)。
_ALLOWED_SYMBOLS = set("<>`=|/\\-+*_#~.:;!?\"'()[]{}$%&@^¥€£ 　\t\r\n")


def _is_noise(ch: str) -> bool:
    if ch in _ALLOWED_SYMBOLS:
        return False
    cat = unicodedata.category(ch)
    return cat in ("So", "Sm", "Sk", "Sc", "Cc")


def _noise_ratio(text: str) -> float:
    if not text:
        return 0.0
    bad = sum(1 for ch in text if _is_noise(ch))
    return bad / len(text)


# broken run
# 2026-10-19 定标教训: 合同扫描件里连续 -/_/. 是合法排版 (填空线"______ 元"、
# 目录点线"……1"、markdown 表格分隔"|------|"), 首灌 356 份合同 42 份全被误卡。
# 这三个字符的 run 不再触发; 真正的 OCR 乱码 run (□?█ 等) 仍触发。
_RUN_EXCLUDE = set("-_.")
_RUN_RE = re.compile(r"([^\s])\1{5,}")


def _find_suspicious_runs(text: str) -> list[str]:
    return [m.group(1) for m in _RUN_RE.finditer(text) if m.group(1) not in _RUN_EXCLUDE]


def _mark_density(text: str) -> float:
    if not text:
        return 0.0
    marks = sum(text.count(ch) for ch in MARK_CHARS)
    return marks / max(len(text), 1) * 500


def low_confidence_signals(
    content: str,
    pages: int | None,
    route: str,
) -> list[dict[str, Any]]:
    """返回命中的信号列表(空 = 质量正常, 可直接入库)。

    只用于 OCR 类路线; 原生文本路线不该调用(调用也只会因 route 白名单直接返回空)。
    """
    if route not in ("scanned_pdf", "image", "pdf_ocr_fallback"):
        return []
    text = (content or "").strip()
    if not text:
        # 空内容由质量门禁处理, 这里不重复判
        return []

    signals: list[dict[str, Any]] = []
    n_pages = int(pages or 1)

    # sparse: 仅多页(>=2)才看页均字数
    if n_pages >= 2:
        per_page = len(text) / n_pages
        if per_page < SPARSE_MIN_CHARS_PER_PAGE:
            signals.append({
                "rule": "sparse",
                "msg": f"平均每页仅 {per_page:.0f} 字(<{SPARSE_MIN_CHARS_PER_PAGE}), 疑似漏识别",
                "value": round(per_page, 1),
            })

    # noise
    nr = _noise_ratio(text)
    if nr > NOISE_RATIO_MAX:
        signals.append({
            "rule": "noise",
            "msg": f"异常字符占比 {nr:.1%}(> {NOISE_RATIO_MAX:.0%}), 疑似识别乱码",
            "value": round(nr, 4),
        })

    # broken run
    runs = _find_suspicious_runs(text)
    if runs:
        signals.append({
            "rule": "broken_run",
            "msg": f"存在 {len(runs)} 处连续重复字符(如 '{runs[0]*6}'), 疑似识别错误",
            "value": len(runs),
        })

    # mark density (只对较长文本判, 短文本单页证件不判)
    if len(text) >= 300:
        md = _mark_density(text)
        if md > MARK_DENSITY_PER_500:
            signals.append({
                "rule": "mark_density",
                "msg": f"占位/乱码符号密度 {md:.1f}/500字(> {MARK_DENSITY_PER_500}), 疑似识别不清",
                "value": round(md, 1),
            })

    return signals
