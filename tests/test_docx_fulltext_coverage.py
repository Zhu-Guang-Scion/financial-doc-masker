# -*- coding: utf-8 -*-
"""WF1（v2.0.3 候选）F1：docx XML 级 w:t 全收集——覆盖面回归套件。

对应 D2 诊断 guangtu_d2_l1.md 类别 A（E2/E3/E4 docx 遍历缺口）：
  ① 文本框文本送检断言（sdt/AlternateContent Choice+Fallback 内 txbxContent，
     改前红：doc.paragraphs 枚举不到，零送检）；
  ② 超链接目录送检断言（w:fldChar TOC + w:hyperlink 内 run，改前红：
     paragraph.runs 不含 hyperlink run → 送检空串早退）；
  ③ TOC 域回滚防护断言（遮蔽命中所在 TOC 域转静态文本：剥 fldChar/
     instrText/hyperlink 包裹保文本；非 TOC 域与未命中域不受影响）；
  ④ 正文/表格既有路径不回归（改前绿保持）；
  ⑤ 页眉超链接覆盖（页眉部件 XML 级遍历）；
  ⑥ 江图真实文件三实例闭合（E2 封面标题文本框 / E3 封面编制行文本框 /
     E4 目录首行超链接条目；语料缺失时 skip）。

先红后绿纪律：①②③（及⑤⑥的覆盖面断言）在旧实现下必须为红；④任何
实现下都必须为绿。官方验证链路支持 python 逐文件直跑（文件尾直跑块），
pytest 收集走模块级 def test_*（conftest 收集护栏口径）。
"""
from __future__ import annotations

import inspect
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 本地真实语料根（env FINMASK_CORPUS_DIR 可覆盖；缺失时 ⑥ skip）
from _fixtures_common import CORPUS_DIR

# ---------------------------------------------------------------------------
# 最小合成 docx 构造（不经 python-docx 模板，zip 直组 OOXML）
# ---------------------------------------------------------------------------

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_WPS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_V = "urn:schemas-microsoft-com:vml"

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType='
    '"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)

_DOC_HEAD = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:document xmlns:w="{_W}" xmlns:mc="{_MC}" xmlns:wp="{_WP}" '
    f'xmlns:wps="{_WPS}" xmlns:a="{_A}" xmlns:r="{_R}" '
    f'xmlns:v="{_V}">'
    "<w:body>"
)
_DOC_TAIL = '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>'


def _p(text=""):
    """一个段落；text 非空时生成单个普通 run。"""
    if text:
        return f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
    return "<w:p/>"


def _textbox_paragraph(word: str) -> str:
    """封面文本框结构（对齐江图原件 E2/E3 形态）：w:r 内 mc:AlternateContent，
    mc:Choice（wps 文本框）与 mc:Fallback（VML 文本框）各一份 txbxContent，
    内容同串——视觉一份、XML 两份。"""
    txbx = (
        "<w:txbxContent>"
        + _p()
        + f'<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>{word}</w:t></w:r></w:p>'
        + _p()
        + "</w:txbxContent>"
    )
    return (
        "<w:p><w:r>"
        "<mc:AlternateContent>"
        '<mc:Choice Requires="wps">'
        "<w:drawing>"
        '<wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" '
        'relativeHeight="1" behindDoc="0" locked="0" layoutInCell="1" allowOverlap="1">'
        '<wp:extent cx="5274310" cy="1171575"/>'
        '<wp:docPr id="1" name="文本框 1"/>'
        f'<a:graphic><a:graphicData uri="{_WPS}">'
        "<wps:wsp>"
        "<wps:spPr><a:xfrm><a:off x='0' y='0'/>"
        "<a:ext cx='5274310' cy='1171575'/></a:xfrm>"
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></wps:spPr>'
        f"<wps:txbx>{txbx}</wps:txbx>"
        "</wps:wsp>"
        "</a:graphicData></a:graphic>"
        "</wp:anchor>"
        "</w:drawing>"
        "</mc:Choice>"
        "<mc:Fallback>"
        "<w:pict>"
        '<v:shape style="width:415pt;height:92pt">'
        f"<v:textbox>{txbx}</v:textbox>"
        "</v:shape>"
        "</w:pict>"
        "</mc:Fallback>"
        "</mc:AlternateContent>"
        "</w:r></w:p>"
    )


