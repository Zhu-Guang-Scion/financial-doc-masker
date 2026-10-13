# -*- coding: utf-8 -*-
"""品牌预设配置（brands.json）测试集（v1.8.1）。

覆盖：缺失文件静默回空、环境变量 FINMASK_BRANDS 覆盖、BOM 容忍、坏 JSON
降级警告、防呆拒绝（通用词/过短/空白非文本）与去重、合法配置三表加载、
映射模式不注入、引擎停用表并集守卫（司南特例）、域名片段实例属性消费、
以及配置化前后金标准等值断言（golden_snapshot.json，重构前采集——
配置化零行为的单测级证明）。

运行：python tests/test_brand_config.py
"""
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 金标准等值断言依赖预设注入：品牌预设已外置（v1.8.1），测试显式加载语料配置
_BRANDS_TEST_JSON = ROOT / "_eval" / "brands_test.json"
_GOLDEN_SNAPSHOT = ROOT / "_goal_work" / "v181_impl" / "golden_snapshot.json"
os.environ["FINMASK_BRANDS"] = str(_BRANDS_TEST_JSON)

from app.app_config import brand_config_path
from app.brand_config import (ENV_OVERRIDE, load_brand_config, resolve_config_path,
                              _norm_key)
from app import masking_plan as mp
from app.analysis_cache import brands_config_hash
from app.rules_engine import (MaskOptions, RuleEngine, _BRAND_STOP_ALL,
                              NON_NAME_BLACKLIST, _ORG_SUFFIX_VOCAB_ALL,
                              _ORG_SUFFIX_REMOVABLE_VOCAB, _ORG_SUFFIX_ATOMS,
                              _norm_org_vocab)


@contextlib.contextmanager
def _env_brands(value):
    """临时设置/清除 FINMASK_BRANDS，用完恢复现场。"""
    old = os.environ.get(ENV_OVERRIDE)
    if value is None:
        os.environ.pop(ENV_OVERRIDE, None)
    else:
        os.environ[ENV_OVERRIDE] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(ENV_OVERRIDE, None)
        else:
            os.environ[ENV_OVERRIDE] = old


