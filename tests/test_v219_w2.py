# -*- coding: utf-8 -*-
"""v2.1.9 工程池 W2（开源 review R 系 P2 修复）回归：配置+UI+扫描层七项。

覆盖面（先红后绿锚，逐项对应 review 条目）：
  R1-P2-3  wordlists_private：FINMASK_WORDLISTS 显式指定但文件不存在 →
           警告改述事实（「指定的文件不存在：{env}」，旧文案「未设」
           与事实相反，误导排障方向）；
  R1-P2-4  brand_config：FINMASK_BRANDS 显式指定但路径不存在 → 一条
           cfg.warnings 人话警告；默认路径缺失保持零警告静默（常态）；
  R1-P2-5  缺档警告去重：装载器警告归因自身（stacklevel=1），默认过滤
           口径下 rules_engine 与 brand_config 两处模块级调用只警告一次
           （每位置一次），always 口径下仍每次可见（既有断言零影响）；
  R3-P1-1  candidate_scanner：meta.failed_files 计数产出（UI 黄条早已
           消费、扫描器此前从未生成该键）+ candidate_flow.PARTIAL_BANNER
           文案可兑现化（零持久化红线决定无日志可详）；
  R3-P2-4  candidate_scanner：>64 MiB 超大文件闸（跳过提取不阻塞批，
           meta.oversize_files 计数；测试用小阈值注入，不写真 65MB 文件）；
  R3-P2-1/2 ui_main_window：清除缓存两处文案（空输出目录指引 + 确认
           弹窗汇总映射表未删除说明）——字符串级断言（文案为方法内
           字面量而非模块常量，源码级绑定等价且零 GUI 依赖）。

运行：pytest tests/test_v219_w2.py
"""
import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------
# 环境隔离（沿 test_candidate_scanner._isolate_env 同款口径）
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """brands 配置与 G2 调试 env 全隔离；cwd 隔离以防相对路径夹具漂移。"""
    monkeypatch.setenv("FINMASK_BRANDS", "__no_such_brands__.json")
    for k in ("FINMASK_G2_FREQ_MIN", "FINMASK_G2_NGRAM_MAX",
              "FINMASK_G2_SINGLE_SUFFIX_MIN", "FINMASK_G2_DERIVE_MIN_FREQ"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)


# --------------------------------------------------------------------------
# R1-P2-3：wordlists env 显式指定缺档 → 事实文案
# --------------------------------------------------------------------------
def test_wordlists_env_missing_file_message(tmp_path, monkeypatch):
    """env 指向不存在路径 → 警告含「指定的文件不存在：{env}」，不再误称
    「未设」；返回值恒为全空 list（开源默认态）。"""
    env_path = tmp_path / "absent_wordlists.json"
    monkeypatch.setenv("FINMASK_WORDLISTS", str(env_path))
    from app import wordlists_private as wl
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        got = wl.load_private_wordlists()
    msgs = [str(w.message) for w in rec]
    hit = next((m for m in msgs if "指定的文件不存在" in m), None)
    assert hit is not None, msgs
    assert "FINMASK_WORDLISTS" in hit and str(env_path) in hit
    assert "未设" not in hit  # 误导方向已除（旧文案与事实相反）
    assert all(v == [] for v in got.values())


# --------------------------------------------------------------------------
# R1-P2-5：缺档警告每进程一次（默认过滤口径）
# --------------------------------------------------------------------------
def test_wordlists_missing_warn_once_under_default_filter(tmp_path, monkeypatch):
    """rules_engine 与 brand_config 模块级各调一次装载 → 同警告两遍是
    review 实锤。修法=警告归因装载器自身（stacklevel=1）：两调用点共享
    同一位置与 __warningregistry__，default 动作（=每位置一次）下只出一条。
    本用例以 default 口径连调两次复现级联，断言恰一条。"""
    monkeypatch.setenv("FINMASK_WORDLISTS", "")  # 同既有缺档态用例口径
    from app import wordlists_private as wl
    reg = getattr(wl, "__warningregistry__", None)
    if reg:  # 防此前 always/default 口径写过的注册表项串扰
        reg.clear()
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("default")
        wl.load_private_wordlists()
        wl.load_private_wordlists()
    hits = [w for w in rec if "wordlists_private" in str(w.message)
            and "未找到" in str(w.message)]
    assert len(hits) == 1, [str(w.message) for w in rec]
    # 归因契约锚：警告落在装载器自身位置（两调用点共享同位置=去重前提）
    assert "wordlists_private.py" in str(hits[0].filename)


# --------------------------------------------------------------------------
# R1-P2-4：brand_config env 显式指定缺档警告（默认缺档保持静默）
# --------------------------------------------------------------------------
def test_brand_config_env_missing_warns(tmp_path, monkeypatch):
    """FINMASK_BRANDS 显式指定但文件不存在 → 恰一条人话警告 + 空配置。"""
    monkeypatch.setenv("FINMASK_BRANDS", str(tmp_path / "nope_brands.json"))
    from app import brand_config as bc
    cfg = bc.load_brand_config()
    assert cfg.brands == [] and cfg.aliases == []
    assert len(cfg.warnings) == 1, cfg.warnings
    assert "指定的配置文件不存在" in cfg.warnings[0]
    assert str(tmp_path / "nope_brands.json") in cfg.warnings[0]


