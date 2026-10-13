# -*- coding: utf-8 -*-
"""v2.1.9 零行为工程池 W1 测试（开源 review R 系 P2 修复，handlers 层七项）。

全部为留痕/注释/文案/导入统一类改动的纯可测面：
- 契约/docstring 文本断言（__init__ record 四参、analysis_cache 敏感面）；
- pdf_handler 无绝对包名动态导入 rules_engine 残留（源码文本断言）；
- md_handler UTF-8 读取留痕文案（文本级 + 真跑 process 的行为级）；
- pptx/xls/docx 失败链留痕（FakeReport + 桩模块/桩对象构造异常路径，
  不跑 GUI/OCR，不依赖真实 python-pptx / pywin32 / python-docx 安装）。

控制流零变化约束的对偶验证：零命中 md 逐字节不变等既有契约不回归
（相关既有套件另行回归，见完成标准）。
"""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.handlers import (  # noqa: E402
    HANDLER_REGISTRY,
    __doc__ as handlers_doc,
    docx_handler,
    md_handler,
    pptx_handler,
    xls_handler,
)
import app.analysis_cache as analysis_cache  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]
_PDF_HANDLER_SRC = (_REPO / "app" / "handlers" / "pdf_handler.py").read_text(
    encoding="utf-8")
_MD_HANDLER_SRC = (_REPO / "app" / "handlers" / "md_handler.py").read_text(
    encoding="utf-8")


class _FakeReport:
    """与 tests/test_md_handler.py 同形的最小回调桩（record 四参契约）。"""

    def __init__(self):
        self.records = []
        self.logs = []

    def record(self, original, replacement, rule, location):
        self.records.append((original, replacement, rule, location))

    def log(self, msg):
        self.logs.append(msg)

    def progress(self, *a):
        pass


class _NullEngine:
    """零命中引擎桩：find_spans 恒空（不触发任何替换/记录）。"""

    def find_spans(self, text):
        return []


# ---------------------------------------------------------------------------
# R2-P2-1：handlers/__init__.py 契约 docstring（record 四参 + records 双写）
# ---------------------------------------------------------------------------

def test_handlers_docstring_declares_record_four_params():
    """处理器统一签名 docstring 必须写明 record 四参接口（可被调用方检索）。"""
    assert "record(original, replacement, rule, location)" in handlers_doc
    # records 语义：供映射表 / 遮蔽台账双写消费
    assert "records" in handlers_doc
    assert "映射表" in handlers_doc and "台账" in handlers_doc


def test_handler_registry_unchanged():
    """docstring 改动不动注册表（六扩展名分流照旧）。"""
    assert set(HANDLER_REGISTRY) == {".docx", ".md", ".xlsx", ".xls",
                                     ".pptx", ".pdf"}


# ---------------------------------------------------------------------------
# R2-P2-2：analysis_cache 敏感面声明
# ---------------------------------------------------------------------------

def test_analysis_cache_docstring_declares_sensitive_surface():
    doc = analysis_cache.__doc__
    # 缓存含 OCR 识别全文（即文档原文）
    assert "OCR 识别全文" in doc
    # 敏感级与映射表/plan 同级，勿外发
    assert "敏感级" in doc
    assert "映射表" in doc and "plan" in doc
    assert "勿随项目外发" in doc
    # UI 清除分析缓存联动清除
    assert "清除分析缓存" in doc


# ---------------------------------------------------------------------------
# R2-P2-4：pdf_handler __import__ 统一（源码文本断言，不 import 重模块）
# ---------------------------------------------------------------------------

def test_pdf_handler_has_no_dynamic_rules_engine_import():
    """绝对包名 __import__ 动态导入必须清零（打包器重定位隐患）。"""
    assert '__import__("app.rules_engine"' not in _PDF_HANDLER_SRC
    # 统一改用的模块顶引用仍在（from .. import rules_engine as ocr_mod_rules）
    assert "from .. import rules_engine as ocr_mod_rules" in _PDF_HANDLER_SRC
    # 三处原调用点均已改用 ocr_mod_rules 属性访问
    assert _PDF_HANDLER_SRC.count("ocr_mod_rules._KEYWORD_BEFORE") == 2
    assert _PDF_HANDLER_SRC.count("ocr_mod_rules.parse_amount") == 1


# ---------------------------------------------------------------------------
# R2-P1-1 / R2-P2-5：md 编码链留痕（文本级 + 行为级）
# ---------------------------------------------------------------------------

