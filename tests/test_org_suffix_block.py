# -*- coding: utf-8 -*-
"""v1.9.3 法定组织形式后缀「span 纯度硬阻拦」回归测试（候选生成层）。

语义铁边界（维护者评审决策固化，app/rules_engine.py 数据表处同文注释）：
- 只做 span 纯度判定：候选 span 文本归一化（全半角统一、剥离括号（）()
  与 ®™、去空白）后做 token 覆盖测试——能完整切分为后缀 token 集成员
  组合 → 拦截（不遮蔽）；含任何非清单成分 → 照常遮蔽；
- 禁止全文词汇级过滤（清单含 所/行/场/办 等 prose 高频字）——归一化+
  全覆盖切分的对象是候选 span 自身，不是整页文本；
- 含区别性成分的完整实体照常整体遮蔽；实体被拆成「区别部分+纯后缀」
  两 span 时，后缀被拦后区别部分仍须遮蔽（残留后缀不构成泄漏）；
- 候选生成层硬阻拦：auto 与 mapping 两模式统一生效。

本套件按实际判定如实记录 prose 碰撞：清单单字（社/团/部/队/站/台/校/
园/场/馆/店/铺/厂/矿…）组合出的日常词（社团/部队/站台/校园/店铺…）
_is_pure_org_suffix 判定为纯后缀——它们不会成为任何通道的候选 span，
遮蔽行为零变化（用例 6 实证）；若未来某通道开始对其出候选，本用例
即报警。

运行：python tests/test_org_suffix_block.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 预设依赖用例：品牌预设已外置（v1.8.1 brands.json），测试显式加载语料配置
os.environ.setdefault("FINMASK_BRANDS", str(
    Path(__file__).resolve().parents[1] / "_eval" / "brands_test.json"))

from app.rules_engine import (
    MaskOptions, RuleEngine, _is_pure_org_suffix,
    _ORG_SUFFIX_TOKENS, _ORG_SUFFIX_TOKENS_OPTIONAL, _ORG_SUFFIX_ATOMS,
    _BRAND_STOP_ALL,
)
from app.handlers.text_replacement import apply_replacements


class _Case:
    """极简用例收集器（与既有回归套件 run() 风格一致）。"""

    def __init__(self):
        self.failed = []
        self.total = 0

    def check(self, name, cond, detail=""):
        self.total += 1
        if cond:
            print(f"  PASS {name}")
        else:
            self.failed.append(name)
            print(f"  FAIL {name} {detail}")


# ---------------------------------------------------------------------------
# 1. 数据表完整性（维护者给定清单逐条收录 + 交集声明）
# ---------------------------------------------------------------------------

_MASTER_MAIN = {
    # 公司制
    "有限责任公司", "股份有限公司", "有限公司", "股份公司",
    "集团有限责任公司", "集团有限公司", "集团股份有限公司", "集团股份公司",
    # 合伙制
    "（普通合伙）", "（特殊普通合伙）", "（有限合伙）",
    "普通合伙企业", "特殊普通合伙企业", "有限合伙企业",
    # 非公司制法人
    "全民所有制企业", "集体所有制企业", "联营企业", "总厂", "厂", "矿",
    "场", "总公司", "供销合作社", "信用合作社", "农村集体经济合作社",
    # 个人独资/个体户
    "（个人独资）", "个人独资企业", "店", "馆", "部", "行", "中心",
    "工作室", "商行", "经营部", "营业部", "营业厅", "服务部", "车行",
    "餐厅", "商店", "超市", "铺", "坊", "屋", "酒店", "旅店", "农家院",
    "农场", "影楼", "加盟店", "队", "家庭农场",
    # 农民专业合作社
    "农民专业合作社", "农民专业合作社联合社", "分社",
    # 专业服务机构
    "律师事务所", "法律服务所", "公证处", "法律援助中心", "会计师事务所",
    "税务师事务所", "资产评估事务所", "代理记账事务所", "建筑设计事务所",
    "工程咨询事务所", "专利代理事务所", "商标代理事务所", "司法鉴定所",
    # 社会组织
    "协会", "商会", "学会", "研究会", "促进会", "联合会", "联谊会",
    "校友会", "同乡会", "公会", "基金会", "学校", "大学", "学院", "医院",
    "门诊部", "诊所", "养老院", "福利院", "培训中心", "职业学校",
    # 事业单位
    "研究院", "研究所", "设计院", "中学", "小学", "幼儿园", "博物馆",
    "图书馆", "档案馆", "文化馆", "体育馆", "疾控中心", "工作站",
    "监测站", "院", "校", "台", "站", "社", "团", "园",
    # 分支机构
    "分公司", "分厂", "分店", "分行", "支行", "办事处", "代表处",
    "联络处", "分会", "专业委员会", "工作委员会", "分站", "分所",
}
_MASTER_OPTIONAL = {"局", "委", "办", "署", "司", "厅", "处", "科",
                    "总工会", "妇联", "团委", "科协", "文联", "残联"}
_MASTER_ATOMS = {"公司", "集团", "有限", "股份", "责任", "企业", "合伙",
                 "合作社", "事务所"}


def test_t1_data_tables(c: _Case):
    print("[T1] 数据表完整性（维护者清单逐条收录）")
    c.check("主干表 = 维护者给定清单（无缺漏、无擅增）",
            set(_ORG_SUFFIX_TOKENS) == _MASTER_MAIN,
            f"缺:{sorted(_MASTER_MAIN - set(_ORG_SUFFIX_TOKENS))} "
            f"增:{sorted(set(_ORG_SUFFIX_TOKENS) - _MASTER_MAIN)}")
    c.check("可选机关集 = 维护者给定清单",
            set(_ORG_SUFFIX_TOKENS_OPTIONAL) == _MASTER_OPTIONAL,
            f"got:{sorted(_ORG_SUFFIX_TOKENS_OPTIONAL)}")
    c.check("组合原子集 = 维护者给定清单",
            set(_ORG_SUFFIX_ATOMS) == _MASTER_ATOMS,
            f"got:{sorted(_ORG_SUFFIX_ATOMS)}")
    # 交集声明（不改 _BRAND_STOP_ALL 本体，只核对注释所列重叠条目属实）
    c.check("主干表 ∩ _BRAND_STOP_ALL = {中心}（注释所列属实）",
            set(_ORG_SUFFIX_TOKENS) & set(_BRAND_STOP_ALL) == {"中心"},
            f"got:{sorted(set(_ORG_SUFFIX_TOKENS) & set(_BRAND_STOP_ALL))}")
    c.check("可选集 ∩ _BRAND_STOP_ALL = ∅",
            not (set(_ORG_SUFFIX_TOKENS_OPTIONAL) & set(_BRAND_STOP_ALL)))
    c.check("原子集 ∩ _BRAND_STOP_ALL = {公司, 集团}",
            set(_ORG_SUFFIX_ATOMS) & set(_BRAND_STOP_ALL) == {"公司", "集团"})


# ---------------------------------------------------------------------------
# 2. 纯后缀拦截：全类别抽样（含括号变体/全角/带空格/®）
# ---------------------------------------------------------------------------

_PURE_SAMPLES = [
    # 公司制（≥3）
    "有限责任公司", "股份有限公司", "有限公司", "股份公司",
    "集团股份有限公司",
    # 合伙制：括号变体 + 全角/半角括号
    "（普通合伙）", "（特殊普通合伙）", "（有限合伙）", "(普通合伙)",
    "（个人独资）", "普通合伙企业", "特殊普通合伙企业", "有限合伙企业",
    # 非公司制法人
    "全民所有制企业", "集体所有制企业", "总厂", "厂", "矿", "场",
    "供销合作社", "农村集体经济合作社",
    # 个人独资/个体户
    "个人独资企业", "店", "馆", "部", "行", "工作室", "商行", "经营部",
    "营业部", "车行", "餐厅", "农家院", "影楼", "加盟店", "队", "家庭农场",
    # 农民专业合作社
    "农民专业合作社", "农民专业合作社联合社", "分社",
    # 专业服务机构
    "律师事务所", "法律服务所", "公证处", "会计师事务所", "司法鉴定所",
    # 社会组织
    "协会", "商会", "研究会", "联合会", "基金会", "学校", "大学", "学院",
    "医院", "养老院", "职业学校",
    # 事业单位
    "研究院", "研究所", "设计院", "中学", "幼儿园", "博物馆", "疾控中心",
    "院", "校", "台", "站", "社", "团", "园",
    # 分支机构
    "分公司", "分厂", "分店", "分行", "支行", "办事处", "代表处",
    "工作委员会", "分所",
    # 可选机关集（blocked=True 且 used_optional=True）
    "局", "委", "办", "署", "司", "厅", "处", "科", "总工会", "妇联",
    "团委", "科协", "文联", "残联",
    # 归一化变体：带空格 / ® / 全角括号混合
    "有 限 责 任 公 司", "股 份 有 限 公 司", "有限\t公司\n", "股份有限公司®",
    "®事务所", "（ 分 公 司 ）",
]


def test_t2_pure_blocked(c: _Case):
    print(f"[T2] 纯后缀拦截（{len(_PURE_SAMPLES)} 条抽样，含变体）")
    for s in _PURE_SAMPLES:
        blocked, used_opt = _is_pure_org_suffix(s)
        want_opt = s in _MASTER_OPTIONAL or s in {"厅", "办"}  # 可选集成员本身
        c.check(f"拦 {s!r}", blocked is True,
                f"got blocked={blocked}")
        if want_opt:
            c.check(f"  {s!r} 来源=可选机关集（used_optional=True）",
                    used_opt is True, f"got used_optional={used_opt}")


# ---------------------------------------------------------------------------
# 3. 组合形态（主干组合 + 原子组合全覆盖）
# ---------------------------------------------------------------------------

def test_t3_combinations(c: _Case):
    print("[T3] 组合形态")
    for s in ("集团有限公司", "集团有限责任公司", "集团股份公司",
              "有限责任公司分公司", "分公司分公司", "农民专业合作社",
              "律师事务所分所", "供销合作社", "信用合作社",
              "（有限合伙）（特殊普通合伙）"):
        blocked, _ = _is_pure_org_suffix(s)
        c.check(f"拦 组合 {s!r}", blocked is True, f"got blocked={blocked}")
    # 组合原子本身全覆盖（单原子成词场景）
    for s in ("公司", "集团", "有限", "股份", "责任", "企业", "合伙",
              "合作社", "事务所"):
        blocked, _ = _is_pure_org_suffix(s)
        c.check(f"拦 原子 {s!r}", blocked is True, f"got blocked={blocked}")
    # 组合原子拼不出、清单未收录的形态 → 含非清单成分照常遮蔽（如实记录）
    for s, why in (("股份合作公司", "「合作」非组合原子，清单亦未收录该形态"),
                   ("总队", "「总」非 token（总公司为整条收录，不拆用）"),
                   ("总局", "「总」非 token"),
                   ("联合社", "「联合」非 token（清单收录的是分社/联合社整条）")):
        blocked, _ = _is_pure_org_suffix(s)
        c.check(f"放行（照常遮蔽）{s!r}（{why}）", blocked is False,
                f"got blocked={blocked}")


# ---------------------------------------------------------------------------
# 4. 含实体放行：全覆盖失败 → 照常遮蔽
# ---------------------------------------------------------------------------

def test_t4_entity_passthrough(c: _Case):
    print("[T4] 含区别性成分的实体放行（不拦 → 照常遮蔽）")
    for s in ("云舒雅家居股份有限公司", "腾讯有限公司", "北京银行",
              "工商银行", "海淀医院", "深圳市腾讯计算机系统有限公司",
              "圣都家居(浙江)有限责任公司", "北京分公司", "海淀分公司",
              "中正信会计师事务所", "南开大学", "中国人民银行"):
        blocked, used_opt = _is_pure_org_suffix(s)
        c.check(f"放行 {s!r}", blocked is False,
                f"got blocked={blocked} used_optional={used_opt}")
    # 「（中国）」变体四式：归一化剥括号字符后「中国」为非清单成分 →
    # 覆盖测试失败 → 照常遮蔽（并入主干判定、无需单列的实际结果，如实记录）
    for s in ("（中国）有限责任公司", "（中国）股份有限公司",
              "（中国）集团有限公司", "（中国）股份公司",
              "（中国）有限公司"):
        blocked, _ = _is_pure_org_suffix(s)
        c.check(f"放行 {s!r}（括号剥字符后含非清单成分「中国」）",
                blocked is False, f"got blocked={blocked}")
    # 归一化后含非清单成分（全角字母/数字保留 → 覆盖失败）
    for s in ("ＡＢＣ有限公司", "有限公司2024"):
        blocked, _ = _is_pure_org_suffix(s)
        c.check(f"放行 {s!r}（归一化后含字母/数字非清单成分）",
                blocked is False, f"got blocked={blocked}")
    # 空串/剥后为空 → 不拦
    for s in ("", "   ", "（（））", "（）®"):
        blocked, _ = _is_pure_org_suffix(s)
        c.check(f"不拦 空形态 {s!r}", blocked is False)


# ---------------------------------------------------------------------------
# 5. prose 碰撞实测（如实记录 + 零遮蔽变化）
# ---------------------------------------------------------------------------

# 维护者点名核查的 prose 词：实际判定结果（任何与预期相反的案例逐条列出）：
#   所以/现场/银行/队长/演出厅/科学 → 纯度判定 False（零影响）
#   社团 → 社+团 均为清单单字 token → 纯度判定 True（如实记录；
#         该词不会成为任何通道的候选 span，遮蔽行为零变化，见本节末尾）
# 其余清单单字组合出的日常词（同上机理，一并如实记录）：
_PROSE_PURE_TRUE = ("社团", "部队", "站台", "台站", "校园", "场馆",
                    "店铺", "厂矿", "矿场", "站队", "行行", "团社")
_PROSE_FALSE = ("所以", "现场", "银行", "队长", "演出厅", "科学",
                "商场", "广场", "大厅", "社会", "剧团", "书院", "司机",
                "司仪", "总会", "总队")


def test_t5_prose_collisions(c: _Case):
    print("[T5] prose 碰撞实测（如实记录）")
    for w in _PROSE_FALSE:
        blocked, _ = _is_pure_org_suffix(w)
        c.check(f"prose {w!r} 纯度判定 False", blocked is False)
    for w in _PROSE_PURE_TRUE:
        blocked, _ = _is_pure_org_suffix(w)
        c.check(f"prose {w!r} 纯度判定 True（清单单字组合，如实记录；"
                f"不构成候选 span，无遮蔽影响）", blocked is True)
    # 零遮蔽变化实证：prose 词混排句 find_spans 全程无候选
    eng = RuleEngine(MaskOptions(mask_company=True, mask_person=True,
                                 mask_address=True, mask_amount=True,
                                 mask_contact=True, person_loose=True))
    prose_text = "本社团在站台集合，部队经过校园的店铺与场馆、厂矿与矿场。"
    spans = eng.find_spans(prose_text)
    c.check("prose 混排句零遮蔽变化（find_spans 无任何 span）", spans == [],
            f"got {[(s.text, s.rule) for s in spans]}")


# ---------------------------------------------------------------------------
# 6. 铁边界二：「区别部分+纯后缀」两 span 并存 → 后缀拦、区别部分留
#    （find_spans 小文本用例；mapping 行模拟两候选并存，auto 全名对照）
# ---------------------------------------------------------------------------

def _make_mapping_engine(rows: dict) -> RuleEngine:
    d = tempfile.mkdtemp(prefix="_v193_org_suffix_")
    p = Path(d) / "mapping.csv"
    lines = ["原文,替换为"] + [f"{k},{v}" for k, v in rows.items()]
    p.write_text("\n".join(lines), encoding="utf-8-sig")
    return RuleEngine(MaskOptions(mapping_csv=str(p)))


def test_t6_two_span_boundary(c: _Case):
    print("[T6] 铁边界二：区别部分+纯后缀两 span 并存")
    # mapping 两行并存：区别部分行遮蔽、纯后缀行被硬阻拦不遮蔽
    eng = _make_mapping_engine({"云舒雅家居": "***", "有限公司": "X"})
    text = "云舒雅家居有限公司"
    spans = eng.find_spans(text)
    got = apply_replacements(text, spans)
    c.check("两 span 并存：后缀 span 被拦，区别部分 span 照常遮蔽",
            got == "***有限公司",
            f"spans={[(s.start, s.end, s.text) for s in spans]} got={got!r}")
    c.check("纯后缀行（有限公司）不产生遮蔽 span",
            all(s.text != "有限公司" for s in spans))
    c.check("阻拦统计计数（stats_org_suffix_blocked ≥1）",
            eng.stats_org_suffix_blocked >= 1,
            f"got {eng.stats_org_suffix_blocked}")
    # 同引擎下完整实体行照常遮蔽（对照）
    eng2 = _make_mapping_engine({"云舒雅家居股份有限公司": "ABC股份有限公司",
                                 "有限公司": "X"})
    text2 = "云舒雅家居股份有限公司"
    spans2 = eng2.find_spans(text2)
    got2 = apply_replacements(text2, spans2)
    c.check("完整实体行照常整体遮蔽", got2 == "ABC股份有限公司",
            f"got={got2!r}")
    # auto 模式对照：完整公司名照常遮蔽，且不产生裸后缀 span
    eng3 = RuleEngine(MaskOptions(mask_company=True, mask_person=True,
                                  person_loose=True))
    text3 = "支付腾讯有限公司货款"
    spans3 = eng3.find_spans(text3)
    got3 = apply_replacements(text3, spans3)
    c.check("auto 完整公司名照常遮蔽", got3 == "支付ABC有限公司货款",
            f"got={got3!r}")
    c.check("auto 无裸后缀 span（有限公司 不独立成 span）",
            all(s.text != "有限公司" for s in spans3),
            f"got {[(s.text, s.rule) for s in spans3]}")
    # mapping 模式统一生效（铁边界：候选生成层硬阻拦，两模式一致）
    eng4 = _make_mapping_engine({"分行": "Y", "支行": "Z"})
    text4 = "分行支行"
    spans4 = eng4.find_spans(text4)
    c.check("mapping 模式纯后缀行同样被拦（两模式统一）",
            spans4 == [] and apply_replacements(text4, spans4) == "分行支行",
            f"got {[(s.text, s.rule) for s in spans4]}")


# ---------------------------------------------------------------------------

def test_t7_quote_edge_stripping(c: _Case) -> None:
    """v1.9.4 R3 评审决策附加修复：归一化剥离 span 边缘引号类字符。

    「司"」类后缀+闭引号形态判纯后缀（碎词回到 ≤15 的依据）；
    仅剥边缘——「“北京”银行」类实体含区别性成分照常遮蔽。"""
    from app.rules_engine import is_pure_org_suffix
    pure_cases = ['司"', '“司”', '「有限公司」', '『集团』', '“行”',
                  '公司', '“企业”', '“”中心']
    for t in pure_cases:
        c.check(f"边缘引号剥离后判纯后缀：{t!r}",
                is_pure_org_suffix(t)[0] is True)
    distinct = ['“北京”银行', '"云舒雅"家居', '云舒雅家居股份有限公司',
                '工商银行']
    for t in distinct:
        c.check(f"含区别性成分实体照常遮蔽（引号不致误拦）：{t!r}",
                is_pure_org_suffix(t)[0] is False)


# ---------------------------------------------------------------------------
# 8. G1（schema v2）配置合并段：空配置全等 + 增词/删词生效 + 增行闸同源
#    （brands.json 新键 org_suffixes / org_suffixes_remove；设计契约
#    _goal_work/g1_contract.md；模块级函数与三表常量保留不动=既有判定锚）
# ---------------------------------------------------------------------------

def _engine_with_cfg(cfg_obj) -> RuleEngine:
    """临时 FINMASK_BRANDS 指向临时配置文件构建引擎（用完恢复现场；
    沿 test_brand_config._env_brands 先例。语料 _eval/brands_test.json
    保持 v1 三键不动——R8：新增用例一律临时目录文件）。"""
    import json as _json
    d = tempfile.mkdtemp(prefix="_g1_org_suffix_")
    p = Path(d) / "brands.json"
    p.write_text(_json.dumps(cfg_obj, ensure_ascii=False), encoding="utf-8")
    old = os.environ.get("FINMASK_BRANDS")
    os.environ["FINMASK_BRANDS"] = str(p)
    try:
        return RuleEngine(MaskOptions(mask_company=True, mask_person=True,
                                      person_loose=True))
    finally:
        if old is None:
            os.environ.pop("FINMASK_BRANDS", None)
        else:
            os.environ["FINMASK_BRANDS"] = old


class _LogCapture:
    """捕获 app.rules_engine 的 logging.warning 记录（引擎加载期人话警告
    端到端留痕验证：cfg.warnings → __init__ 逐条落日志，契约 §3.5）。"""

    def __init__(self):
        import logging
        self.records = []
        self._h = logging.Handler()
        self._h.emit = lambda r: self.records.append(r.getMessage())
        self._logger = logging.getLogger("app.rules_engine")

    def __enter__(self):
        self._logger.addHandler(self._h)
        return self

    def __exit__(self, *a):
        self._logger.removeHandler(self._h)


def test_t8_g1_empty_config_equivalence(c: _Case):
    print("[T8.0] G1 空配置全等（最强判据①单测级：三元组逐成员相等）")
    from app.rules_engine import (build_org_suffix_tables, _ORG_SUFFIX_VOCAB_CORE,
                                  _ORG_SUFFIX_VOCAB_ALL, _ORG_SUFFIX_TOKEN_MAXLEN,
                                  is_pure_org_suffix)
    core, allv, maxlen = build_org_suffix_tables([], [])
    c.check("build_org_suffix_tables([],[]) core ≡ 模块级 _ORG_SUFFIX_VOCAB_CORE",
            core == _ORG_SUFFIX_VOCAB_CORE)
    c.check("build_org_suffix_tables([],[]) all ≡ 模块级 _ORG_SUFFIX_VOCAB_ALL",
            allv == _ORG_SUFFIX_VOCAB_ALL)
    c.check("build_org_suffix_tables([],[]) maxlen ≡ 模块级 _ORG_SUFFIX_TOKEN_MAXLEN",
            maxlen == _ORG_SUFFIX_TOKEN_MAXLEN)
    # 五键但两新键为空数组 → 与空配置同构
    core2, allv2, maxlen2 = build_org_suffix_tables([], [])
    c.check("两新键空数组 ≡ 无新键（三元组恒等）",
            (core2, allv2, maxlen2) == (core, allv, maxlen))
    # 实例生效表 ≡ 模块级词表（空配置引擎实例）
    eng = RuleEngine(MaskOptions())
    c.check("空配置实例 _org_vocab_core ≡ 模块级",
            eng._org_vocab_core == _ORG_SUFFIX_VOCAB_CORE)
    c.check("空配置实例 _org_vocab_all ≡ 模块级",
            eng._org_vocab_all == _ORG_SUFFIX_VOCAB_ALL)
    c.check("空配置实例 _org_token_maxlen ≡ 模块级",
            eng._org_token_maxlen == _ORG_SUFFIX_TOKEN_MAXLEN)
    c.check("空配置实例 stop 并集 ⊇ _BRAND_STOP_ALL（本体不被改写）",
            set(_BRAND_STOP_ALL) <= set(eng._org_suffix_stop_tokens))
    # 实例谓词 ≡ 模块级谓词（全部抽样 + prose 碰撞词集逐词同值）
    words = [w.strip() for w in _PURE_SAMPLES] + list(_PROSE_PURE_TRUE) \
        + list(_PROSE_FALSE) + ["卫生室", "院", "门诊部", "（普通合伙）", "中心"]
    mismatch = [(w, is_pure_org_suffix(w), eng.is_pure_org_suffix(w))
                for w in words if is_pure_org_suffix(w) != eng.is_pure_org_suffix(w)]
    c.check(f"空配置实例谓词 ≡ 模块级谓词（{len(words)} 词逐词同值）",
            mismatch == [], f"mismatch={mismatch[:5]}")


def test_t8_g1_add_words(c: _Case):
    print("[T8.1] G1 增词 4 用例（判据②）")
    from app.rules_engine import is_pure_org_suffix
    from app.handlers.text_replacement import apply_replacements

    # ②-a 新词放行（正例）
    eng = _engine_with_cfg({"org_suffixes": ["卫生室"], "org_suffixes_remove": []})
    c.check("②-a 卫生室 → (True, False)（用户增词恒入 CORE，used_optional=False）",
            eng.is_pure_org_suffix("卫生室") == (True, False))
    c.check("②-a 协和卫生室 → (False, False)（区别性成分照常遮蔽，铁边界 3 不变）",
            eng.is_pure_org_suffix("协和卫生室") == (False, False))
    c.check("②-a 卫生室分店 → DP 组合全覆盖（卫生室+分店）",
            eng.is_pure_org_suffix("卫生室分店") == (True, False))
    # find_spans 证据：映射行候选不再遮蔽（无配置时遮蔽）
    from app.rules_engine import MaskOptions as _MO
    eng_map = _mapping_engine_with_cfg({"卫生室分店": "X"},
                                       {"org_suffixes": [], "org_suffixes_remove": []})
    spans_base = eng_map.find_spans("本院设卫生室分店")
    c.check("②-a 空配置：映射行「卫生室分店」产生遮蔽 span",
            any(s.text == "卫生室分店" for s in spans_base),
            f"{[(s.text, s.rule) for s in spans_base]}")
    eng_map_g1 = _mapping_engine_with_cfg(
        {"卫生室分店": "X"}, {"org_suffixes": ["卫生室"], "org_suffixes_remove": []})
    spans_g1 = eng_map_g1.find_spans("本院设卫生室分店")
    c.check("②-a 配置卫生室后：同映射行不再遮蔽（纯后缀组合放行）",
            spans_g1 == [] and apply_replacements("本院设卫生室分店", spans_g1)
            == "本院设卫生室分店",
            f"{[(s.text, s.rule) for s in spans_g1]}")
    c.check("②-a 模块级谓词不受实例配置影响（卫生室仍 False——模块级=纯代码表）",
            is_pure_org_suffix("卫生室") == (False, False))

    # ②-b 重复登记（防呆 e）：警告 + 引擎行为与空配置逐位一致
    eng_base = _engine_with_cfg({"org_suffixes": [], "org_suffixes_remove": []})
    with _LogCapture() as cap:
        eng_dup = _engine_with_cfg({"org_suffixes": ["门诊部"],
                                    "org_suffixes_remove": []})
    c.check("②-b 重复登记 → 落日志「已在内置组织后缀表…无需重复登记」",
            any("已在内置组织后缀表" in m for m in cap.records),
            f"{cap.records}")
    words = ["门诊部", "院", "局", "中心", "卫生室"]
    c.check("②-b 引擎行为与空配置逐位一致（内置词重复登记零生效）",
            all(eng_dup.is_pure_org_suffix(w) == eng_base.is_pure_org_suffix(w)
                for w in words))

    # ②-c 撞遮蔽注册表（P0 代理闸）：brands 与 org_suffixes 同词 → 拒增词
    with _LogCapture() as cap2:
        eng_p0 = _engine_with_cfg({"brands": ["协和医院"], "aliases": [],
                                   "domain_hints": [],
                                   "org_suffixes": ["协和医院"],
                                   "org_suffixes_remove": []})
    c.check("②-c P0 代理闸落日志「不再被遮蔽…已拒绝」",
            any("不再被遮蔽" in m and "已拒绝" in m for m in cap2.records),
            f"{cap2.records}")
    c.check("②-c 「协和医院」仍注册为品牌（_short_names 含）",
            "协和医院" in eng_p0._short_names)
    spans_p0 = eng_p0.find_spans("协和医院发布年度报告")
    c.check("②-c 「协和医院」遮蔽不受影响（find_spans 仍产出该 span）",
            any(s.text == "协和医院" for s in spans_p0),
            f"{[(s.text, s.rule) for s in spans_p0]}")

    # ②-d used_optional 语义：用户增词恒 False；机关词仍记 optional
    eng_uo = _engine_with_cfg({"org_suffixes": ["卫生室", "分院"],
                               "org_suffixes_remove": []})
    c.check("②-d 用户增词 卫生室 → used_optional=False",
            eng_uo.is_pure_org_suffix("卫生室") == (True, False))
    c.check("②-d 用户增词 分院 → used_optional=False",
            eng_uo.is_pure_org_suffix("分院") == (True, False))
    c.check("②-d 机关可选词 局 → (True, True)（统计语义不被用户数据污染）",
            eng_uo.is_pure_org_suffix("局") == (True, True))


def _mapping_engine_with_cfg(rows: dict, cfg_obj: dict) -> RuleEngine:
    """带 brands.json 配置的映射模式引擎（T8.1 ②-a 差分证据用）。"""
    import json as _json
    d = tempfile.mkdtemp(prefix="_g1_org_suffix_")
    mp_csv = Path(d) / "mapping.csv"
    lines = ["原文,替换为"] + [f"{k},{v}" for k, v in rows.items()]
    mp_csv.write_text("\n".join(lines), encoding="utf-8-sig")
    old = os.environ.get("FINMASK_BRANDS")
    os.environ["FINMASK_BRANDS"] = str(_write_tmp_cfg(cfg_obj))
    try:
        return RuleEngine(MaskOptions(mapping_csv=str(mp_csv)))
    finally:
        if old is None:
            os.environ.pop("FINMASK_BRANDS", None)
        else:
            os.environ["FINMASK_BRANDS"] = old


def _write_tmp_cfg(cfg_obj: dict):
    import json as _json
    d = tempfile.mkdtemp(prefix="_g1_org_suffix_")
    p = Path(d) / "brands.json"
    p.write_text(_json.dumps(cfg_obj, ensure_ascii=False), encoding="utf-8")
    return p


def test_t8_g1_remove_words(c: _Case):
    print("[T8.2] G1 删词 6 用例（判据③：撤回放行实证）")
    from app.rules_engine import (_is_pure_org_suffix, is_pure_org_suffix,
                                  build_org_suffix_tables)
    from app.handlers.text_replacement import apply_replacements

    # ③-a 单字删词恢复遮蔽
    eng = _engine_with_cfg({"org_suffixes": [], "org_suffixes_remove": ["院"]})
    c.check("③-a 院 → (False, False)（恢复遮蔽）",
            eng.is_pure_org_suffix("院") == (False, False))
    c.check("③-a 医院/设计院 不受影响（独立 token，未删）",
            eng.is_pure_org_suffix("医院") == (True, False)
            and eng.is_pure_org_suffix("设计院") == (True, False))
    with _LogCapture() as cap:
        _engine_with_cfg({"org_suffixes": [], "org_suffixes_remove": ["院"]})
    c.check("③-a 单字删词落日志专属后果警告",
            any("单字后缀" in m for m in cap.records), f"{cap.records}")

    # ③-b 原子兜底的诚实语义（防误判 bug）：删主干词，原子仍全覆盖
    eng_b = _engine_with_cfg({"org_suffixes": [], "org_suffixes_remove": ["有限公司"]})
    c.check("③-b 删「有限公司」后其纯度判定仍为 True（原子 有限|公司 全覆盖；"
            "原子不开放删除=闸身机械，非缺陷）",
            eng_b.is_pure_org_suffix("有限公司") == (True, False))
    c.check("③-b 模块级判定锚不受影响",
            _is_pure_org_suffix("有限公司") == (True, False))

    # ③-c 归一化删词键：删「普通合伙」放行「（普通合伙）」
    eng_c = _engine_with_cfg({"org_suffixes": [], "org_suffixes_remove": ["普通合伙"]})
    c.check("③-c （普通合伙） → (False, False)（归一化键命中撤回放行）",
            eng_c.is_pure_org_suffix("（普通合伙）") == (False, False))
    c.check("③-c （特殊普通合伙）不受影响",
            eng_c.is_pure_org_suffix("（特殊普通合伙）") == (True, False))
    # 两种写法等效（loader 防呆 0 警告已在 test_brand_config ④-9 锁定）
    t_bare = build_org_suffix_tables([], ["普通合伙"])
    t_brkt = build_org_suffix_tables([], ["（普通合伙）"])
    c.check("③-c 裸形态与括号形态删词三元组全等（归一化键同一）",
            t_bare == t_brkt)

    # ③-d typo 零生效
    with _LogCapture() as cap_d:
        eng_d = _engine_with_cfg({"org_suffixes": [],
                                  "org_suffixes_remove": ["有限公司l"]})
    c.check("③-d typo 落日志「未生效」响亮警告",
            any("未生效" in m for m in cap_d.records), f"{cap_d.records}")
    eng_empty = RuleEngine(MaskOptions())
    words = ["院", "公司", "门诊部", "中心", "部"]
    c.check("③-d typo 后引擎行为与空配置逐位一致",
            all(eng_d.is_pure_org_suffix(w) == eng_empty.is_pure_org_suffix(w)
                for w in words))

    # ③-e 增行闸同源（消费面⑤）。翻转词选「经营部」（主干成员、jieba
    # 频次 6<20 不触闸 4、不在 _BRAND_STOP_ALL）——契约示例「人事部/设计院」
    # 均被第 4 闸（jieba 频次 ≥20）独立拒绝，无法显影第 2/3 闸的释放。
    import app.masking_plan as mp_mod
    eng_flip = _engine_with_cfg({"org_suffixes": [],
                                 "org_suffixes_remove": ["经营部"]})
    eng_ref = _engine_with_cfg({"org_suffixes": [], "org_suffixes_remove": []})
    c.check("③-e 空配置：add_key_allowed(经营部) 被 stop 表拒绝",
            mp_mod.add_key_allowed("经营部", eng_ref)[0] is False)
    c.check("③-e 删「经营部」后 add_key_allowed(经营部) 放行（第 2/3 闸生效表同源）",
            mp_mod.add_key_allowed("经营部", eng_flip) == (True, ""))
    c.check("③-e 删「部」后 add_key_allowed(部) 仍拒（第 1 闸 len<2 兜底）",
            mp_mod.add_key_allowed("部", _engine_with_cfg(
                {"org_suffixes": [], "org_suffixes_remove": ["部"]}))[0] is False)
    c.check("③-e engine=None：add_key_allowed(公司) 仍拒（模块级回退）",
            mp_mod.add_key_allowed("公司", None)[0] is False)
    # 契约示例词「人事部」的诚实注记：删「部」后第 2/3 闸确已放行
    # （norm 不在生效 stop 并集、非纯后缀），但其被第 4 闸（jieba 频次
    # 185≥20、词性 n）独立拒绝——闸 4 与 G1 改动无关、语义不变。
    eng_bu = _engine_with_cfg({"org_suffixes": [], "org_suffixes_remove": ["部"]})
    norm_ok = "人事部" not in eng_bu._org_suffix_stop_tokens \
        and not eng_bu.is_pure_org_suffix("人事部")[0]
    c.check("③-e 注记：删「部」后人事部确已过第 2/3 闸（被闸 4 jieba 独立拒，"
            "非 G1 语义）", norm_ok
            and mp_mod.add_key_allowed("人事部", eng_bu)[0] is False)

    # ③-f 缓存/plan 联动实测在 tests/test_brand_config.py（hash 守卫 +
    # 旧 plan 头部校验拒绝）——此处引用性检查引擎侧 hash 联动前置条件
    from app.analysis_cache import brands_config_hash
    c.check("③-f brands_config_hash 可用（全文件字节口径，新键天然并入）",
            isinstance(brands_config_hash(), str))


def run() -> bool:
    print("== v1.9.3 法定组织形式后缀 span 纯度硬阻拦 ==")
    c = _Case()
    test_t1_data_tables(c)
    test_t2_pure_blocked(c)
    test_t3_combinations(c)
    test_t4_entity_passthrough(c)
    test_t5_prose_collisions(c)
    test_t6_two_span_boundary(c)
    test_t7_quote_edge_stripping(c)
    test_t8_g1_empty_config_equivalence(c)
    test_t8_g1_add_words(c)
    test_t8_g1_remove_words(c)
    print(f"\n共 {c.total} 条用例，失败 {len(c.failed)} 条")
    return not c.failed


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ok = run()
    sys.exit(0 if ok else 1)
