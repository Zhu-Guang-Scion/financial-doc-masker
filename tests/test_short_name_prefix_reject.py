# -*- coding: utf-8 -*-
"""v2.1.7 批乙 D：「三重」残段注册尾巴堵口（先红后绿）。

机理：SHORT_NAME_RE 捕获类排除 \\s（含换行）——行界截断的「三重」
「三重一」不含整词「三重一大」，子串闸 any(k in name) 全落空 → 注册
成功 → 全文子串遮蔽成残段（夜间差异归因约 5 笔实证）。堵法=词表补
「三重」+ 禁词真前缀否决。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rules_engine import RuleEngine, MaskOptions  # noqa: E402


def _registered(text, word):
    eng = RuleEngine(MaskOptions())
    eng.find_spans(text)
    return word in eng._short_names


def test_sanchong_linebreak_truncation_rejected_red():
    assert not _registered("三重\n（试行）以下简称三重\n，本三重决策适用。", "三重"), \
        "行界截断「三重」仍被注册"


def test_sanchongyi_truncation_rejected_red():
    assert not _registered("（以下简称三重一\n大）", "三重一"), \
        "截断「三重一」仍被注册"


def test_normal_short_name_still_registers():
    assert _registered("（以下简称昙栎）本制度所称昙栎。", "昙栎"), \
        "正常简称注册被误伤"


def test_whole_word_three_rejected():
    assert not _registered("（以下简称三重一大）", "三重一大")


def test_prefix_veto_other_truncations():
    """禁词真前缀否决：「银行」是「银行卡」前缀 → 不注册。"""
    assert not _registered("（以下简称银行\n卡）", "银行")