def test_md_handler_source_contains_utf8_trace_copy():
    """UTF-8 成功路径留痕文案落源（R2-P1-1，文本级断言）。"""
    assert "已按 UTF-8 读取" in _MD_HANDLER_SRC
    assert "GBK 文档在窄字符区间可能被误判为 UTF-8" in _MD_HANDLER_SRC


def test_md_utf8_success_logs_one_trace_line(tmp_path):
    """行为级：UTF-8 严格解码成功 → 恰一行留痕；零命中逐字节不变不回归。"""
    src = tmp_path / "说明.md"
    src.write_bytes("公开说明：本文件不含敏感信息。\r\n".encode("utf-8"))
    dst = tmp_path / "说明_脱敏后.md"
    rep = _FakeReport()
    stats = md_handler.process(src, dst, _NullEngine(), None, rep)
    assert stats["masked"] == 0
    hits = [m for m in rep.logs if "已按 UTF-8 读取" in m]
    assert len(hits) == 1, f"应恰一行留痕：{rep.logs}"
    # 零行为约束：无 BOM utf-8 读 → utf-8 写（newline=""）逐字节不变
    assert dst.read_bytes() == src.read_bytes()


def test_md_gb18030_fallback_mentions_utf16(tmp_path):
    """R2-P2-5：兜底档提示点名 UTF-16/Unicode。"""
    src = tmp_path / "坏档.md"
    # 0x81 0x30 0x81 0x30：UTF-8 与 GBK 均解码失败、GB18030 合法的 4 字节序
    src.write_bytes(b"\x81\x30\x81\x30" + "公开说明\r\n".encode("gb18030"))
    rep = _FakeReport()
    md_handler.process(src, tmp_path / "坏档_脱敏后.md",
                       _NullEngine(), None, rep)
    hits = [m for m in rep.logs if "GB18030" in m]
    assert hits, f"应留兜底档提示：{rep.logs}"
    assert any("UTF-16/Unicode" in m and "转存 UTF-8" in m for m in hits)


# ---------------------------------------------------------------------------
# R2-P1-3：pptx 组合形状读取失败留痕（桩模块构造异常路径）
# ---------------------------------------------------------------------------

class _BadShape:
    """shape_type 读取抛错的形状（R2-P1-3 异常路径构造）。

    with_name=False 时不设 name 属性 → 留痕回退到序号（第N个形状）。
    无 has_text_frame/has_table → except 落到普通形状分支后全跳过。
    """

    def __init__(self, with_name: bool):
        if with_name:
            self.name = "组合 7"

    @property
    def shape_type(self):
        raise RuntimeError("shape_type 不可读（模拟损坏组合形状）")


class _OkShape:
    shape_type = 17  # 任意非 GROUP 值

    def __init__(self, text):
        run = types.SimpleNamespace(text=text)
        para = types.SimpleNamespace(runs=[run])
        self.has_text_frame = True
        self.text_frame = types.SimpleNamespace(paragraphs=[para])


def test_pptx_group_shape_failure_logged(monkeypatch, tmp_path):
    """组合形状类型读取失败：留痕一行（含名称/序号），控制流照旧跳过。"""
    pptx_mod = types.ModuleType("pptx")
    enum_mod = types.ModuleType("pptx.enum")
    shapes_mod = types.ModuleType("pptx.enum.shapes")
    shapes_mod.MSO_SHAPE_TYPE = types.SimpleNamespace(GROUP="GROUP_FAKE")
    enum_mod.shapes = shapes_mod
    pptx_mod.enum = enum_mod

    def _fake_presentation(path):
        slide1 = types.SimpleNamespace(
            shapes=[_BadShape(with_name=True), _BadShape(with_name=False)])
        slide2 = types.SimpleNamespace(shapes=[_OkShape("普通文本框内容")])

        class _Prs:
            slides = [slide1, slide2]

            def save(self, p):
                pass
        return _Prs()

    pptx_mod.Presentation = _fake_presentation
    monkeypatch.setitem(sys.modules, "pptx", pptx_mod)
    monkeypatch.setitem(sys.modules, "pptx.enum", enum_mod)
    monkeypatch.setitem(sys.modules, "pptx.enum.shapes", shapes_mod)

    rep = _FakeReport()
    stats = pptx_handler.process(tmp_path / "样本.pptx",
                                 tmp_path / "样本_脱敏后.pptx",
                                 _NullEngine(), None, rep)
    assert stats == {"masked": 0, "slides": 2}
    skips = [m for m in rep.logs if "类型读取失败已跳过" in m]
    assert len(skips) == 2, f"两个坏形状各留一行：{rep.logs}"
    assert all("内部文本框未经脱敏" in m and "请人工复核" in m for m in skips)
    # 名称可得 → 形状「组合 7」；名称不可得 → 序号「第2个形状」
    assert any("形状「组合 7」" in m and "第1页" in m for m in skips)
    assert any("第2个形状" in m for m in skips)
    # 异常类型与消息入痕（回溯可诊断）
    assert any("RuntimeError" in m for m in skips)