def _toc_body(word: str) -> str:
    """目录域结构（对齐江图原件 E4 形态）：首条目段落含 [fldChar begin]
    [instrText TOC][fldChar separate] + w:hyperlink 包裹的条目文本；
    次条目段落含 PAGEREF 嵌套域；末段落 [fldChar end]。另附未命中的
    PAGE 域作「非 TOC 域零破坏」对照。"""
    return (
        # 目录首行（文本在 w:hyperlink 内，paragraph.runs 不可见）
        "<w:p>"
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> TOC \\o "1-2" \\h \\z \\u </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        f'<w:hyperlink w:anchor="_Toc1"><w:r><w:t>{word}</w:t></w:r></w:hyperlink>'
        "<w:r><w:tab/></w:r>"
        "<w:r><w:t>1</w:t></w:r>"
        "</w:p>"
        # 目录次行（PAGEREF 嵌套域）
        "<w:p>"
        '<w:hyperlink w:anchor="_Toc2"><w:r><w:t>第二章 测试条目</w:t></w:r>'
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> PAGEREF _Toc2 \\h </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        "<w:r><w:t>2</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        "</w:hyperlink>"
        "</w:p>"
        # 目录域结束
        '<w:p><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
        # 非 TOC 的 PAGE 域（未命中 → 必须原样保留）
        "<w:p>"
        '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
        '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
        "<w:r><w:t>3</w:t></w:r>"
        '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        "</w:p>"
    )


def build_docx(path: Path, body_inner: str) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc_xml = _DOC_HEAD + body_inner + _DOC_TAIL
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", doc_xml)
    return path


# ---------------------------------------------------------------------------
# 引擎 / 报表桩
# ---------------------------------------------------------------------------

_WORD = "江城图书馆"

_OPTIONS = dict(
    mask_company=False, mask_person=False, mask_amount=False,
    mask_contact=False, mask_address=False, mask_stamp=False,
    ocr_image_text=False, deep_recheck=False, person_loose=False,
)


def _make_options():
    from app.rules_engine import MaskOptions

    return MaskOptions(custom_words=[_WORD], **_OPTIONS)


def _make_engine(extra_words=()):
    from app.rules_engine import MaskOptions, RuleEngine

    return RuleEngine(MaskOptions(custom_words=[_WORD, *extra_words], **_OPTIONS))


class _Report:
    """docx_handler.process 所需的最小报表桩。"""

    def __init__(self):
        self.records = []
        self.logs = []

    def log(self, msg):
        self.logs.append(str(msg))

    def record(self, original, replacement, rule, location):
        self.records.append((original, replacement, rule, location))


def _run(src: Path, dst: Path, extra_words=()):
    """执行 docx 处理，返回 (报表, 输出全部件 w:t 串接)。"""
    from app.handlers import docx_handler

    report = _Report()
    docx_handler.process(src, dst, _make_engine(extra_words), _make_options(), report)
    return report, _all_wt_text(dst)


def _all_wt_text(docx_path: Path) -> str:
    """输出 docx 全部件 w:t 串接（覆盖面断言的取证口径：正文+页眉+页脚等）。"""
    from lxml import etree

    out = []
    with zipfile.ZipFile(docx_path) as z:
        for name in z.namelist():
            if not name.startswith("word/") or not name.endswith(".xml"):
                continue
            try:
                root = etree.fromstring(z.read(name))
            except etree.XMLSyntaxError:
                continue
            for t in root.iter(f"{{{_W}}}t"):
                out.append(t.text or "")
    return "".join(out)


def _doc_xml(docx_path: Path) -> str:
    with zipfile.ZipFile(docx_path) as z:
        return z.read("word/document.xml").decode("utf-8")


@pytest.fixture()
def workdir(tmp_path):
    d = tmp_path / "docx_fulltext"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# ① 文本框文本送检（改前红：零送检）
# ---------------------------------------------------------------------------

def test_textbox_text_masked(workdir):
    """文本框（AlternateContent 内 txbxContent）敏感词必须被遮蔽并落台账。"""
    src = build_docx(workdir / "in.docx",
                     _p("普通正文段落，不含敏感词。") + _textbox_paragraph(_WORD))
    dst = workdir / "out.docx"

    report, full = _run(src, dst)
    assert _WORD not in full, f"文本框敏感词残留：{full[:200]!r}"
    assert any(rec[0] == _WORD for rec in report.records), \
        f"文本框命中未落台账：{report.records!r}"
    assert any("文本框" in rec[3] for rec in report.records), \
        f"文本框位置标注缺失：{[rec[3] for rec in report.records]!r}"
    # 无关正文零误伤
    assert "普通正文段落，不含敏感词。" in full


