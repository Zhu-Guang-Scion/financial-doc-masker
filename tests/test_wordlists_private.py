# -*- coding: utf-8 -*-
"""词表数据外置工程测试（评审决策 1b 第二步 · 先红后绿）。

断言三面：
  ① 装载器：env 链/缺档响亮警告（开源默认态）/unknown-key 防呆；
  ② 合并点：带外置档时各表条目并入（等价于外置前全量）；缺档=纯通用默认；
  ③ 源码卫生：外置条目字面零残留在入库源码（词表 v3 重洗的前提）。
"""
import json
import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 语料条目字面（本测试文件随开源仓库发布——用代称锚，勿写真词全形）
_STOP_EXCEPTION_KEY = "config_stop_exception"
_GIVEN_EXTRA_KEY = "given_chars_extra"


def _sample_file(tmp_path, extra=None):
    data = {"config_stop_exception": ["示例特例词"],
            "given_chars_extra": ["甲乙丙"],
            "single_surnames_extra": ["阙"],
            "non_name_blacklist_extra": ["示例碎片"],
            "company_stem_blacklist_extra": ["示例字号"],
            "non_name_whitelist_person_extra": ["示例真名"],
            "brand_generic_tails_extra": ["示例尾词"],
            "biz_cat_words_extra": ["示例品类"],
            "biz_unit_stop_extra": ["示例泛称"],
            "unknown_key_for_guard": ["x"]}
    data.update(extra or {})
    p = tmp_path / "wordlists_private.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_load_env_chain_and_merge(tmp_path, monkeypatch):
    p = _sample_file(tmp_path)
    monkeypatch.setenv("FINMASK_WORDLISTS", str(p))
    from app import wordlists_private as wl
    got = wl.load_private_wordlists()
    assert got["config_stop_exception"] == ["示例特例词"]
    assert wl.chars_joined("given_chars_extra") == "甲乙丙"


def test_missing_file_loud_warning(monkeypatch):
    monkeypatch.setenv("FINMASK_WORDLISTS", "")
    monkeypatch.chdir(tmp_home := Path("."))
    from app import wordlists_private as wl
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        got = wl.load_private_wordlists()
    assert any("wordlists_private" in str(w.message) and "未找到" in str(w.message)
               for w in rec), [str(w.message) for w in rec]
    assert all(v == [] for v in got.values())


def test_unknown_key_guard(tmp_path, monkeypatch):
    p = _sample_file(tmp_path)
    monkeypatch.setenv("FINMASK_WORDLISTS", str(p))
    from app import wordlists_private as wl
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        wl.load_private_wordlists()
    assert any("unknown_key_for_guard" in str(w.message) for w in rec)


def test_brand_config_consumes_private_exception(tmp_path, monkeypatch):
    """合并点①：CONFIG_CHANNEL_STOP_EXCEPTION 来自外置（带档=特例在/缺档=空）。"""
    p = _sample_file(tmp_path)
    monkeypatch.setenv("FINMASK_WORDLISTS", str(p))
    import importlib
    import app.brand_config as bc
    importlib.reload(bc)
    assert "示例特例词" in bc.CONFIG_CHANNEL_STOP_EXCEPTION
    assert bc._config_channel_forbidden_words() <= bc.write_forbidden_set()


def test_source_has_no_externalized_literals():
    """③ 源码卫生：外置条目零残留（词面以首字+* 代称锚定，词表 v3 前提）。"""
    import inspect
    import app.rules_engine as re_mod
    import app.brand_config as bc
    src_re = inspect.getsource(re_mod)
    src_bc = inspect.getsource(bc)
    # 外置验证锚=词面构造法（源码零字面量；司南为替身通用词不收）
    anchors = ["司" + "米", "李确实", "明曰", "华信高", "石科院", "高科",
               "厨柜", "橱柜", "门业", "衣柜", "厨电", "卫浴", "门窗",
               "文光普亭植", "逸运", "家" + "忠" + "威"]
    def _code_lines(src):
        out = []
        for ln in src.splitlines():
            t = ln.strip()
            if not t or t.startswith("#"):
                continue
            body = ln.split("#", 1)[0]  # 剥行内注释（词面判定面=代码本体）
            if body.strip():
                out.append(body)
        return out
    code_re = _code_lines(src_re)
    code_bc = _code_lines(src_bc)
    offenders = [a for a in anchors
                 if any(a in ln for ln in code_re) or any(a in ln for ln in code_bc)]
    assert offenders == [], offenders  # 词面本体零残留（注释形态史不计）
