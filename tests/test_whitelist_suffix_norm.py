# -*- coding: utf-8 -*-
"""v2.1.7 批乙 C13：白名单后缀归一档（先红后绿）。

现缺口：白名单仅整 span 全等放行——「甲乙控股」放不掉
「甲乙控股集团有限公司」。新档：公司候选剥「纯组织后缀」残余后与
白名单全等才放行；人名维持全等（「张三」不得放掉「张三丰」）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rules_engine import RuleEngine, MaskOptions  # noqa: E402


def test_whitelist_spares_suffixed_company_red():
    eng = RuleEngine(MaskOptions(whitelist=["昙栎控股"]))
    hits = [(s.text, s.rule) for s in eng.find_spans("中标单位为昙栎控股集团有限公司。")]
    assert not any(h[1] == "company" and h[0].startswith("昙栎控股") for h in hits), \
        f"白名单未放行后缀形态：{hits}"


def test_whitelist_exact_match_still_spares():
    eng = RuleEngine(MaskOptions(whitelist=["昙栎控股"]))
    hits = [(s.text, s.rule) for s in eng.find_spans("中标单位为昙栎控股。")]
    assert not any(h[0] == "昙栎控股" for h in hits)


def test_non_whitelisted_company_still_masked():
    eng = RuleEngine(MaskOptions(whitelist=["昙栎控股"]))
    hits = [(s.text, s.rule) for s in eng.find_spans("供应商为桥月建设有限公司。")]
    assert any(h[1] == "company" and "桥月建设" in h[0] for h in hits), \
        "非白名单公司遮蔽被误放"


def test_whitelist_person_stays_exact():
    """人名全等语义：白名单「王桥」不得放掉「王桥月」。"""
    eng = RuleEngine(MaskOptions(whitelist=["王桥"]))
    hits = [(s.text, s.rule) for s in eng.find_spans("姓名：王桥月")]
    assert any(h[1] == "person" and h[0] == "王桥月" for h in hits), \
        "人名白名单放宽到子串=漏遮"


def test_whitelist_word_must_cover_head():
    """白名单词必须是候选头缀——中缀/尾缀不得放行（防滥放）。"""
    eng = RuleEngine(MaskOptions(whitelist=["控股"]))
    hits = [(s.text, s.rule) for s in eng.find_spans("中标单位为昙栎控股集团有限公司。")]
    assert any(h[1] == "company" for h in hits), "中缀白名单不得放行"
