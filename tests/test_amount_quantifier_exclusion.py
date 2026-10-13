# -*- coding: utf-8 -*-
"""v2.1.7 批乙 C10/C12：万/亿量词语境排除 + 千万复合档（先红后绿）。

C10：3万人次/500万股/2亿吨/10万辆类语境数字非金额（当前被第 4 支
[万亿] 命中误遮）；正当目标（大于10万/额度1亿/3万元）不受影响。
C12：￥2千万当前被单字类截成「￥2千」（量级误读 2000 vs 2×10⁷）；
裸 2千万/2千万元 整笔零命中。

例句均为构造句。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rules_engine import RuleEngine, MaskOptions  # noqa: E402


def _spans(text):
    eng = RuleEngine(MaskOptions(mask_amount=True))
    return [(s.text, s.rule) for s in eng.find_spans(text)]


def test_quantifier_contexts_not_amount_red():
    hits = _spans("全年培训超 3 万人次，配股 500 万股，年运量 2 亿吨，年产 10 万辆汽车。")
    amounts = [h for h in hits if h[1] == "amount"]
    assert amounts == [], f"量词语境被误判金额：{amounts}"


def test_legit_bare_wan_yi_still_masked():
    hits = _spans("单笔额度最高 1 亿，累计大于 10 万。")
    assert any(h[1] == "amount" for h in hits), "正当裸万/亿目标被误排"


def test_yuan_anchored_amount_still_masked():
    hits = _spans("人均报销 3万元/人，合计 4.5 亿元。")
    assert sum(1 for h in hits if h[1] == "amount") >= 2


def test_qianwan_with_currency_full_match_red():
    hits = _spans("注册资本￥2千万，实缴 1千万元。")
    texts = [h[0] for h in hits if h[1] == "amount"]
    assert "￥2千万" in texts, f"￥2千万 未整笔命中（当前被截断）：{texts}"
    assert "1千万元" in texts, f"1千万元 未命中：{texts}"


def test_bare_qianwan_masked_red():
    hits = _spans("项目预算 2千万，分两年投入。")
    assert any(h[1] == "amount" and "千万" in h[0] for h in hits), \
        f"裸 2千万 整笔零命中：{hits}"


def test_parse_qianwan_value():
    from app.rules_engine import _parse_amount_qian
    assert _parse_amount_qian("￥2千万") == 20000000.0
    assert _parse_amount_qian("2千万元") == 20000000.0
    assert _parse_amount_qian("3百亿") == 30000000000.0
    assert _parse_amount_qian("2千元") == 2000.0
    assert _parse_amount_qian("3万元") == 30000.0


def test_wan_not_qian_kilo_regression():
    """2.0.2 刻意决策回归锚：裸「千」仍不收（千瓦/千年类）。"""
    hits = _spans("装机 3千千瓦，历时 8千年。")
    assert not any(h[1] == "amount" and h[0].endswith("千") for h in hits)


def test_money_context_exemptions_audit_fix():
    """独立审计 MAJOR-1 修订：排除字开头但实为真金额语境的形态不得漏遮。"""
    for text in ("总价：20万人民币", "售价 520万人民币", "本金 3 亿年化收益 4.5%",
                 "拨款 5 亿个人养老金账户", "月流水 500 万月结", "违约金 10 万日结"):
        hits = _spans(text)
        assert any(h[1] == "amount" for h in hits), f"真金额语境漏遮：{text} -> {hits}"


def test_quantifier_still_excluded_after_fix():
    """豁免修订不得回退量词排除本体（含审计 MINOR-4 补的 套/间）。"""
    hits = _spans("全年培训超 3 万人次，配股 500 万股，房源 300 万套，商铺 50 万间。")
    assert not any(h[1] == "amount" for h in hits), hits


def test_bai_standalone_not_in_alternation():
    """独立审计 MAJOR-2 修订：独立「百」不入匹配面——￥2百 维持历史截断遮
    （旧口径 span=￥2），不得变全裸。"""
    hits = _spans("押金￥2百整。")
    assert any(h[1] == "amount" for h in hits), f"￥2百 从截断遮变全裸：{hits}"
    assert not any(h[0] == "￥2百" for h in hits), "独立百不应产生全形态命中"
