# -*- coding: utf-8 -*-
# TODO(开源后续轮): 人名夹具虚构化重构（映射表 §四）
"""v2.0.4 F4'「行首职务标签锚定」通道先红后绿回归（人名漏遮三线治理·B 线）。

契约：_goal_work/v204_contract.md C-B 节（第 0 阶段冻结，本文件只消费不定义）。
语料：共享对抗样本库 tests/_v204_adversarial.py（LINES 17 行，禁另建）——
9 正例（江图名单实录版式及变体）+ 1 对照（既有枚举通道顿号名单）+ 7 对抗。

先红后绿（基线 8b47b9a 探针 _goal_work/v204_b_before_probe.txt）：
- 改前红：正例 4 行缺遮——林晏清（广∉GIVEN_CHARS）、组　长：王某、宋怀瑾
  （双缺）、组  长:宋怀瑾（缺）、沈远（威∉GIVEN_CHARS）；其余 5 行为
  v2.0.3 F3a/b/c 已修复行（保持绿）；
- 核心红例：宋怀瑾在「家/忠」不入 GIVEN_CHARS 前提下经本通道以结构信号
  生成候选（与线 A 词表正交——本通道的核心价值验证点）；
- 8 对抗（含对照行）改前已绿，断言保持：结构词排除优先于标签锚定、
  正文叙述标签行不触发、表格表头不触发；
- 去重契约：唐某/张某 接缝形态经 F3b 镜像重扫回收，同人名同位置仅
  一次遮蔽、不双生成。

运行：python tests/test_f4_label_anchor.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("FINMASK_BRANDS", str(
    Path(__file__).resolve().parents[1] / "_eval" / "brands_test.json"))

from app.handlers.text_replacement import apply_replacements
from app.rules_engine import (GIVEN_CHARS, HIGH_RISK_SURNAMES, SINGLE_SURNAMES,
                              MaskOptions, RuleEngine)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _v204_adversarial import ADVERSARIAL_LINES, LINES, POSITIVE_LINES, build_docx

try:
    import pytest
except ImportError:  # pragma: no cover - 直跑环境无 pytest
    pytest = None


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
    # UI 默认口径：人名开 + 宽松档开（与 v2.0.3 WF3 套件一致）
    return RuleEngine(MaskOptions(mask_person=True, person_loose=True))


def _verify_no_wordlist_dependency(eng) -> bool:
    """A 线落地 家/忠/威 后的「不依赖名用字表」等价真验法：临时摘除三字，
    断言宋怀瑾候选仍经结构信号生成（镜像验证），随后恢复原表。"""
    _saved = getattr(eng, "GIVEN_CHARS", GIVEN_CHARS)
    given = set(_saved)
    given.difference_update({"家", "忠", "威"})
    eng.GIVEN_CHARS = frozenset(given)
    try:
        spans = [s for s in eng.find_spans("组\u3000长：王某、宋怀瑾")
                 if s.rule == "person"]
        return "宋怀瑾" in {s.text for s in spans}
    finally:
        if _saved is GIVEN_CHARS:
            eng.GIVEN_CHARS = _saved  # 恢复实例引用与模块表同源
        else:
            eng.GIVEN_CHARS = _saved


def _person_spans(text: str, eng: RuleEngine = None):
    eng = eng or _engine()
    return [s for s in eng.find_spans(text) if s.rule == "person"]


def _mask(text: str, eng: RuleEngine = None) -> str:
    eng = eng or _engine()
    return apply_replacements(text, eng.find_spans(text))


# 9 正例的期望输出（行原文 → 脱敏后全文；括注/职务串不遮）。
# v2.1.5 期望对齐（维护者评审决策 2026-09-24）：原期望为「虚构姓替换/等宽
# 星数」形态（林→黄、宋→方、沈→蒋；X某 渲染 2 星），该机制经净树
# worktree 考古确证已不存在于现行实现——期望按现行口径（保姓+原名长
# 星数）对齐，非功能缺失。
POSITIVE_EXPECT = {
    "林晏清   党委书记": "林**   党委书记",
    "副组长： 唐某   党委纪检委员、纪检监察室主任":
        "副组长： 唐*   党委纪检委员、纪检监察室主任",
    "组\u3000长：王某、宋怀瑾": "组\u3000长：王*、宋**",
    "组  长:宋怀瑾、何某": "组  长:宋**、何*",
    "成  员： 罗某   党委委员、文献流通部副主任":
        "成  员： 罗*   党委委员、文献流通部副主任",
    "颜某   第一支部纪检委员、文献流通部副主任":
        "颜*   第一支部纪检委员、文献流通部副主任",
    "惠某   第三支部纪检委员": "惠*   第三支部纪检委员",
    "张某   第四支部纪检委员": "张*   第四支部纪检委员",
    "沈远   纪检监察室工作人员": "沈*   纪检监察室工作人员",
}

# 对照行（既有枚举通道已正常遮蔽的顿号枚举——防三线改坏既有行为）
CONTROL_LINE = ("组  员：潘某、张伟、巫某、陈某、刘某、肖某、唐某、林某")
CONTROL_NAMES = {"潘某", "张伟", "巫某", "陈某", "刘某", "肖某",
                 "唐某", "林某"}


# ---------------------------------------------------------------------------
# 词表状态（合并态更新：A 线预批落地 家/忠/威 ∈GIVEN_CHARS、广 挂起——
# 依据 _goal_work/v204_a_report.md 预批评审决策；通道正交性的核心验证不变：
# 宋怀瑾经「方」姓+结构信号生成，不依赖家/忠入表，下方端到端用例锁定）
# ---------------------------------------------------------------------------

def test_wordlist_orthogonality(c: _Case):
    print("· 词表合并态：家/忠/威 已按 A 线预批落地；广 挂起；通道核心验证=宋怀瑾不依赖家/忠")
    for ch in ("家", "忠", "威"):
        c.check(f"「{ch}」∈GIVEN_CHARS（A 线预批落地）", ch in GIVEN_CHARS)
    c.check("「广」∉GIVEN_CHARS（挂起，误报 19>3）", "广" not in GIVEN_CHARS)
    c.check("「方」∈SINGLE_SURNAMES", "方" in SINGLE_SURNAMES)
    c.check("「黄」∈SINGLE_SURNAMES", "黄" in SINGLE_SURNAMES)
    c.check("「蒋」∈SINGLE_SURNAMES", "蒋" in SINGLE_SURNAMES)
    c.check("「方」为高危姓", "方" in HIGH_RISK_SURNAMES)
    c.check("「蒋」为高危姓", "蒋" in HIGH_RISK_SURNAMES)


# ---------------------------------------------------------------------------
# 9 正例逐行：候选产出 + 全文脱敏 + 严格档置信（结构信号，非宽松档）
# ---------------------------------------------------------------------------

def test_positive_nine_lines(c: _Case):
    print("· 共享对抗库 9 正例逐行断言候选产出（docx 逐段口径：每行独立喂入）")
    for ln, expect, _ in LINES:
        if not expect:
            continue
        eng = _engine()
        spans = _person_spans(ln, eng)
        texts = {s.text for s in spans}
        c.check(f"[正例] {ln!r} 候选覆盖 {sorted(expect)}",
                expect <= texts, f"got={sorted(texts)}")
        for name in sorted(expect):
            conf = {s.confidence for s in spans if s.text == name}
            label = f"[正例] {ln!r} 「{name}」置信=高（结构证据，走完整流水线）"
            c.check(label, conf == {"高"}, f"conf={conf}")
        want = POSITIVE_EXPECT[ln]
        got = _mask(ln, eng)
        c.check(f"[正例] {ln!r} 全文脱敏", got == want, f"got={got!r}")


# ---------------------------------------------------------------------------
# 核心红例：宋怀瑾（家/忠 双缺）+ 林晏清（广缺）+ 沈远（威缺）经标签/版式
# 结构信号生成候选——改前 4 行缺遮（红），改后全遮（绿）
# ---------------------------------------------------------------------------

def test_core_red_cases(c: _Case):
    print("· 核心红例：无名用字表支撑下的结构信号候选")
    eng = _engine()
    # 宋怀瑾（高危姓，家/忠 双缺）：行首标签「组　长：」锚定
    spans = _person_spans("组\u3000长：王某、宋怀瑾", eng)
    texts = {s.text for s in spans}
    c.check("宋怀瑾 生成候选（组　长：锚定）", "宋怀瑾" in texts,
            f"got={sorted(texts)}")
    c.check("宋怀瑾 候选不经名用字表（摘除家/忠/威镜像验证）",
            _verify_no_wordlist_dependency(eng))
    # 宋怀瑾：半角冒号+标签内嵌空格变体
    texts2 = {s.text for s in _person_spans("组  长:宋怀瑾、何某", eng)}
    c.check("宋怀瑾 生成候选（组  长: 锚定）", "宋怀瑾" in texts2,
            f"got={sorted(texts2)}")
    # 林晏清（广缺）：无标签名单行（人名+3 空格+职务括注）
    texts3 = {s.text for s in _person_spans("林晏清   党委书记", eng)}
    c.check("林晏清 生成候选（职务括注版式锚定）", "林晏清" in texts3,
            f"got={sorted(texts3)}")
    # 沈远（高危姓，威缺）：同上
    texts4 = {s.text for s in _person_spans("沈远   纪检监察室工作人员", eng)}
    c.check("沈远 生成候选（职务括注版式锚定）", "沈远" in texts4,
            f"got={sorted(texts4)}")


# ---------------------------------------------------------------------------
# 8 对抗：不遮、不触发（多数改前已绿，保持）+ 对照行防改坏
# ---------------------------------------------------------------------------

def test_adversarial_and_control(c: _Case):
    print("· 共享对抗库 8 对抗/对照行断言（结构词排除 > 标签锚定）")
    for ln, expect, forbid in LINES:
        if expect or not forbid:
            continue
        eng = _engine()
        spans = eng.find_spans(ln)
        texts = {s.text for s in spans}
        c.check(f"[对抗] {ln!r} 禁遮集不命中", not (forbid & texts),
                f"hit={sorted(forbid & texts)}")
        got = _mask(ln, eng)
        c.check(f"[对抗] {ln!r} 整行原样", got == ln, f"got={got!r}")
    # 对照行：既有枚举通道 8 人全遮不回退（本通道不吞不抢——「组  员」不在
    # 标签封闭清单内，仍由枚举证据覆盖）
    eng = _engine()
    spans = _person_spans(CONTROL_LINE, eng)
    texts = {s.text for s in spans}
    c.check("[对照] 顿号枚举 8 人仍全遮（既有通道不回退）",
            CONTROL_NAMES <= texts, f"got={sorted(texts)}")
    c.check("[对照] 「组  员」不被当标签遮蔽",
            not any(s.text.startswith("组") and "员" in s.text for s in spans))


# ---------------------------------------------------------------------------
# 判别规则专项：结构词排除优先于标签锚定（契约 C-B 关键）
# ---------------------------------------------------------------------------

def test_struct_word_exclusion(c: _Case):
    print("· 结构词排除专项：标签后首单元含 室/部/组/等/名单/单位/如下 → 整行不产生人名候选")
    eng = _engine()
    for ln in ("成  员：纪检监察室工作人员、支部纪检委员等",   # 契约 N3 实录形态
               "组　长：办公室成员、监察室主任",               # 首单元含室
               "成　员：成员名单如下",                          # 首单元含名单/如下
               "组长：各单位负责人"):                           # 首单元含单位
        spans = _person_spans(ln, eng)
        c.check(f"[结构词] {ln!r} 零人名候选", not spans,
                f"got={[(s.text, s.rule) for s in spans]}")
        c.check(f"[结构词] {ln!r} 整行原样", _mask(ln, eng) == ln)


# ---------------------------------------------------------------------------
# OCR 拼接行口径：多行 \n 连投（pdf_handler 行拼接形态），逐行独立判定
# ---------------------------------------------------------------------------

def test_multiline_joined_rows(c: _Case):
    print("· OCR 行拼接口径：\\n 连投多行，标签行/括注行/结构词行/叙述行互不串扰")
    eng = _engine()
    text = ("江城图书馆内部审计小组构成如下：\n"
            "组\u3000长：王某、宋怀瑾\n"
            "林晏清   党委书记\n"
            "副组长： 唐某   党委纪检委员、纪检监察室主任\n"
            "成  员：纪检监察室工作人员、支部纪检委员等\n"
            "组长汇报了工作")
    spans = _person_spans(text, eng)
    texts = {s.text for s in spans}
    for name in ("王某", "宋怀瑾", "林晏清", "唐某"):
        c.check(f"[连投] 「{name}」被遮", name in texts, f"got={sorted(texts)}")
    got = _mask(text, eng)
    for frag in ("成  员：纪检监察室工作人员、支部纪检委员等", "组长汇报了工作"):
        c.check(f"[连投] 行 {frag!r} 原样", frag in got)
    c.check("[连投] 王某、宋怀瑾 全文替换",
            "组\u3000长：王*、宋**" in got, f"got={got!r}")


# ---------------------------------------------------------------------------
# 去重契约：接缝形态同人名仅一次遮蔽、不双生成（F3b 协同）
# ---------------------------------------------------------------------------

def test_dedup_gap_names(c: _Case):
    print("· 去重契约：唐某/张某 接缝形态仅一次遮蔽，通道不双生成")
    eng = _engine()
    for ln, name in (("副组长： 唐某   党委纪检委员、纪检监察室主任", "唐某"),
                     ("张某   第四支部纪检委员", "张某")):
        spans = [s for s in _person_spans(ln, eng) if s.text == name]
        c.check(f"[去重] {name!r} 恰一个 span", len(spans) == 1,
                f"got={[(s.start, s.end, s.text) for s in spans]}")
        if spans:
            s = spans[0]
            # v2.1.5 期望对齐：原「区间含接缝空格（合并串替换）」为已
            # 消失机制残留——现行为词面 span（text 无空格、无星号）。
            c.check(f"[去重] {name!r} span 为词面形态（无空格合并串）",
                    " " not in s.text and "*" not in s.text,
                    f"text={s.text!r}")
    got = _mask("副组长： 唐某   党委纪检委员、纪检监察室主任", eng)
    c.check("[去重] 唐某 替换为合并串 唐*", "唐*" in got, f"got={got!r}")


# ---------------------------------------------------------------------------
# 端到端：合成 docx（docx_handler 逐段喂入口径）
# ---------------------------------------------------------------------------

def test_docx_end_to_end(c: _Case):
    print("· 端到端：共享对抗库 build_docx 合成 docx，逐段 find_spans 断言")
    import io
    from docx import Document

    tmp = Path(__file__).resolve().parents[1] / "_goal_work" / "v204_b_输出"
    tmp.mkdir(parents=True, exist_ok=True)
    docx_path = tmp / "v204_b_adversarial_e2e.docx"
    build_docx(docx_path)
    doc = Document(str(docx_path))
    eng = _engine()
    paras = [p.text for p in doc.paragraphs]
    c.check("[端到端] 段落数=17", len(paras) == len(LINES),
            f"got={len(paras)}")
    pos_missing, adv_hit = [], []
    for para in paras:
        spans = _person_spans(para, eng)
        texts = {s.text for s in spans}
        for ln, expect, forbid in LINES:
            if para != ln:
                continue
            if expect and not (expect <= texts):
                pos_missing.append((ln, sorted(expect - texts)))
            if forbid and (forbid & texts):
                adv_hit.append((ln, sorted(forbid & texts)))
    c.check("[端到端] 9 正例全部产出候选", not pos_missing, f"miss={pos_missing}")
    c.check("[端到端] 对抗行零命中", not adv_hit, f"hit={adv_hit}")


def main() -> bool:
    print("== v2.0.4 F4' 行首职务标签锚定通道（先红后绿）测试 ==")
    c = _Case()
    test_wordlist_orthogonality(c)
    test_positive_nine_lines(c)
    test_core_red_cases(c)
    test_adversarial_and_control(c)
    test_struct_word_exclusion(c)
    test_multiline_joined_rows(c)
    test_dedup_gap_names(c)
    test_docx_end_to_end(c)
    print(f"\n共 {c.total} 条检查，失败 {len(c.failed)} 条")
    return not c.failed


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(0 if main() else 1)
