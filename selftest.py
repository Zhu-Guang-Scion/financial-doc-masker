# -*- coding: utf-8 -*-
"""脱敏引擎自检脚本（无界面）：验证校验算法与规则引擎内置链路。

运行：python selftest.py
"""
from app.handlers.text_replacement import apply_replacements
from app.rules_engine import MaskOptions, RuleEngine
from app.utils import validate_id_card, validate_phone, validate_uscc


def main() -> None:
    # 1) 校验算法
    assert validate_id_card("110101199003078515"), "身份证校验位应为有效"
    assert not validate_id_card("110101199003078510"), "身份证校验位应为无效"
    assert validate_phone("13812345678"), "常见号段应为有效"
    assert not validate_phone("12345678901"), "非 1[3-9] 开头应为无效"
    assert validate_uscc("91310001MA1FL000XY"), "统一社会信用代码校验位应为有效"
    print("校验算法：通过")

    # 2) 规则引擎
    options = MaskOptions(
        mask_company=True,
        mask_person=True,
        mask_contact=True,
        mask_amount=True,
        custom_words=["马玉", "雄商置业"],
    )
    engine = RuleEngine(options)
    text = (
        "联系人：马玉，电话13812345678，邮箱ma.yu@example.com，"
        "身份证110101199003078515，统一社会信用代码91310001MA1FL000XY，"
        "公司：北京雄商置业有限公司，合同金额￥1,234,567.89元。"
    )
    spans = engine.find_spans(text)
    print("命中明细：")
    for sp in spans:
        print(f"  [{sp.rule:8s}] {sp.text!r} -> {sp.replacement!r}")

    rules = {sp.rule for sp in spans}
    for expected in ("custom", "phone", "email", "id_card", "uscc", "company", "amount"):
        assert expected in rules, f"规则 {expected} 未命中"
    assert all(not (a.start < b.end and b.start < a.end)
               for i, a in enumerate(spans) for b in spans[i + 1:]), "区间存在重叠"

    # 3) 文本替换
    print("脱敏结果：", apply_replacements(text, spans))
    print("自检通过 ✔")


if __name__ == "__main__":
    main()
