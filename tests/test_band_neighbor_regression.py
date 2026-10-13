# -*- coding: utf-8 -*-
"""band_recheck 连带遮蔽回归用例（阶段二 G2-1 先补用例）。

场景：条带内品牌实体行紧邻职务词短行（「总经理」）。连带机制（v1.7 引入、
G2-1 裁定撤除）会把未命中实体的邻行按横幅整体连带吞掉——职务词行零敏感
内容却入映射表（观感误报）。撤除后：仅实体行命中，邻行保持可见。

运行：python tests/test_band_neighbor_regression.py
（真 fitz 空白页 + fake OCR/engine/report，不依赖真实权重。）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf as fitz

from app.handlers.pdf_handler import _band_recheck
from app.ocr_engine import OCRLine, OCRPageResult

OCR_DPI = 200  # 与 pdf_handler 一致


class _FakeEngine:
    """仅提供 _band_recheck 的 words 集合来源。"""

    def __init__(self, short_names):
        self._short_names = dict(short_names)
        self._mapping = {}
        self._word_keys = set()
        self._company_alias = {}
        self._person_conf = {}


class _FakeOCR:
    """条带复检按 160pt 步进把页面切成多个条带分别 OCR；真实 OCR 各条带
    内容不同——本 fake 仅首次调用（第一条带）返回目标行，其余条带为空。"""

    def __init__(self, lines):
        self._lines = lines
        self._calls = 0

    def recognize(self, image_bytes, dpi):
        self._calls += 1
        out = OCRPageResult()
        if self._calls == 1:
            for text, bbox in self._lines:
                out.lines.append(OCRLine(text=text, bbox=bbox, confidence=0.95))
        return out


class _FakeReport:
    def __init__(self):
        self.records = []
        self.logs = []

    def record(self, **kw):
        self.records.append(kw)

    def log(self, msg):
        self.logs.append(msg)


def _make_page():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    return doc, page


def test_neighbor_title_row_not_masked():
    """品牌行命中；紧邻职务词短行「总经理」不得被连带遮盖。"""
    doc, page = _make_page()
    # 行 1：品牌实体行（band 内点坐标，y 50-70）
    # 行 2：职务词短行（y 70-88，垂直中心距 19 ≤ 1.0 倍行高——旧连带窗口内）
    ocr = _FakeOCR([
        ("云舒雅家居", (100.0, 50.0, 300.0, 70.0)),
        ("总经理", (100.0, 70.0, 160.0, 88.0)),
    ])
    engine = _FakeEngine({"云舒雅家居": "***"})
    report = _FakeReport()
    hits = _band_recheck(page, engine, (lambda: ocr), report, 0, fitz)
    doc.close()

    assert hits == 1, f"应仅品牌行 1 次命中，实际 hits={hits}"
    joined_rules = [r for r in report.records if r.get("rule") == "band_recheck"]
    assert len(joined_rules) == 1, f"band_recheck 记录应仅品牌行，实际 {joined_rules}"
    assert joined_rules[0]["original"] == "云舒雅家居"
    assert not any("连带" in str(r.get("location", "")) for r in report.records), \
        "存在连带遮蔽记录——连带机制未撤除"
    assert not any(r.get("original") == "总经理" for r in report.records), \
        "职务词行被连带遮盖"


def test_long_neighbor_row_not_masked():
    """≥13 字正文长行不连带（长句保护在撤连带后仍无关紧要——一并断言）。"""
    doc, page = _make_page()
    ocr = _FakeOCR([
        ("云舒雅家居", (100.0, 50.0, 300.0, 70.0)),
        ("本制度由行政部门负责解释与修订", (100.0, 70.0, 420.0, 88.0)),
    ])
    engine = _FakeEngine({"云舒雅家居": "***"})
    report = _FakeReport()
    hits = _band_recheck(page, engine, (lambda: ocr), report, 0, fitz)
    doc.close()

    assert hits == 1, f"应仅品牌行 1 次命中，实际 hits={hits}"
    assert not any(r.get("original") == "本制度由行政部门负责解释与修订"
                   for r in report.records), "正文长行被连带遮盖"


CASES = [test_neighbor_title_row_not_masked, test_long_neighbor_row_not_masked]


def run():
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {fn.__name__}: [{type(e).__name__}] {e}")
    print(f"共 {len(CASES)} 条，失败 {failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
