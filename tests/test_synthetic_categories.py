# -*- coding: utf-8 -*-
"""合成类别回归测试（v1.7.5 R14）——语料缺失敏感类别的引擎级固化。

- 银行账号/卡号：标签引导 9~25 位纯数字应遮（部分遮蔽保留前4后4）；
  无标签裸长数字放行（存疑放行原则）；
- 身份证：有效校验位应遮（部分遮蔽）；无效校验位放行；
- 金额：亿级/裸万字尾缀应遮（R14 分支重序修复）；
- 红章/图片实体：属 PDF 渲染+OCR 链路，由每轮全量跑批的合成样本亲检
  覆盖（工程红线项）。

运行：python tests/test_synthetic_categories.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rules_engine import MaskOptions, RuleEngine
from app.handlers.text_replacement import apply_replacements
from app.utils import parse_amount

SHOULD_MASK = [
    # 银行账号（R14 新增 account 通道）
    ("银行账号：6222020200112233445。", "银行账号：6222***********3445。"),
    ("账号 621225360111222333", "账号 6212**********2333"),
    ("收款卡号：6228480402564890018，", "收款卡号：6228***********0018，"),
    # 无效校验位身份证（R17：ID 形态串一律部分遮蔽，不依赖校验位）
    ("无效校验位样本：440104199203150630", "无效校验位样本：440104********0630"),
    # 金额（R14 分支重序 + 亿/裸万）
    ("差错金额上限0.5万元", "差错金额上限***"),
    ("预付12.5万元定金", "预付***定金"),
    ("授信额度4.5亿元", "授信额度***"),
    ("备用金￥2.6万", "备用金***"),
]

SHOULD_NOT_MASK = [
    # 无标签裸长数字（单号/编号类，存疑放行）
    "订单号 Y1168072200002",
    "凭证号 w1910003960900002",
    # 表单标签本身（纠偏白名单）
    "联系电话：", "开户行：工商银行",
]


def main() -> None:
    failed = []
    print(f"应遮组：{len(SHOULD_MASK)} 条")
    for text, expected in SHOULD_MASK:
        eng = RuleEngine(MaskOptions(mask_amount=True, mask_contact=True,
                                     amount_mode="mask"))
        got = apply_replacements(text, eng.find_spans(text))
        if got == expected:
            print(f"  PASS {text!r}")
        else:
            failed.append(text)
            print(f"  FAIL {text!r}\n      期望: {expected!r}\n      实际: {got!r}")

    print(f"不应遮组：{len(SHOULD_NOT_MASK)} 条")
    for text in SHOULD_NOT_MASK:
        eng = RuleEngine(MaskOptions(mask_amount=True, mask_contact=True,
                                     amount_mode="mask"))
        got = apply_replacements(text, eng.find_spans(text))
        if got == text:
            print(f"  PASS {text!r}（未改动）")
        else:
            failed.append(text)
            print(f"  FAIL {text!r} -> {got!r}")

    print("parse_amount 辅助断言：")
    checks = [("4.5亿元", 450000000.0), ("2.6万", 26000.0), ("0.5万元", 5000.0),
              ("￥12,345.67", 12345.67)]
    for s, want in checks:
        got = parse_amount(s)
        if got is not None and abs(got - want) < 0.01:
            print(f"  PASS parse_amount({s!r}) == {want}")
        else:
            failed.append(s)
            print(f"  FAIL parse_amount({s!r}) = {got}，期望 {want}")

    total = len(SHOULD_MASK) + len(SHOULD_NOT_MASK) + len(checks)
    print(f"\n共 {total} 条用例，失败 {len(failed)} 条")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
