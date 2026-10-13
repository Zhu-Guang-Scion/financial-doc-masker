# -*- coding: utf-8 -*-
"""v2.1.7 批甲 P1-3——PDF 内嵌图每页截断留痕：_embed_img_cap_note 纯单测。

背景：_mask_embedded_images 收集每页 >=60x40pt 且 >=0.4% 页面积的内嵌图
rect，按面积降序只识别前 _EMBED_IMG_MAX_PER_PAGE=8 张（耗时控制）——第 9 张
起图内文字不经识别直接保留（漏遮面）。此前该截断完全静默、无任何日志/台账；
v2.1.7 起切片前由纯函数 _embed_img_cap_note(total, limit) 生成单行中文消息，
调用方拼页号后经 report.log 留痕（先例：同函数 OCR 失败日志）。控制流零
变化：排序、切片、后续 OCR 行为逐行不变。

本文件只做纯函数单测（不触 PDF/OCR/fitz 运行时路径）；导入惯例与
tests/test_startup_ocr_log.py 一致（sys.path 指仓库根后
from app.handlers import pdf_handler）。

运行：python -m pytest tests/test_embed_img_cap_log.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.handlers import pdf_handler


def test_note_none_when_under_limit():
    # total < limit：不截断，无需留痕
    assert pdf_handler._embed_img_cap_note(7, 8) is None


def test_note_none_when_equal_limit():
    # total = limit：恰好取满上限，无截断
    assert pdf_handler._embed_img_cap_note(8, 8) is None


def test_note_none_when_empty():
    # total = 0：收集为空的页本就提前 return 0，函数口径同样返回 None
    assert pdf_handler._embed_img_cap_note(0, 8) is None


def test_note_message_when_over_limit():
    # total > limit：返回单行中文消息，同时含三个数字与「人工复核」
    msg = pdf_handler._embed_img_cap_note(12, 8)
    assert msg is not None
    assert isinstance(msg, str)
    assert "12" in msg          # 本页内嵌图共 N 张
    assert "8" in msg           # 按面积取大识别前 limit 张
    assert "4" in msg           # 其余 total-limit 张
    assert "人工复核" in msg
    assert "\n" not in msg      # 单行消息
    assert "第" not in msg      # 页号由调用方拼，不塞进本函数


def test_note_message_over_limit_by_one():
    # 边界 +1：恰好超出一张也要留痕（9 张、其余 1 张）
    msg = pdf_handler._embed_img_cap_note(9, 8)
    assert msg is not None
    assert "9" in msg and "1" in msg and "人工复核" in msg


def test_note_limit_zero():
    # 极端口径 limit=0：非零 total 必留痕；total=0 不留痕
    assert pdf_handler._embed_img_cap_note(1, 0) is not None
    assert pdf_handler._embed_img_cap_note(0, 0) is None