def test_textbox_choice_fallback_both_rewritten(workdir):
    """Choice/Fallback 双份 txbxContent 必须同步改写（只改一份 = 新旧阅读器
    呈现不一致），但台账只记视觉一份（双份去重）。"""
    src = build_docx(workdir / "in.docx", _textbox_paragraph(_WORD))
    dst = workdir / "out.docx"

    report, _full = _run(src, dst)
    xml = _doc_xml(dst)
    assert xml.count(_WORD) == 0, "Choice/Fallback 有副本未同步改写"
    hits = [rec for rec in report.records if rec[0] == _WORD]
    assert len(hits) == 1, f"Choice/Fallback 双份未去重，台账 {len(hits)} 笔：{hits!r}"


# ---------------------------------------------------------------------------
# ② 超链接目录送检（改前红：runs 空 → 空串早退）
# ---------------------------------------------------------------------------

def test_hyperlink_toc_entry_masked(workdir):
    """TOC 域缓存条目（w:hyperlink 内 run）必须被送检遮蔽并落台账。"""
    src = build_docx(workdir / "in.docx", _toc_body(_WORD))
    dst = workdir / "out.docx"

    report, full = _run(src, dst)
    assert _WORD not in full, f"目录超链接敏感词残留：{full[:200]!r}"
    assert any(rec[0] == _WORD for rec in report.records), \
        f"目录命中未落台账：{report.records!r}"
    assert any("目录" in rec[3] for rec in report.records), \
        f"目录位置标注缺失：{[rec[3] for rec in report.records]!r}"
    # 目录次行（非敏感）文本保留
    assert "第二章 测试条目" in full


# ---------------------------------------------------------------------------
# ③ TOC 域回滚防护（命中域转静态；非命中域零破坏）
# ---------------------------------------------------------------------------

def test_hit_toc_field_converted_to_static(workdir):
    """遮蔽命中所在 TOC 域必须转静态文本：剥 fldChar/instrText/hyperlink
    包裹、保留（已遮蔽的）文本——杜绝 F9/打印预览域刷新回滚遮蔽。"""
    src = build_docx(workdir / "in.docx", _toc_body(_WORD))
    dst = workdir / "out.docx"

    _report, _full = _run(src, dst)
    xml = _doc_xml(dst)
    assert " TOC " not in xml, "TOC 域指令未剥离，F9 刷新仍会回滚遮蔽"
    assert " PAGEREF " not in xml, "嵌套 PAGEREF 域未一并剥离"
    assert "<w:hyperlink" not in xml, "TOC 条目 hyperlink 包裹未解除"
    # 目录可见文本保留（页码等）
    assert ">1</w:t>" in xml and ">2</w:t>" in xml, "目录文本被误删"


def test_non_toc_field_untouched(workdir):
    """未命中的非 TOC 域（PAGE）必须原样保留——域结构零破坏。"""
    src = build_docx(workdir / "in.docx", _toc_body(_WORD))
    dst = workdir / "out.docx"

    _report, _full = _run(src, dst)
    xml = _doc_xml(dst)
    assert " PAGE " in xml, "非 TOC 的 PAGE 域被误伤剥离"
    assert xml.count('w:fldCharType="begin"') == 1, "PAGE 域结构被破坏"
    assert ">3</w:t>" in xml, "PAGE 域缓存文本被误删"


def test_clean_toc_field_kept_dynamic(workdir):
    """未发生遮蔽命中的 TOC 域不转静态（目录仍可正常刷新）——只处理命中域。"""
    body = _toc_body(_WORD).replace(_WORD, "某某单位某手册", 1)  # 仅首行去敏感词
    src = build_docx(workdir / "in.docx", body)
    dst = workdir / "out.docx"

    _report, _full = _run(src, dst)
    xml = _doc_xml(dst)
    assert " TOC " in xml, "未命中的 TOC 域被误转静态文本"
    assert "<w:hyperlink" in xml, "未命中 TOC 域的 hyperlink 被误解除"


# ---------------------------------------------------------------------------
# ④ 正文/表格既有路径不回归（改前绿保持）
# ---------------------------------------------------------------------------

def test_plain_body_and_table_regression(workdir):
    """正文段落与表格单元格既有遮蔽路径零回归（改前改后都必须绿）。"""
    body = (
        _p(f"{_WORD}财务管理办法（正文段落）。")
        + "<w:tbl><w:tr><w:tc><w:tcPr/><w:p>"
        + f"<w:r><w:t>编制单位：{_WORD}办公室</w:t></w:r>"
        + "</w:p></w:tc></w:tr></w:tbl>"
        + _p("无敏感内容的收尾段落。")
    )
    src = build_docx(workdir / "in.docx", body)
    dst = workdir / "out.docx"

    report, full = _run(src, dst)
    assert _WORD not in full, f"正文/表格敏感词残留：{full[:200]!r}"
    locs = [rec[3] for rec in report.records]
    assert any("正文" in loc for loc in locs), f"正文位置标注缺失：{locs!r}"
    assert any("表格" in loc for loc in locs), f"表格位置标注缺失：{locs!r}"
    # 无关文本零误伤
    assert "无敏感内容的收尾段落" in full
    assert "（正文段落）。" in full


