# -*- coding: utf-8 -*-
"""v2.1.8 并入：仅脱敏指定词模式（words_only，预研方案 A 定案）——先红后绿。

语义：③区开关开启时，引擎只跑 自定义词（+映射表若同时挂载=并集），
自动识别全停（品牌注入/学习/人名/金额/地址/证照/简称声明），复检词集
收敛为指定词集；指定词豁免纯后缀/白名单两道全局闸（配置词风雨无阻）；
plan 头部带模式维度（跨模式不得互相重放）。默认关=零行为。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rules_engine import RuleEngine, MaskOptions  # noqa: E402

_TEXT = ("云舒雅家居股份有限公司中标，供应商桥月实业有限公司，法定代表人王桥月，"
         "联系电话 13800001234，合同金额 12,345.67 元。")


def _spans(**kw):
    eng = RuleEngine(MaskOptions(**kw))
    return eng, [(s.text, s.rule) for s in eng.find_spans(_TEXT)]


def test_words_only_masks_custom_and_nothing_else_red():
    eng, hits = _spans(words_only=True, custom_words=["桥月实业"])
    assert any(h == ("桥月实业", "custom") for h in hits), \
        f"指定词必须命中 custom：{hits}"
    texts = "".join(h[0] for h in hits)
    assert "云舒雅" not in texts, "brands 静态词在 words_only 下不得遮蔽"
    assert "王桥月" not in texts and "13800001234" not in texts, \
        "人名/电话自动识别在 words_only 下必须全停"
    assert not any(h[1] == "amount" for h in hits), "金额自动识别必须全停"


def test_words_only_alias_still_works():
    eng, hits = _spans(words_only=True, custom_words=["桥月实业", "昙栎建设"])
    repl = eng.find_spans("桥月实业与昙栎建设")  # 同引擎批内代号稳定
    m = {s.text: s.replacement for s in repl if s.rule == "custom"}
    assert m.get("桥月实业") == "敏1" and m.get("昙栎建设") == "敏2"


def test_words_only_no_learning_channel():
    eng = RuleEngine(MaskOptions(words_only=True, custom_words=["桥月实业"]))
    eng.find_spans("慕蓝朵整家事业部与桥月实业签约")
    assert not any("慕蓝朵" in k for k in eng._short_names), \
        "事业部品牌学习通道在 words_only 下不得注册"


def test_words_only_gate_exemptions():
    """纯后缀/白名单词作为指定词：豁免两道全局闸（风雨无阻）。"""
    eng = RuleEngine(MaskOptions(words_only=True, custom_words=["集团有限公司"]))
    hits = [(s.text, s.rule) for s in eng.find_spans("桥月集团有限公司盖章")]
    assert any(h == ("集团有限公司", "custom") for h in hits), \
        f"纯后缀形态的指定词被硬阻拦静默丢弃：{hits}"
    eng2 = RuleEngine(MaskOptions(words_only=True, custom_words=["王桥月"],
                                  whitelist=["王桥月"]))
    hits2 = [(s.text, s.rule) for s in eng2.find_spans("签字人王桥月")]
    assert any(h[1] == "custom" for h in hits2), "白名单词作为指定词被静默拦截"


def test_words_only_with_mapping_union():
    """叠加映射表=并集（词典层现状语义）。"""
    import csv as _csv
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        mp = Path(td) / "m.csv"
        with open(mp, "w", newline="", encoding="utf-8-sig") as f:
            w = _csv.writer(f)
            w.writerow(["原文", "替换为"])
            w.writerow(["云舒雅家居股份有限公司", "某某公司"])
        eng = RuleEngine(MaskOptions(words_only=True, custom_words=["桥月实业"],
                                     mapping_csv=str(mp)))
        hits = [(s.text, s.rule) for s in eng.find_spans(_TEXT)]
        rules = {h[1] for h in hits}
        assert "custom" in rules and "mapping" in rules, f"并集语义：{hits}"
        assert not any(h[1] in ("company", "person", "phone", "amount")
                       for h in hits), "自动识别仍必须全停"


def test_words_only_default_off_zero_behavior():
    """默认关=零行为：自动识别照常。"""
    _, hits = _spans(custom_words=["桥月实业"])
    rules = {h[1] for h in hits}
    assert "company" in rules or "person" in rules or "phone" in rules, \
        "默认路径自动识别回退"


def test_words_only_snapshot_and_plan_mode_guard(tmp_path):
    """plan 快照含 words_only 键；跨模式 plan 不得互相重放（P0）。"""
    from app import masking_plan as mp
    # 快照键
    b = mp.PlanBuilder(Path("a.pdf"), MaskOptions(words_only=True))
    hdr = b.data["header"]
    fropts = hdr.get("first_round_options") or {}
    assert fropts.get("words_only") is True, "first_round_options 须含 words_only"
    b2 = mp.PlanBuilder(Path("a.pdf"), MaskOptions())
    assert (b2.data["header"].get("first_round_options") or {}) \
        .get("words_only") is False
    # 跨模式 load_plan 拒绝：手工构造合法四键 plan + words_only 冲突
    from app.analysis_cache import content_hash, brands_config_hash
    from app.utils import APP_VERSION
    src = tmp_path / "样例.pdf"
    src.write_bytes(b"%PDF-1.4 minimal")
    out_root = tmp_path / "out"
    plan_dir = mp.plan_dir_for(out_root)
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = mp.plan_path_for(src, out_root)
    import json
    plan_path.write_text(json.dumps({
        "header": {
            "plan_schema_version": mp.PLAN_SCHEMA_VERSION,
            "app_version": APP_VERSION,
            "src_content_hash": content_hash(src),
            "brands_hash": brands_config_hash(),
            "words_only": True,
        },
        "filename": {}, "table_rows": [],
        "pages": [{"pno": 0, "mode": "native", "text": "x"}],
    }, ensure_ascii=False), encoding="utf-8")
    got, reason = mp.load_plan(src, MaskOptions(output_dir=str(out_root)))
    assert got is None and "words_only" in reason, f"跨模式必须拒绝：{reason}"
    got2, reason2 = mp.load_plan(
        src, MaskOptions(words_only=True, output_dir=str(out_root)))
    assert got2 is not None, f"同模式必须命中：{reason2}"


def test_words_only_recheck_word_set_converged():
    """复检词集收敛为指定词集（不扫任何注册实体）。"""
    from app.handlers.pdf_handler import _recheck_word_set
    eng = RuleEngine(MaskOptions(words_only=True,
                                 custom_words=["桥月实业", "昙栎建设"]))
    eng._short_names["云舒雅"] = "***"
    eng._person_conf["王桥月"] = "高"
    words = _recheck_word_set(eng, eng.opt)
    assert words == {"桥月实业", "昙栎建设"}, f"词集未收敛：{words}"


def test_words_only_mapping_union_recheck():
    from app.handlers.pdf_handler import _recheck_word_set
    eng = RuleEngine(MaskOptions(words_only=True, custom_words=["桥月实业"]))
    eng._mapping["云舒雅"] = "某某"  # 直接注入模拟挂表
    words = _recheck_word_set(eng, eng.opt)
    assert words == {"桥月实业", "云舒雅"}


def test_words_only_suppresses_stamp_and_amount_gates():
    """评估批 P1-1/P1-2：words_only 下印章与金额通道随模式停度（默认路径零影响）。"""
    from app.handlers import pdf_handler as ph
    assert ph._stamp_on(MaskOptions(mask_stamp=True)) is True
    assert ph._stamp_on(MaskOptions(mask_stamp=True, words_only=True)) is False
    assert ph._amount_on(MaskOptions(mask_amount=True)) is True
    assert ph._amount_on(MaskOptions(mask_amount=True, words_only=True)) is False
    eng = RuleEngine(MaskOptions(mask_amount=True, words_only=True))
    assert eng.mask_numeric(12345.67) is None, "数值格金额通道在 words_only 下必须停用"
    eng2 = RuleEngine(MaskOptions(mask_amount=True))
    assert eng2.mask_numeric(12345.67) is not None, "默认路径金额通道不得回退"