def _write_cfg(p: Path, obj) -> Path:
    p.write_text(obj if isinstance(obj, str) else
                 json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return p


# 引擎实际传入的停用表并集（与 rules_engine.__init__ 同一表达式）
ENGINE_FORBIDDEN = (_BRAND_STOP_ALL - {"司南"}) | frozenset(NON_NAME_BLACKLIST)


def case_missing_file_silent():
    """缺失文件 → 空配置零警告（缺失是常态，不吵）。"""
    cfg = load_brand_config(path=ROOT / "_eval" / "__no_such_brands__.json")
    assert (cfg.brands, cfg.aliases, cfg.domain_hints, cfg.warnings) == ([], [], [], [])


def case_env_override():
    """环境变量 FINMASK_BRANDS 覆盖生效（临时 setenv，用完恢复）。"""
    with tempfile.TemporaryDirectory() as td:
        p = _write_cfg(Path(td) / "brands_env.json", {
            "brands": ["环境变量品牌"], "aliases": ["别名甲"], "domain_hints": ["envhint"]})
        with _env_brands(str(p)):
            assert resolve_config_path() == p
            cfg = load_brand_config()
    assert cfg.brands == ["环境变量品牌"], cfg.brands
    assert cfg.aliases == ["别名甲"], cfg.aliases
    assert cfg.domain_hints == ["envhint"], cfg.domain_hints
    assert cfg.warnings == []


def case_bom_tolerated():
    """记事本 BOM（utf-8-sig）文件容忍。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "brands_bom.json"
        p.write_text(json.dumps({"brands": ["云舒雅"]}, ensure_ascii=False),
                     encoding="utf-8-sig")
        cfg = load_brand_config(path=p)
    assert cfg.brands == ["云舒雅"], cfg.brands
    assert cfg.warnings == []


def case_bad_json_downgrade():
    """坏 JSON → 空配置 + 人话警告（不抛异常）。"""
    with tempfile.TemporaryDirectory() as td:
        p = _write_cfg(Path(td) / "brands_bad.json", '{"brands": ["云舒雅",')
        cfg = load_brand_config(path=p)
    assert (cfg.brands, cfg.aliases, cfg.domain_hints) == ([], [], [])
    assert len(cfg.warnings) == 1, cfg.warnings
    assert "JSON" in cfg.warnings[0], cfg.warnings


def case_non_dict_top():
    """顶层非 dict（如数组）→ 空配置 + 警告。"""
    with tempfile.TemporaryDirectory() as td:
        p = _write_cfg(Path(td) / "brands_arr.json", '["云舒雅"]')
        cfg = load_brand_config(path=p)
    assert (cfg.brands, cfg.aliases, cfg.domain_hints) == ([], [], [])
    assert len(cfg.warnings) == 1 and "对象" in cfg.warnings[0], cfg.warnings


def case_non_list_key():
    """键值非 list → 警告 + 忽略该键（其余键正常）。"""
    with tempfile.TemporaryDirectory() as td:
        p = _write_cfg(Path(td) / "brands_type.json", {
            "brands": "云舒雅", "aliases": [], "domain_hints": ["acme"]})
        cfg = load_brand_config(path=p)
    assert cfg.brands == [] and cfg.domain_hints == ["acme"]
    assert len(cfg.warnings) == 1 and "brands" in cfg.warnings[0], cfg.warnings


def case_sanitize():
    """防呆：通用词拒绝（文案精确匹配）/ 过短拒绝 / 空白与非文本忽略 / 去重零警告。"""
    with tempfile.TemporaryDirectory() as td:
        p = _write_cfg(Path(td) / "brands_fool.json", {
            "brands": ["公司", "定制", "张", "", "   ", 123, "云舒雅", "云舒雅",
                       "  云舒雅  ", "司南", "慕蓝朵"],
            "aliases": ["高科", "华信高"],
            "domain_hints": ["acme"]})
        cfg = load_brand_config(path=p, forbidden_words=ENGINE_FORBIDDEN)
    assert cfg.brands == ["云舒雅", "司南", "慕蓝朵"], cfg.brands
    assert cfg.aliases == [] and cfg.domain_hints == ["acme"]
    assert "配置项「公司」过于宽泛，遮蔽它可能误伤大量正常文本，已忽略" in cfg.warnings
    assert "配置项「定制」过于宽泛，遮蔽它可能误伤大量正常文本，已忽略" in cfg.warnings
    assert "配置项「张」太短，遮蔽它容易误伤大片正常文本，已忽略（至少 2 个字）" in cfg.warnings
    assert "配置项「高科」过于宽泛，遮蔽它可能误伤大量正常文本，已忽略" in cfg.warnings
    assert "配置项「华信高」过于宽泛，遮蔽它可能误伤大量正常文本，已忽略" in cfg.warnings
    assert cfg.warnings.count("配置项存在空白或非文本条目，已忽略") == 3  # ""/"   "/123
    assert len(cfg.warnings) == 8, cfg.warnings  # 重复条目去重不产生警告


def case_brands_test_json_loads():
    """合法配置（语料 brands_test.json）加载 → 三表正确、零警告。"""
    cfg = load_brand_config(path=_BRANDS_TEST_JSON)
    assert cfg.brands == ["云舒雅", "慕蓝朵", "司南橱柜", "司南厨柜", "云舒",
                          "云住良材", "司南"], cfg.brands
    assert cfg.aliases == []
    assert cfg.domain_hints == ["acme", "acme", "acme", "acme"]
    assert cfg.warnings == []


def case_golden_equivalence():
    """金标准等值：配置化后引擎状态与样例 spans 和重构前快照完全一致。"""
    assert _GOLDEN_SNAPSHOT.exists(), f"金标准快照缺失：{_GOLDEN_SNAPSHOT}"
    snap = json.loads(_GOLDEN_SNAPSHOT.read_text(encoding="utf-8"))
    eng = RuleEngine(MaskOptions())
    assert sorted(eng._short_names.keys()) == snap["short_names_keys"]
    assert sorted(eng._static_brand_words) == snap["static_brand_words"]
    assert list(eng._brand_domain_hints) == snap["brand_domain_hints"]
    spans = [{"start": s.start, "end": s.end, "rule": s.rule, "text": s.text,
              "replacement": s.replacement}
             for s in eng.find_spans(snap["sample_text"])]
    assert spans == snap["spans"], spans


def case_mapping_mode_no_injection():
    """映射模式：有 mapping 时配置不注入（沿现「严格按映射表」语义）。"""
    with tempfile.TemporaryDirectory() as td:
        csv_p = Path(td) / "mapping.csv"
        csv_p.write_text("原文,替换为\n机密项目,XYZ项目\n", encoding="utf-8-sig")
        eng = RuleEngine(MaskOptions(mapping_csv=str(csv_p)))
        assert eng._mapping_only
        assert "云舒雅" not in eng._short_names, eng._short_names
        assert "司南" not in eng._short_names, eng._short_names
        assert eng.find_spans("不含司南橱柜与云舒雅的引用") == []


def case_forbidden_union_guard():
    """守卫：引擎停用表并集——配「司南」通过（预设历史收编特例）；配「公司」拒绝。"""
    # 误报治理否决项必须原样保留（学习通道约束不受配置通道影响）
    assert "司南" in _BRAND_STOP_ALL
    assert "华信高" in NON_NAME_BLACKLIST and "高科" in NON_NAME_BLACKLIST
    with tempfile.TemporaryDirectory() as td:
        p = _write_cfg(Path(td) / "b1.json", {"brands": ["司南"]})
        cfg = load_brand_config(path=p, forbidden_words=ENGINE_FORBIDDEN)
        assert cfg.brands == ["司南"] and cfg.warnings == [], (cfg.brands, cfg.warnings)
        _write_cfg(p, {"brands": ["公司"]})
        cfg = load_brand_config(path=p, forbidden_words=ENGINE_FORBIDDEN)
        assert cfg.brands == []
        assert cfg.warnings == ["配置项「公司」过于宽泛，遮蔽它可能误伤大量正常文本，已忽略"]


def case_domain_hints_engine():
    """引擎实际消费配置的域名片段：命中整址遮 ***，未命中部分遮蔽。"""
    with tempfile.TemporaryDirectory() as td:
        p = _write_cfg(Path(td) / "brands_dm.json", {
            "brands": [], "aliases": [], "domain_hints": ["acme"]})
        with _env_brands(str(p)):
            eng = RuleEngine(MaskOptions())
            r1 = [s.replacement for s in eng.find_spans("邮箱 ica@acme.com")]
            r2 = [s.replacement for s in eng.find_spans("邮箱 ica@example-brand.example")]
    assert r1 == ["***"], r1
    assert r2 == ["i***@example-brand.example"], r2


def case_default_resolution():
    """默认解析顺序：env 未设 → 程序目录 brands.json；不存在 → None。"""
    with _env_brands(None):
        p = resolve_config_path()
    assert p is None or p == brand_config_path(), p


# ---------------------------------------------------------------------------
# G1（schema v2）：组织后缀双键装载 + 防呆矩阵（契约 §5-④ 14 格逐格落用例）
# ---------------------------------------------------------------------------

# 与 rules_engine.__init__ 完全相同的注入实参（G1 防呆同规格；brand_config
# 不 import 引擎的约束只约束产品码，测试侧注入词集属契约 §3.0 设计）
_ENGINE_INJECT = dict(
    forbidden_words=ENGINE_FORBIDDEN,
    org_suffix_builtin_vocab=_ORG_SUFFIX_VOCAB_ALL,
    org_suffix_removable_vocab=_ORG_SUFFIX_REMOVABLE_VOCAB,
    org_suffix_atom_vocab=_norm_org_vocab(_ORG_SUFFIX_ATOMS),
)


def _load_cfg(td: Path, name: str, obj):
    return load_brand_config(path=_write_cfg(Path(td) / name, obj), **_ENGINE_INJECT)


def case_norm_key_alignment():
    """两侧口径对齐锁定（契约 §3.1-d/§5-④）：loader 轻归一化与引擎
    _norm_org_suffix_text 同规则 stdlib 复刻——括号/全半角/空白/边缘引号。"""
    from app.rules_engine import _norm_org_suffix_text
    for w in ("普通合伙", "（普通合伙）", "(普通合伙)", " 卫 生 室 ", "ＡＢＣ",
              "司”", "“中心”", "股份有限公司®"):
        assert _norm_key(w) == _norm_org_suffix_text(w), (w, _norm_key(w),
                                                          _norm_org_suffix_text(w))


def case_matrix_add_non_list():
    """④-1 org_suffixes 非 list → 1 条「不是列表」，该键空、其余键正常。"""
    with tempfile.TemporaryDirectory() as td:
        cfg = _load_cfg(Path(td), "m1.json", {
            "brands": [], "org_suffixes": "卫生室", "domain_hints": ["acme"]})
    assert cfg.org_suffixes == [] and cfg.domain_hints == ["acme"]
    assert len(cfg.warnings) == 1 and "org_suffixes" in cfg.warnings[0] \
        and "不是列表" in cfg.warnings[0], cfg.warnings


def case_matrix_add_blank_nonstr():
    """④-2 空白/非文本条目 → 3 条忽略警告，空表。"""
    with tempfile.TemporaryDirectory() as td:
        cfg = _load_cfg(Path(td), "m2.json", {"org_suffixes": ["", "  ", 123]})
    assert cfg.org_suffixes == []
    assert cfg.warnings.count("配置项存在空白或非文本条目，已忽略") == 3
    assert len(cfg.warnings) == 3, cfg.warnings


def case_matrix_add_too_short():
    """④-3 单字增词 → 1 条「至少 2 个字」拒绝（评审决策②：维持 ≥2，单字不开放）。"""
    with tempfile.TemporaryDirectory() as td:
        cfg = _load_cfg(Path(td), "m3.json", {"org_suffixes": ["卫"]})
    assert cfg.org_suffixes == []
    assert len(cfg.warnings) == 1 and "至少 2 个字" in cfg.warnings[0], cfg.warnings


def case_matrix_add_forbidden():
    """④-4 停用表命中（公司）→ 1 条「过于宽泛」，空表。"""
    with tempfile.TemporaryDirectory() as td:
        cfg = _load_cfg(Path(td), "m4.json", {"org_suffixes": ["公司"]})
    assert cfg.org_suffixes == []
    assert cfg.warnings == ["配置项「公司」过于宽泛，遮蔽它可能误伤大量正常文本，已忽略"], \
        cfg.warnings


def case_matrix_add_p0_registered():
    """④-5 撞遮蔽侧注册表（brands 同文件）→ P0 代理闸拒绝（契约 §3.1-d）。"""
    with tempfile.TemporaryDirectory() as td:
        cfg = _load_cfg(Path(td), "m5.json", {
            "brands": ["协和医院"], "org_suffixes": ["协和医院"]})
    assert cfg.brands == ["协和医院"], cfg.brands
    assert cfg.org_suffixes == [], cfg.org_suffixes
    assert len(cfg.warnings) == 1 and "不再被遮蔽" in cfg.warnings[0] \
        and "已拒绝" in cfg.warnings[0], cfg.warnings


def case_matrix_add_p0_mapping_injected():
    """P0 代理闸注入方向：映射键/自定义词经 org_suffix_add_forbidden 注入
    同样拒绝（引擎调用点的真实碰撞集；brands/aliases 由 loader 自查）。"""
    with tempfile.TemporaryDirectory() as td:
        p = _write_cfg(Path(td) / "m5b.json", {"org_suffixes": ["协和医院"]})
        cfg = load_brand_config(path=p, forbidden_words=ENGINE_FORBIDDEN,
                                org_suffix_add_forbidden=frozenset({"协和医院"}))
    assert cfg.org_suffixes == [] and len(cfg.warnings) == 1, (cfg.org_suffixes,
                                                               cfg.warnings)


def case_matrix_add_builtin_dup():
    """④-6 已内置（原子「有限公司」）→ 1 条「已在内置」，零生效（评审决策③）。"""
    with tempfile.TemporaryDirectory() as td:
        cfg = _load_cfg(Path(td), "m6.json", {"org_suffixes": ["有限公司"]})
    assert cfg.org_suffixes == []
    assert len(cfg.warnings) == 1 and "已在内置组织后缀表" in cfg.warnings[0], \
        cfg.warnings


def case_matrix_add_dedup_strip_silent():
    """④-7 strip 等价 + 组内去重 → 0 条警告，["卫生室"]。"""
    with tempfile.TemporaryDirectory() as td:
        cfg = _load_cfg(Path(td), "m7.json", {"org_suffixes": [" 卫生室 ", "卫生室"]})
    assert cfg.org_suffixes == ["卫生室"], cfg.org_suffixes
    assert cfg.warnings == [], cfg.warnings


def case_matrix_rm_single_char():
    """④-8 单字删词 → 放行 + 1 条专属后果警告（不硬禁，评审决策①方向）。"""
    with tempfile.TemporaryDirectory() as td:
        cfg = _load_cfg(Path(td), "m8.json", {"org_suffixes_remove": ["院"]})
    assert cfg.org_suffixes_remove == ["院"], cfg.org_suffixes_remove
    assert len(cfg.warnings) == 1 and "单字后缀" in cfg.warnings[0] \
        and "请确认" in cfg.warnings[0], cfg.warnings


def case_matrix_rm_normalized_key():
    """④-9（判据③-c）「（普通合伙）」与「普通合伙」等效：均 0 条警告，
    归一化键同键（loader 不产生伪警告；引擎侧删词生效见
    test_org_suffix_block G1 删词段）。"""
    with tempfile.TemporaryDirectory() as td:
        c1 = _load_cfg(Path(td), "m9a.json", {"org_suffixes_remove": ["（普通合伙）"]})
        c2 = _load_cfg(Path(td), "m9b.json", {"org_suffixes_remove": ["普通合伙"]})
    assert c1.warnings == [] and c2.warnings == [], (c1.warnings, c2.warnings)
    assert c1.org_suffixes_remove == ["（普通合伙）"]
    assert c2.org_suffixes_remove == ["普通合伙"]


def case_matrix_rm_typo():
    """④-10（判据③-d）typo → 1 条「未生效」响亮警告 + 零生效。"""
    with tempfile.TemporaryDirectory() as td:
        cfg = _load_cfg(Path(td), "m10.json", {"org_suffixes_remove": ["有限公司l"]})
    assert cfg.org_suffixes_remove == []
    assert len(cfg.warnings) == 1 and "未生效" in cfg.warnings[0], cfg.warnings


def case_matrix_rm_atom():
    """④-11 原子（有限）→ 1 条「不开放删除」，零生效（闸身不开放配置）。"""
    with tempfile.TemporaryDirectory() as td:
        cfg = _load_cfg(Path(td), "m11.json", {"org_suffixes_remove": ["有限"]})
    assert cfg.org_suffixes_remove == []
    assert len(cfg.warnings) == 1 and "组合切分原子" in cfg.warnings[0], cfg.warnings


def case_matrix_unknown_key():
    """④-12 未知键 → 逐键「未知键…已忽略」警告 + 他键正常（G1 新增防呆）。"""
    with tempfile.TemporaryDirectory() as td:
        cfg = _load_cfg(Path(td), "m12.json", {
            "brands": ["定制品牌"], "org_suffix": ["打错的键名"],
            "domain_hints": ["acme"]})
    assert cfg.brands == ["定制品牌"] and cfg.domain_hints == ["acme"]
    assert len(cfg.warnings) == 1 and "未知键「org_suffix」" in cfg.warnings[0], \
        cfg.warnings


def case_matrix_v1_file_zero_warning():
    """④-13 v1 三键老文件 → 0 条警告（向后兼容：合法 v1 文件零打扰）。"""
    cfg = load_brand_config(path=_BRANDS_TEST_JSON, **_ENGINE_INJECT)
    assert cfg.warnings == [], cfg.warnings
    assert cfg.org_suffixes == [] and cfg.org_suffixes_remove == []


def case_matrix_rm_non_list():
    """③-键级防呆：org_suffixes_remove 非 list → 该键忽略，不拖累他键。"""
    with tempfile.TemporaryDirectory() as td:
        cfg = _load_cfg(Path(td), "m14.json", {
            "aliases": ["别名甲"], "org_suffixes_remove": {"院": 1}})
    assert cfg.org_suffixes_remove == [] and cfg.aliases == ["别名甲"]
    assert len(cfg.warnings) == 1 and "org_suffixes_remove" in cfg.warnings[0], \
        cfg.warnings


def case_matrix_bad_json_with_new_keys():
    """④-14 坏 JSON（含新键）→ 1 条「不是有效的 JSON」，整文件降级空配置。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "bad.json"
        p.write_text('{"brands": ["云舒雅"], "org_suffixes": ["卫生室"],',
                     encoding="utf-8")
        cfg = load_brand_config(path=p, **_ENGINE_INJECT)
    assert (cfg.brands, cfg.org_suffixes, cfg.org_suffixes_remove) == ([], [], [])
    assert len(cfg.warnings) == 1 and "不是有效的 JSON" in cfg.warnings[0], cfg.warnings


# ---------------------------------------------------------------------------
# G1 判据③-f：brands_hash 全文件字节口径对新键自动联动（零产品码，守卫测试）
# ---------------------------------------------------------------------------

def case_hash_guard_new_keys():
    """仅改 org_suffixes / org_suffixes_remove → brands_config_hash 变化；
    五键空数组新模板 vs v1 三键文件 → hash 不同（字节不同是事实语义，
    plan/缓存按「文件变过」处理，多一次重建、不构成正确性——契约 §4.1 如实记录）。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "brands.json"
        _write_cfg(p, {"brands": [], "aliases": [], "domain_hints": []})
        with _env_brands(str(p)):
            h_v1 = brands_config_hash()
            _write_cfg(p, {"brands": [], "aliases": [], "domain_hints": [],
                           "org_suffixes": [], "org_suffixes_remove": []})
            h_v2_empty = brands_config_hash()
            _write_cfg(p, {"brands": [], "aliases": [], "domain_hints": [],
                           "org_suffixes": ["卫生室"], "org_suffixes_remove": []})
            h_add = brands_config_hash()
            _write_cfg(p, {"brands": [], "aliases": [], "domain_hints": [],
                           "org_suffixes": ["卫生室"], "org_suffixes_remove": ["院"]})
            h_rm = brands_config_hash()
            _write_cfg(p, {"brands": [], "aliases": [], "domain_hints": [],
                           "org_suffixes": ["分院"], "org_suffixes_remove": ["院"]})
            h_add2 = brands_config_hash()
        assert h_v1 != "none"
        # 空数组新键 ≠ v1 文件（字节事实；行为零差、仅多一次重建）
        assert h_v2_empty != h_v1
        # 仅改任一新键 → hash 变（plan 头部/缓存头部失效的 schema 侧基础）
        assert h_add != h_v2_empty
        assert h_rm != h_add
        assert h_add2 != h_add


def case_plan_header_rejects_old_plan():
    """③-f 实测一次：仅改 org_suffixes 一键 → 旧 plan 头部校验拒绝
    （load_plan「头部不匹配：brands_hash 不一致」→ 回落全流程重建路径）。"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "样例.pdf"
        src.write_bytes(b"%PDF-1.4\n%%EOF\n")
        cfg_p = root / "brands.json"
        _write_cfg(cfg_p, {"brands": [], "aliases": [], "domain_hints": [],
                           "org_suffixes": ["卫生室"], "org_suffixes_remove": []})
        options = MaskOptions(output_dir=str(root / "out"))
        with _env_brands(str(cfg_p)):
            b = mp.PlanBuilder(src, options)
            b.set_page(0, "native", 1, 0)
            b.set_native_text(0, "云舒雅家居股份有限公司与张三的协议")
            b.add_native_spans(0, [(0, 12, "company", "ABC股份有限公司", "高",
                                    "云舒雅家居股份有限公司")])
            b.save(mp.output_root(src, options))
            plan, reason = mp.load_plan(src, options)
            assert plan is not None, reason
            # 仅改 org_suffixes 一键（卫生室→分院）→ 旧 plan 作废
            _write_cfg(cfg_p, {"brands": [], "aliases": [], "domain_hints": [],
                               "org_suffixes": ["分院"], "org_suffixes_remove": []})
            plan2, reason2 = mp.load_plan(src, options)
        assert plan2 is None, "改 org_suffixes 后旧 plan 应被头部校验拒绝"
        assert "brands_hash" in reason2 and "不一致" in reason2, reason2


CASES = [
    ("缺失文件→空配置零警告", case_missing_file_silent),
    ("环境变量FINMASK_BRANDS覆盖生效", case_env_override),
    ("BOM文件容忍", case_bom_tolerated),
    ("坏JSON→空配置+人话警告", case_bad_json_downgrade),
    ("顶层非dict→空配置+警告", case_non_dict_top),
    ("非list键→警告+忽略该键", case_non_list_key),
    ("防呆拒绝/忽略/去重", case_sanitize),
    ("合法配置三表正确(brands_test.json)", case_brands_test_json_loads),
    ("金标准等值断言(重构前=配置化后)", case_golden_equivalence),
    ("映射模式配置不注入", case_mapping_mode_no_injection),
    ("停用表并集守卫(司南特例/公司拒绝)", case_forbidden_union_guard),
    ("引擎消费域名片段配置", case_domain_hints_engine),
    ("默认路径解析顺序", case_default_resolution),
    # ---- G1（schema v2）：防呆矩阵 14 格 + 口径对齐 + hash 守卫 ----
    ("G1两侧归一化口径对齐锁定", case_norm_key_alignment),
    ("④-1 org_suffixes非list→忽略该键", case_matrix_add_non_list),
    ("④-2 空白/非文本条目→3条忽略", case_matrix_add_blank_nonstr),
    ("④-3 单字增词→拒绝(至少2个字)", case_matrix_add_too_short),
    ("④-4 停用表命中→过于宽泛拒绝", case_matrix_add_forbidden),
    ("④-5 撞brands注册表→P0代理闸拒绝", case_matrix_add_p0_registered),
    ("④-5b 注入注册表(映射/自定义词)→拒绝", case_matrix_add_p0_mapping_injected),
    ("④-6 已内置重复登记→警告+零生效", case_matrix_add_builtin_dup),
    ("④-7 strip+去重→零警告生效", case_matrix_add_dedup_strip_silent),
    ("④-8 单字删词→放行+后果警告", case_matrix_rm_single_char),
    ("④-9 括号/裸形态同键等效(0警告)", case_matrix_rm_normalized_key),
    ("④-10 typo删词→未生效响亮警告", case_matrix_rm_typo),
    ("④-11 原子删词→不开放删除", case_matrix_rm_atom),
    ("④-12 未知键→逐键警告忽略", case_matrix_unknown_key),
    ("④-13 v1三键老文件→0条警告", case_matrix_v1_file_zero_warning),
    ("org_suffixes_remove非list→忽略该键", case_matrix_rm_non_list),
    ("④-14 坏JSON含新键→整文件降级", case_matrix_bad_json_with_new_keys),
    ("③-f hash守卫:新键变更→brands_hash变", case_hash_guard_new_keys),
    ("③-f 仅改org_suffixes→旧plan头部拒绝", case_plan_header_rejects_old_plan),
]


def run() -> bool:
    failed = []
    for name, fn in CASES:
        try:
            fn()
            print(f"  PASS {name}")
        except AssertionError as exc:
            failed.append(name)
            print(f"  FAIL {name}\n      {exc}")
        except Exception as exc:  # 用例自身异常同样计失败
            failed.append(name)
            print(f"  FAIL {name}\n      {type(exc).__name__}: {exc}")
    print(f"\n共 {len(CASES)} 条用例，失败 {len(failed)} 条")
    return not failed


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ok = run()
    sys.exit(0 if ok else 1)
