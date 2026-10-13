# -*- coding: utf-8 -*-
"""v2.1.7 批乙 P1-5：复检词集按 mask_person/mask_company 开关分桶（先红后绿）。

真实不一致路径：冻结重放轮 register_confidences 回注 _person_conf 不查
开关——用户关掉人名遮蔽后复检轮仍按人名词集扫。公司桶与首轮
_dict_candidates 同口径不门控（单侧门控反而制造首轮遮/复检漏的新不一致）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rules_engine import RuleEngine, MaskOptions  # noqa: E402


def _engine_with_words():
    eng = RuleEngine(MaskOptions())
    eng._short_names["昙栎建设"] = "***"
    eng._company_alias["桥月实业"] = "ABC公司"
    eng._person_conf["王某某"] = "高"
    eng._word_keys = ["自定义风雨无阻"]
    return eng


def test_recheck_words_person_gated_red():
    from app.handlers.pdf_handler import _recheck_word_set
    eng = _engine_with_words()
    opt_off = MaskOptions(mask_person=False)
    words = _recheck_word_set(eng, opt_off)
    assert "王某某" not in words, "mask_person=False 时人名词仍入复检词集"
    assert "昙栎建设" in words and "桥月实业" in words, "公司桶不应被门控"
    assert "自定义风雨无阻" in words, "用户自定义词不应被门控"


def test_recheck_words_person_on_default():
    from app.handlers.pdf_handler import _recheck_word_set
    eng = _engine_with_words()
    words = _recheck_word_set(eng, MaskOptions())
    assert "王某某" in words and "昙栎建设" in words


def test_recheck_words_none_options_legacy():
    from app.handlers.pdf_handler import _recheck_word_set
    eng = _engine_with_words()
    words = _recheck_word_set(eng, None)
    assert "王某某" in words, "options 缺席（防御默认）维持全集口径"


def test_pure_suffix_still_filtered():
    from app.handlers.pdf_handler import _recheck_word_set
    eng = _engine_with_words()
    eng._short_names["有限公司"] = "***"
    words = _recheck_word_set(eng, None)
    assert "有限公司" not in words, "纯后缀词过滤不得回退"
