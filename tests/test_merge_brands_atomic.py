# -*- coding: utf-8 -*-
"""merge_brands_atomic 落盘端七闸（W1-W7）测试矩阵（v2.1.0 G2-S2）。

契约 §二：写入端在「不信任展示端状态」的前提下全量重跑装载端同规格校验；
合并写非覆盖（仅 brands 尾追加，五键+未知键逐字保留）；原子写；同一解析
路径；写后回读校验；占用降级。判据词一律用已公开判例词（云舒雅/司南/江图/
江城图书馆），测试产物全部落 tmp_path（零持久化红线：不落仓库目录）。

运行：python -m pytest tests/test_merge_brands_atomic.py -q
（用例依赖 pytest tmp_path/monkeypatch 夹具，官方入口=pytest。）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import brand_config as bc
from app.brand_config import (ENV_OVERRIDE, MergeResult, _norm_key,
                              load_brand_config, merge_brands_atomic,
                              resolve_config_path, write_forbidden_set)
from app.rules_engine import _BRAND_STOP_ALL, NON_NAME_BLACKLIST

# 与 rules_engine.__init__ 装载点逐字同一表达式（同源工程红线的测试侧锚）
from app.brand_config import CONFIG_CHANNEL_FORBIDDEN_WORDS as _CCFW
ENGINE_FORBIDDEN = _CCFW | frozenset(NON_NAME_BLACKLIST)  # 权威常量同源（外置档注入例外）


@pytest.fixture(autouse=True)
def _isolated_brands_env(tmp_path, monkeypatch):
    """每条用例：FINMASK_BRANDS 指向用例私有 tmp 文件（未写不建文件），
    monkeypatch 用完自动恢复现场——测试零落仓库目录（零持久化红线）。"""
    target = tmp_path / "brands.json"
    monkeypatch.setenv(ENV_OVERRIDE, str(target))
    yield target


def _write_cfg(p: Path, obj) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(obj if isinstance(obj, str) else
                 json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
    return p


def _read_cfg(p: Path):
    return json.loads(p.read_text(encoding="utf-8-sig"))


def _raw_bytes(p: Path) -> bytes:
    return p.read_bytes()


def _five_key_template(brands):
    return {"brands": list(brands), "aliases": [], "domain_hints": [],
            "org_suffixes": [], "org_suffixes_remove": []}


def _no_leftover_tmp(target: Path):
    leftovers = [p for p in target.parent.glob(".brands_*.tmp") if p.is_file()]
    assert leftovers == [], leftovers


# ---------------------------------------------------------------------------
# W1 写前全量重跑装载端同规格校验
# ---------------------------------------------------------------------------

def test_w1_forbidden_set_same_source_as_loader():
    """同源工程红线：写入端停用集与引擎装载点实参同一表达式（例外=权威常量）。"""
    from app.brand_config import CONFIG_CHANNEL_STOP_EXCEPTION as _EXC
    assert write_forbidden_set() == frozenset(ENGINE_FORBIDDEN)
    for exc in _EXC:
        assert exc in _BRAND_STOP_ALL and exc not in write_forbidden_set()
    assert "华信高" in write_forbidden_set()  # NON_NAME_BLACKLIST 成员
    extra = write_forbidden_set({"自有停用词"})
    assert "自有停用词" in extra and extra >= write_forbidden_set()


def test_w1_rejects_bad_words_keeps_good(tmp_path):
    """坏词拒绝：通用词/过短/空白/非文本 → 人话警告逐条拒绝；好词照常写入。"""
    target = Path(os.environ[ENV_OVERRIDE])
    _write_cfg(target, _five_key_template(["云舒雅"]))
    r = merge_brands_atomic(["公司", "定制", "张", "", "   ", 123, None,
                             "江图"])
    assert r.ok and r.written == ["江图"], (r.written, r.warnings)
    assert "配置项「公司」过于宽泛，遮蔽它可能误伤大量正常文本，已忽略" in r.warnings
    assert "配置项「定制」过于宽泛，遮蔽它可能误伤大量正常文本，已忽略" in r.warnings
    assert "配置项「张」太短，遮蔽它容易误伤大片正常文本，已忽略（至少 2 个字）" in r.warnings
    # ""/"   "（空白）+ 123/None（非文本）→ 同一句忽略警告共 4 条
    assert r.warnings.count("配置项存在空白或非文本条目，已忽略") == 4, r.warnings
    cfg = load_brand_config(path=target)
    assert cfg.brands == ["云舒雅", "江图"], cfg.brands


def test_w1_simi_special_case_allowed(tmp_path):
    """司南特例：装载点差集放行——配置通道写入「司南」必须收（镜像漂移防线）。"""
    target = Path(os.environ[ENV_OVERRIDE])
    _write_cfg(target, _five_key_template([]))
    r = merge_brands_atomic(["司南"])
    assert r.ok and r.written == ["司南"], (r.written, r.warnings)
    assert load_brand_config(path=target).brands == ["司南"]


def test_w1_all_rejected_writes_nothing(tmp_path):
    """全部被拒 → ok=True（无致命错误）但零写入、零文件改动。"""
    target = Path(os.environ[ENV_OVERRIDE])
    _write_cfg(target, _five_key_template(["云舒雅"]))
    before = _raw_bytes(target)
    r = merge_brands_atomic(["公司", "张"])
    assert r.ok and r.written == []
    assert "没有新增词可写入" in r.warnings[-1]
    assert _raw_bytes(target) == before


def test_w1_empty_input_no_op(tmp_path):
    """空输入 → 零警告拒绝、零文件创建（ok=True 空操作）。"""
    target = Path(os.environ[ENV_OVERRIDE])
    r = merge_brands_atomic([])
    assert r.ok and r.written == [] and r.skipped_dup == []
    assert not target.exists()


def test_w1_extra_forbidden_rejects(tmp_path):
    """extra_forbidden 追加停用词照常生效（调用方可注入自有停用面）。"""
    target = Path(os.environ[ENV_OVERRIDE])
    _write_cfg(target, _five_key_template([]))
    r = merge_brands_atomic(["江图"], extra_forbidden={"江图"})
    assert r.ok and r.written == []
    assert any("江图」过于宽泛" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# W2 归一化去重（组内 + 跨 brands/aliases，_norm_key 口径，保留首次）
# ---------------------------------------------------------------------------

def test_w2_dedup_in_group_and_cross_key(tmp_path):
    """组内（含全角空白等归一化等价）与跨 brands/aliases 重复 → 沉默略过，
    skipped_dup 收原始词面（界面汇总告知用），不进 warnings。"""
    target = Path(os.environ[ENV_OVERRIDE])
    _write_cfg(target, {"brands": ["司南"], "aliases": ["广 图"],
                        "domain_hints": [], "org_suffixes": [],
                        "org_suffixes_remove": []})
    r = merge_brands_atomic(["江图", "江图", "江　图", "云舒雅", "江城图书馆"])
    assert r.ok
    # 清洗后判例词虚构化：江图不在停用集——归一同键去重语义不变
    assert r.written == ["江图", "云舒雅", "江城图书馆"], r.written
    assert r.skipped_dup == ["江图", "江　图"], r.skipped_dup  # 全角变体归一同键去重
    # 每条被略过的入参原始词面都进 skipped_dup（界面「{n} 条…已略」计数口径）

    assert not any("重复" in w for w in r.warnings)  # 沉默去重（loader 同语义）
    cfg = load_brand_config(path=target)
    assert cfg.brands == ["司南", "江图", "云舒雅", "江城图书馆"]


def test_w2_norm_key_fullwidth_variant_equivalence(tmp_path):
    """_norm_key 口径：全角/半角变体（NFKC 归一）与既有登记同键 → 略过。"""
    target = Path(os.environ[ENV_OVERRIDE])
    _base = "ACME-B品牌" + "中心"
    _variant = "ＡＣＭＥ-B品牌" + "中心"  # 全角字母，NFKC→半角同键
    _write_cfg(target, _five_key_template([_base]))
    r = merge_brands_atomic([_variant])
    assert r.written == [] and r.skipped_dup == [_variant],         (r.written, r.skipped_dup)
    assert _norm_key(_variant) == _norm_key(_base)



def test_w3_merge_preserves_all_other_keys_verbatim(tmp_path):
    """G1 双键 + v1 三键 + 未知键 + 键序：合并后除 brands 外逐字原样。"""
    target = Path(os.environ[ENV_OVERRIDE])
    original = {
        "aliases": ["阿索"],
        "domain_hints": ["acme", "acme"],
        "org_suffixes": ["卫生室", "分院"],
        "org_suffixes_remove": ["院"],
        "custom_unknown_key": {"嵌套": [1, 2, {"x": None}]},
        "brands": ["云舒雅"],
    }  # 故意非字母序：键序亦应保留
    _write_cfg(target, original)
    r = merge_brands_atomic(["江图", "江城图书馆"])
    assert r.ok and r.written == ["江图", "江城图书馆"]
    after = _read_cfg(target)
    assert list(after.keys()) == list(original.keys())  # 键序保留
    for key in ("aliases", "domain_hints", "org_suffixes",
                "org_suffixes_remove", "custom_unknown_key"):
        assert after[key] == original[key], key  # 逐字等值
    assert after["brands"] == ["云舒雅", "江图", "江城图书馆"]  # 旧词保序在前、尾追加


def test_w3_existing_non_list_brands_key(tmp_path):
    """既有 brands 键非列表（loader 语义=警告+忽略该项）→ 按空表处理写入新词，
    其余键不受牵连。"""
    target = Path(os.environ[ENV_OVERRIDE])
    _write_cfg(target, {"brands": "云舒雅", "aliases": ["阿索"],
                        "domain_hints": []})
    r = merge_brands_atomic(["江图"])
    assert r.ok and r.written == ["江图"]
    assert any("brands」键不是列表" in w for w in r.warnings)
    after = _read_cfg(target)
    assert after["brands"] == ["江图"] and after["aliases"] == ["阿索"]


def test_w3_refuses_bad_json_and_non_dict(tmp_path):
    """坏 JSON / 顶层非对象 → 拒绝写入（合并写不能建立在不可解析文件上），
    ok=False、原文件逐字节不动。"""
    target = Path(os.environ[ENV_OVERRIDE])
    for bad in ('{"brands": ["云舒雅",', '["云舒雅"]'):
        _write_cfg(target, bad)
        before = _raw_bytes(target)
        r = merge_brands_atomic(["江图"])
        assert not r.ok and r.written == []
        assert any("不是有效的 JSON" in w or "顶层不是 JSON 对象" in w
                   for w in r.warnings)
        assert _raw_bytes(target) == before


# ---------------------------------------------------------------------------
# W4 原子写 + W7 占用/只读降级（写失败旧文件原样、零临时文件残留）
# ---------------------------------------------------------------------------

def test_w4_w7_occupied_permission_error_keeps_old_file(tmp_path, monkeypatch):
    """W7 占用模拟：os.replace 抛 PermissionError → ok=False + 人话警告，
    旧文件逐字节原样、无半写、无临时文件残留。"""
    target = Path(os.environ[ENV_OVERRIDE])
    _write_cfg(target, _five_key_template(["云舒雅"]))
    before = _raw_bytes(target)

    def _occupied(src, dst, **kwargs):
        raise PermissionError(32, "另一个程序正在使用此文件，进程无法访问。")

    monkeypatch.setattr(bc.os, "replace", _occupied)
    r = merge_brands_atomic(["江图"])
    assert not r.ok and r.written == []
    assert any("占用" in w and "原配置未改动" in w for w in r.warnings), r.warnings
    assert _raw_bytes(target) == before
    _no_leftover_tmp(target)


def test_w4_w7_generic_oserror_degrades(tmp_path, monkeypatch):
    """W7 一般 OSError（磁盘满/只读目录等）→ ok=False + 原文件不动。"""
    target = Path(os.environ[ENV_OVERRIDE])
    _write_cfg(target, _five_key_template(["云舒雅"]))
    before = _raw_bytes(target)

    def _disk_full(src, dst, **kwargs):
        raise OSError(28, "设备上没有空间。")

    monkeypatch.setattr(bc.os, "replace", _disk_full)
    r = merge_brands_atomic(["江图"])
    assert not r.ok
    assert any("无法写入" in w for w in r.warnings)
    assert _raw_bytes(target) == before
    _no_leftover_tmp(target)


def test_w4_w7_read_occupied_degrades(tmp_path, monkeypatch):
    """W7 读端占用：读原文失败 → ok=False，不写不崩。"""
    target = Path(os.environ[ENV_OVERRIDE])
    _write_cfg(target, _five_key_template(["云舒雅"]))

    def _read_fail(self, *a, **k):
        raise PermissionError(32, "占用")

    monkeypatch.setattr(Path, "read_text", _read_fail)
    r = merge_brands_atomic(["江图"])
    monkeypatch.undo()
    assert not r.ok and r.written == []
    assert any("无法读取" in w for w in r.warnings), r.warnings
    assert "江图" not in _read_cfg(target)["brands"]


def test_w4_tmp_cleanup_on_failure(tmp_path, monkeypatch):
    """mkstemp 成功后写/替换阶段失败 → 临时文件被清理、旧文件原样。"""
    target = Path(os.environ[ENV_OVERRIDE])
    _write_cfg(target, _five_key_template([]))

    real_replace = bc.os.replace

    def _replace_fail(src, dst, **kwargs):
        # 临时文件此刻真实存在（mkstemp 已落盘）——模拟替换瞬间被占用
        assert Path(src).exists()
        raise PermissionError(32, "占用")

    monkeypatch.setattr(bc.os, "replace", _replace_fail)
    r = merge_brands_atomic(["江图"])
    monkeypatch.setattr(bc.os, "replace", real_replace)
    assert not r.ok
    _no_leftover_tmp(target)
    assert _read_cfg(target)["brands"] == []


# ---------------------------------------------------------------------------
# W5 写入目标走 resolve_config_path 同一解析（None → 默认路径）
# ---------------------------------------------------------------------------

def test_w5_env_priority(tmp_path):
    """FINMASK_BRANDS 优先（与读侧同一解析）：写入落在 env 指向文件。"""
    target = Path(os.environ[ENV_OVERRIDE])
    r = merge_brands_atomic(["江图"])
    assert r.ok
    assert target.exists() and _read_cfg(target)["brands"] == ["江图"]


def test_w5_none_falls_back_to_default_path(tmp_path, monkeypatch):
    """env 未设且默认路径不存在（resolve→None）→ 落 brand_config_path() 默认
    路径（测试内替换到 tmp，绝不写仓库真实默认路径）。"""
    monkeypatch.delenv(ENV_OVERRIDE, raising=False)
    fake_default = tmp_path / "default" / "brands.json"
    monkeypatch.setattr(bc, "brand_config_path", lambda: fake_default)
    assert resolve_config_path() is None
    r = merge_brands_atomic(["江图"])
    assert r.ok
    assert fake_default.exists()
    cfg = load_brand_config(path=fake_default)
    assert cfg.brands == ["江图"] and cfg.warnings == []
    assert json.loads(fake_default.read_text(encoding="utf-8")) == \
        _five_key_template(["江图"])


# ---------------------------------------------------------------------------
# W6 写后回读校验（新词在生效列表 + 零新增警告）
# ---------------------------------------------------------------------------

def test_w6_readback_success_zero_new_warnings(tmp_path):
    """正常路径：回读每个新词在生效 brands 列表、零新增警告、ok=True。"""
    target = Path(os.environ[ENV_OVERRIDE])
    _write_cfg(target, _five_key_template(["云舒雅"]))
    r = merge_brands_atomic(["江图", "江城图书馆"])
    assert r.ok
    cfg = load_brand_config(path=target, forbidden_words=ENGINE_FORBIDDEN)
    assert all(w in cfg.brands for w in r.written)
    assert cfg.warnings == []


def test_w6_readback_mismatch_fails_loud(tmp_path, monkeypatch):
    """回读不一致（模拟：回读调用返回缺词配置）→ ok=False + 「校验未通过」
    人话警告（响亮优于静默）。"""
    target = Path(os.environ[ENV_OVERRIDE])
    _write_cfg(target, _five_key_template([]))

    from app.brand_config import BrandConfig
    real_load = bc.load_brand_config
    calls = {"n": 0}

    def _shaky_load(*a, **k):
        cfg = real_load(*a, **k)
        calls["n"] += 1
        if calls["n"] >= 2:  # 第 2 次=写入后的回读校验调用
            broken = BrandConfig()
            broken.warnings = list(cfg.warnings)
            return broken  # brands 凭空丢失 → 回读缺失
        return cfg

    monkeypatch.setattr(bc, "load_brand_config", _shaky_load)
    r = merge_brands_atomic(["江图"])
    monkeypatch.undo()
    assert not r.ok and r.written == []
    assert any("校验未通过" in w and "回读缺失 1 条" in w for w in r.warnings), \
        r.warnings


def test_w6_preexisting_warnings_not_counted_as_new(tmp_path):
    """文件既有脏条目产生的基线警告（未知键等）在写入后不得被误判为「新增」。"""
    target = Path(os.environ[ENV_OVERRIDE])
    _write_cfg(target, {"brands": ["云舒雅"], "org_suffix": ["打错的键名"],
                        "aliases": [], "domain_hints": []})
    base_n = len(load_brand_config(
        path=target, forbidden_words=ENGINE_FORBIDDEN).warnings)
    assert base_n >= 1
    r = merge_brands_atomic(["江图"])
    assert r.ok, r.warnings
    after = load_brand_config(path=target, forbidden_words=ENGINE_FORBIDDEN)
    assert len(after.warnings) == base_n  # 零新增（既有未知键警告原样，不重复计）
    assert after.brands == ["云舒雅", "江图"]


# ---------------------------------------------------------------------------
# 首次创建（文件缺失常态路径）+ 原文件编码兼容 + 契约签名
# ---------------------------------------------------------------------------

def test_first_write_creates_five_key_schema(tmp_path):
    """缺失文件（常态）→ 首次写入落成五键 schema 文件，读侧零警告。"""
    target = Path(os.environ[ENV_OVERRIDE])
    assert not target.exists()
    r = merge_brands_atomic(["江图"])
    assert r.ok
    assert set(_read_cfg(target).keys()) == {
        "brands", "aliases", "domain_hints", "org_suffixes",
        "org_suffixes_remove"}
    assert load_brand_config(path=target).warnings == []


def test_bom_file_merged_without_bom_loss(tmp_path):
    """记事本 BOM 文件（utf-8-sig 读侧容忍）→ 合并写后读侧仍零警告。"""
    target = Path(os.environ[ENV_OVERRIDE])
    target.write_text(json.dumps({"brands": ["云舒雅"]}, ensure_ascii=False),
                      encoding="utf-8-sig")
    r = merge_brands_atomic(["江图"])
    assert r.ok
    cfg = load_brand_config(path=target)
    assert cfg.brands == ["云舒雅", "江图"] and cfg.warnings == []


def test_merge_result_shape():
    """契约签名：MergeResult 四字段（written/skipped_dup/warnings/ok）。"""
    r = MergeResult()
    assert list(r.__dataclass_fields__.keys()) == \
        ["written", "skipped_dup", "warnings", "ok"]
    assert _norm_key("（江图）") == _norm_key("江图")