# ---------------------------------------------------------------------------
# R2-P1-2：xls COM 收尾链留痕（桩模块构造 Close/Quit 双失败）
# ---------------------------------------------------------------------------

def test_xls_com_close_quit_failures_logged(monkeypatch, tmp_path):
    """COM wb.Close()/app.Quit() 双失败：各留一行；清理链仍走完不中断。"""
    pythoncom = types.ModuleType("pythoncom")
    pythoncom.CoInitialize = lambda: None
    pythoncom.CoUninitialize = lambda: None

    class _FakeWB:
        @property
        def Worksheets(self):
            raise RuntimeError("Worksheets 不可用（构造主体失败点）")

        def Close(self, SaveChanges=False):
            raise OSError("Close 失败（句柄悬挂）")

    class _FakeApp:
        def __init__(self):
            self.Workbooks = types.SimpleNamespace(Open=lambda p: _FakeWB())

        def Quit(self):
            raise OSError("Quit 失败（进程残留）")

    client_mod = types.ModuleType("win32com.client")
    client_mod.DispatchEx = lambda progid: _FakeApp()
    win32com_mod = types.ModuleType("win32com")
    win32com_mod.client = client_mod
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom)
    monkeypatch.setitem(sys.modules, "win32com", win32com_mod)
    monkeypatch.setitem(sys.modules, "win32com.client", client_mod)

    src = tmp_path / "样本.xls"
    src.write_bytes(b"\xd0\xcf\xd2\xb5 dummy")  # COM 打开为桩，字节任意
    dst = tmp_path / "样本_脱敏后.xls"
    rep = _FakeReport()
    # 主体失败照旧外抛（控制流零变化：finally 只补留痕不清异常）
    with pytest.raises(RuntimeError, match="Worksheets"):
        xls_handler.process(src, dst, _NullEngine(), None, rep)
    assert any("工作簿关闭失败" in m and "占用" in m for m in rep.logs), \
        f"Close 失败应留痕：{rep.logs}"
    assert any("Excel/WPS 进程退出失败" in m and "残留" in m for m in rep.logs), \
        f"Quit 失败应留痕：{rep.logs}"


# ---------------------------------------------------------------------------
# R2-P2-7：docx 页眉页脚部件收集失败留痕（桩对象直接构造）
# ---------------------------------------------------------------------------

def test_docx_collect_parts_failure_logged_with_partname_hint():
    """部件读取失败：留痕一行并尽力携带定位（partname 不可得时回退属性名）。"""

    class _Part:
        def __init__(self, pn):
            self.partname = pn

    class _HF:
        def __init__(self, pn):
            self.part = _Part(pn)

    class _Section:
        def __init__(self):
            self.header = _HF("/word/header1.xml")
            self.footer = _HF("/word/footer1.xml")

        @property
        def first_page_header(self):
            raise KeyError("header3.xml 部件缺失")

        # first_page_footer/even_page_* 未定义 → getattr AttributeError，同路径

    class _Doc:
        part = _Part("/word/document.xml")
        sections = [_Section()]

    rep = _FakeReport()
    kinds = [k for k, _p in docx_handler._collect_parts(_Doc(), rep)]
    assert kinds[0] == "正文"
    assert "页眉" in kinds and "页脚" in kinds
    fails = [m for m in rep.logs if "部件读取失败已跳过" in m]
    assert fails, f"失败部件应各留一行：{rep.logs}"
    assert any("first_page_header" in m for m in fails)  # 定位（属性名回退）
    assert all("请人工复核" in m for m in fails)
    # report=None 旧口径静默降级仍可用（兼容直接调用方）
    kinds2 = [k for k, _p in docx_handler._collect_parts(_Doc())]
    assert kinds2[0] == "正文"
