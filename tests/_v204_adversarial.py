# -*- coding: utf-8 -*-
# TODO(开源后续轮): 人名夹具虚构化重构（映射表 §四）
"""v2.0.4 共享对抗样本库（第 0 阶段主 agent 冻结，B/C 两线共用，禁各自另建）。

契约：_goal_work/v204_contract.md C-D 节。正例=江图名单实录版式及变体（应遮）；
对抗=职务标签正文叙述/竖排非名单/标签后非人名/表格表头（不应遮、不触发）。

两条使用路径：
1. 行级断言（引擎级单测）：LINES 常量 + EXPECTED/FORBIDDEN 直接喂
   rules_engine 人名通道断言候选产出；
2. 合成 docx（端到端）：build_docx(lines) 构造最小 docx 走 docx_handler 全链
   （WF1 后的全文本送检），配 custom/brands 词或纯结构通道断言。

名单实录出处：江城市图书馆内部制度汇编_转自DOC.docx 段 4257-4267（竖排名单）
与段 1674/2417/2506（正文标签行）；人名已按敏感面惯例保留（本库为回归资产，
真实人名在语料原文对照下使用，报告引用时示意化）。
"""
from __future__ import annotations

# ---- 行级清单（line, 该行应遮的人名集合, 该行不应出现的遮蔽原文集合）----
# should_mask=None 表示该行不参与「必须遮」断言（如纯对抗行只断言 FORBIDDEN）。
LINES: list[tuple[str, set[str] | None, set[str]]] = [
    # 正例（实录形态）
    ("林晏清   党委书记", {"林晏清"}, set()),
    ("副组长： 唐某   党委纪检委员、纪检监察室主任", {"唐某"}, set()),
    ("组\u3000长：王某、宋怀瑾", {"王某", "宋怀瑾"}, set()),
    ("组  长:宋怀瑾、何某", {"宋怀瑾", "何某"}, set()),
    ("成  员： 罗某   党委委员、文献流通部副主任", {"罗某"}, set()),
    ("颜某   第一支部纪检委员、文献流通部副主任", {"颜某"}, set()),
    ("惠某   第三支部纪检委员", {"惠某"}, set()),
    ("张某   第四支部纪检委员", {"张某"}, set()),
    ("沈远   纪检监察室工作人员", {"沈远"}, set()),
    # 对照（既有通道已正常遮蔽的顿号枚举——防三线改坏既有行为）
    ("组  员：潘某、张伟、巫某、陈某、刘某、肖某、唐某、林某", set(), set()),
    # 对抗（不应遮）
    ("组长汇报了工作", set(), {"组长"}),
    ("第一章 总则……1", set(), {"第一章"}),
    ("（一）加强支出审批控制。", set(), {"支出"}),
    ("组长单位", set(), {"组长"}),
    ("成员名单如下", set(), {"成员"}),
    ("成  员：纪检监察室工作人员、支部纪检委员等", set(), {"纪检监察室工作人员"}),
    ("姓　名　职　务", set(), {"姓　名"}),
]

POSITIVE_LINES = [ln for ln, m, _ in LINES if m]
ADVERSARIAL_LINES = [ln for ln, m, f in LINES if not m]

_DOCX_PARAS = [
    "江城图书馆内部审计小组构成如下：",
    *[ln for ln, _, _ in LINES],
]


def build_docx(path) -> list[str]:
    """构造含全部 LINES 行的最小 docx，返回写入的段落文本（供断言）。"""
    from docx import Document

    doc = Document()
    for ln, _, _ in LINES:
        doc.add_paragraph(ln)
    doc.save(str(path))
    return [ln for ln, _, _ in LINES]
