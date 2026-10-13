# -*- coding: utf-8 -*-
"""v2.1.7 批乙 C11：标签金额通道年份否定（先红后绿）。

捕获类 \\d{3,} 吞「余额\\n2023」「合计：2024」四位年份与 YYYYMMDD 八位
日期；本通道有意绕过 _amount_ok（无单位闸），此前文本层完全不设防
（docx/xlsx/pptx/PDF 原生页四链路共用）。镜像 pdf OCR 金额列口径
（1900-2100 四位 / YYYYMMDD 八位 fullmatch 拒）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rules_engine import RuleEngine, MaskOptions  # noqa: E402


def _amount_texts(text):
    eng = RuleEngine(MaskOptions(mask_amount=True))
    return [s.text for s in eng.find_spans(text) if s.rule == "amount"]


def test_labeled_year_not_amount_red():
    hits = _amount_texts("上年末余额\n2023\n本年合计：2024")
    assert hits == [], f"标签通道吞年份：{hits}"


def test_labeled_yyyymmdd_not_amount_red():
    hits = _amount_texts("退款金额 20240115 已处理")
    assert hits == [], f"八位日期被吞：{hits}"


def test_labeled_real_amount_still_masked():
    hits = _amount_texts("退款金额\n2223 已原路退回，合计 12,345.67。")
    assert any("2223" in h for h in hits), "真实标签裸数字被误伤"
    assert any("12,345.67" in h for h in hits)


def test_year_like_real_amount_edge():
    """1998/2099 等临界年份形态按年份否定拒；1899 与 2101 不在年份窗（构造）。"""
    hits = _amount_texts("余额 2099\n合计 1899")
    assert not any(h in ("2099",) for h in hits), "2099 应按年份否定拒"
    # 1899 不匹配 (?:19|20)\d\d ？——1899 匹配 18\\d\\d 不匹配 (19|20)；
    # 它是 4 位裸数字且无标签外语境，本就不过 \d{3,}+阈值通道？——有标签，
    # \d{3,} 捕获 1899，parse 值 1899 > 0 阈值 → 当前会遮；保持不动即可。
    assert "1899" in hits, "非年份窗形态维持既有遮蔽口径"


def test_eight_digit_real_amount_not_rejected_audit_fix():
    """独立审计 MINOR-3 修订：8 位带月/日合法性校验——「合计 20500000」
    （月位 50 非法）是真金额须遮；「20240115」（合法日期）仍拒。"""
    hits = _amount_texts("合计 20500000 与 退款金额 20240115。")
    assert "20500000" in hits, "8 位真金额被年份否定误拒"
    assert "20240115" not in hits, "合法 8 位日期形态应拒"
