# -*- coding: utf-8 -*-
"""v2.1.9 B12：filename 记账条件恒真修复——先红后绿。

缺陷：finalize_output_name 的记录条件用 src_name（含扩展名）拼比较串
→ `final.name != "报告.pdf_脱敏后.pdf"` 恒真 → 每文件必产一条纯改名
filename 映射/台账行（54 行/批噪音，v2.1.8 words_only 评估批发现）。
修复语义：仅当文件名真实脱敏（词干被替换）或碰撞加序时才记账。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dispatcher import finalize_output_name  # noqa: E402
from app.rules_engine import RuleEngine, MaskOptions  # noqa: E402


def _finalize(tmp_path, src_name, stem, engine):
    tmp = tmp_path / (stem + "_脱敏后" + Path(src_name).suffix)
    tmp.write_bytes(b"x")
    return finalize_output_name(tmp, src_name, stem, MaskOptions(), engine)


def test_no_rename_no_record(tmp_path):
    eng = RuleEngine(MaskOptions())
    final, rec = _finalize(tmp_path, "样例.pdf", "样例", eng)
    assert final.name == "样例_脱敏后.pdf"
    assert rec is None, f"词干零改动不得记 filename 行（B12 噪音）：{rec}"


def test_real_rename_records(tmp_path):
    eng = RuleEngine(MaskOptions(custom_words=["机密项目"]))
    final, rec = _finalize(tmp_path, "机密项目结项.pdf", "机密项目结项", eng)
    assert final.name != "机密项目结项_脱敏后.pdf", "自定义词应替换文件名词干"
    assert rec is not None and rec[2] == "filename"
    assert rec[0] == "机密项目结项.pdf"


def test_collision_suffix_records(tmp_path):
    eng = RuleEngine(MaskOptions(custom_words=["机密项目"]))
    (tmp_path / "敏1结项_脱敏后.pdf").write_bytes(b"y")  # 替换后目标名已被占用
    final, rec = _finalize(tmp_path, "机密项目结项.pdf", "机密项目结项", eng)
    assert final.name == "敏1结项_脱敏后(1).pdf"
    assert rec is not None, "碰撞加序=真实改名，须记账"
