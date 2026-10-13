# -*- coding: utf-8 -*-
# TODO(开源后续轮): 人名夹具虚构化重构（映射表 §四）
"""v2.0.4 F4「竖排空格对齐名单版式广义通道」先红后绿回归（人名漏遮三线治理·C 线）。

契约：_goal_work/v204_contract.md C-C 节（冻结，本文件只消费不定义）。
语料：共享对抗样本库 tests/_v204_adversarial.py（LINES 17 行，禁另建）+ 本套件
构造的「纯名单无职务括注」新形态（契约 C-C 增量场景：竖排 ≥3 行短 CJK 串 +
行内空格对齐，行尾无闭集职务词）。构造行人名全部取自共享库名单名册（示意化
纪律：林晏清/罗某/沈远/唐某/张某 已为回归资产），描述串为非敏感虚构职务。

先红后绿（基线 5189653 探针，即 A+B 合流态）：
- 结构识别红：`_layout_list_spans` 未实现 → 版式检出断言红（本通道的核心
  验证点=「通道应识别版式」的结构识别，非仅遮蔽结果）；
- 遮蔽红：纯名单 3 连行「林晏清/罗某/沈远」中林晏清 改前不遮
  （广∉GIVEN_CHARS，宽松档击杀）；非闭集括注 3 连行（外聘法律顾问/特邀
  稽核专员/后勤联络员——均不在 _JOB_ANNOTATION_WORDS 闭集）三人全不遮；
- 对抗保持绿（改前已绿）：目录页码行/条款序号行/表头词行/不足 3 行——
  纯名单无括注形态在现行代码下「无通道触发」天然不遮，对抗方向改前即绿，
  本套件以「版式不检出」结构断言+整行原样双向锁定。

运行：python tests/test_f4_layout_list.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("FINMASK_BRANDS", str(
    Path(__file__).resolve().parents[1] / "_eval" / "brands_test.json"))

from app.handlers.text_replacement import apply_replacements
import app.rules_engine as _re_mod
from app.rules_engine import MaskOptions, RuleEngine

# C 线通道入口（实现前为 None → 结构断言显式红，套件其余部分照常跑出改前状态）
_layout_list_spans = getattr(_re_mod, "_layout_list_spans", None)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _v204_adversarial import ADVERSARIAL_LINES, LINES, POSITIVE_LINES


class _Case:
    """与既有套件同构的用例收集器（conftest `c` fixture 按
    request.module._Case 取本类；python 直跑时由 main() 直接驱动）。"""

    def __init__(self):
        self.failed = []
        self.total = 0

    def check(self, name, cond, detail=""):
        self.total += 1
        if cond:
            print(f"  PASS {name}")
        else:
            self.failed.append(f"{name}: {detail}")
            print(f"  ✘ {name} {detail}")


def _engine() -> RuleEngine:
    # UI 默认口径：人名开 + 宽松档开（与 B 线套件一致）
    return RuleEngine(MaskOptions(mask_person=True, person_loose=True))


def _person_spans(text: str, eng: RuleEngine = None):
    eng = eng or _engine()
    return [s for s in eng.find_spans(text) if s.rule == "person"]


def _mask(text: str, eng: RuleEngine = None) -> str:
    eng = eng or _engine()
    return apply_replacements(text, eng.find_spans(text))


# ---------------------------------------------------------------------------
# 构造语料（契约 C-C 增量场景：无行首标签、行尾无闭集职务词）
# ---------------------------------------------------------------------------

# 纯名单：共享库名册三人逐行，无任何职务括注（江图名单摘去括注的版式形态）
PURE_LIST = "林晏清\n罗某\n沈远"
PURE_LIST_NAMES = {"林晏清", "罗某", "沈远"}

# 接缝形态入窗：双字名内嵌空格的纯名单行（F3b 接缝 × F4 版式复合）
PURE_SEAM_LIST = "唐某\n张某\n林晏清"

# 非闭集描述括注：括注均不在 _JOB_ANNOTATION_WORDS 闭集（B 通道不管辖）
DESC_LIST = ("赵子航   外聘法律顾问\n"
             "钱学若   特邀稽核专员\n"
             "孙敏行   后勤联络员")
DESC_LIST_NAMES = {"赵子航", "钱学若", "孙敏行"}

# 混合页：行首标签行（B 管辖）+ 纯名单行（C 管辖）+ 括注行连投
MIXED_PAGE = ("江城图书馆内部审计小组名单\n"
              "组\u3000长：宋怀瑾、王某\n"
              "林晏清\n"
              "罗某\n"
              "沈远\n"
              "（本名单自发布之日起执行）")

# B∪C 重叠窗：三行括注均为闭集职务词（B 通道已锚定），且都是纯 CJK 尾
# （C 窗口同样可解析）→ 两通道 span 全同，去重契约=同人名同位置恰一 span
BC_OVERLAP = ("林晏清   党委书记\n"
              "罗某   党委委员\n"
              "沈远   纪检监察室工作人员")

# N2 对抗页构造（行间形态不一致=非名单版式）：
TOC_PAGES = (
    # 目录行带页码数字尾（宽空格对齐形态）
    "第一章　　总则　　　　1\n第二章　　财务管理　　　　5\n第三章　　资产管理　　　　9",
    # 条款序号行（（一）（二）（三））
    "（一）加强支出审批控制\n（二）规范费用报销流程\n（三）严格印章使用管理",
    # 目录残余形态：无页码但单元为「第X章」结构词（单元级守卫）
    "第一章　　总则\n第二章　　财务管理\n第三章　　资产管理",
    # 表头词行（姓名/职务 类结构词守卫）+ 两行数据：窗口不成形
    "姓　名　职　务\n张三　　会计\n李四　　出纳",
    # 表尾词行：单位/合计/金额（结构词守卫，防表尾栏位窗）
    "单位\n金额\n合计",
    # 不足 3 行：不构成版式窗
    "林晏清\n罗某",
)


# ---------------------------------------------------------------------------
# 共享对抗库全量（闸门加倍条款第一项：共享对抗库全过）
# ---------------------------------------------------------------------------

def test_shared_adversarial_library(c: _Case):
    print("· 共享对抗库 LINES 17 行全量（正例候选+对抗零命中+脱敏逐字）")
    c.check("共享库规模 17 行", len(LINES) == 17, f"got={len(LINES)}")
    for ln, expect, forbid in LINES:
        eng = _engine()
        texts = {s.text for s in eng.find_spans(ln)}
        if expect:
            c.check(f"[库正例] {ln!r} 候选覆盖", expect <= texts,
                    f"got={sorted(texts)}")
            want = apply_replacements(ln, eng.find_spans(ln))
            c.check(f"[库正例] {ln!r} 脱敏后括注无损",
                    all(nm not in want for nm in expect), f"got={want!r}")
        else:
            c.check(f"[库对抗] {ln!r} 禁遮集不命中", not (forbid & texts),
                    f"hit={sorted(forbid & texts)}")


# ---------------------------------------------------------------------------
# 结构识别（通道核心验证点：版式检出与否——改前红，改后绿）
# ---------------------------------------------------------------------------

def test_layout_structure_detection(c: _Case):
    print("· 版式结构识别：纯名单/非闭集括注/接缝行 ≥3 连行 → 窗口检出+人名锚定")
    if _layout_list_spans is None:
        c.check("通道函数 _layout_list_spans 已实现", False, "未实现（改前红态）")
        return
    for label, page, names in (("纯名单", PURE_LIST, PURE_LIST_NAMES),
                               ("非闭集括注", DESC_LIST, DESC_LIST_NAMES),
                               ("接缝纯名单", PURE_SEAM_LIST, {"唐某", "张某"})):
        spans, rows = _layout_list_spans(page)
        c.check(f"[结构] {label} 检出 ≥3 行版式窗", rows >= 3, f"rows={rows}")
        got = {page[a:b] for a, b in spans}
        c.check(f"[结构] {label} 人名单元锚定 {sorted(names)}", names <= got,
                f"got={sorted(got)}")
    # 混合页：标签行打断 → 纯名单 3 连行恰成窗
    spans, rows = _layout_list_spans(MIXED_PAGE)
    c.check("[结构] 混合页纯名单 3 连行成窗", rows >= 3, f"rows={rows}")
    got = {MIXED_PAGE[a:b] for a, b in spans}
    c.check("[结构] 混合页锚定不含标签行人名（B 管辖不双锚）",
            not ({"宋怀瑾", "王某"} & got), f"got={sorted(got)}")


def test_layout_masking_end_to_end(c: _Case):
    print("· 端到端遮蔽：纯名单/非闭集括注页人名全遮+括注无损+置信=高（结构证据）")
    eng = _engine()
    # 纯名单 3 连行：林晏清 改前不遮（红），改后三人全遮且置信=高
    spans = _person_spans(PURE_LIST, eng)
    texts = {s.text for s in spans}
    c.check("[端到端] 纯名单三人全遮", PURE_LIST_NAMES <= texts,
            f"got={sorted(texts)}")
    conf = {s.confidence for s in spans if s.text in PURE_LIST_NAMES}
    c.check("[端到端] 纯名单置信=高（版式结构证据，非宽松档）", conf == {"高"},
            f"conf={conf}")
    # v2.1.5 期望对齐（维护者评审决策 2026-09-24）：原「虚构姓/等宽星数」
    # 机制已不存在，按现行保姓+原名长星数口径对齐。
    want = "林**\n罗*\n沈*"
    got = _mask(PURE_LIST, eng)
    c.check("[端到端] 纯名单逐行替换", got == want, f"got={got!r}")
    # 非闭集括注页：三人全遮（B 闭集不管辖），括注原样无损
    spans2 = _person_spans(DESC_LIST, eng)
    texts2 = {s.text for s in spans2}
    c.check("[端到端] 非闭集括注三人全遮", DESC_LIST_NAMES <= texts2,
            f"got={sorted(texts2)}")
    got2 = _mask(DESC_LIST, eng)
    for tail in ("外聘法律顾问", "特邀稽核专员", "后勤联络员"):
        c.check(f"[端到端] 括注 {tail!r} 原样无损", tail in got2, f"got={got2!r}")
    # 接缝纯名单：词面 span 恰一（去重）+替换 唐*/张*。
    # v2.1.5 期望对齐：原「span 区间含接缝空格（合并串）」为已消失机制
    # 残留——现行为词面 span（text 无空格），断言改锁词面形态恰一。
    spans3 = [s for s in _person_spans(PURE_SEAM_LIST, eng)
              if s.text in ("唐某", "张某")]
    c.check("[端到端] 接缝名恰一词面 span（去重）",
            len(spans3) == 2 and all(" " not in s.text for s in spans3),
            f"got={[(s.text, s.start) for s in spans3]}")
    got3 = _mask(PURE_SEAM_LIST, eng)
    c.check("[端到端] 接缝名替换 唐*/张*", "唐*" in got3 and "张*" in got3,
            f"got={got3!r}")


def test_multiline_mixed_page(c: _Case):
    print("· 混合页连投：标签行（B）+纯名单行（C）+括注行互不串扰")
    eng = _engine()
    spans = _person_spans(MIXED_PAGE, eng)
    texts = {s.text for s in spans}
    for name in ("宋怀瑾", "王某", "林晏清", "罗某", "沈远"):
        c.check(f"[混合页] {name} 被遮", name in texts, f"got={sorted(texts)}")
    got = _mask(MIXED_PAGE, eng)
    c.check("[混合页] 标签行替换", "组\u3000长：宋**、王*" in got, f"got={got!r}")
    c.check("[混合页] 括注尾行原样", "（本名单自发布之日起执行）" in got,
            f"got={got!r}")


# ---------------------------------------------------------------------------
# N2 对抗（行间形态不一致=非名单版式；改前已绿=保持项，双向锁定）
# ---------------------------------------------------------------------------

def test_n2_adversarial_pages(c: _Case):
    print("· N2 对抗页：目录页码/条款序号/第X章结构词/表头表尾词/不足3行 → 版式不检出")
    if _layout_list_spans is None:
        c.check("通道函数 _layout_list_spans 已实现", False, "未实现（改前红态）")
        return
    for idx, page in enumerate(TOC_PAGES):
        spans, rows = _layout_list_spans(page)
        c.check(f"[N2-{idx}] 版式窗不检出（rows=0）", rows == 0, f"rows={rows}")
        c.check(f"[N2-{idx}] 零锚定 span", not spans, f"got={sorted(spans)}")
    # 整行原样（含既有通道保持断言——C 通道零贡献由上方 rows=0/零 span 锁定）：
    # N2-0/1/2/4 页无人名通道管辖对象 → 整页原样；
    # N2-3 页的张三/李四行是 F4' B 形态（人名+≥2 空格+闭集职务词），基线
    # （A+B 合流态）即遮——非本通道行为，断言其保持；表头行原样；
    # N2-5 页罗某为宽松档既有遮蔽（改前探针即中置信）、林晏清 原样。
    INTACT_PAGES = (0, 1, 2, 4)
    for idx in INTACT_PAGES:
        got = _mask(TOC_PAGES[idx], _engine())
        c.check(f"[N2-{idx}] 逐行原样", got == TOC_PAGES[idx], f"got={got!r}")
    got3 = _mask(TOC_PAGES[3], _engine())
    c.check("[N2-3] 表头行原样（C 不动）",
            got3.startswith("姓\u3000名\u3000职\u3000务\n"), f"got={got3!r}")
    c.check("[N2-3] 张三/李四 行=F4' 既有遮蔽保持",
            got3 == "姓\u3000名\u3000职\u3000务\n张*\u3000\u3000会计\n李*\u3000\u3000出纳",
            f"got={got3!r}")
    # N2-5 页罗某：v2.1.5 维护者评审决策批——「某」入 GIVEN_CHARS 后宽松档
    # 放行恢复（「X某」=合同/判决书高频打码形态，原闸杀为漏报通道）。
    got5 = _mask(TOC_PAGES[5], _engine())
    c.check("[N2-5] 林晏清 原样（通道不触发）", got5.startswith("林晏清\n"),
            f"got={got5!r}")
    c.check("[N2-5] 罗某 宽松档放行（「某」∈GIVEN_CHARS）", "罗*" in got5,
            f"got={got5!r}")


# ---------------------------------------------------------------------------
# 去重契约：B∪C span 全同不双生成；带顿号括注的 B 行打断 C 窗
# ---------------------------------------------------------------------------

def test_dedup_with_label_channel(c: _Case):
    print("· 去重契约：闭集括注行 B 已锚定 → C 并集全同，同人名同位置恰一 span")
    eng = _engine()
    spans = _person_spans(BC_OVERLAP, eng)
    by_text = {}
    for s in spans:
        by_text.setdefault(s.text, []).append(s)
    for name in ("林晏清", "罗某", "沈远"):
        c.check(f"[去重] {name} 恰一 span", len(by_text.get(name, [])) == 1,
                f"got={[(s.start, s.end) for s in by_text.get(name, [])]}")
    conf = {s.confidence for s in spans if s.text in
            ("林晏清", "罗某", "沈远")}
    c.check("[去重] 置信=高", conf == {"高"}, f"conf={conf}")
    # 顿号括注行不是 C 行（含标点）→ 插入窗中打断：C 不新增、B 照常全遮
    trio = ("林晏清   党委书记\n"
            "颜某   第一支部纪检委员、文献流通部副主任\n"
            "罗某   党委委员")
    if _layout_list_spans is not None:
        _, rows = _layout_list_spans(trio)
        c.check("[去重] 顿号行打断窗口（rows=0）", rows == 0, f"rows={rows}")
    spans2 = _person_spans(trio, eng)
    texts2 = {s.text for s in spans2}
    c.check("[去重] 顿号行三人仍全遮（B 通道不回退）",
            {"林晏清", "颜某", "罗某"} <= texts2, f"got={sorted(texts2)}")


# ---------------------------------------------------------------------------
# 观察模式开关（契约 C-C 闸门加倍条款：不达标降级路径的机械验证）
# ---------------------------------------------------------------------------

def test_observation_mode_switch(c: _Case):
    print("· 观察模式：_F4_LAYOUT_MASKING=False → 版式仍检出计数、不产生遮蔽")
    if _layout_list_spans is None:
        c.check("通道函数 _layout_list_spans 已实现", False, "未实现（改前红态）")
        return
    c.check("开关常量 _F4_LAYOUT_MASKING 存在",
            hasattr(_re_mod, "_F4_LAYOUT_MASKING"), "缺失")
    saved = getattr(_re_mod, "_F4_LAYOUT_MASKING", None)
    try:
        _re_mod._F4_LAYOUT_MASKING = False
        # 版式检出照常（观察语义）
        spans, rows = _layout_list_spans(PURE_LIST)
        c.check("[观察] 版式窗仍检出", rows >= 3, f"rows={rows}")
        c.check("[观察] 零锚定 span（不遮蔽）", not spans, f"got={sorted(spans)}")
        # 引擎端到端：林晏清 不因本通道遮蔽（宽松档能力照旧，不回退既有行为）
        eng = _engine()
        texts = {s.text for s in eng.find_spans(PURE_LIST)
                 if s.rule == "person"}
        c.check("[观察] 林晏清 不遮（通道静默）", "林晏清" not in texts,
                f"got={sorted(texts)}")
        # v2.1.5 维护者评审决策批：「某」入 GIVEN_CHARS——宽松档「X某」放行恢复
        c.check("[观察] 罗某 宽松档放行（「某」∈GIVEN_CHARS）", "罗某" in texts,
                f"got={sorted(texts)}")
    finally:
        _re_mod._F4_LAYOUT_MASKING = saved


def main() -> bool:
    print("== v2.0.4 F4 竖排空格对齐名单版式广义通道（先红后绿）测试 ==")
    c = _Case()
    test_shared_adversarial_library(c)
    test_layout_structure_detection(c)
    test_layout_masking_end_to_end(c)
    test_multiline_mixed_page(c)
    test_n2_adversarial_pages(c)
    test_dedup_with_label_channel(c)
    test_observation_mode_switch(c)
    print(f"\n共 {c.total} 条检查，失败 {len(c.failed)} 条")
    return not c.failed


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(0 if main() else 1)
