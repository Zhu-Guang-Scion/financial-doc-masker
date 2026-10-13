# -*- coding: utf-8 -*-
"""candidate_flow 流程层测试（v2.1.0 G2-S2）。

覆盖（契约 §一/§二/§三 + B 报告 §3 防呆 13 条展示端半区）：
- 上下文数字打码（≥7 位）/ S6 非专名形态过滤 的纯函数矩阵；
- S4 五登记面比对回填（_norm_key 归一化：全半角/括号/空白等价）与 S5 跨键
  撞车检测（候选 ∩ 现有 org_suffixes 增词）；
- build_review_items：S1/S2/S3/S6 拦截、扫描器 hidden_reason 透传、person 组
  排除（J19）、默认排序（置信分↓同分频次↓，J15）、统计计数、徽标文案；
- 分页切片（50/页）/搜索过滤/可勾选行集合；
- 入口①推荐闸（N≥3 且存在未登记候选 + 批文件集指纹内存 set 不重复提示）；
- mock ScanSession（按契约 §一签名）的全流程编排：勾选 → merge_brands_atomic
  → 结果汇总文案（ima 条件展示）→ session.close() 零持久化闭环。

判据词一律用已公开判例词（云舒雅/司南/江图/江城图书馆/江城文库研究中心）；
写入测试产物全部落 tmp_path（零持久化红线）。运行：pytest tests/test_candidate_flow.py
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import brand_config as bc
from app import candidate_flow as cf
from app.brand_config import ENV_OVERRIDE

# ---------------------------------------------------------------------------
# 契约 §一 的 mock（S1 并行开发：对签名编程，不依赖真实扫描器）
# ---------------------------------------------------------------------------

@dataclass
class MockCandidate:
    """契约 CandidateWord 的最小鸭子类型（字段名逐一对齐契约签名）。"""
    word: str
    freq: int = 1
    files_n: int = 1
    channels: list = field(default_factory=list)
    org_form: bool = False
    declared: bool = False
    already_configured: str | None = None  # 扫描器恒置 None（契约修正一）
    score: float = 0.0
    hidden_reason: str | None = None
    person_likely: bool = False
    first_loc: dict = field(default_factory=lambda: {"file_idx": 0,
                                                     "start": 0, "end": 0})


class MockScanSession:
    """契约 ScanSession 的可测复刻：candidates/meta/visible/context_text/
    source_files/close（close 后零残留——零持久化闭环的行为断言锚）。"""

    def __init__(self, candidates, texts=None, names=None, meta=None):
        self.candidates = list(candidates)
        self._texts = dict(texts or {})
        self._names = list(names or [])
        self.meta = dict(meta or {"files_n": len(self._names),
                                  "total_chars": 0,
                                  "ocr_pages_skipped": 0,
                                  "elapsed_s": 0.0})
        self.closed = False

    def visible(self):
        return [c for c in self.candidates
                if not c.hidden_reason and not c.person_likely]

    def context_text(self, cand, span=15):
        if self.closed:
            return ""
        loc = cand.first_loc
        text = self._texts.get(loc["file_idx"], "")
        start = max(0, loc["start"] - span)
        end = min(len(text), loc["end"] + span)
        return text[start:end]

    def source_files(self, cand, limit=5):
        if self.closed:
            return []
        return self._names[:limit]

    def close(self):
        self.closed = True
        self.candidates.clear()
        self._texts.clear()
        self._names.clear()
        self.meta.clear()


# ---------------------------------------------------------------------------
# 基础纯函数：数字打码 / 非专名形态 / 归一化
# ---------------------------------------------------------------------------

def test_mask_long_digits_matrix():
    """≥7 位数字串打码「〔数字×N〕」；6 位放过；全角数字同口径；空串安全。"""
    assert cf.mask_long_digits("电话13800138000完") == "电话〔数字×11〕完"
    assert cf.mask_long_digits("编号20260913末") == "编号〔数字×8〕末"
    assert cf.mask_long_digits("金额8600元") == "金额8600元"        # 4 位放过
    assert cf.mask_long_digits("单号1234567。") == "单号〔数字×7〕。"
    assert cf.mask_long_digits("合同号２０２６０９）") == "合同号２０２６０９）"   # 全角 6 位放过
    assert cf.mask_long_digits("合同号２０２６０９１）") == "合同号〔数字×7〕）"  # 全角 7 位打码
    assert cf.mask_long_digits("") == ""
    assert cf.mask_long_digits(None) == ""


def test_is_non_entity_form_matrix():
    """S6：纯数字/纯符号/纯单字英文 → True；含实体字 → False。"""
    assert cf.is_non_entity_form("20260913")            # 纯数字串
    assert cf.is_non_entity_form("1,300.50")            # 数字+符号
    assert cf.is_non_entity_form("——")                  # 纯符号
    assert cf.is_non_entity_form("％＃")                 # 全角符号
    assert cf.is_non_entity_form("A")                   # 纯单字英文
    assert cf.is_non_entity_form("  ")                  # 空白
    assert cf.is_non_entity_form("")
    assert not cf.is_non_entity_form("江图")
    assert not cf.is_non_entity_form("江城图书馆")
    assert not cf.is_non_entity_form("A4纸")            # 字母+实体字（含 CJK）
    assert not cf.is_non_entity_form("3M公司")


# ---------------------------------------------------------------------------
# S4 五登记面比对回填 / S5 跨键撞车
# ---------------------------------------------------------------------------

def _faces(**over) -> cf.RegistrationFaces:
    base = dict(
        brands=("云舒雅",),
        aliases=("江 图",),                      # 故意带空格：验证 _norm_key 口径
        custom_words=("江城文库研究中心",),
        mapping_keys=("（普通合伙）项目",),       # 括号归一等价口径
        org_suffix_adds=("卫生室",),
    )
    base.update(over)
    return cf.RegistrationFaces(**base)


def test_s4_five_faces_backfill_matrix():
    """五面逐一命中 → 面字段名；未命中 → None（契约修正一：流程层回填）。"""
    faces = _faces()
    assert cf.match_registration_face("云舒雅", faces) == "brands"
    assert cf.match_registration_face("江图", faces) == "aliases"     # 剥空白同键
    assert cf.match_registration_face("江城文库研究中心", faces) == "custom_words"
    assert cf.match_registration_face("（普通合伙）项目", faces) == "mapping_keys"
    assert cf.match_registration_face("卫生室", faces) == "org_suffix_adds"
    assert cf.match_registration_face("江城图书馆", faces) is None
    assert cf.match_registration_face("ＳＯＦＯ", faces) is None


def test_s4_norm_key_fullwidth_equivalence():
    """全角/半角、括号、空白差异在比对中等价（与 brand_config._norm_key 同源）。"""
    faces = cf.RegistrationFaces(brands=("ＳＯＦＯ",), aliases=(),
                                 custom_words=(), mapping_keys=(), org_suffix_adds=())
    assert cf.match_registration_face("SOFO", faces) == "brands"
    faces2 = cf.RegistrationFaces(brands=("司南",), aliases=(), custom_words=(),
                                  mapping_keys=(), org_suffix_adds=())
    assert cf.match_registration_face(" 司南 ", faces2) == "brands"


def test_s5_cross_key_collision_detection():
    """S5：候选 ∩ 现有 org_suffixes 增词（归一化）→ True；其余 → False。"""
    faces = _faces()
    assert cf.collides_org_suffix_adds("卫生室", faces)
    assert cf.collides_org_suffix_adds(" 卫 生 室 ", faces)  # 剥空白同键
    assert not cf.collides_org_suffix_adds("江图", faces)


def test_s2_generic_forbidden_set_same_source():
    """S2 集合与装载点同源（write_forbidden_set 同一表达式）；司南特例放行。"""
    forbidden = cf.generic_forbidden_set()
    assert "定制" in forbidden and "公司" in forbidden
    assert "包括" in forbidden            # NON_NAME_BLACKLIST 成员
    assert "司南" not in forbidden        # -{"司南"} 差集（配置通道白名单特例）


def test_s3_pure_org_suffix_same_source():
    """S3 与 masking_plan/引擎 is_pure_org_suffix 同源：纯后缀组合判真；
    G1 用户增词示例（卫生室）不属内置后缀组合 → 不拦（走 S5 撞车道）。"""
    assert cf.is_pure_org_suffix_word("有限公司")
    assert cf.is_pure_org_suffix_word("中心")
    assert not cf.is_pure_org_suffix_word("江城图书馆")
    assert not cf.is_pure_org_suffix_word("江图")
    assert not cf.is_pure_org_suffix_word("卫生室")


# ---------------------------------------------------------------------------
# build_review_items：展示端防呆链 + 排序 + 统计
# ---------------------------------------------------------------------------

def _cand(word, **kw):
    return MockCandidate(word=word, **kw)


def test_build_review_items_gates_and_badges():
    """S1/S2/S3/S6 拦截、扫描器 hidden_reason 透传、S4 禁选、S5 可选+必确认。"""
    faces = _faces()
    cands = [
        _cand("张", freq=9, score=10),                    # S1 长度 <2
        _cand("定制", freq=8, score=20),                   # S2 泛词同源
        _cand("有限公司", freq=7, score=30),               # S3 纯组织后缀
        _cand("20260913", freq=6, score=40),               # S6 非专名形态
        _cand("扫描器拦截词", freq=5, score=50,
              hidden_reason="功能字闸"),                    # 扫描器 DN 闸透传
        _cand("云舒雅", freq=37, score=90),                # S4 brands → 禁选
        _cand("卫生室", freq=18, score=80),                # S5 撞车 → 可选+必确认
        _cand("江城图书馆", freq=46, score=95),             # 正常未登记
        _cand("某某某", freq=99, score=99, person_likely=True),  # J19 不进展示
    ]
    items, stats = cf.build_review_items(cands, faces)
    by_word = {it.word: it for it in items}
    assert "某某某" not in by_word and stats["person_hidden"] == 1
    assert by_word["张"].hidden_reason == "候选词过短" and not by_word["张"].selectable
    assert by_word["定制"].hidden_reason == "命中通用词停用表"
    assert by_word["有限公司"].hidden_reason == "纯组织后缀"
    assert by_word["有限公司"].badge == cf.BADGE_PURE_SUFFIX
    assert by_word["20260913"].hidden_reason == "非专名形态"
    assert by_word["扫描器拦截词"].hidden_reason == "功能字闸"  # 原样透传
    assert by_word["扫描器拦截词"].is_hidden and not by_word["扫描器拦截词"].selectable

    # S4 已配置筛除（2026-09-16 碎词治理批：原为灰显禁选占位，维护者指令改
    # 筛除）：五面精确命中 → 不进清单，只计 stats["configured_filtered"]
    assert "云舒雅" not in by_word and stats["configured_filtered"] == 1

    ws = by_word["卫生室"]  # S5：撞车行可勾选 + 必弹二次确认 + 警示徽标
    assert ws.collision_org_suffix and ws.requires_confirm and ws.selectable
    assert ws.badge == cf.FACE_LABELS["org_suffix_adds"]

    gt = by_word["江城图书馆"]  # 正常行：可勾、无徽标、已登记面为 None
    assert gt.selectable and gt.already_configured is None and gt.badge == cf.BADGE_NONE

    assert stats["hidden_n"] == 5      # 张/定制/有限公司/20260913/扫描器拦截词
    assert stats["visible_n"] == 2     # 卫生室/江城图书馆（云舒雅已筛除）
    assert stats["unregistered_n"] == 2  # 卫生室(可选)/江城图书馆


def test_build_review_items_default_sort_j15():
    """默认排序（J15）：置信分降序，同分频次降序。"""
    cands = [_cand("甲甲", score=50, freq=5), _cand("乙乙", score=90, freq=1),
             _cand("丙丙", score=50, freq=9), _cand("丁丁", score=70, freq=1)]
    items, _ = cf.build_review_items(cands, _faces())
    assert [it.word for it in items] == ["乙乙", "丁丁", "丙丙", "甲甲"]


def test_build_review_items_simi_passes_s2(tmp_path):
    """司南特例：S2 同源集合不拦（-司南 差集）；brands 精确命中 → 筛除计数。"""
    faces = cf.RegistrationFaces(brands=("司南",), aliases=(), custom_words=(),
                                 mapping_keys=(), org_suffix_adds=())
    items, stats = cf.build_review_items([_cand("司南", freq=20, score=70)], faces)
    assert items == [] and stats["configured_filtered"] == 1


def test_s4b_covered_by_configured_matrix():
    """S4b 已配置覆盖筛选（2026-09-16）：已配置词 ⊂ 候选 → True（遮蔽引擎
    find 无边界匹配，短词已覆盖长候选全部出现）；反向（候选 ⊂ 配置词）→
    False（独立简称仍是真实需求，残片由扫描器外部容器闸管）；撞车面除外。"""
    faces = _faces()
    assert cf.covered_by_configured("云舒雅集团", faces)          # brands 云舒雅 ⊂
    assert cf.covered_by_configured("云舒雅家居股份有限公司", faces)
    assert cf.covered_by_configured("江城文库研究中心委员会", faces)  # custom ⊂
    assert not cf.covered_by_configured("城文库研究中心", faces)   # 反向：候选是配置词子串 → 不筛
    assert not cf.covered_by_configured("江城图书馆", faces)      # 与配置词无包含
    assert not cf.covered_by_configured("卫生室扩建办", faces)    # 撞车面不参与覆盖判定
    assert not cf.covered_by_configured("", faces)
    # 归一化口径：全角/空白差异下同键
    faces2 = cf.RegistrationFaces(brands=("江 图",), aliases=(), custom_words=(),
                                  mapping_keys=(), org_suffix_adds=())
    assert cf.covered_by_configured("江图联合分馆", faces2)       # 剥空白后包含


def test_s4b_covered_filters_items():
    """S4b 覆盖筛选进 build_review_items：被覆盖候选不进清单，计入
    stats["covered_filtered"]；独立简称（反向）保留。"""
    faces = _faces()
    cands = [
        _cand("云舒雅集团", freq=10, score=80),        # 覆盖 → 筛除
        _cand("城文库研究中心", freq=5, score=70),      # 反向 → 保留（真实独立需求）
        _cand("江城图书馆", freq=46, score=60),         # 无关 → 保留
    ]
    items, stats = cf.build_review_items(cands, faces)
    by_word = {it.word: it for it in items}
    assert "云舒雅集团" not in by_word and stats["covered_filtered"] == 1
    assert by_word["城文库研究中心"].selectable
    assert by_word["江城图书馆"].selectable
    assert stats["visible_n"] == 2 and stats["unregistered_n"] == 2


# ---------------------------------------------------------------------------
# 过滤 / 分页 / 可勾选集合
# ---------------------------------------------------------------------------

def _many_items(n: int) -> list:
    return [cf.ReviewItem(word=f"候选词{i:03d}", freq=n - i, score=100 - i)
            for i in range(n)]


def test_filter_items_substring_and_norm():
    items = _many_items(5)
    items.append(cf.ReviewItem(word="ＧＵＡＮＧ", score=1))
    assert len(cf.filter_items(items, "")) == 6
    assert [it.word for it in cf.filter_items(items, "003")] == ["候选词003"]
    assert [it.word for it in cf.filter_items(items, "guang")] == ["ＧＵＡＮＧ"]  # 全角归一后命中
    assert cf.filter_items(items, "不存在") == []


def test_pagination_slice_and_count():
    """分页切片（50/页）：页数、越界夹取、末页残行。"""
    items = _many_items(123)
    assert cf.page_count(123) == 3 and cf.page_count(0) == 1 and cf.page_count(50) == 1
    p1 = cf.page_slice(items, 1)
    p3 = cf.page_slice(items, 3)
    p9 = cf.page_slice(items, 9)          # 越界 → 末页
    assert len(p1) == 50 and len(p3) == 23 and len(p9) == 23
    assert p1[0].word == "候选词000" and p3[0].word == "候选词100"
    assert cf.page_slice(items, 0) == p1  # 页码夹取下界
    assert cf.page_slice(items, 2, per_page=10)[0].word == "候选词010"


def test_selectable_of_excludes_disabled():
    faces = _faces()
    items, _ = cf.build_review_items(
        [_cand("云舒雅"), _cand("江城图书馆"), _cand("定制"), _cand("卫生室")],
        faces)
    sel = cf.selectable_of(items)
    assert {it.word for it in sel} == {"江城图书馆", "卫生室"}


# ---------------------------------------------------------------------------
# 入口①推荐闸（≥3 且存在未登记候选 + 批指纹不重复提示）
# ---------------------------------------------------------------------------

def test_batch_fingerprint_stable_and_order_insensitive():
    a = cf.batch_fingerprint(["d:\\语料\\b.pdf", "d:\\语料\\a.pdf"])
    b = cf.batch_fingerprint(["d:/语料/a.pdf", "d:/语料/b.pdf"])
    c = cf.batch_fingerprint(["d:/语料/a.pdf"])
    assert a == b and a != c
    assert len(a) == 64  # sha256 hex


def test_should_recommend_gate_matrix():
    """N≥3 且存在未登记候选；同批指纹已提示 → False（降频防扰）。"""
    seen = set()
    fp = "fp-1"
    assert not cf.should_recommend(2, 5, fp, seen)   # 候选 <3
    assert not cf.should_recommend(3, 0, fp, seen)   # 无未登记候选
    assert cf.should_recommend(3, 1, fp, seen)
    cf.mark_batch_seen(fp, seen)
    assert not cf.should_recommend(10, 10, fp, seen)  # 同批不重复提示
    assert cf.should_recommend(10, 10, "fp-2", seen)  # 新批照常
    assert cf.should_recommend(3, 1, fp, None)        # 无记忆集时不拦


# ---------------------------------------------------------------------------
# mock ScanSession 全流程编排：勾选 → 防呆 → merge → 文案 → close 零持久化
# ---------------------------------------------------------------------------

@pytest.fixture()
def brands_env(tmp_path, monkeypatch):
    target = tmp_path / "brands.json"
    monkeypatch.setenv(ENV_OVERRIDE, str(target))
    return target


def _seed_brands(target: Path, words):
    target.write_text(json.dumps({"brands": list(words), "aliases": [],
                                  "domain_hints": [], "org_suffixes": ["卫生室"],
                                  "org_suffixes_remove": []},
                                 ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    return target


def test_mock_session_contract_surface():
    """mock ScanSession 契约面：visible 过滤 hidden+person；close 后零残留。"""
    texts = {0: "这是一段上下文江城图书馆出现在正文中"}
    cands = [MockCandidate(word="江城图书馆", freq=5, score=90,
                           first_loc={"file_idx": 0, "start": 9, "end": 14}),
             MockCandidate(word="隐词", hidden_reason="DN1"),
             MockCandidate(word="某人名", person_likely=True)]
    session = MockScanSession(cands, texts=texts, names=["甲.pdf", "乙.pdf", "丙.pdf"])
    assert [c.word for c in session.visible()] == ["江城图书馆"]
    ctx = session.context_text(cands[0], span=15)
    assert "江城图书馆" in ctx and len(ctx) <= 32
    assert session.source_files(cands[0], limit=2) == ["甲.pdf", "乙.pdf"]
    session.close()
    assert session.closed and session.candidates == [] and session.visible() == []
    assert session.context_text(cands[0]) == "" and session.source_files(cands[0]) == []


def test_full_flow_select_gates_merge_and_copy(brands_env):
    """全流程：mock 扫描 → 五面回填 → 展示端防呆 → 勾选可选项 → merge →
    成功文案（含计数、重复略过提示、无 ima 第 2 条）→ 日志行零词面。"""
    _seed_brands(brands_env, ["云舒雅", "司南"])
    faces = cf.RegistrationFaces(
        brands=("云舒雅", "司南"), aliases=(), custom_words=("旧词甲",),
        mapping_keys=(), org_suffix_adds=("卫生室",))
    session = MockScanSession(
        [MockCandidate(word="江城图书馆", freq=46, files_n=15, score=95),
         MockCandidate(word="江图", freq=41, files_n=12, score=88),
         MockCandidate(word="卫生室", freq=18, files_n=4, score=80),
         MockCandidate(word="云舒雅", freq=37, files_n=10, score=90)],
        names=["甲.pdf"])
    items, stats = cf.build_review_items(session.visible(), faces)
    # 未登记可选=江城图书馆/江图/卫生室（撞车行仍可勾选、计入未登记）；云舒雅禁选
    assert stats["unregistered_n"] == 3 and stats["hidden_n"] == 0
    # 勾选全部可选项（卫生室属 S5 需二次确认——编排测试直接视为已确认）
    selected = [it.word for it in cf.selectable_of(items)]
    assert set(selected) == {"江图", "江城图书馆", "卫生室"}
    outcome = cf.commit_selected(selected, ima_hint=False)
    result = outcome["result"]
    assert result.ok and set(result.written) == {"江图", "江城图书馆", "卫生室"}
    # S4 禁选面（云舒雅）不在 written：清单状态被落盘端校验尊重
    assert "云舒雅" not in result.written
    assert outcome["total_after"] == 5
    dlg = outcome["dialog"]
    assert dlg["title"] == cf.SUCCESS_TITLE
    assert "新增 3 个词" in dlg["body"] and "共 5 个词" in dlg["body"]
    assert "无需重启" in dlg["body"] and "重新执行一次脱敏" in dlg["body"]
    assert "ima" not in dlg["body"]          # ima_hint=False → 第 2 条不展示
    log = outcome["log_line"]
    assert "+3 词（总计 5" in log and "brands.json" in log
    for word in ("江图", "江城图书馆", "卫生室"):  # 日志零候选词面（红线）
        assert word not in log
    # 写后 brands 文件内容正确（合并写非覆盖：org_suffixes 原样）
    data = json.loads(brands_env.read_text(encoding="utf-8-sig"))
    assert data["brands"] == ["云舒雅", "司南", "江城图书馆", "江图", "卫生室"]
    assert data["org_suffixes"] == ["卫生室"]
    session.close()
    assert session.closed


def test_full_flow_commit_dup_skip_reports(brands_env):
    """跨键登记重复：勾选词与既有 aliases 重复 → 落盘端沉默略过 + 弹窗计数告知。"""
    _seed_brands(brands_env, ["江城图书馆"])
    faces = cf.RegistrationFaces(brands=("江城图书馆",), aliases=(), custom_words=(),
                                 mapping_keys=(), org_suffix_adds=())
    session = MockScanSession([MockCandidate(word="江城图书馆", freq=9, score=80),
                               MockCandidate(word="江图", freq=5, score=70)],
                              names=["甲.pdf"])
    items, _ = cf.build_review_items(session.visible(), faces)
    sel = [it.word for it in cf.selectable_of(items)]
    assert sel == ["江图"]  # 江城图书馆已登记 → 禁选（标记不去重）
    outcome = cf.commit_selected(["江图", "江城图书馆"], ima_hint=False)
    result = outcome["result"]
    assert result.ok and result.written == ["江图"]
    assert result.skipped_dup == ["江城图书馆"]
    assert "另有 1 条与既有登记重复，已略" in outcome["dialog"]["body"]


def test_full_flow_commit_occupied_degrades(brands_env, monkeypatch):
    """W7：写入被占用 → ok=False + 错误弹窗文案（人话、含「原配置未改动」）。"""
    _seed_brands(brands_env, [])

    def _occupied(src, dst, **kwargs):
        raise PermissionError(32, "占用")

    monkeypatch.setattr(bc.os, "replace", _occupied)
    outcome = cf.commit_selected(["江图"], ima_hint=False)
    result = outcome["result"]
    assert not result.ok and result.written == []
    assert outcome["dialog"]["title"] == cf.WRITE_FAIL_TITLE
    assert "占用" in outcome["dialog"]["body"] and "原配置未改动" in outcome["dialog"]["body"]
    assert json.loads(brands_env.read_text(encoding="utf-8-sig"))["brands"] == []


def test_scanner_missing_degrades_friendly():
    """扫描组件未就绪（S1 未合流态）：返回 (None, 人话提示)，绝不抛异常。"""
    mod, err = cf.load_scanner_module()
    if mod is None:
        assert err and "候选词扫描组件" in err and "手动填入" in err
        assert cf.SCANNER_NOT_READY_TITLE == "扫描组件未就绪"
    else:  # S1 已合流的环境：模块可导入且暴露契约签名
        assert hasattr(mod, "scan_sources")


def test_success_dialog_ima_conditional(brands_env):
    """交付复验建议第 2 条=条件展示（开源中性化后口径）：显式 hint 控制。"""
    _seed_brands(brands_env, [])
    outcome = cf.commit_selected(["江图"], ima_hint=True)
    assert "若脱敏产物已对外交付" in outcome["dialog"]["body"]
    outcome2 = cf.commit_selected(["江城图书馆"], ima_hint=False)
    assert "若脱敏产物已对外交付" not in outcome2["dialog"]["body"]


def test_zero_candidate_and_partial_copy():
    """异常降级文案（B 报告 §6）：零候选/全部失败/部分失败黄条/部分结果。"""
    z = cf.ZERO_CANDIDATE_BODY.format(m=15)
    assert "15" in z and "扫描件" in z and "手动填入" in z
    f = cf.ALL_FAILED_BODY.format(m=8)
    assert f.startswith("8 份文件均无法读取")
    p = cf.PARTIAL_BANNER.format(k=3)
    assert "3 份文件解析失败" in p
    n = cf.PARTIAL_RESULT_NOTE.format(m=10, total=15)
    assert "10/15" in n


def test_discover_entry_copy():
    """入口②常驻按钮与 ⓘ 说明文案（B 报告 §1.2 方案 A 原文口径）。"""
    assert cf.DISCOVER_BUTTON == "从文档发现候选词..."
    assert "扫描上方文件列表" in cf.DISCOVER_INFO
    assert "写入 brands.json" in cf.DISCOVER_INFO and "跨会话生效" in cf.DISCOVER_INFO
