# -*- coding: utf-8 -*-
"""印章成品矩形终闸与图片保护回归锁（夜间深检批工程项 ②，2026-10-11）。

两个修复锚点（handlers 评审 P0-3/P1-1，视觉审查红框表格→整页黑条归因）：
① 合并终闸：形状闸此前作用于单个连通域，merge/extend 在闸后无界合并——
   成品矩形长边 >55% 页短边时回退为合并前组件（逐个已过形状闸）；
② redaction 图片保护：stamp 通道 apply_redactions 补 images=IMAGE_NONE
   （文本通道同款），印章误报区域不再物理抹除内嵌照片像素。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import app.handlers.pdf_handler as ph
from app.handlers.pdf_handler import (
    STAMP_LONG_EDGE_MAX_RATIO, _merge_same_line_rects,
    _stamp_final_gate, _STAMP_MERGE_TRACE)


class _Page:
    class _Rect:
        width, height = 595.0, 842.0

    rect = _Rect()

    def __init__(self):
        self.logs = []

    def log_path(self):
        pass


def _mkpage():
    return _Page()


def test_final_gate_splits_oversized_merged_rect():
    """红框表格行合并成整页横条 → 终闸回退为逐个组件。"""
    _STAMP_MERGE_TRACE.clear()
    # 页短边 595：终闸阈值 ≈327pt。构造 8 个 60pt 宽红框（间隙 40pt<3×行高
    # → merge 合并成 ~760pt 横条）——merge 前逐个 60pt 均远小于阈值
    comps = [(40.0 + i * 100.0, 200.0, 100.0 + i * 100.0, 260.0)
             for i in range(8)]
    merged = _merge_same_line_rects(comps)
    assert len(merged) == 1, f"应合并为一条：{merged}"
    assert max(merged[0][2] - merged[0][0], merged[0][3] - merged[0][1]) \
        > STAMP_LONG_EDGE_MAX_RATIO * 595
    out = _stamp_final_gate(_mkpage(), list(merged), None, 0)
    # 回退为组件级：每个 ≤60pt 宽（行高 60 也在阈内）
    assert len(out) >= 6, f"终闸应回退为多组件：{out}"
    for r in out:
        assert max(r[2] - r[0], r[3] - r[1]) <= STAMP_LONG_EDGE_MAX_RATIO * 595


def test_final_gate_keeps_normal_stamp():
    """单个正常尺寸印章矩形（合并前后同形）终闸直放。"""
    _STAMP_MERGE_TRACE.clear()
    r = [(300.0, 400.0, 400.0, 500.0)]  # 100pt，远小于 327 阈
    out = _stamp_final_gate(_mkpage(), r, None, 0)
    assert out == r


def test_stamp_redaction_uses_image_none(monkeypatch):
    """stamp 通道 apply_redactions 携带 images=IMAGE_NONE（图片像素保护）。"""
    import fitz  # noqa: F401  环境内 PyMuPDF 在位
    calls = {}

    class _P:
        class _Rect:
            width, height = 595.0, 842.0

        rect = _Rect()
        number = 0

        def add_redact_annot(self, r):
            pass

        def apply_redactions(self, **kw):
            calls.update(kw)

        def draw_rect(self, *a, **kw):
            pass

    class _R:
        def record(self, **kw):
            pass

        def log(self, m):
            pass

    ph._draw_black = lambda page, bbox: None
    n = ph._mask_stamps_from_png(_P(), b"", _R(), 0)
    assert n == 0  # 空 png 无检出（detect 返回 None 或空）——不炸即可
    # 直接验证关键字路径：伪造 rects 走主流程
    import unittest.mock as mock
    with mock.patch.object(ph, "detect_red_regions", return_value=[(300, 400, 380, 480)]):
        with mock.patch.object(ph, "_extend_rects_to_text_lines",
                               side_effect=lambda p, rs: rs):
            calls.clear()
            ph._mask_stamps_from_png(_P(), b"x", _R(), 0)
            assert calls.get("images") == getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0), \
                f"stamp redaction 应带 IMAGE_NONE：{calls}"