# ---------------------------------------------------------------------------
# ⑤ 页眉部件 XML 级覆盖
# ---------------------------------------------------------------------------

def test_header_hyperlink_masked(workdir):
    """页眉内的超链接文本同样必须送检（页眉部件 XML 级遍历）。"""
    src = workdir / "in.docx"
    body = (
        _p("正文唯一段落。")
        + '<w:sectPr><w:headerReference r:id="rIdH" w:type="default"/>'
        '<w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'
    )
    header_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:hdr xmlns:w="{_W}"><w:p>'
        f'<w:hyperlink w:anchor="x"><w:r><w:t>{_WORD}页眉链接</w:t></w:r></w:hyperlink>'
        "</w:p></w:hdr>"
    )
    src.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            _CONTENT_TYPES.replace(
                "</Types>",
                '<Override PartName="/word/header1.xml" ContentType='
                '"application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/></Types>',
            ),
        )
        z.writestr(
            "_rels/.rels",
            _RELS,
        )
        z.writestr("word/document.xml", _DOC_HEAD + body + _DOC_TAIL)
        # 页眉关系挂在文档部件自己的 rels（word/_rels/document.xml.rels）
        z.writestr(
            "word/_rels/document.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rIdH" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/header" Target="header1.xml"/>'
            "</Relationships>",
        )
        z.writestr("word/header1.xml", header_xml)

    dst = workdir / "out.docx"
    report, full = _run(src, dst)
    assert _WORD not in full, f"页眉超链接敏感词残留：{full[:200]!r}"
    assert any("页眉" in rec[3] for rec in report.records), \
        f"页眉位置标注缺失：{[rec[3] for rec in report.records]!r}"
    assert "正文唯一段落。" in full


# ---------------------------------------------------------------------------
# ⑥ 江图真实文件三实例闭合（语料缺失时 skip）
# ---------------------------------------------------------------------------

_GT_DOC = (CORPUS_DIR
           / "江城市图书馆（江图）测试样本"
           / "江城图书馆内控流程手册.docx")


@pytest.mark.skipif(not _GT_DOC.exists(), reason="本地语料未配置（真实语料不在盘）")
def test_guangtu_real_file_three_instances_closed(workdir):
    """真实文件 E2（封面标题文本框）/E3（封面编制行文本框）/E4（目录首行）
    全部闭合：输出全部件 w:t 零残留 + E4 所在 TOC 域已转静态 + 位置留痕。"""
    src = workdir / "江图原件.docx"
    shutil.copyfile(_GT_DOC, src)
    dst = workdir / "江图脱敏.docx"

    report, full = _run(src, dst, extra_words=("江图", "江城市图书馆"))
    assert _WORD not in full, "E2/E3 文本框或正文残留「江城图书馆」"
    assert "江城市图书馆" not in full, "残留「江城市图书馆」"
    assert "江图" not in full, "残留「江图」"
    xml = _doc_xml(dst)
    assert " TOC " not in xml, "E4 所在 TOC 域未转静态（F9 刷新回滚风险）"
    locs = [rec[3] for rec in report.records]
    assert any("文本框" in loc for loc in locs), f"E2/E3 位置留痕缺失：{locs[:20]!r}"
    assert any("目录" in loc for loc in locs), f"E4 位置留痕缺失：{locs[:20]!r}"


# ---------------------------------------------------------------------------
# python 直跑入口（官方验证链路口径）
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    failures = 0
    total = 0
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v) and not k.startswith("test__")]
    for name, fn in tests:
        if name == "test_guangtu_real_file_three_instances_closed" and not _GT_DOC.exists():
            print(f"SKIP {name}（语料缺失）")
            continue
        total += 1
        kwargs = {}
        with tempfile.TemporaryDirectory() as td:
            sig = inspect.signature(fn)
            if "workdir" in sig.parameters:
                wd = Path(td) / "docx_fulltext"
                wd.mkdir(parents=True, exist_ok=True)
                kwargs["workdir"] = wd
            try:
                fn(**kwargs)
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: [{type(e).__name__}] {e}")
    print(f"\n{total - failures}/{total} passed, {failures} failed")
    sys.exit(1 if failures else 0)
