# -*- coding: utf-8 -*-
"""地址脱敏回归测试（v1.6）。

应脱敏组：完整地址（行政区划+街巷/门牌/楼宇）→ 结构保持替换（××市××区××路×号…）。
不应脱敏组：机构名内嵌地名、孤立城市提及、城市枚举 → 原文不变。

运行：python tests/test_address_regression.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.handlers.text_replacement import apply_replacements
from app.rules_engine import MaskOptions, RuleEngine

# (文本, 期望输出)
SHOULD_MASK = [
    ("注册地址：深圳市南山区科技园南区高新南一道 8 号创维大厦 A 座 18 层",
     "注册地址：××市××区××区××道×号××大厦×座×层"),
    ("办公地址\n北京市西城区裕民路18 号2206 室",
     "办公地址\n××市××区××路×号×室"),
    ("公司住所：浙江省杭州市余杭区文一西路969号",
     "公司住所：××省××市××区××路×号"),
    ("地址：科技园南路6号2栋301室",
     "地址：××路×号×栋×室"),
    ("公司注册地位于杭州市滨江区江南大道588号。",
     "公司注册地位于××市××区××大道×号。"),
    ("通讯地址：内蒙古自治区呼和浩特市赛罕区敕勒川大街1号",
     "通讯地址：××自治区××市××区××大街×号"),
]

# 原文必须原样保留（机构名一部分 / 孤立城市 / 城市枚举）
SHOULD_NOT_MASK = [
    # v1.7.5 R14：泛指城市类别词本身放行（纠偏白名单；费用办法 p9 表头实证）
    "一类城市：北京、上海、广州、深圳", "二类城市：省会城市、厦门、青岛。",
    "出差住宿标准按一类城市、二类城市、三类城市划分。",
    "上海浦东发展银行股份有限公司",
    "广州市天河区人民检察院",
    "公司位于深圳市。",
    "本产品销往北京、上海、广州、深圳等城市",
    "深圳市南山区腾讯科技有限公司",
    "上海市浦东新区发展银行",
]

TOTAL_CASES = len(SHOULD_MASK) + len(SHOULD_NOT_MASK)


def run() -> bool:
    # 仅开地址规则，排除公司/人名/金额规则对断言的干扰
    engine = RuleEngine(MaskOptions(mask_company=False, mask_person=False,
                                    mask_amount=False, mask_contact=False,
                                    mask_address=True))
    failed = []

    print(f"应脱敏组：{len(SHOULD_MASK)} 条")
    for text, expected in SHOULD_MASK:
        got = apply_replacements(text, engine.find_spans(text))
        if got == expected:
            print(f"  PASS {text[:24]}… -> {got}")
        else:
            failed.append((text, expected, got))
            print(f"  FAIL {text}\n      期望: {expected}\n      实际: {got}")

    print(f"不应脱敏组：{len(SHOULD_NOT_MASK)} 条")
    for text in SHOULD_NOT_MASK:
        got = apply_replacements(text, engine.find_spans(text))
        if got == text:
            print(f"  PASS {text[:24]}…（未改动）")
        else:
            failed.append((text, text, got))
            print(f"  FAIL {text} 被误脱敏为: {got}")

    print(f"\n共 {TOTAL_CASES} 条用例，失败 {len(failed)} 条")
    return not failed


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ok = run()
    sys.exit(0 if ok else 1)