def test_brand_config_default_missing_stays_silent(tmp_path, monkeypatch):
    """默认路径缺失（用户未自定义=常态）零警告；显式传 path 的缺档场景
    行为不变（沿 test_brand_config.case_missing_file_silent 契约）。"""
    from app import brand_config as bc
    monkeypatch.delenv("FINMASK_BRANDS", raising=False)
    monkeypatch.setattr(bc, "brand_config_path",
                        lambda: tmp_path / "no_default_brands.json")
    cfg = bc.load_brand_config()
    assert cfg.warnings == [], cfg.warnings
    cfg2 = bc.load_brand_config(path=tmp_path / "explicit_missing.json")
    assert cfg2.warnings == [], cfg2.warnings


# --------------------------------------------------------------------------
# R3-P2-4：扫描器超大文件闸（小阈值注入，不写真 65MB 文件）
# --------------------------------------------------------------------------
def test_scanner_oversize_gate_skips_and_counts(tmp_path, monkeypatch):
    """>阈值文件在闸口回空、计入 meta.oversize_files；未超文件照常提取；
    跳过不阻塞批（F8 降级哲学）。"""
    from app import candidate_scanner as cs
    monkeypatch.setattr(cs, "_MAX_EXTRACT_BYTES", 32)  # 小阈值注入
    big = tmp_path / "超大.docx"
    big.write_bytes(b"0" * 64)          # 64 B > 32 B 阈值
    ok = tmp_path / "小.md"
    ok.write_text("云舒雅家居", encoding="utf-8")  # 15 B < 阈值
    assert cs._is_oversize(big) and not cs._is_oversize(ok)
    assert cs._extract_file(big) == ("", 0)  # 闸口前置检查本身
    sess = cs.scan_sources([big, ok])
    try:
        assert sess.meta["oversize_files"] == 1
        assert sess.meta["failed_files"] == 0
        assert sess.meta["files_n"] == 2      # 跳过仍计入文件数（空文本）
        assert sess.meta["total_chars"] >= len("云舒雅家居")  # 未超文件真被读
    finally:
        sess.close()


def test_scanner_oversize_default_threshold_is_64mib():
    """阈值常量契约：64 MiB（review 定档，防回归漂移）。"""
    from app import candidate_scanner as cs
    assert cs._MAX_EXTRACT_BYTES == 64 * 1024 * 1024


# --------------------------------------------------------------------------
# R3-P1-1：failed_files 计数（损坏文件触发 except → meta 计 1）
# --------------------------------------------------------------------------
def test_scanner_failed_files_counted(tmp_path):
    """坏 .docx（非 zip 字节流）→ _extract_file 抛异常被 F8 吞掉、
    meta.failed_files 计 1；不阻塞批（批内其余照常）。"""
    from app import candidate_scanner as cs
    bad = tmp_path / "坏.docx"
    bad.write_bytes(b"surely not a zip archive")
    good = tmp_path / "好.md"
    good.write_text("江城图书馆章程内容。", encoding="utf-8")
    sess = cs.scan_sources([bad, good])
    try:
        assert sess.meta["failed_files"] == 1
        assert sess.meta["oversize_files"] == 0
        assert sess.meta["files_n"] == 2
    finally:
        sess.close()


# --------------------------------------------------------------------------
# R3-P1-1：PARTIAL_BANNER 文案契约（可兑现表述）
# --------------------------------------------------------------------------
def test_partial_banner_copy_contract():
    """黄条文案含自查指引、不含「详见处理日志」（扫描器零持久化红线
    决定无日志可详）；{k} 占位与首句口径不变。"""
    from app import candidate_flow as cf
    b = cf.PARTIAL_BANNER
    assert "请检查文件是否被占用或损坏" in b
    assert "详见处理日志" not in b
    assert "3 份文件解析失败" in b.format(k=3)


# --------------------------------------------------------------------------
# R3-P2-1/2：清除缓存两处文案（字符串级；见模块头注说明）
# --------------------------------------------------------------------------
def test_ui_clear_cache_copy_strings():
    """①空输出目录报错补「若处理时输出目录留空，请填入当时源文件所在
    目录」（留空时缓存/台账写在源文件目录——analysis_cache 同源口径）；
    ②确认弹窗补「汇总映射表未删除（复核重跑需要），如需清除请手动删除」。
    文案为方法内字面量而非模块常量，源码级断言等价绑定且零 GUI 依赖。"""
    src = (ROOT / "app" / "ui_main_window.py").read_text(encoding="utf-8")
    # ①：原句仍在（补句非替换）+ 留空指引在
    assert "再清除分析缓存" in src
    assert "若处理时输出目录留空，请填入当时源文件所在目录" in src
    # ②：确认弹窗补半句在
    assert "汇总映射表未删除（复核重跑需要），如需清除请手动" in src
    assert "确定清除吗？" in src
