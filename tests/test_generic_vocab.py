# -*- coding: utf-8 -*-
"""通用业务语汇表 G（app/generic_vocab.py）守卫测试（G2/S1）。

覆盖（任务书交付物 3）：
- 来源格式守卫：种子「表名:行号」/ 补种「补种:理由」两种格式逐条校验；
- 来源对账：种子条目的行号逐一回查 app/rules_engine.py 原文（引号 token
  精确匹配，维护者抽检口径的程序化前置）；
- 地名排除硬约束：_PLACE_NAMES/_PLACE_WORDS 中的词不得入表；
- 判例词不可被 G 完整切分（DN2 不误杀云舒雅/江图类的表级守卫）；
- 编译面：GENERIC_VOCAB frozenset 与显式枚举表一致、规模下限。

运行：pytest tests/test_generic_vocab.py 或 python tests/test_generic_vocab.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import generic_vocab as gv
from app.rules_engine import (
    _PLACE_NAMES,
    _PLACE_WORDS,
    _org_suffix_full_cover,
)

# 判例词全集（任务书公开判例，合成验证锚）
EXEMPLARS = ("云舒雅", "司南", "慕蓝朵", "云住良材", "江图", "江城市图书馆",
             "江城图书馆", "江城文库研究中心", "中共江城图书馆委员会",
             "云舒雅家居", "司南橱柜", "慕蓝朵直营专卖店")

_SEED_RE = re.compile(r"^(_?[A-Z_]+):(\d+)$")
_SEED_TABLES = {"NON_NAME_BLACKLIST", "_BIZ_UNIT_STOP", "_BRAND_GENERIC_TAILS",
               "_ENUM_ORG_SUFFIXES", "_INDUSTRY_WORDS", "COMMON_WORDS",
               "GIVEN_CHARS", "SINGLE_SURNAMES", "_BIZ_CAT_WORDS",
               "_COMPANY_STEM_BLACKLIST", "_COMPANY_NAME_PREFIX_BLACKLIST"}  # 外置工程后行号漂移重锚，来源表白名单扩至全部词表常量


def test_source_format_guard():
    """逐条来源依据格式：种子「表名:行号」或「补种:理由一句话」。"""
    assert len(gv.GENERIC_VOCAB_TABLE) >= 250, len(gv.GENERIC_VOCAB_TABLE)
    for word, src in gv.GENERIC_VOCAB_TABLE:
        assert isinstance(word, str) and word, (word, src)
        m = _SEED_RE.match(src)
        if m:
            assert m.group(1) in _SEED_TABLES, (word, src)
            assert int(m.group(2)) > 0, (word, src)
        else:
            assert src.startswith("补种:") and len(src) > len("补种:"), (word, src)


def test_seed_line_numbers_audit():
    """种子条目行号逐一回查 rules_engine.py：该行引号 token 精确含此词。"""
    lines = (ROOT / "app" / "rules_engine.py").read_text(encoding="utf-8").splitlines()
    token_cache: dict = {}

    def tokens(ln: int) -> set:
        if ln not in token_cache:
            token_cache[ln] = set(re.findall(r'"([^"]+)"', lines[ln - 1]))
        return token_cache[ln]

    checked = 0
    for word, src in gv.GENERIC_VOCAB_TABLE:
        m = _SEED_RE.match(src)
        if not m:
            continue
        name, ln = m.group(1), int(m.group(2))
        assert 1 <= ln <= len(lines), (word, src)
        assert word in tokens(ln), f"行号对账失败: {word} -> {src}"
        checked += 1
    assert checked >= 140, checked  # 种子复用条目规模下限


def test_place_exclusion_hard_constraint():
    """地名排除硬约束：_PLACE_NAMES/_PLACE_WORDS 同源排除（契约 §1）。"""
    for word, _src in gv.GENERIC_VOCAB_TABLE:
        assert word not in _PLACE_NAMES, word
        assert word not in _PLACE_WORDS, word
    # 刻意不收的关键非种子：广州/图书 不在 G —— 江城图书馆 类的存活锚
    for w in ("广州", "图书", "中共", "文汇", "云舒雅", "司南", "慕蓝朵"):
        assert w not in gv.GENERIC_VOCAB, w


def test_exemplars_not_g_coverable():
    """判例词不可被 G 完整切分（DN2 表级守卫：含区分性成分 → 覆盖失败）。"""
    for w in EXEMPLARS:
        assert not _org_suffix_full_cover(w, gv.GENERIC_VOCAB, gv.GENERIC_VOCAB_MAXLEN), w


def test_generic_shapes_coverable():
    """对照面：通用部门词躯干确实可被 G 完整切分（DN2 闸有牙）。
    注：党委会 类党务头词经 DN2 部门头路径隐藏（去头躯干为空 → 视为覆盖），
    不属本表覆盖判据。"""
    for w in ("财务管理", "财务管理中心", "人力资源", "项目部", "技术部"):
        assert _org_suffix_full_cover(w, gv.GENERIC_VOCAB, gv.GENERIC_VOCAB_MAXLEN), w


def test_compiled_set_consistency():
    """编译面：frozenset 与显式枚举一致；无重复词条（去重后）。"""
    assert isinstance(gv.GENERIC_VOCAB, frozenset)
    words = [w for w, _ in gv.GENERIC_VOCAB_TABLE]
    assert len(words) == len(set(words))
    assert gv.GENERIC_VOCAB == set(words)
    assert gv.GENERIC_VOCAB_MAXLEN == max(len(w) for w in words)


def test_composition_counts():
    """构成统计口径：种子复用 + 补种，两类均达设计书规模（A 报告 §6）。"""
    seed_n = sum(1 for _w, s in gv.GENERIC_VOCAB_TABLE if _SEED_RE.match(s))
    plus_n = sum(1 for _w, s in gv.GENERIC_VOCAB_TABLE if s.startswith("补种:"))
    assert seed_n >= 140, seed_n
    # 设计书基线 100-200 + g2 实考四轮补种（每词带实证理由，维护者抽检口径）
    assert 100 <= plus_n <= 300, plus_n
    assert seed_n + plus_n == len(gv.GENERIC_VOCAB_TABLE)


if __name__ == "__main__":
    import traceback
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print("FAILURES:", failures)
    sys.exit(1 if failures else 0)
