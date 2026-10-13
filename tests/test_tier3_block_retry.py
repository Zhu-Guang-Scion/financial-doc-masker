# -*- coding: utf-8 -*-
"""3x 分块重试档状态守卫（撤除结案版）。

背景：3x 分块重试档经全量回归评估后撤除——2x 档召回配合列头锚定已完整
接替 3x 的独有产出，渲染金标准零新增可读，撤除安全。本用例守卫
「3x 已移除」的现状；若未来恢复 3x（如新增低清场景漏报证据），请同步
改写本用例并重跑全量回归。

运行：python tests/test_tier3_block_retry.py
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PDF_HANDLER = Path(__file__).resolve().parents[1] / "app" / "handlers" / "pdf_handler.py"


def test_tier3_call_removed():
    """3x 分块调用已按全量回归结论撤除（重试链仅 1x→2x），不得擅自恢复。"""
    src = PDF_HANDLER.read_text(encoding="utf-8")
    assert "_ocr_clip_tiled(page, r, ocr, zoom, 3, fitz)" not in src, \
        "3x 分块调用被恢复——全量回归已实证 2x+列头锚定完全接替；" \
        "恢复属行为变更，须走全量回归判定"
    assert "def _ocr_clip_tiled(" in src, "_ocr_clip_tiled 函数被移除（2x 仍依赖）"


def test_tier3_tiled_function_signature():
    """_ocr_clip_tiled 支持 factor 参数（2x/3x 共用分块实现）。"""
    src = PDF_HANDLER.read_text(encoding="utf-8")
    assert "def _ocr_clip_tiled(page, r, ocr, zoom, factor, fitz" in src, \
        "分块 OCR 函数签名变更——请确认 3x 档仍可用"


CASES = [test_tier3_call_removed, test_tier3_tiled_function_signature]


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
