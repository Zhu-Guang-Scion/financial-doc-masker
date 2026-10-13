# -*- coding: utf-8 -*-
# TODO(开源后续轮): 人名夹具虚构化重构（映射表 §四）
"""v2.0.3 WF3：附件竖排名单人名四缺口 P0 回归（批次 D-示例 诊断修复）。

D3 诊断（_goal_work/guangtu_d3_l2.md）：「内部审计小组成员名单」附件竖排
9 人漏 8，四类机理缺口（全部引擎级 app/rules_engine.py 人名区）：
- D 类×1：SINGLE_SURNAMES 缺「惠」→ F3a 先红后绿；
- C 类×2：双字名内嵌排版空格（唐某/张某）被 _CJK_RUN_RE 切断、候选
  先天不生成 → F3b 候选阶段「短跨度空格归一重试」（镜像重扫完整人名
  流水线，仅回收跨越接缝的候选；不改 _CJK_RUN_RE 对其他通道的语义）；
- B 类×5：宽松档名用字表 GIVEN_CHARS 缺字——本轮按维护者批示只补低频
  「逸/运」（F3c）；高频「广/忠/威」挂账不补（误报证据与移交记录见
  _goal_work/v203_wf3_report.md）。v2.0.4 起 宋怀瑾/林晏清/沈远 三人
  由 F4' 行首职务标签锚定通道以结构信号闭合（词表仍未补字，见
  tests/test_f4_label_anchor.py），对应 语料行 期望更新为遮蔽；
- A 类（名单版式无上下文证据）无独立改动，随 F3b/c 宽松档放行解决。

本套件语料逐段取自 D3 probe_person_gates.txt 的名单段落原文（真实人名
示意化副本），先红（F3a/b/c 各一）后绿；既有闸门防误报断言（merged 候选
仍走完整评分/闸门）同文件固化。

运行：python tests/test_person_list_layout_p0.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 预设依赖用例：品牌预设已外置（与其他回归套件一致）
os.environ.setdefault("FINMASK_BRANDS", str(
    Path(__file__).resolve().parents[1] / "_eval" / "brands_test.json"))

from app.handlers.text_replacement import apply_replacements
from app.rules_engine import MaskOptions, RuleEngine

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
    # UI 默认口径：人名开 + 宽松档开（D3 §2.4 开关=开分支）
    return RuleEngine(MaskOptions(mask_person=True, person_loose=True))


def _mask(text: str, eng: RuleEngine = None) -> str:
    eng = eng or _engine()
    return apply_replacements(text, eng.find_spans(text))


# ---------------------------------------------------------------------------
# F3a：SINGLE_SURNAMES 补「惠」（D 类缺口；语料=名单 P4265 行）
# ---------------------------------------------------------------------------

def test_f3a_surname_hui(c: _Case):
    print("· F3a 姓氏表补「惠」：惠某 名单形态")
    # 语料原行（惠某   第三支部纪检委员）：惠 不在姓氏表 → 候选先天不生成
    got = _mask("惠某   第三支部纪检委员")
    c.check("惠某 被遮为 惠*",
            got == "惠*   第三支部纪检委员", repr(got))
    # 纯宽松档形态（无任何上下文）：v2.1.5 维护者评审决策批——「某」入
    # GIVEN_CHARS（合同/判决书「李某」「王某」高频形态，孤立「X某」
    # 为活漏报通道），宽松档放行恢复；姓表补「惠」+某入表 → 惠*。
    got2 = _mask("惠某")
    c.check("孤立 惠某 宽松档放行（「某」∈GIVEN_CHARS）", got2 == "惠*",
            repr(got2))
    # 防误报守卫：惠 开头的普通词不因补姓误放行（惠及=及属虚词尾字闸）
    got3 = _mask("惠及全体员工")
    c.check("「惠及」不被当人名", got3 == "惠及全体员工", repr(got3))


# ---------------------------------------------------------------------------
# F3b：双字名内嵌排版空格归一（C 类缺口；语料=名单 P4261/P4266 行）
# ---------------------------------------------------------------------------

def test_f3b_gap_space_merge(c: _Case):
    print("· F3b 排版空格归一重试：唐某 / 张某 / 李 四光")
    # 语料原行：双半角空格内嵌（总跨度 4 含空格）→ 宽松档放行
    got = _mask("副组长： 唐某   党委纪检委员、纪检监察室主任")
    c.check("「唐某」被遮（区间含空格）",
            got == "副组长： 唐*   党委纪检委员、纪检监察室主任", repr(got))
    got = _mask("张某   第四支部纪检委员")
    c.check("「张某」被遮", got == "张*   第四支部纪检委员", repr(got))
    # 单个半角空格 + 高危姓（李）：表单字段强证据（score=3）走严格档放行——
    # 证明合并候选走完整评分/闸门（高危姓强证据豁免与主通道同规则）
    got = _mask("姓名：李 四光")
    c.check("「李 四光」表单字段强证据被遮", got == "姓名：李**", repr(got))
    # 单个全角空格 + 低危姓宽松档
    got = _mask("姓名　王　秀英")
    c.check("「王　秀英」全角空格被遮", got == "姓名　王**", repr(got))

    print("· F3b 误报自查：合并候选必须过全部闸门（不绕闸直遮）")
    # 名用字闸：合并串「李州」的 州 ∉ GIVEN_CHARS → 击杀（与无空格「李州」同判）
    got = _mask("李  州")
    c.check("「李  州」名用字闸击杀不遮", got == "李  州", repr(got))
    # 接缝两侧必须紧邻非汉字边界：长词内部空格不合并（张三丰|李四光）
    got = _mask("张三丰 李四光")
    c.check("「张三丰 李四光」不合并不遮", got == "张三丰 李四光", repr(got))
    # 总跨度 ≤4（含空格）：两侧 2 字段（跨度 5）不合并不遮
    got = _mask("会计 人员名单已公示")
    c.check("「会计 人员」超短跨度约束不遮", got == "会计 人员名单已公示", repr(got))
    # 高危姓 + 职务词接缝：「组长」合并串被 名用字闸（长∉GIVEN）+词条闸双杀；
    # 宋怀瑾 由 v2.0.4 F4' 行首标签锚定以结构信号遮蔽（家/忠 仍未入表），
    # 「馆  长」括注不遮（F3b 接缝合并候选被姓氏闸天然排除）
    got = _mask("组　长： 宋怀瑾   馆  长")
    c.check("「组　长」「馆  长」接缝不产生误报（宋怀瑾 经 F4' 遮蔽）",
            got == "组　长： 宋**   馆  长",
            repr(got))


# ---------------------------------------------------------------------------
# F3c：GIVEN_CHARS 补低频「逸/运」（B 类缺口本轮批次；广/忠/威挂账不测绿）
# ---------------------------------------------------------------------------

def test_f3c_given_chars_yi_yun(c: _Case):
    print("· F3c 名用字表补「逸/运」：罗某 / 颜某 名单形态")
    got = _mask("成  员： 罗某   党委委员、文献流通部副主任")
    c.check("罗某 被遮为 罗*",
            got == "成  员： 罗*   党委委员、文献流通部副主任", repr(got))
    got = _mask("颜某   第一支部纪检委员、文献流通部副主任")
    c.check("颜某 被遮为 颜*",
            got == "颜*   第一支部纪检委员、文献流通部副主任", repr(got))

    print("· F3c 误报自查：加字后普通词形态仍被既有闸门拦截")
    got = _mask("改革应运而生，成效显著")
    c.check("「应运而生」汉字邻接闸拦截", got == "改革应运而生，成效显著", repr(got))
    got = _mask("这里的环境很安逸")
    c.check("「安逸」汉字邻接闸拦截", got == "这里的环境很安逸", repr(got))


# ---------------------------------------------------------------------------
# 整名单集成：9 行语料逐行期望（5 行本轮修复 + 3 行挂账原样 + 1 行对照）
# ---------------------------------------------------------------------------

# (行号, 原文, 期望输出, 说明)。期望与原文一致者为挂账行（广/忠/威 缺字，
# 本轮按批示不补；移交主 agent task_queue 登记）。
# v2.1.5 期望对齐（维护者评审决策 2026-09-24）：原「虚构姓/等宽星数」形态
# （方**/黄**/蒋**、X某 2 星）机制已不存在，按现行保姓+原名长星数对齐。
CORPUS_LINES = [
    ("P4259", "组　长： 宋怀瑾   馆  长",
     "组　长： 宋**   馆  长",
     "F4' 修复（v2.0.4 标签锚定；家/忠 未入表，结构信号放行）"),
    ("P4260", "林晏清   党委书记",
     "林**   党委书记",
     "F4' 修复（无标签名单行职务括注锚定；广 未入表）"),
    ("P4261", "副组长： 唐某   党委纪检委员、纪检监察室主任",
     "副组长： 唐*   党委纪检委员、纪检监察室主任", "F3b 修复"),
    ("P4262", "成  员： 罗某   党委委员、文献流通部副主任",
     "成  员： 罗*   党委委员、文献流通部副主任", "F3c 修复（逸）"),
    ("P4263", "颜某   第一支部纪检委员、文献流通部副主任",
     "颜*   第一支部纪检委员、文献流通部副主任", "F3c 修复（运）"),
    ("P4264", "陈智颖   第二支部纪检委员、信息咨询部副主任",
     "陈**   第二支部纪检委员、信息咨询部副主任", "对照：修复前即遮"),
    ("P4265", "惠某   第三支部纪检委员",
     "惠*   第三支部纪检委员", "F3a 修复"),
    ("P4266", "张某   第四支部纪检委员",
     "张*   第四支部纪检委员", "F3b 修复"),
    ("P4267", "沈远   纪检监察室工作人员",
     "沈*   纪检监察室工作人员",
     "F4' 修复（无标签名单行职务括注锚定；威 未入表）"),
]


def test_corpus_nine_lines(c: _Case):
    print("· 名单 9 行集成（D3 语料逐段副本；同一引擎逐行喂入=docx 逐段口径）")
    eng = _engine()
    for pno, src, want, note in CORPUS_LINES:
        got = _mask(src, eng)
        c.check(f"[{pno}] {note}", got == want, f"got={got!r}")


def main() -> bool:
    print("== v2.0.3 WF3 人名四缺口 P0（名单版式）测试 ==")
    c = _Case()
    test_f3a_surname_hui(c)
    test_f3b_gap_space_merge(c)
    test_f3c_given_chars_yi_yun(c)
    test_corpus_nine_lines(c)
    print(f"\n共 {c.total} 条检查，失败 {len(c.failed)} 条")
    return not c.failed


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(0 if main() else 1)
