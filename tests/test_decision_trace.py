# -*- coding: utf-8 -*-
"""v2.1.7 批甲 A1① 决策留痕旁路测试。

覆盖：
- 采集器开/关（FINMASK_DECISION_TRACE）与 accept/reject 双侧行；
- 零行为锁：留痕开/关两种引擎配置对同一文本产出的敏感区间完全一致；
- sidecar 写出（决策留痕_*.csv 列契约，不与映射表/台账文件名冲突）；
- B3-3 批内文本拼接（join_page_texts / PlanBuilder.page_texts_joined）。

例词均为构造词（公开基线仓库惯例），不涉真实语料词面。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rules_engine import RuleEngine, MaskOptions  # noqa: E402


_TEXT = (
    "云舒雅家居股份有限公司与慕蓝朵整家事业部签署协议，法定代表人为王某某，"
    "联系电话 13800138000。以下简称昙栎。规范管理制度若干，超 3 万人次参与。"
)


def _engine(**kw) -> RuleEngine:
    return RuleEngine(MaskOptions(**kw))


def test_trace_default_on_accept_and_reject_rows():
    eng = _engine(mask_amount=True)
    spans = eng.find_spans(_TEXT)
    assert spans, "构造文本应命中敏感区间"
    rows = eng._decision_trace
    assert rows, "默认开启应产生决策留痕行"
    kinds = {r[1] for r in rows}
    assert "accept" in kinds and "reject" in kinds
    # 行结构=（文件, 类型, 类别, 词, 证据, 置信度）
    for r in rows:
        assert len(r) == 6
        assert r[1] in ("accept", "reject")
        assert r[2]  # 类别非空
    # 词面必须出自原文（未落到产物词的构造校验）
    joined = "".join(r[3] for r in rows if r[1] == "accept")
    assert "云舒雅" in joined or "13800138000" in joined


def test_trace_env_off_disables_collection(monkeypatch):
    monkeypatch.setenv("FINMASK_DECISION_TRACE", "0")
    eng = _engine()
    eng.find_spans(_TEXT)
    assert eng._decision_trace == []
    assert eng._trace_on is False


def test_trace_zero_behavior_lock(monkeypatch):
    """零行为锁：留痕开/关，同一文本的敏感区间逐项全等。"""
    monkeypatch.setenv("FINMASK_DECISION_TRACE", "0")
    eng_off = _engine(mask_amount=True)
    spans_off = eng_off.find_spans(_TEXT)
    monkeypatch.delenv("FINMASK_DECISION_TRACE", raising=False)
    eng_on = _engine(mask_amount=True)
    spans_on = eng_on.find_spans(_TEXT)
    a = [(s.start, s.end, s.rule, s.text, s.replacement, s.confidence)
         for s in spans_off]
    b = [(s.start, s.end, s.rule, s.text, s.replacement, s.confidence)
         for s in spans_on]
    assert a == b and a, "留痕旁路不得改变任何遮蔽判定"


def test_trace_determinism_same_input_same_rows():
    e1, e2 = _engine(mask_amount=True), _engine(mask_amount=True)
    e1.find_spans(_TEXT)
    e2.find_spans(_TEXT)
    assert e1._decision_trace == e2._decision_trace


def test_decision_trace_csv_writer_contract(tmp_path):
    from app.dispatcher import write_decision_trace_csv, DECISION_TRACE_HEADER
    rows = [("样例.pdf", "accept", "company", "昙栎建设", "src=main", "高"),
            ("样例.pdf", "reject", "person", "王情", "gate=loose_dictword", "")]
    path = write_decision_trace_csv(tmp_path, rows)
    assert path.name.startswith("决策留痕_") and path.name.endswith(".csv")
    import csv as _csv
    with open(path, encoding="utf-8-sig", newline="") as f:
        data = list(_csv.reader(f))
    assert data[0] == DECISION_TRACE_HEADER == \
        ["文件", "类型", "类别", "词", "证据", "置信度"]
    assert data[1:] == [list(r) for r in rows]
    # 文件名不与映射表/台账 glob 冲突（跑批恰一份断言/台账消费面）
    assert not path.name.startswith(("映射表_汇总", "遮蔽台账_"))


def test_engine_file_stamp_carries_into_rows():
    eng = _engine()
    eng._trace_file = "构造文档.pdf"
    eng.find_spans("规范管理办法（试行）第三章")
    assert eng._decision_trace
    assert all(r[0] == "构造文档.pdf" for r in eng._decision_trace)


def test_join_page_texts_builder_and_plan(tmp_path):
    from app.masking_plan import join_page_texts, PlanBuilder, MaskingPlan
    pages = [
        {"pno": 0, "text": "第一页原生文本", "ocr": {"items": []}},
        {"pno": 1, "text": "", "ocr": {"items": [["截图行甲", 0, 0, 1, 1, 0.99],
                                                 ["截图行乙", 0, 1, 1, 2, 0.98]]}},
        {"pno": 2, "text": "  ", "ocr": {}},  # 空页跳过
    ]
    joined = join_page_texts(pages)
    assert "第一页原生文本" in joined
    assert "截图行甲" in joined and "截图行乙" in joined
    # 纯空白页（第 3 页）整体跳过：内容行恰 3 行
    assert len([l for l in joined.splitlines() if l.strip()]) == 3
    builder = PlanBuilder(Path("a.pdf"), MaskOptions())
    builder.set_native_text(0, "原生页零")
    builder.set_ocr_items(1, [("行一", (0, 0, 1, 1), 0.9)])
    assert "原生页零" in builder.page_texts_joined()
    assert "行一" in builder.page_texts_joined()
    plan = MaskingPlan({"header": {}, "filename": {}, "table_rows": [],
                        "pages": pages})
    assert "截图行甲" in plan.texts_joined()
