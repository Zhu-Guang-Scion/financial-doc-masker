# -*- coding: utf-8 -*-
"""OnnxV6Engine.recognize() 双格式兼容单测（可行性报告 R2 阻塞项回归）。

rapidocr_onnxruntime 1.4.x 返回 [poly, text, score] 三元组；rapidocr 3.x
风格为 (poly, (text, score)) 嵌套。主干 recognize() 必须两种都正确解析，
坏项跳过不崩、bbox 按 dpi 换算为 PDF 点。

运行：python tests/test_ocr_v6_recognize.py
（纯 mock，不加载真实权重、不依赖 rapidocr 安装。）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ocr_engine import OnnxV6Engine, OCRPageResult

POLY = [[10, 20], [110, 20], [110, 40], [10, 40]]


class _FakeOCR:
    def __init__(self, result):
        self._result = result

    def __call__(self, image_bytes):
        return self._result, (0.1, 0.2)


def _engine(result):
    eng = OnnxV6Engine.__new__(OnnxV6Engine)  # 绕过 __init__（不加载权重）
    eng._ocr = _FakeOCR(result)
    return eng


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


@case
def triplet_format():
    """1.4.x 三元组 [poly, text, score]。"""
    out = _engine([[POLY, "云舒雅家居股份有限公司", 0.97]]).recognize(b"", 72)
    assert len(out.lines) == 1
    ln = out.lines[0]
    assert ln.text == "云舒雅家居股份有限公司"
    assert abs(ln.confidence - 0.97) < 1e-6
    assert ln.bbox == (10.0, 20.0, 110.0, 40.0)


@case
def nested_format():
    """3.x 嵌套 (poly, (text, score))。"""
    out = _engine([(POLY, ("合同用条款", 0.85))]).recognize(b"", 72)
    assert len(out.lines) == 1
    assert out.lines[0].text == "合同用条款"
    assert out.lines[0].bbox == (10.0, 20.0, 110.0, 40.0)


@case
def xyxy_short_poly():
    """[x0,y0,x1,y1] 四值短格式同样支持。"""
    out = _engine([[[10, 20, 110, 40], "短格式", 0.9]]).recognize(b"", 72)
    assert out.lines[0].bbox == (10.0, 20.0, 110.0, 40.0)


@case
def dpi_scaling():
    """bbox 按 72/dpi 换算（144dpi → ×0.5）。"""
    out = _engine([[POLY, "混合行", 0.5]]).recognize(b"", 144)
    assert out.lines[0].bbox == (5.0, 10.0, 55.0, 20.0)


@case
def bad_items_skipped():
    """None 项 / 空项 / 异形项跳过，不影响其余行。"""
    out = _engine([None, [], [POLY, "好行", 0.9], ["只有一项"]]).recognize(b"", 72)
    assert len(out.lines) == 1
    assert out.lines[0].text == "好行"


@case
def empty_result():
    """result=None / 空列表 → 空页结果而非异常。"""
    for empty in (None, []):
        out = _engine(empty).recognize(b"", 72)
        assert isinstance(out, OCRPageResult)
        assert len(out.lines) == 0


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
    total = len(CASES)
    print(f"共 {total} 条，失败 {failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
