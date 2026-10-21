"""11 号需求单测: 表格双形态切片 / 熔断 / 三格式解析 / pptx / 拒收文案。

对齐 docs/11-文件格式扩展需求方案-V1.0.md 验收标准 1/4/5/7/8。
"""

from pathlib import Path

import pytest

from app.services.tabular_parser import (
    MAX_SHEET_ROWS, ROWS_PER_BLOCK, TabularError, _classify,
    parse_csv, parse_xlsx, sheet_to_paragraphs,
)


# ---------------------------------------------------------- sheet_to_paragraphs

def _grid(header, rows):
    return [header] + rows


class TestClassify:
    def test_data_sheet(self):
        rows = _grid(["姓名", "金额"], [["张三", "1200"], ["李四", "1500"]])
        assert _classify(rows) == "data"

    def test_doc_sheet(self):
        long = "本制度适用于公司全体员工，" * 10  # >50 字
        rows = _grid(["条款"], [[long], [long]])
        assert _classify(rows) == "doc"

    def test_empty_sheet(self):
        assert _classify([["", ""], ["  "]]) == "empty"

    def test_mixed_preface(self):
        intro = "以下是 2025 年度全员缴费明细汇总表，数据由社保系统于 2026 年 1 月导出，金额单位为元，如有出入以社保局回执为准。"
        rows = [[intro, ""], [intro, "", ""], ["姓名", "单位", "金额"],
                ["张三", "A", "1200"], ["李四", "B", "1500"]]
        assert _classify(rows) == "mixed"


class TestDataForm:
    def test_header_repeated_in_every_block(self):
        header = ["姓名", "金额"]
        body = [[f"员工{i}", str(i)] for i in range(45)]
        paras = sheet_to_paragraphs("缴费.xlsx", "Sheet1", _grid(header, body))
        assert len(paras) == 3  # 45 行 / 20 → 3 块
        for p in paras:
            assert "表头: 姓名 | 金额" in p   # 每块重复表头
            assert "【缴费.xlsx · Sheet1】" in p

    def test_block_size(self):
        body = [[f"a{i}", f"b{i}"] for i in range(ROWS_PER_BLOCK * 2 + 1)]
        paras = sheet_to_paragraphs("f.xlsx", "s", _grid(["x", "y"], body))
        assert len(paras) == 3


class TestDocForm:
    def test_sentenceified(self):
        long1 = "员工未按规定请假且未获批准的，连续旷工三天及以上，予以劝退处理。"
        long2 = "旷工半天扣发当天工资，旷工一天扣发两天工资。"
        paras = sheet_to_paragraphs("制度.xlsx", "附则", [[ "内容"], [long1], [long2]])
        assert len(paras) >= 1
        joined = "\n".join(paras)
        assert long1 in joined and long2 in joined

    def test_empty_rows_skipped(self):
        paras = sheet_to_paragraphs("f", "s", [["", ""], ["", ""]])
        assert paras == []


class TestMergedCells:
    def test_merged_value_propagated(self):
        # xlsx 合并单元格展开在 _xlsx_rows; 这里构造真实文件测
        openpyxl = pytest.importorskip("openpyxl")
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "T"
        ws["A1"] = "部门"
        ws["B1"] = "姓名"
        ws["A2"] = "技术部"
        ws.merge_cells("A2:A4")
        ws["B2"] = "张三"
        ws["B3"] = "李四"
        ws["B4"] = "王五"
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "merged.xlsx"
            wb.save(p)
            doc = parse_xlsx(p)
        assert doc.content.count("技术部") >= 3  # 左上值填充整个合并区


# ---------------------------------------------------------- 三格式端到端(落文件)

