# -*- coding: utf-8 -*-
"""夜间深检批误报闸回归锁（2026-09-24）：视觉审查+三评审闭环归因的三个
系统性误报形态的修复锚点。

形态与归因（报告见 _goal_work/night_review/）：
① 「归属于母公司」族碎词（rules_engine 评审 P0-1 四步闭环）：OCR 行拼接×
   snap 吸附推翻停用字回溯——_COMPANY_STEM_PROSE 已收「所属/下属」
   漏「归属/隶属于」；
② 文种简称注册（视觉 S1，trace 实证）：「（以下简称规范）」把文种词注册为
   公司简称→全文子串遮蔽（「规范→UVW5公司」单页 7 处实证）——
   _SHORT_NAME_REJECT_SUBSTR 已收「办法/制度/流程」漏其余文种族；
③ 通用术语简称注册：「（以下简称三重一大）」党务术语被注册并替换为
   「XX公司」——句子语义破坏（6+ 页实证）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rules_engine import MaskOptions, RuleEngine


def _eng() -> RuleEngine:
    return RuleEngine(MaskOptions(mask_company=True, mask_person=True,
                                  person_loose=True))


def _company_texts(text, eng=None):
    eng = eng or _eng()
    return [s.text for s in eng.find_spans(text) if s.rule == "company"]


def test_stem_prose_guishu_family():
    """「归属于/隶属于」+组织词 不产生 company 碎片 span。"""
    for t in ("归属于母公司股东的净利润为100万元",
              "权益归属于公司股东的部分",
              "该资产隶属于集团总部管理"):
        got = _company_texts(t)
        assert not any(x.startswith(("归属于", "隶属于")) for x in got), \
            f"碎词误命中：{t!r} → {got}"


def test_short_name_genre_words_rejected():
    """文种词族做简称声明不注册（后续出现不遮）。"""
    for genre in ("规范", "细则", "条例", "指引", "标准", "手册", "规定", "大纲"):
        t = (f"《云川家居用品管理办法》（以下简称{genre}）自发布之日起施行，"
             f"本{genre}适用于全公司各部门")
        got = _company_texts(t)
        assert genre not in got, f"文种词 {genre!r} 被注册为简称：{got}"


def test_short_name_common_term_rejected():
    """通用党务/公文术语简称声明不注册。"""
    t = ("重大决策、重要人事任免、重大项目安排、大额资金运作事项决策"
         "（以下简称三重一大）须集体讨论，三重一大事项决策前应充分论证")
    got = _company_texts(t)
    assert "三重一大" not in got, f"术语被注册为简称：{got}"


def test_real_short_name_still_registers():
    """真简称声明照常注册（防误伤闸门过宽）。"""
    t = "云川家居用品股份有限公司（以下简称云川家居）的子公司适用，云川家居负责解释"
    got = _company_texts(t)
    assert "云川家居" in got, f"真简称漏注册：{got}"
