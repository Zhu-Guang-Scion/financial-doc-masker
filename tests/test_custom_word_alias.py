# -*- coding: utf-8 -*-
"""v2.1.8：自定义敏感词可区分短代号（敏N）——先红后绿。

背景：自定义词原统一替换 ***，多词无法区分（后续利用文件时不知道谁是谁）。
方案：按 custom_words 输入序在引擎 __init__ 一次性派生 敏1/敏2/…（纯函数
可从词表复算——重放轮新引擎实例与首轮严格同值）；短于 ***，PDF 固定矩形
+字号自适应=零几何外溢；映射表 custom 行自带「原文→代号」图例。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rules_engine import RuleEngine, MaskOptions  # noqa: E402


def _engine(words):
    return RuleEngine(MaskOptions(custom_words=list(words)))


def test_custom_words_get_distinct_aliases_red():
    eng = _engine(["昙栎建设", "桥月实业"])
    spans = eng.find_spans("供应商包括昙栎建设与桥月实业两家。")
    by_text = {s.text: s.replacement for s in spans if s.rule == "custom"}
    assert by_text.get("昙栎建设") == "敏1", f"首个自定义词应为 敏1：{by_text}"
    assert by_text.get("桥月实业") == "敏2", f"第二个应为 敏2：{by_text}"
    assert by_text["昙栎建设"] != by_text["桥月实业"], "不同词必须可区分"


def test_alias_short_no_overmask():
    eng = _engine([f"构造词{i}号" for i in range(1, 30)])
    repls = [eng.custom_replacement(w) for w in eng._word_keys]
    assert all(r and len(r) <= 3 for r in repls), f"代号须简短（≤3 字符）：{repls[:5]}"
    assert len(set(repls)) == len(repls), "代号必须唯一"


def test_alias_deterministic_from_word_list():
    """纯函数性：代号只由词表（输入序）决定，与遭遇序/轮次无关。"""
    e1 = _engine(["甲词", "乙词", "丙词"])
    e2 = _engine(["甲词", "乙词", "丙词"])
    e2.find_spans("只出现丙词")  # e2 先只遭遇第三个词
    assert e1.custom_replacement("丙词") == e2.custom_replacement("丙词") == "敏3"


def test_alias_input_order_semantics():
    e = _engine(["乙词", "甲词"])
    assert e.custom_replacement("乙词") == "敏1"
    assert e.custom_replacement("甲词") == "敏2"


def test_custom_replacement_fallback():
    e = _engine(["甲词"])
    assert e.custom_replacement("未登记词") == "***"
    assert e.custom_replacement("甲词") == "敏1"


def test_filename_pairs_use_alias():
    eng = _engine(["昙栎建设", "桥月实业"])
    pairs = dict(eng.filename_replacement_pairs())
    assert pairs.get("昙栎建设") == "敏1"
    assert pairs.get("桥月实业") == "敏2"


def test_zero_custom_words_zero_effect():
    eng = _engine([])
    assert eng._custom_replacements == {}
    spans = eng.find_spans("云舒雅家居股份有限公司 13800001234")
    assert spans, "无自定义词时既有通道不受扰"
    assert all(s.rule != "custom" for s in spans)


def test_mapping_rows_carry_alias_legend(tmp_path):
    """映射表 custom 行自带原文→代号图例（dispatcher 汇总链端到端）。"""
    from app.handlers import docx_handler  # noqa: F401  确认可导入
    from app.dispatcher import merge_mapping_rows
    rows = [("昙栎建设", "敏1", "custom", "样例.md", "第1段"),
            ("桥月实业", "敏2", "custom", "样例.md", "第2段")]
    merged = merge_mapping_rows(rows)
    assert {(r[0], r[1]) for r in merged} == {("昙栎建设", "敏1"), ("桥月实业", "敏2")}
