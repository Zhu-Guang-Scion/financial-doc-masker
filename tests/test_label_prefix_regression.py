# -*- coding: utf-8 -*-
"""_LABEL_PREFIX_RE trim 链标签剥离回归用例（阶段二 G3-4）。

背景：_LABEL_PREFIX_RE（R19 引入）用于公司 trim 链剥离「规章文件名称」类
行拼接标签前缀（OCR 行拼接场景：「规章文件名称云舒雅家居股份有限公司」，
标签不剥则公司候选从「文件」起算被停用字截停→漏检）。审计确认其至今无
单测兜底，本文件补齐；G3-3 重构 _company_candidates 前必须保持全绿。

运行：python tests/test_label_prefix_regression.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rules_engine import MaskOptions, RuleEngine, _LABEL_PREFIX_RE

# ---------- 正则层 ----------

REGEX_CASES = [
    # (head, 应匹配, 期望 end（剥前缀后剩余起点）)
    ("规章文件名称云舒雅家居股份有限公司", True, 6),   # R19 实景：双前缀
    ("管理表单名称广州极川三维信息科技有限公司", True, 6),
    ("制度名称宁柯贸易", True, 4),
    ("文件名称云舒雅家居股份有限公司", True, 4),       # 单前缀
    ("办法名称宁柯贸易有限公司", True, 4),
    ("规章制度名称宁柯贸易", True, 6),                 # 前缀词+同尾词
    ("公司名称管理规定", False, 0),                    # 「公司名称」非标签族
    ("名称云舒雅家居股份有限公司", False, 0),         # 孤立「名称」不剥
    ("文件清单云舒雅家居股份有限公司", False, 0),     # 非「名称」结尾
]


def test_regex_layer():
    for head, should, end in REGEX_CASES:
        m = _LABEL_PREFIX_RE.match(head)
        if should:
            assert m is not None, f"「{head}」应匹配标签前缀"
            assert m.end() == end, f"「{head}」end={m.end()} 期望 {end}"
            assert len(head) - m.end() >= 2 or head in ("办法名称宁柯贸易有限公司",)
        else:
            assert m is None, f"「{head}」不应匹配标签前缀（match={m and m.group()}）"


# ---------- 行为层（find_spans 端到端） ----------

def _engine():
    return RuleEngine(MaskOptions(mask_company=True))


BEHAVIOR_CASES = [
    # (文本, 应遮原文, 标签部分不得单独成 span)
    ("规章文件名称云舒雅家居股份有限公司",
     "云舒雅家居股份有限公司", "规章文件名称"),
    ("管理表单名称广州极川三维信息科技有限公司",
     "广州极川三维信息科技有限公司", "管理表单名称"),
]


def test_behavior_trims_label_then_masks_company():
    eng = _engine()
    for text, company, label in BEHAVIOR_CASES:
        spans = eng.find_spans(text)
        covered = {text[s:e] for s, e, *_ in [(sp[0], sp[1]) for sp in spans]} \
            if spans and isinstance(spans[0], (tuple, list)) else \
            {text[sp.start:sp.end] for sp in spans}
        assert any(company in c for c in covered), \
            f"「{text}」公司名未被遮（covered={covered}）——标签剥离失效"
        assert not any(c == label or (label in c and company not in c)
                       for c in covered), \
            f"「{text}」标签部分被误遮（covered={covered}）"


def test_label_alone_not_masked():
    """标签文本单独出现（后无公司名）不产生遮盖。"""
    eng = _engine()
    for text in ("规章文件名称", "管理表单名称", "文件名称"):
        spans = eng.find_spans(text)
        covered = {text[sp.start:sp.end] for sp in spans}
        assert not covered, f"「{text}」被误遮（covered={covered}）"


CASES = [test_regex_layer, test_behavior_trims_label_then_masks_company,
         test_label_alone_not_masked]


def run():
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {fn.__name__}: [{type(e).__name__}] {e}")
    print(f"共 {len(CASES)} 条，失败 {failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
