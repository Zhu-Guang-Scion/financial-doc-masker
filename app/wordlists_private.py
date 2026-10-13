# -*- coding: utf-8 -*-
"""语料治理条目外置档装载器（词表数据外置工程 · 2026-09-21）。

定位：代码内功能性词表只装**通用默认值**；案例语料来源的治理条目（品牌/
人名/机构碎片/行业形状等）全部外置到不入库数据文件，与 brands.json 同构：

  加载链：env FINMASK_WORDLISTS → 程序目录 wordlists_private.json → 缺省空

缺档响亮提示（防「外置文件缺失=行为静默退化」——FINMASK_BRANDS 断链教训
第五条）：缺档时经 warnings.warn 提示一次「语料治理条目未加载（开源默认
态）」，不阻断主流程；工装与测试可断言该警告。

键契约（与私档 wordlists_private.json 一一对应；unknown-key 告警沿
brand_config 防呆先例）：
  config_stop_exception          配置通道停用集特例例外（brand_config）
  given_chars_extra              名用字表语料增补段（rules_engine）
  single_surnames_extra          单姓表语料增补（rules_engine）
  non_name_blacklist_extra       姓氏开头非人名黑名单·机构碎片（rules_engine）
  company_stem_blacklist_extra   公司字号黑名单·语料条目（rules_engine）
  non_name_whitelist_person_extra 业务尾字真名白名单（rules_engine）
  brand_generic_tails_extra      品牌前缀行业尾词·家居形状（rules_engine）
  biz_cat_words_extra            品类词表·家居形状（rules_engine）

本模块禁止反向 import app 内其它模块（被 rules_engine/brand_config 模块级
调用，防环）。
"""
from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path

_KNOWN_KEYS = frozenset({
    "config_stop_exception", "given_chars_extra", "single_surnames_extra",
    "non_name_blacklist_extra", "company_stem_blacklist_extra",
    "non_name_whitelist_person_extra", "brand_generic_tails_extra",
    "biz_cat_words_extra",
})

def _resolve_path() -> Path | None:
    env = os.environ.get("FINMASK_WORDLISTS", "").strip()
    if env:
        p = Path(env)
        return p if p.is_file() else None
    for cand in (Path("wordlists_private.json"),
                 Path(sys.executable).parent / "wordlists_private.json",
                 Path.cwd() / "wordlists_private.json"):
        if cand.is_file():
            return cand
    return None


def load_private_wordlists() -> dict:
    """装载外置档；缺档=空 dict+一次响亮警告。返回值各键恒为 list（缺省空）。

    v2.1.9：缺档警告按成因分诊——env FINMASK_WORDLISTS 显式指定
    但文件不存在时，原文案「FINMASK_WORDLISTS 未设且程序目录无档」与
    事实相反，误导排障方向；改述事实（指定的文件不存在）。

    v2.1.9：缺档警告改 stacklevel=1（归因到本装载器自身）。此前
    stacklevel=2 归因到调用方，而 rules_engine 与 brand_config 在各自
    模块级都调用本函数（装载级联），同一警告被归因到两个位置 → 默认
    过滤器（default 动作=每位置一次）下打两遍。归因到本模块后两个调用
    点共享同一位置与 __warningregistry__，默认口径每进程只警告一次；
    simplefilter("always")（测试/-W always）下仍每次可见，既有断言
    （test_wordlists_private）零影响。不采用模块级布尔标志：那会在
    "always" 口径下也吞掉第二次警告，破坏既有测试对缺档态的重触发断言。
    """
    p = _resolve_path()
    if p is None:
        env = os.environ.get("FINMASK_WORDLISTS", "").strip()
        if env:
            warnings.warn(
                f"环境变量 FINMASK_WORDLISTS 指定的文件不存在：{env}"
                "——语料治理条目未加载，引擎按通用默认词表运行"
                "（开源默认态）。请检查该环境变量的取值。",
                stacklevel=1)
        else:
            warnings.warn(
                "词表外置档 wordlists_private.json 未找到（FINMASK_WORDLISTS 未设且"
                "程序目录无档）：语料治理条目未加载，引擎按通用默认词表运行"
                "（开源默认态）。如需语料口径请配置外置档路径。",
                stacklevel=1)
        return {k: [] for k in _KNOWN_KEYS}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        warnings.warn(f"词表外置档解析失败（{type(e).__name__}），按通用默认词表运行：{p}")
        return {k: [] for k in _KNOWN_KEYS}
    out = {k: [] for k in _KNOWN_KEYS}
    for k, v in raw.items():
        if k.startswith("_"):
            continue  # _meta 等说明键
        if k not in _KNOWN_KEYS:
            warnings.warn(f"词表外置档未知键 {k!r} 已忽略（键契约见 wordlists_private 模块头）")
            continue
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            out[k] = v
    return out


def chars_joined(key: str) -> str:
    """given_chars 类键：多段字符串拼接（frozenset(str) 语义与原代码一致）。"""
    return "".join(load_private_wordlists()[key])
