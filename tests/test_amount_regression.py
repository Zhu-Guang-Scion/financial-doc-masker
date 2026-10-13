# -*- coding: utf-8 -*-
"""金额识别回归测试集（v1.7.3）。

- 应遮组：常规金额（货币符号/千分位/小数/元）+ v1.7.3 标签引导裸数字；
- 不应遮组：版本号/编号/订单号/日期/短序号等误判源。

运行：python tests/test_amount_regression.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rules_engine import MaskOptions, RuleEngine
from app.handlers.text_replacement import apply_replacements

SHOULD_MASK = [
    # 常规通道（回归保护）
    ("合同金额￥12,345.67元", "合同金额***元"),
    ("货款3000元", "货款***"),
    ("报价 12345.67", "报价 ***"),
    # v1.7.3 标签引导裸数字（表格「退款金额\n2223」类，存疑评审决策 2026-09-06）
    ("退款金额\n2223", "退款金额\n***"),
    ("订货金额：37,156", "订货金额：***"),
    ("货款金额 37156", "货款金额 ***"),
    ("合计 23500.50", "合计 ***"),
    ("金额超过1000元的", "金额超过***的"),
    ("押金 5000", "押金 ***"),
    ("小计：1,234", "小计：***"),
    # v1.7.5 R14：金额分支重序修复（旧序中 0.5万元/12.5万元 被裸小数分支吃掉后
    # 遭无单位闸否决 → 整笔漏遮）+ 亿单位支持
    ("差错金额上限0.5万元", "差错金额上限***"),
    ("预付12.5万元定金", "预付***定金"),
    ("授信额度4.5亿元", "授信额度***"),
    ("备用金￥2.6万", "备用金***"),
]

SHOULD_NOT_MASK = [
    "版本号 1.2 保持不变",
    "文件编号 ACME-FIN-W-001",
    "订单号 Y1168072200002",
    "金额为 12 个月内有效",
    "序号 5 金额见附件",
    "生效日期 2023-03-20",
    "申请退款日期 2022.3.28",
    "统一社会信用代码 913100MA1FL5XXXX",
    "金额（人民币）合计栏",
    "共 3 家子公司",
]


def run() -> bool:
    failed = []
    print(f"应遮组：{len(SHOULD_MASK)} 条（amount_mode=mask）")
    for text, expected in SHOULD_MASK:
        eng = RuleEngine(MaskOptions(mask_amount=True, amount_mode="mask"))
        got = apply_replacements(text, eng.find_spans(text))
        if got == expected:
            print(f"  PASS {text!r} -> {got!r}")
        else:
            failed.append((text, expected, got))
            print(f"  FAIL {text!r}\n      期望: {expected!r}\n      实际: {got!r}")

    print(f"不应遮组：{len(SHOULD_NOT_MASK)} 条")
    for text in SHOULD_NOT_MASK:
        eng = RuleEngine(MaskOptions(mask_amount=True, amount_mode="mask"))
        got = apply_replacements(text, eng.find_spans(text))
        if got == text:
            print(f"  PASS {text!r}（未改动）")
        else:
            failed.append((text, text, got))
            print(f"  FAIL {text!r} 被误脱敏为: {got!r}")

    # 缩放模式冒烟：标签裸数字在 scale 模式下应有公式可还原
    eng = RuleEngine(MaskOptions(mask_amount=True, amount_mode="scale"))
    got = apply_replacements("退款金额\n2223", eng.find_spans("退款金额\n2223"))
    ok_scale = ("[金额缩放公式]" in got) or ("2223" not in got)
    print(f"  {'PASS' if ok_scale else 'FAIL'} scale 模式标签裸数字: {got!r}")
    if not ok_scale:
        failed.append(("scale 2223", "含公式或已遮", got))

    total = len(SHOULD_MASK) + len(SHOULD_NOT_MASK) + 1
    print(f"\n共 {total} 条用例，失败 {len(failed)} 条")
    return not failed


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
