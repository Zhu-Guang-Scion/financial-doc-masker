# -*- coding: utf-8 -*-
"""v2.1.5 观感收窄轮口径锁：OCR 实体块外扩与 stamp 吞行半径。

背景（云键 109 页审计报告实测）：R19 外扩（12%+6pt/±3pt）叠加整行分支
（≥60% 行）后遮蔽面积翻倍（413k→861k pt²，pad/字宽 p50=2.6），块频繁
压住邻字与上下行；stamp 同行扩展锚 5×印章框高（≈500pt 吞行半径）把
118pt 印章框扩成 446pt 横条。本套件锁定收窄后口径：
  ① 整行分支外扩 = 半字宽/±1pt（_gap_patch_rect whole 同口径）；
  ② 折算分支外扩 = min(12%+6pt, 1.4×cw+2pt)、±1.5pt（窄 span 保持 R19）；
  ③ stamp 吞行半径锚 2×邻字高（保底 8pt），不再锚印章框高。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.handlers.pdf_handler as ph


class _It:
    """OCR 行桩：text + bbox（pt）。"""

    def __init__(self, text, bbox):
        self.text = text
        self.bbox = bbox


class _Page:
    number = 0

    def get_text(self, *a, **k):
        return []


class _Report:
    def __init__(self):
        self.rows = []

    def record(self, **k):
        self.rows.append(k)


def _run_dsol(monkeypatch, line_text, bbox, sub_s, sub_e, preset="m"):
    """跑 _draw_spans_on_lines 单行单 span，返回捕获的绘制矩形。"""
    rects = []
    monkeypatch.setattr(ph, "_draw_replacement",
                        lambda page, rect, repl: rects.append(
                            ph._import_fitz().Rect(rect)))
    it = _It(line_text, bbox)
    spans = [(sub_s, sub_e, "company", "***", "", line_text[sub_s:sub_e])]
    ph._draw_spans_on_lines(_Page(), [it], [(it, 0, 0)], spans, None,
                            _Report(), 0, "(OCR)", plan=None, preset=preset)
    assert len(rects) == 1
    return rects[0]


def test_whole_line_branch_half_char_pad(monkeypatch):
    """整行分支（span 覆盖 ≥60% 行）：外扩恰半字宽/±1pt。

    20 字行、span 覆盖 15 字（75% ≥60%）：cw=10pt，块左右各越出行框
    5pt（±0.6 容差）、上下各 1pt。
    """
    bbox = (100.0, 200.0, 300.0, 212.0)  # 宽 200pt / 20 字 → cw=10
    r = _run_dsol(monkeypatch, "字" * 20, bbox, 0, 15)
    assert r.x0 == pytest.approx(95.0, abs=0.6)
    assert r.x1 == pytest.approx(305.0, abs=0.6)
    assert r.y0 == pytest.approx(199.0, abs=0.3)
    assert r.y1 == pytest.approx(213.0, abs=0.3)


def test_partial_branch_capped_for_wide_span(monkeypatch):
    """折算分支窄 span：12%+6pt 未超 cap 时保持 R19 口径。

    20 字行（cw=10）、span 4 字：折算宽 40pt + 内置 1% 行宽 pad 两侧
    各 2pt = 44pt；pad=min(44×0.12+6, 1.4×10+2)=11.28pt；纵向
    _span_rect_in_line 自带 ±1 再加 vpad 1.5。
    """
    bbox = (100.0, 200.0, 300.0, 212.0)
    r = _run_dsol(monkeypatch, "字" * 20, bbox, 0, 4)
    pad = min(44 * 0.12 + 6.0, 10.0 * 1.4 + 2.0)  # =11.28
    assert r.x0 == pytest.approx(98.0 - pad, abs=0.8)
    assert r.x1 == pytest.approx(142.0 + pad, abs=0.8)
    assert r.y0 == pytest.approx(197.5, abs=0.3)   # y0-1-1.5
    assert r.y1 == pytest.approx(214.5, abs=0.3)   # y1+1+1.5


def test_partial_branch_cap_engages_on_long_span(monkeypatch):
    """折算分支超宽 span：12% 比例项超过 cap 时取 cap（1.4×cw+2pt）。

    span 11 字（55% 行，折算分支）：折算宽 110pt + 1% 行宽 pad 4pt
    =114pt；12%+6=19.68 > cap 16 → 取 16pt。
    """
    bbox = (100.0, 200.0, 300.0, 212.0)
    r = _run_dsol(monkeypatch, "字" * 20, bbox, 2, 13)
    x0_span = 100.0 + 2 * 10 - 2.0   # 折算 118（含内置 pad）
    x1_span = 100.0 + 13 * 10 + 2.0  # 折算 232
    pad = min((x1_span - x0_span) * 0.12 + 6.0, 10.0 * 1.4 + 2.0)  # =16
    assert pad == pytest.approx(16.0, abs=1e-6)
    assert r.x0 == pytest.approx(x0_span - pad, abs=1.0)
    assert r.x1 == pytest.approx(x1_span + pad, abs=1.0)


def test_stamp_extension_radius_anchored_to_word_height(monkeypatch):
    """stamp 吞行半径锚 2×邻字高：远距邻字不再被吞（5×框高旧口径回归锁）。

    印章框（100,200)-(220,300)（h=100pt），邻字「落」(320,235)-(330,247)
    （字高 12pt、gap=100pt）：旧口径 gap≤5×100=500 → 吞；新口径
    2×12=24 <100 → 不吞。
    """
    class _P:
        number = 0

        def get_text(self, *a, **k):
            return [(320.0, 235.0, 330.0, 247.0, "落", 0, 0, 0)]

    out = ph._extend_rects_to_text_lines(_P(), [(100.0, 200.0, 220.0, 300.0)])
    assert out == [(100.0, 200.0, 220.0, 300.0)]


def test_stamp_extension_still_swallows_close_fragment():
    """防残段语义保留：紧贴印章的小字碎片（gap ≤2×字高）仍被吞入。"""
    class _P:
        number = 0

        def get_text(self, *a, **k):
            return [(230.0, 240.0, 242.0, 252.0, "碎", 0, 0, 0)]

    out = ph._extend_rects_to_text_lines(_P(), [(100.0, 200.0, 220.0, 300.0)])
    # gap=10pt ≤ 2×12=24 → 吞：x1 扩到 242
    assert out[0][2] == pytest.approx(242.0, abs=0.5)
    assert out[0][0] == 100.0 and out[0][1] == 200.0 and out[0][3] == 300.0