@pytest.fixture()
def tmp_xlsx(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "缴费"
    ws.append(["姓名", "25年单位缴费(元)"])
    for i in range(25):
        ws.append([f"员工{i}", 1000 + i])
    p = tmp_path / "缴费信息.xlsx"
    wb.save(p)
    return p


def test_parse_xlsx_end_to_end(tmp_xlsx):
    doc = parse_xlsx(tmp_xlsx)
    assert doc.metadata["format"] == "xlsx"
    assert "25年单位缴费" in doc.content
    assert "员工24" in doc.content
    blocks = doc.content.split("\n\n")
    assert all("表头:" in b for b in blocks)


def test_parse_xlsx_row_fuse(tmp_path):
    pytest.importorskip("openpyxl")
    # 不真造 5 万行(慢), patch max_row 检查熔断路径
    from app.services import tabular_parser as tp
    class FakeWS:
        title = "big"
        max_row = MAX_SHEET_ROWS + 1
        merged_cells = []
        def iter_rows(self, values_only=True):
            return iter([])
    with pytest.raises(TabularError, match="超过上限"):
        tp._xlsx_rows(FakeWS())


def test_parse_csv_gbk(tmp_path):
    p = tmp_path / "工资.csv"
    p.write_bytes("姓名,金额\n张三,1200\n李四,1500\n".encode("gbk"))
    doc = parse_csv(p)
    assert "张三" in doc.content and "1200" in doc.content
    assert doc.metadata["parse_method"] == "tabular:csv"


def test_parse_csv_bad_encoding(tmp_path):
    p = tmp_path / "坏.csv"
    p.write_bytes(b"\xff\xfe\x00\x01broken")
    with pytest.raises(TabularError, match="编码"):
        parse_csv(p)


def test_empty_xlsx_raises(tmp_path):
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.title = "空"
    p = tmp_path / "empty.xlsx"
    wb.save(p)
    with pytest.raises(TabularError, match="任何文本"):
        parse_xlsx(p)


# ---------------------------------------------------------- pptx

def test_parse_pptx(tmp_path):
    pytest.importorskip("pptx")
    from pptx import Presentation
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "2026 年度安全制度"
    s2 = prs.slides.add_slide(prs.slide_layouts[5])
    s2.shapes.title.text = "旷工处理办法：连续三天劝退"
    p = tmp_path / "制度.pptx"
    prs.save(p)
    from app.services.pptx_parser import parse_pptx
    doc = parse_pptx(p)
    assert "【页 1】" in doc.content and "旷工" in doc.content
    assert doc.metadata["format"] == "pptx"


# ---------------------------------------------------------- 路由 + 拒收文案

def test_router_new_formats():
    from app.services.router import route_file
    for name, expect in [("a.xlsx", "tabular"), ("a.xls", "tabular"),
                         ("a.csv", "tabular"), ("a.pptx", "pptx"),
                         ("a.jpg", "image"), ("a.xlsm", "tabular")]:
        d = route_file(Path("/tmp/" + name))
        assert d.route == expect, name


def test_router_reject_messages():
    from app.services.router import route_file
    d = route_file(Path("/tmp/a.doc"))
    assert d.ok is False and "另存为" in d.reason
    for name in ("a.wps", "a.et", "a.zip", "a.rar", "a.ofd", "a.ppt"):
        d = route_file(Path("/tmp/" + name))
        assert d.ok is False and d.reason, name


def test_direct_pdf_gate_fail_falls_back_to_ocr(monkeypatch, tmp_path):
    """direct_pdf 门禁不过 → 自动降级 OCR 重解析(2026-10-16 兜底)"""
    import app.services.upload_worker as uw

    # 造一个"文字层薄"的解析结果: 124页 x 44字 → 门禁必挂
    thin = uw.ParsedDoc if hasattr(uw, "ParsedDoc") else None
    from app.services.parser import ParsedDoc
    bogus = ParsedDoc(content="页眉" * 22, metadata={"format": "pdf", "pages": 1})

    calls = {"ocr": 0}

    def fake_parse_file(fp, doc_id=None, progress_cb=None):
        return bogus  # 直读产出不足, 触发降级

    def fake_parse_scanned(fp, progress_cb=None):
        calls["ocr"] += 1
        return ParsedDoc(
            content=("这是一页完整的宣传册正文识别结果,包含公司简介、业务范围、"
                     "合作伙伴名单与联系方式等详细描述信息,每页文字量足够通过质量门禁。" * 120),
            metadata={"format": "pdf", "pages": 1, "parse_method": "ocr"},
        )

    monkeypatch.setattr(uw, "parse_file", fake_parse_file, raising=False)
    monkeypatch.setattr(uw, "parse_pdf_scanned", fake_parse_scanned, raising=False)

    # parser 模块里 import 的才是 process_document 实际调用的名字
    import app.services.parser as ps
    monkeypatch.setattr(ps, "parse_file", fake_parse_file)

    f = tmp_path / "brochure.pdf"
    f.write_bytes(b"%PDF-1.4 fake")

    class _Store:
        def update(self, doc_id, **kw): pass
    # 不真入库: chunk 后 index_chunks mock 掉
    monkeypatch.setattr(uw, "index_chunks", lambda *a, **k: __import__("asyncio").Future(), raising=False)

    result = uw.process_document(
        doc_id="testfb", file_path=f, route="direct_pdf",
        file_hash="hashfb", filename="brochure.pdf", store=_Store(),
        on_delete_file=False,
    )
    # OCR 兜底必须被调用过
    assert calls["ocr"] == 1, "门禁失败后应自动降级 OCR 重解析"


def test_gate_single_page_certificate_low_line():
    """单页扫描件(证件)走 60 字低线, 多页文档仍 300 字门禁"""
    from app.services.gate import check_content

    short_text = "姓名赵立娟性别女民族汉出生1990年3月5日住址北京市某某路某某号公民身份号码11010119900305002X签发机关公安局"  # ~60+ 字
    # 多页/普通路由: 依旧 300 门禁
    assert check_content(short_text, pages=None).rule == "short"
    # 单页扫描件低线放行
    g = check_content(short_text, pages=1, allow_short=True)
    assert g.ok, g.reason
    # 低线也不是 0: 20 字照样拒
    assert check_content("身份证扫描件" * 2, pages=1, allow_short=True).rule == "short"
