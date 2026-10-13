# -*- coding: utf-8 -*-
"""v2.1.1 学习通道形态闸测试（契约 §一；先红后绿）。

契约语义：`_learn_biz_unit_brands` 两个注册子块（事业部捕获块 + 品牌+品类
复合词块）在 `self._short_names[brand] = "***"` 之前，brand 必须满足
「纯 CJK 2~4 字」或「纯 ASCII 字母 2~4 字」——含数字/%/‰/小数点/币种符号
等一律拒绝；既有守卫（词频/常用词/黑名单/地名/_BRAND_STOP/碎片防护/机构尾）
原样保留，合法学习词结果不得改变。

红用例背景（_goal_work/tester_feedback_diag_20260913.md 全链归因）：折扣比例
矩阵表 OCR 行无分隔符拼接出「15%衣柜」子串，jieba 把「15%」切为整 token、
「衣柜」∈_BIZ_CAT_WORDS，品类词前 2~4 字未知词闸全部放行 → 注册
`_short_names["15%"]="***"` → 同引擎跨页 26 处折扣单元格被 company 通道遮蔽。
测试夹具直接使用该真实 token 形态。

测试环境：FINMASK_BRANDS 指向不存在文件（同 tests/test_candidate_scanner.py
的 _isolate_env 同法）——brands.json 静态词（慕蓝朵/司南等）不注入，学习
通道行为可单测。

运行：python -m pytest tests/test_learn_morph_gate.py -q
（官方直跑口径：python tests/test_learn_morph_gate.py）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.rules_engine import MaskOptions, RuleEngine  # noqa: E402

ENV_OVERRIDE = "FINMASK_BRANDS"

# 红用例主文本：诊断文档捕获的注册邻接现场形态（jieba 真实切分：
# ['15%', '衣柜', '（', '按', '18mm', '康纯板', '计算', '）', '云舒雅', '木门']）
RED_TEXT = "15%衣柜（按18mm康纯板计算）云舒雅木门"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """brands 配置全隔离（同源装载走空配置 = 静态预设零注入）。"""
    monkeypatch.setenv(ENV_OVERRIDE, "__no_such_brands__.json")


@pytest.fixture(autouse=True)
def _oss_min_bizcat(monkeypatch):
    """OSS 缺档态对齐（词表 v3 演练批）：品类词表 _BIZ_CAT_WORDS 装在
    外置档 biz_cat_words_extra 键，开源缺档=空 → 「品牌+品类复合词」
    学习子块失活（test_ascii_letters_still_learned /
    test_composite_block_unchanged 依赖品类词「木门」「衣柜」）。
    本套件注入用例所需最小品类词（零敏感词面），恢复与有档态一致的
    子块行为；monkeypatch 用后自动还原，不污染其他用例。"""
    import app.rules_engine as _re_mod
    monkeypatch.setattr(_re_mod, "_BIZ_CAT_WORDS",
                        frozenset(_re_mod._BIZ_CAT_WORDS) | {"木门", "衣柜"})


def _engine() -> RuleEngine:
    """构造引擎并显式初始化 jieba 词典。

    为什么必须显式初始化：词频闸读 `jieba.dt.FREQ`，而引擎惰性加载路径在
    首次 tokenize 前拿到的是**空 FREQ**（未初始化态）——同一进程内先跑过
    一次分词与否，会改变词频闸的否决结果（如 云舒雅 在已初始化词典中
    freq=20 → 词频闸否决，其遮蔽在生产由 brands.json 静态注入覆盖，不经
    学习通道）。显式初始化消除这一过程态差异，使本套件与用例执行顺序
    无关（pytest 定义序 / 直跑排序序均绿）。
    """
    eng = RuleEngine(MaskOptions(mask_company=True))
    eng._get_jieba().initialize()
    return eng


# --------------------------------------------------------------------------
# 红→绿：数字/符号形态一律拒绝（含 find_spans 端到端零 span）
# --------------------------------------------------------------------------
def test_red_percent_token_not_learned_zero_span():
    """红用例主形态：「15%衣柜」邻接串。

    修复前（先红演示）：_short_names 注册 "15%" 且 find_spans 产出
    ('15%', company) span（26 处折扣单元格误遮的根因通道）；
    修复后（转绿）：不注册、零 span。
    注：同串「云舒雅木门」的品类学习在已初始化词典态被既有词频闸否决
    （freq(云舒雅)=20 ≥ 20，与本闸无关的既有行为；云舒雅生产遮蔽由
    brands.json 静态注入覆盖），故合法学习零误伤由下方专用绿用例锚定。
    """
    eng = _engine()
    spans = eng.find_spans(RED_TEXT)
    assert "15%" not in eng._short_names, \
        f"形态闸失效：15% 被学习注册（_short_names={dict(eng._short_names)}）"
    assert [s for s in spans if "15%" in s.text] == [], \
        f"15% 仍被产出 span: {[(s.text, s.rule) for s in spans]}"


def test_red_direct_call_both_blocks_reject_numeric():
    """直调 _learn_biz_unit_brands：两个子块对数字/符号形态一律不注册。

    子块 2（品牌+品类）红形态族：15%（百分比）、18mm（字母数字）、
    1.5（小数点）——jieba 均切为品类词前整 token（实测切分见模块注释）。
    """
    for bad_text in ("15%衣柜", "18mm衣柜板材", "1.5衣柜门板"):
        eng = _engine()
        eng._learn_biz_unit_brands(bad_text)
        assert not eng._short_names, \
            f"{bad_text!r} 形态闸失效：注册了 {sorted(eng._short_names)}"


def test_fullwidth_percent_not_learned():
    """全角百分号形态：jieba 切 ['15','％','衣柜']，「％」单字本被长度闸拒；
    锁定修复后同样零注册（形态闸对符号零放行）。"""
    eng = _engine()
    eng._learn_biz_unit_brands("15％衣柜矩阵")
    assert not eng._short_names


# --------------------------------------------------------------------------
# 绿：形态闸 ASCII 字母分支 + 两子块合法学习行为不变（对照修复前断言）
# --------------------------------------------------------------------------
def test_ascii_letters_still_learned():
    """纯 ASCII 字母 2~4 字：形态闸放行分支（361° 类真实数字品牌留待显式
    配置口子，纯字母品牌如 ABC 不受影响）。修复前同样注册——行为不变。"""
    eng = _engine()
    eng._learn_biz_unit_brands("ABC木门系列")
    assert "ABC" in eng._short_names, "ASCII 字母品牌被形态闸误伤"


def test_biz_unit_block_unchanged():
    """子块 1（事业部捕获）：慕蓝朵事业部/华鹤事业部 照常学习（修复前
    同结果）。_BIZ_UNIT_RE 本为纯 CJK 捕获，形态闸在此为纵深防御。"""
    for text, brand in (("慕蓝朵事业部经销商管理办法", "慕蓝朵"),
                        ("华鹤事业部 enclosed", "华鹤")):
        eng = _engine()
        eng._learn_biz_unit_brands(text)
        assert brand in eng._short_names, f"{text!r} 合法学习被误伤"
        spans = eng.find_spans(text)
        assert any(s.text == brand for s in spans), text


def test_composite_block_unchanged():
    """子块 2（品牌+品类复合词）：经销商华鹤木门/经销商梓阳衣柜 照常学习
    （品类词前 2~4 字未知词；修复前同结果）。"""
    for text, brand in (("经销商华鹤木门促销", "华鹤"),
                        ("经销商梓阳衣柜定制合同", "梓阳")):
        eng = _engine()
        eng._learn_biz_unit_brands(text)
        assert brand in eng._short_names, f"{text!r} 合法学习被误伤"


def test_existing_guards_unchanged():
    """既有守卫原样保留：司南橱柜 不经学习通道复活（T2-D4，_BRAND_STOP_ALL
    否决；生产环境由 brands.json 静态注入覆盖——空配置下不注册即修复前
    行为）；梓阳科技衣柜 中品类前 token=科技（jieba 高频词）仍被词频闸拒。"""
    for text in ("司南橱柜经销商大会", "梓阳科技衣柜"):
        eng = _engine()
        eng._learn_biz_unit_brands(text)
        assert not eng._short_names, f"{text!r} 学习行为发生变化: {sorted(eng._short_names)}"


# --------------------------------------------------------------------------
# 直跑口径（python tests/test_learn_morph_gate.py）
# --------------------------------------------------------------------------
if __name__ == "__main__":
    os.environ[ENV_OVERRIDE] = "__no_such_brands__.json"
    _failed = []
    _total = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _total += 1
            try:
                _fn()
                print(f"  PASS {_name}")
            except AssertionError as _e:
                _failed.append(_name)
                print(f"  FAIL {_name}: {_e}")
    print(f"{_total - len(_failed)}/{_total} passed")
    sys.exit(1 if _failed else 0)
