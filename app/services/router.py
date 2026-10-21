"""格式路由模块（L1 · Format Router）

接入层改造 4 层框架的第 1 层：任何文件进来先判断"这是什么"，
再决定走哪条解析路径。判定逻辑前置且成本趋零（抽样试提取，
不是全量解析），避免解析完才发现是扫描版。

路由结果 RouteDecision.route:
    - "direct_pdf"  : 原生文字 PDF，pypdf 直读（零成本）
    - "scanned_pdf" : 扫描版 PDF（抽样文字层为 0），走 OCR 路径
    - "docx"        : Word 文档，python-docx 直读
    - "text"        : txt / md，直接读
    - "tabular"     : csv / xlsx，结构化解析（保留行列）
    - "image"       : jpg/png 图片，走 OCR 路径
    - "unsupported" : 不支持的格式

配套能力探测（不抛异常，只报告）：
    ok           — 该路由是否具备可执行的解析器
    reason       — 不可执行时的原因（如 OCR 未开通、缺依赖）
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from app.core.logger import get_logger

logger = get_logger(__name__)

# 扩展名粗分表
PDF_EXTS = {".pdf"}
DOCX_EXTS = {".docx"}
TEXT_EXTS = {".txt", ".md", ".markdown"}
TABULAR_EXTS = {".csv", ".xlsx", ".xlsm", ".xls"}
PPTX_EXTS = {".pptx"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff"}
# 一期明确拒收(11 号 FR-02): 文案直接给"另存为"指路
LEGACY_REJECT = {
    ".doc": "老版 Word 格式暂不支持，请用 WPS 另存为 .docx 后上传",
    ".wps": "WPS 私有格式请另存为 .docx 后上传",
    ".et": "WPS 私有格式请另存为 .xlsx 后上传",
    ".ofd": "OFD 版式文件暂不支持，请导出 PDF 后上传",
    ".zip": "压缩包不支持，请解包后逐个上传（每个文件需单独设定密级）",
    ".rar": "压缩包不支持，请解包后逐个上传（每个文件需单独设定密级）",
    ".ppt": "老版 PPT 格式请另存为 .pptx 后上传",
}

# 文字层抽样参数：头/中/尾各抽 N 页，任何一页有字即判原生
_SAMPLE_PAGES_PER_ZONE = 2


class RouteDecision(NamedTuple):
    route: str          # 见模块 docstring
    ok: bool            # 是否存在可用的下游解析器
    reason: str         # ok=False 时的原因说明
    detail: str = ""    # 附加信息（如抽样命中字数）


def route_file(file_path: Path) -> RouteDecision:
    """判定文件应走的解析路径（低成本，只抽样不解析）。

    Args:
        file_path: 已落地的文件路径

    Returns:
        RouteDecision
    """
    suffix = Path(file_path).suffix.lower()

    if suffix in DOCX_EXTS:
        return RouteDecision("docx", True, "")
    if suffix in TEXT_EXTS:
        return RouteDecision("text", True, "")
    if suffix in TABULAR_EXTS:
        return RouteDecision("tabular", True, "")
    if suffix in PPTX_EXTS:
        return RouteDecision("pptx", True, "")
    if suffix in LEGACY_REJECT:
        return RouteDecision("unsupported", False, LEGACY_REJECT[suffix])
    if suffix in IMAGE_EXTS:
        return _ocr_probe(RouteDecision("image", True, ""))
    if suffix in PDF_EXTS:
        return _route_pdf(file_path)
    return RouteDecision("unsupported", False, f"不支持的文件格式: {suffix}")


def _route_pdf(file_path: Path) -> RouteDecision:
    """PDF 分流：抽样头/中/尾共 ~6 页试提取文字。

    有任何一页产出文字 → direct_pdf（原生电子版）
    全部为 0 字        → scanned_pdf（扫描版，走 OCR）
    pypdf 本身打不开   → 按扫描版处理（损坏/加密也在此暴露）
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return RouteDecision("direct_pdf", False, "缺少依赖 pypdf")

    try:
        reader = PdfReader(str(file_path))
        total = len(reader.pages)
        if total == 0:
            return RouteDecision("scanned_pdf", True, "", "pages=0")

        zones = [0, total // 2, max(total - 1, 0)]
        probe_idx: list[int] = []
        for z in zones:
            for off in range(_SAMPLE_PAGES_PER_ZONE):
                idx = min(z + off, total - 1)
                if idx not in probe_idx:
                    probe_idx.append(idx)

        chars = 0
        for idx in probe_idx:
            try:
                text = reader.pages[idx].extract_text() or ""
            except Exception as exc:  # 加密页 / 损坏页
                logger.warning("路由抽样第 %d 页提取失败: %s", idx + 1, exc)
                text = ""
            chars += len(text.strip())

        # 2026-10-16 修正: 图文混排 PDF(宣传册/画册)只有页眉页脚有文字层,
        # 正文全在图片里。旧规则"有一页有字即原生"会把这类误判 direct_pdf,
        # 直读产出每页几十字被质量门禁拦下(大表格扫描件事故)。
        # 新规则: 抽样页平均字数 < MIN_CHARS_PER_PAGE(80) → 按扫描版走 OCR。
        from app.services.gate import MIN_CHARS_PER_PAGE
        avg_chars = chars / max(len(probe_idx), 1)
        if chars > 0 and avg_chars >= MIN_CHARS_PER_PAGE:
            return RouteDecision("direct_pdf", True, "",
                                 f"sampled_chars={chars} avg={avg_chars:.0f}/page")
        return _ocr_probe(
            RouteDecision("scanned_pdf", True, "",
                          f"pages={total} sampled_chars={chars} "
                          f"avg={avg_chars:.0f}/page < {MIN_CHARS_PER_PAGE}")
        )
    except Exception as exc:
        logger.warning("PDF 路由打开失败(按扫描处理): %s err=%s", file_path.name, exc)
        return _ocr_probe(RouteDecision("scanned_pdf", True, "", f"open_error={exc}"))


def _ocr_probe(decision: RouteDecision) -> RouteDecision:
    """OCR 路径能力探测：模型已配置且依赖就绪才 ok。

    注意：这里只做静态检查（settings 配置 + import），不发真实 API 请求，
    账号额度类错误留给 L2 解析时如实上报。
    """
    from app.config import settings

    key = settings.dashscope_api_key or ""
    if not key or key.startswith("$"):
        return decision._replace(
            ok=False, reason="未配置 DASHSCOPE_API_KEY，OCR 路径不可用"
        )
    try:
        import pypdfium2  # noqa: F401
    except ImportError:
        return decision._replace(ok=False, reason="缺少依赖 pypdfium2（渲染扫描页）")
    return decision
