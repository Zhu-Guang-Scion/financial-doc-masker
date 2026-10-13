# -*- coding: utf-8 -*-
"""_company_candidates 重构安全网 · P0 用例（阶段二 G3-3 硬闸门）。

来源：并行任务三安全网预研（_goal_work/parallel_tasks/g3_3_safety_net.md）
缺口清单 P0×6（G1-G6），全部为「裸奔且历史上出过回归（跷跷板当事分支）」。
G3-3 重构 _company_candidates 函数体前本文件必须全绿（goal_rules 第九节硬
闸门）；重构后任一条 FAIL 即重构违规，回退处理。

运行：python tests/test_company_safety_net_p0.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rules_engine import MaskOptions, RuleEngine


def _spans_text(text):
    eng = RuleEngine(MaskOptions(mask_company=True))
    spans = eng.find_spans(text)
    return {text[sp.start:sp.end] for sp in spans}


CASES = [
    # ---- G1/G2：括号区划跨越（R16 漏报第 5 条/R18 根因，F0.1+F1d）----
    ("G1 全角括号", "网银在线（北京）科技有限公司提供支付服务。",
     {"网银在线（北京）科技有限公司"}, set()),
    ("G2 半角括号", "网银在线(北京)科技有限公司提供支付服务。",
     {"网银在线(北京)科技有限公司"}, set()),
    # ---- G3：行拼接标签剥离（R19，F3b+F1c；与 G3-4 用例互为冗余守卫）----
    ("G3 行拼接标签", "规章文件名称云舒雅家居股份有限公司",
     {"云舒雅家居股份有限公司"}, {"规章文件名称"}),
    # ---- G4：连词前置否决四形态（R18 修复⑧ 10 行 FP 族，F1d+F1c）----
    ("G4a 及其", "各控股(分)子公司及其全资子公司应遵照执行。", set(), set()),
    ("G4b 及各", "各控股(分)子公司及各全资子公司应遵照执行。", set(), set()),
    ("G4c 以及", "各控股(分)子公司以及全资子公司应遵照执行。", set(), set()),
    ("G4d 与其", "各控股(分)子公司与其全资子公司应遵照执行。", set(), set()),
    # ---- G5：职务+后缀族（R16/R22 修复清单第 1 项，F4k）----
    ("G5 职务+后缀", "该副总裁集团与高级经理公司均需签署。",
     set(), {"副总裁集团", "高级经理公司"}),
    # ---- G6：「不含」前缀剥离重验后放行（v1.6.9 漏检根因族，F4m+F1d）----
    ("G6 不含+括号区划", "不含圣都家居(浙江)有限责任公司及分公司。",
     {"圣都家居(浙江)有限责任公司"}, set()),
]


def test_p0_safety_net():
    eng_fail = []
    for name, text, should_mask, must_not in CASES:
        covered = _spans_text(text)
        for want in should_mask:
            if not any(want in c or c in want for c in covered):
                eng_fail.append(f"[{name}] 应遮「{want}」未遮（covered={covered}）")
        for ban in must_not:
            if any(ban in c for c in covered):
                eng_fail.append(f"[{name}] 不应遮「{ban}」被遮（covered={covered}）")
        # 碎词守卫：遮盖块不得切开目标词（截断≠脱敏）
        for c in covered:
            for want in should_mask:
                if c in want and c != want and len(c) >= 2:
                    eng_fail.append(f"[{name}] 疑似碎词：遮块「{c}」是「{want}」子串")
    assert not eng_fail, "\n".join(eng_fail)


CASES_FN = [test_p0_safety_net]


def run():
    failed = 0
    for fn in CASES_FN:
        try:
            fn()
            print(f"  PASS {fn.__name__}（含 {len(CASES)} 个 P0 场景）")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}:\n{e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {fn.__name__}: [{type(e).__name__}] {e}")
    print(f"共 {len(CASES_FN)} 条（{len(CASES)} 场景），失败 {failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
