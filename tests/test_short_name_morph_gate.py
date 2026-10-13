# -*- coding: utf-8 -*-
"""简称声明注册形态闸（v2.1.2 候选 · 评审决策①：复用学习形态闸，先红后绿）。

背景（注册点审计 g211_s1_audit.md 表 #9/#10 实测现场 B/B2/C）：
  简称声明通道捕获类 ``[^\s，。；、）”\"'‘“《]{2,8}`` 不排除数字/符号——
  「（以下简称"15%"）」注册成功并跨页把「本季度完成率15%的考核。」遮为
  company（跨页毒性）；「18mm」同族；去换行副本路径（PDF 行切断声明）
  同样注册。
修复（评审决策①）：复用 v2.1.1 学习形态闸 _BRAND_MORPH_RE——简称须为
  纯 CJK 或纯 ASCII 字母（2~4 字）；含数字/符号形态拒绝注册且不产候选。
  合法简称（云舒雅/宁柯/ABC 类）不受影响。

运行：pytest tests/test_short_name_morph_gate.py 或 python 直跑。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("FINMASK_BRANDS", "__no_such_brands__.json")
os.environ.pop("FINMASK_ONNX_MODELS", None)

import jieba  # noqa: E402

jieba.initialize()

from app.rules_engine import MaskOptions, RuleEngine  # noqa: E402

# 审计报告实测现场（B/B2/C 原文形态）
CASE_DECLARE_15 = "云舒雅家居股份有限公司（以下简称“15%”）的折扣政策如下。"
CASE_CROSS_PAGE = "本季度完成率15%的考核。"
CASE_DECLARE_18MM = "……（以下简称“18mm”）的板材标准。"
CASE_CROSSLINE = "……以下简\n称“15%”，特此说明。"
CASE_LEGIT = "云舒雅家居股份有限公司（以下简称“云舒雅”）的折扣政策如下。"
CASE_LEGIT_2 = "广州市宁柯贸易有限公司以下简称“宁柯”。"


def _engine() -> RuleEngine:
    return RuleEngine(MaskOptions(mask_company=True, person_loose=True))


def test_red_pre_fix_baseline():
    """修复前基线（先红演示固化）：15%/18mm 可经简称声明注册并跨页误遮。
    修复后本用例的前两条断言翻转——注册被形态闸拒绝。"""
    eng = _engine()
    eng.find_spans(CASE_DECLARE_15)
    pre_registered = "15%" in eng._short_names
    # 修复前：注册成立（红）；修复后：被形态闸拒绝（绿）
    assert pre_registered is False, "形态闸应拒绝数字形态简称注册"
    assert not any(s.text == "15%" and s.rule == "company"
                   for s in eng.find_spans(CASE_CROSS_PAGE)), "跨页毒性应消除"


def test_declare_18mm_rejected():
    """审计实测 C：数字+字母形态（18mm）同样拒绝。"""
    eng = _engine()
    eng.find_spans(CASE_DECLARE_18MM)
    assert "18mm" not in eng._short_names


def test_crossline_declaration_rejected():
    """审计实测 B2：去换行副本路径（PDF 行切断声明）同受形态闸约束。"""
    eng = _engine()
    eng.find_spans(CASE_CROSSLINE)
    assert "15%" not in eng._short_names


def test_legit_short_names_unaffected():
    """合法简称（纯 CJK/纯字母）注册与遮蔽不受形态闸影响。"""
    eng = _engine()
    spans = eng.find_spans(CASE_LEGIT)
    assert "云舒雅" in eng._short_names
    assert any(s.text == "云舒雅" and s.rule == "company" for s in spans)
    eng2 = _engine()
    eng2.find_spans(CASE_LEGIT_2)
    assert "宁柯" in eng2._short_names


def test_five_char_cjk_still_registered():
    """v2.1.1 首版判定拦截的漏遮反例：5 字纯 CJK 简称必须照常注册
    （闸 v2=拒数字/符号，CJK 长度自由沿路径原捕获规则）。"""
    eng = _engine()
    eng.find_spans("广州市示范图书馆（以下简称“示范图书馆”）的服务规范如下。")
    assert "示范图书馆" in eng._short_names


def test_letter_form_still_registered():
    """纯字母简称（ABC 类）放行（评审决策口径：纯 CJK 或纯字母 2~4 字）。"""
    eng = _engine()
    eng.find_spans("云峰有限（以下简称“ABC”）的合作条款如下。")
    assert "ABC" in eng._short_names


if __name__ == "__main__":
    import traceback
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print("FAILURES:", failures)
    sys.exit(1 if failures else 0)
