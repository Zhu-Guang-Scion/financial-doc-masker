# -*- coding: utf-8 -*-
"""v2.1.6 字号内嵌连词跨越（方案 a）：snap 层「与」三明治判据。

背景（维护者 2026-09-24 发票实测）：「北京昙与栎餐饮管理有限公司」——
「与」∈_STOP_CHARS_BEFORE_NAME 使回溯在「与」截停；jieba 将生僻字
字号切成独立单字词（北京/侘/与/火/…），单字「与」又 ∈_FUNCTION_TAIL
被吸附规则拒斥 → 词干起点=「火」，「北京侘与」四字泄漏（截断≠脱敏）。

方案 a：单字「与」被丢在左侧时，若其左邻为普通名字字符（∈_NAME_CHARS
且 ∉停用 ∉功能尾字），判为字号内连词（昙与栎 形态）而非句法连词，
允许吸附跨越。句法形态由既有闸拦截：A公司与B公司=adj_break（回溯层）；
误跨的最坏结果=多遮一段疑似字号（P0 宁多遮不漏）。

测试词面全部虚构同构（生僻字 昙/栎 等价 侘 的词典外单字切分形态），
不含真实主体。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from app.rules_engine import MaskOptions, RuleEngine


def _eng() -> RuleEngine:
    return RuleEngine(MaskOptions(mask_company=True, mask_person=True,
                                  person_loose=True))


def _company_spans(text, eng=None):
    eng = eng or _eng()
    return [s for s in eng.find_spans(text) if s.rule == "company"]


def test_bridge_conj_rare_char_full_name():
    """字号内「与」（生僻字三明治）：全名命中，起点不截短。"""
    t = "北京昙与栎餐饮管理有限公司"
    spans = _company_spans(t)
    full = [s for s in spans if s.text == t]
    assert full, f"应整名命中：got={[s.text for s in spans]}"


def test_bridge_conj_left_run_only():
    """连词左邻为普通名字字符即跨（左 run 2 字同构）。"""
    t = "云川桥与月家居用品有限公司"
    spans = _company_spans(t)
    assert any(s.text == t for s in spans), \
        f"应整名命中：got={[s.text for s in spans]}"


def test_syntax_adj_break_still_splits():
    """句法连词既有行为不回退：两家公司名相邻各自独立（adj_break 层）。"""
    t = "云舒雅家居股份有限公司与慕蓝朵智能制造有限公司"
    spans = _company_spans(t)
    texts = {s.text for s in spans}
    assert "云舒雅家居股份有限公司" in texts and \
        "慕蓝朵智能制造有限公司" in texts, f"got={sorted(texts)}"


def test_prose_conj_not_false_positive():
    """公文行文「包括与适用…公司」类不因跨越产生公司误报。"""
    t = "本制度包括与适用范围有关的公司治理要求"
    spans = _company_spans(t)
    assert not spans, f"公文行文不应产公司候选：got={[s.text for s in spans]}"
