# -*- coding: utf-8 -*-
"""品牌预设配置（brands.json）：路径解析、JSON 解析、防呆校验。

v1.8.1 起引擎内置的品牌预设（原 _PRESET_BRANDS / _BRAND_DOMAIN_HINTS 硬编码）
外置为用户可编辑的 brands.json。本模块只负责「读文件 + 防呆」，不含引擎
语义；不 import rules_engine（避免循环依赖）。防呆所需的通用词停用表由
调用方（rules_engine）经 forbidden_words 参数传入。

G1（schema v2）：在 v1 三键之上新增两个同形态字符串数组键——
  org_suffixes        用户增词：并入组织后缀纯度判定主干词表（与代码表并集，
                      放行方向，治误遮）；
  org_suffixes_remove 用户删词白名单：从生效词表放行该词（撤回放行决策，
                      治漏遮方向；仅命中代码主干/可选机关集成员才生效）。
生效表构造在引擎侧（rules_engine.build_org_suffix_tables）；本模块只做
装载与同规格防呆（与引擎同规格）。防呆所需碰撞集全部走参数注入（forbidden_words
先例），本模块依旧不感知引擎词表本体。所有防呆命中只降级 + 人话警告，
绝不抛异常阻塞脱敏主流程。

文件缺失属常态（用户未自定义），静默回空配置零警告——唯一例外：env
FINMASK_BRANDS 显式指定但路径不存在时给一条人话警告（v2.1.9，
配置意图未达成应响亮）；坏文件降级为空配置 + 人话警告，绝不抛异常阻塞
脱敏主流程（与 app_config.load_config 同哲学）。

v2.1.0 起新增写入端 merge_brands_atomic（候选词推荐工具）：只在既有文件上
做「合并写」（brands 数组尾追加，其余键逐字原样保留）+ 原子替换 + 写后回读
校验。装载路径依旧不 import rules_engine（上方循环依赖约束不变）；写入端按
「同源工程红线」设计必须在函数体内延迟导入引擎停用表派生常量（禁止私拷，
杜绝「扫描端拒了、装载端却收」的镜像漂移）——运行期（引擎已完成装载）延迟
导入无循环风险，既有装载函数行为零变化。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .app_config import BRANDS_FILENAME, brand_config_path

DEFAULT_CONFIG_FILENAME = BRANDS_FILENAME  # "brands.json"
ENV_OVERRIDE = "FINMASK_BRANDS"  # 环境变量指定配置路径（先例：FINMASK_ONNX_MODELS）

# 配置的三个列表键：品牌主表 / 别名（与品牌同权脱敏）/ 邮箱域名特征片段
_LIST_KEYS = ("brands", "aliases", "domain_hints")
# G1 schema v2 新增双键（与其余三键平行、互不影响；v1 三键原样不动）
_SUFFIX_ADD_KEY = "org_suffixes"
_SUFFIX_RM_KEY = "org_suffixes_remove"
_ALL_KEYS = _LIST_KEYS + (_SUFFIX_ADD_KEY, _SUFFIX_RM_KEY)

# 引擎 _QUOTE_CHARS 同款码表（chr 构建避免源码混排引号；仅剥边缘）
_LIGHT_QUOTE_CHARS = "".join(map(chr, (0x22, 0x201C, 0x201D,
                                       0x27, 0x2018, 0x2019,
                                       0x300C, 0x300D, 0x300E, 0x300F)))

# ---------------------------------------------------------------------------
# 配置通道停用集权威定义点（全仓唯一保留历史语料特例字面量处）
# ---------------------------------------------------------------------------
# 开源说明：CONFIG_CHANNEL_STOP_EXCEPTION 是配置通道停用集的历史语料特例
# 例外——语义=该词面允许经配置通道登记为敏感词（预设语料已把它收编为预设
# 品牌词，故从「通用词不得经配置注册」防呆中豁免）；学习通道停用集
# （rules_engine._BRAND_STOP_ALL）同样由本常量并集派生（该词面不得经批内
# 学习复活）。派生常量 CONFIG_CHANNEL_FORBIDDEN_WORDS（= 引擎停用全集 −
# 本例外，经模块 __getattr__ 惰性物化）供全部配置通道消费点统一引用：
# rules_engine 装载点 / brand_config 写入端 / candidate_scanner 扫描端。
# 词表数据外置工程（2026-09-21）：特例条目迁出代码至外置档
# wordlists_private.json（config_stop_exception 键，加载链与缺档提示见
# app/wordlists_private.py 模块头）；代码默认=空集（无特例，开源默认态）。
from .wordlists_private import load_private_wordlists as _load_wl
CONFIG_CHANNEL_STOP_EXCEPTION = frozenset(
    w for w in _load_wl()["config_stop_exception"] if isinstance(w, str))


def _config_channel_forbidden_words() -> frozenset:
    """CONFIG_CHANNEL_FORBIDDEN_WORDS 的惰性物化：= 引擎停用全集 − 特例例外。

    引擎停用全集装在 rules_engine，本模块不得顶层反向导入（见模块头循环
    依赖约束），故常量在首次访问时经延迟导入构建并缓存为模块属性（PEP 562）。
    """
    from .rules_engine import _BRAND_STOP_ALL
    value = _BRAND_STOP_ALL - CONFIG_CHANNEL_STOP_EXCEPTION
    globals()["CONFIG_CHANNEL_FORBIDDEN_WORDS"] = value
    return value


def __getattr__(name):
    if name == "CONFIG_CHANNEL_FORBIDDEN_WORDS":
        return _config_channel_forbidden_words()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass
class BrandConfig:
    """brands.json 的解析结果：词条列表 + 人话警告（由调用方落日志）。

    前三键为 v1 原样；后两键为 G1 schema v2 新增（缺省空表 = v1 行为）。
    """

    brands: list = field(default_factory=list)        # 品牌主表（静态注入 ***）
    aliases: list = field(default_factory=list)       # 别名（与品牌同权注入）
    domain_hints: list = field(default_factory=list)  # 邮箱域名特征片段
    org_suffixes: list = field(default_factory=list)  # G1 组织后缀增词（并入主干）
    org_suffixes_remove: list = field(default_factory=list)  # G1 删词白名单
    warnings: list = field(default_factory=list)      # 人话警告（缺失时为零条）


def resolve_config_path() -> Path | None:
    """配置路径解析：环境变量 FINMASK_BRANDS 优先；其次 程序目录/brands.json
    （exe 同目录 / 源码项目根）；都没有 → None（调用方按空配置处理）。"""
    env = os.environ.get(ENV_OVERRIDE, "").strip()
    if env:
        return Path(env)
    default = brand_config_path()
    return default if default.exists() else None


def _norm_key(w: str) -> str:
    """防呆比较键：引擎 _norm_org_suffix_text 同口径的 stdlib 复刻
    （NFKC 全半角统一 + 剥括号（）() 与 ®™ + 去空白 + 剥边缘引号）。

    brand_config 不 import rules_engine（模块头约束），此处按同一规则
    纯 stdlib 重写；与引擎侧口径的逐字对齐由 tests/test_brand_config.py
    G1 矩阵用例锁定（「（普通合伙）」与「普通合伙」同键等语义）。"""
    t = unicodedata.normalize("NFKC", w or "")
    t = re.sub(r"[（）()®™\s]+", "", t)
    return t.strip(_LIGHT_QUOTE_CHARS)


def load_brand_config(path=None, forbidden_words: frozenset | set = frozenset(),
                      org_suffix_add_forbidden: frozenset | set = frozenset(),
                      org_suffix_builtin_vocab: frozenset | set = frozenset(),
                      org_suffix_removable_vocab: frozenset | set = frozenset(),
                      org_suffix_atom_vocab: frozenset | set = frozenset()) -> BrandConfig:
    """加载并校验 brands.json；任何问题只降级 + 警告，绝不抛异常。

    path 为 None 时按 resolve_config_path() 解析；解析不到或文件不存在
    → 空配置零警告（缺失是常态，不吵；例外见下）。forbidden_words 为
    「不得经配置通道注册的通用词」停用表（调用方传引擎停用表并集）；命中
    即拒绝该条并给出人话警告。

    v2.1.9：path 为 None（即走 resolve_config_path 解析链）且
    env FINMASK_BRANDS 显式指定、但该路径不存在时，追加一条 cfg.warnings
    人话警告——显式指定的配置意图落空应响亮；默认路径缺失（用户未自
    定义）保持零警告静默，调用方显式传 path 的缺档场景行为不变。

    G1 注入参数（全部默认空集 → v1 调用点行为逐字不变）：
    - org_suffix_add_forbidden：遮蔽侧注册表（映射键 ∪ 自定义敏感词，
      调用方 rules_engine 在加载前已就绪）——增词命中即拒（P0 代理闸：
      增词目的是放行，若同词已被 brands/映射/自定义词承诺遮蔽则漏遮）；
      同文件 brands/aliases 由本函数自查（cfg 已解析，无需注入）。
    - org_suffix_builtin_vocab：内置三表归一化词集（主干∪原子∪可选）——
      增词重复登记 → 警告 + 零生效（响亮优于静默）。
    - org_suffix_removable_vocab / org_suffix_atom_vocab：可删词集（主干∪
      可选归一化）/ 原子集——删词仅命中可删集才生效（typo 零生效 + 响亮
      警告，天然防「删光」）；原子不开放删除（拆闸身=组合形态整体失拦）。
    """
    cfg = BrandConfig()
    p = Path(path) if path is not None else resolve_config_path()
    if p is None or not p.exists():
        # v2.1.9：env FINMASK_BRANDS 显式指定但文件不存在 → 一条
        # 人话警告（显式指定落空=配置意图未达成，应响亮）；默认路径缺失=
        # 用户未自定义（常态）保持零警告静默。调用方显式传 path 的场景
        # （测试直传、写入端 target）不在此列，行为逐字不变。
        if path is None and p is not None:
            env = os.environ.get(ENV_OVERRIDE, "").strip()
            if env and Path(env) == p:
                cfg.warnings.append(
                    f"环境变量 FINMASK_BRANDS 指定的配置文件不存在：{env}，"
                    f"已按无品牌预设处理（请检查该环境变量的取值）")
        return cfg  # 缺失是常态：用户未自定义，零警告（env 显式指定除外，见上）
    try:
        text = p.read_text(encoding="utf-8-sig")  # 容忍记事本保存的 BOM
    except UnicodeDecodeError:
        # UnicodeDecodeError 是
        # ValueError 子类而非 OSError——WPS/旧记事本以 ANSI/GBK 保存
        # brands.json 时原 except 分支拦不住，异常穿透到 RuleEngine 构造
        # 导致整批处理失败（违反「坏档降级+人话警告」契约）。GBK 回退
        # 沿映射复核先例（ui_main_window.py 读 CSV 同款）。
        try:
            text = p.read_text(encoding="gbk")
            cfg.warnings.append("检测到品牌配置文件为非 UTF-8 编码（GBK/ANSI），"
                                "本次已按 GBK 读取；建议用记事本另存为 UTF-8 "
                                "以获得最佳兼容性。")
        except (UnicodeDecodeError, OSError) as exc:
            cfg.warnings.append(f"品牌配置文件编码无法识别（既非 UTF-8 也非 "
                                f"GBK），已按无品牌预设处理（{exc}）")
            return cfg
    except OSError as exc:
        cfg.warnings.append(f"品牌配置文件读取失败，已按无品牌预设处理（{exc}）")
        return cfg
    try:
        data = json.loads(text)
    except ValueError as exc:  # json.JSONDecodeError 及其父类
        line = getattr(exc, "lineno", 0)
        msg = getattr(exc, "msg", str(exc))
        where = f"第 {line} 行：" if line else ""
        cfg.warnings.append(f"品牌配置文件不是有效的 JSON，已按无品牌预设处理"
                            f"（{where}{msg}）")
        return cfg
    if not isinstance(data, dict):
        cfg.warnings.append("品牌配置文件内容格式不符合预期（应为 {...} 对象），"
                            "已按无品牌预设处理")
        return cfg
    for key in _LIST_KEYS:
        value = data.get(key, [])  # 缺键 → 空表
        if not isinstance(value, list):
            cfg.warnings.append(f"品牌配置中「{key}」不是列表，已忽略该项")
            value = []
        setattr(cfg, key, _sanitize(value, forbidden_words, cfg.warnings))
    # ---- G1 schema v2 双键（键级防呆沿现行三级降级：一键坏不拖累他键）----
    for key in (_SUFFIX_ADD_KEY, _SUFFIX_RM_KEY):
        value = data.get(key, [])
        if not isinstance(value, list):
            cfg.warnings.append(f"品牌配置中「{key}」不是列表，已忽略该项")
            value = []
        if key == _SUFFIX_ADD_KEY:
            # P0 代理闸碰撞集 = 注入注册表 ∪ 同文件 brands/aliases（归一化键）
            registered: set = set()
            for src in (org_suffix_add_forbidden, cfg.brands, cfg.aliases):
                registered.update(_norm_key(w) for w in src if isinstance(w, str))
            registered.discard("")
            setattr(cfg, key, _sanitize_org_add(
                value, forbidden_words, cfg.warnings,
                registered_forbidden=frozenset(registered),
                builtin_vocab=org_suffix_builtin_vocab))
        else:
            setattr(cfg, key, _sanitize_org_remove(
                value, cfg.warnings,
                removable_vocab=org_suffix_removable_vocab,
                atom_vocab=org_suffix_atom_vocab))
    # 未知键防呆（G1 新增；现行 loader 对未知键静默，v2 起留痕可查——
    # 打错键名如 org_suffix 的用户从此有人话警告）
    for key in data:
        if key not in _ALL_KEYS:
            cfg.warnings.append(f"未知键「{key}」，已忽略")
    return cfg


def _sanitize(items: list, forbidden_words, warnings: list) -> list:
    """逐条防呆清洗：非文本/空白、过短、通用词拒绝；去重；全部 strip 后入库。"""
    out: list = []
    for raw in items:
        if not isinstance(raw, str) or not raw.strip():
            warnings.append("配置项存在空白或非文本条目，已忽略")
            continue
        w = raw.strip()
        if len(w) < 2:
            warnings.append(f"配置项「{w}」太短，遮蔽它容易误伤大片正常文本，"
                            f"已忽略（至少 2 个字）")
            continue
        if w in forbidden_words:
            warnings.append(f"配置项「{w}」过于宽泛，遮蔽它可能误伤大量正常文本，已忽略")
            continue
        if w in out:
            continue  # 重复条目去重（保留首次，不警告）
        out.append(w)
    return out


def _sanitize_org_add(items: list, forbidden_words, warnings: list, *,
                      registered_forbidden: frozenset = frozenset(),
                      builtin_vocab: frozenset = frozenset()) -> list:
    """G1 增词方向防呆（沿 _sanitize 同序扩展）：
    a 非文本/空白忽略 → b 单字拒绝 → c 通用词拒绝 → d 遮蔽侧注册表撞车
    拒绝（P0 代理闸）→ e 已内置重复登记警告零生效 → f 组内去重（沉默）。
    增词是放行方向：d 拦「与显式注册表撞车」子集；「增词不得含区别性
    成分」无法通用机检（纯度判定的语义边界本身），残余风险由使用说明
    披露，不设伪闸。"""
    out: list = []
    seen: set = set()
    for raw in items:
        if not isinstance(raw, str) or not raw.strip():
            warnings.append("配置项存在空白或非文本条目，已忽略")
            continue
        w = raw.strip()
        if len(w) < 2:
            warnings.append(f"配置项「{w}」太短，登记为组织后缀至少 2 个字"
                            f"（单字后缀已由内置表覆盖），已忽略")
            continue
        if w in forbidden_words:
            warnings.append(f"配置项「{w}」过于宽泛，遮蔽它可能误伤大量正常文本，已忽略")
            continue
        norm = _norm_key(w)
        if norm in registered_forbidden:
            warnings.append(f"「{w}」已登记为遮蔽词/映射键，若再登记为组织后缀"
                            f"会导致该词不再被遮蔽（漏遮风险），已拒绝")
            continue
        if builtin_vocab and norm in builtin_vocab:
            warnings.append(f"「{w}」已在内置组织后缀表中，无需重复登记")
            continue
        if norm in seen:
            continue  # 组内去重（保留首次，不警告）
        seen.add(norm)
        out.append(w)
    return out


def _sanitize_org_remove(items: list, warnings: list, *,
                         removable_vocab: frozenset = frozenset(),
                         atom_vocab: frozenset = frozenset()) -> list:
    """G1 删词方向防呆（独立轻量管线，不过 forbidden_words 闸——删词只会
    减少放行、加重遮蔽，不存在「过于宽泛」问题）：
    a 非文本/空白忽略 → b 单字放行 + 专属后果警告（队/店类有正当删词
    理由，不硬禁）→ d 组合切分原子零生效（不开放删除）→ c 未命中内置表
    零生效 + 响亮警告（typo 天然防「删光」；删词意图未达成而用户以为
    生效 = 漏遮预期错位，是降级哲学下唯一需要响亮的命中点）→ e 组内
    去重（归一化键，沉默）。
    可删集/原子集注入为空时跳过成员校验（独立 loader 使用不产生伪警告）。"""
    out: list = []
    seen: set = set()
    for raw in items:
        if not isinstance(raw, str) or not raw.strip():
            warnings.append("配置项存在空白或非文本条目，已忽略")
            continue
        w = raw.strip()
        norm = _norm_key(w)
        if len(w) == 1:
            warnings.append(f"「{w}」为单字后缀，删除后将恢复对其组合的遮蔽，"
                            f"可能加重误遮（正常文本中的同名碎字），请确认")
        if atom_vocab and norm in atom_vocab:
            warnings.append(f"「{w}」为组合切分原子，不开放删除")
            continue
        if removable_vocab and norm not in removable_vocab:
            warnings.append(f"「{w}」不在内置组织后缀表中，未生效"
                            f"（请检查写法，含全半角/括号差异会被自动归一）")
            continue
        if norm in seen:
            continue  # 组内去重（归一化键，保留首次，不警告）
        seen.add(norm)
        out.append(w)
    return out


# ---------------------------------------------------------------------------
# v2.1.0 写入端：合并写 brands.json（落盘端 W1-W7）。
# 本节为纯增量：不触碰上方任何装载函数；「安全底线=不信任展示端状态」，
# 写前全量重跑装载端同规格校验，防呆命中只拒绝 + 人话警告，绝不静默丢弃。
# ---------------------------------------------------------------------------

@dataclass
class MergeResult:
    """merge_brands_atomic 的结果。

    written      实际追加进 brands 数组的词（strip 后、按传入顺序）；
    skipped_dup  因与既有登记（brands/aliases，归一化键）或组内重复而略过的
                 原始词面——沿 loader「保留首次、沉默去重」语义，界面侧汇总
                 告知「{n} 条与既有登记重复，已略」（响亮优于静默的界面补偿）；
    warnings     人话提示（拒绝原因/降级说明；调用方落界面日志按零持久化
                 红线只取计数，不落候选词面）；
    ok           False=致命降级（读失败/坏 JSON/写失败/回读不一致），此时
                 written 恒为空、原文件未被改动（原子写承诺）。
    """

    written: list = field(default_factory=list)
    skipped_dup: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    ok: bool = True


def write_forbidden_set(extra_forbidden=None) -> frozenset:
    """W1 停用集：与引擎装载点实参同源（CONFIG_CHANNEL_FORBIDDEN_WORDS 权威
    常量 = 引擎停用全集 − CONFIG_CHANNEL_STOP_EXCEPTION 特例例外；特例词面
    为配置通道白名单照旧放行）；extra_forbidden 供调用方追加自有停用词。
    函数级 import 的理由见模块头 v2.1.0 段。"""
    from .rules_engine import NON_NAME_BLACKLIST
    # 模块内函数体对惰性常量的引用不触发 PEP 562 __getattr__（那仅作用于
    # from-import 属性访问），须直调物化函数
    forbidden = _config_channel_forbidden_words() | frozenset(NON_NAME_BLACKLIST)
    if extra_forbidden:
        forbidden = forbidden | frozenset(
            w for w in extra_forbidden if isinstance(w, str))
    return frozenset(forbidden)


def _merge_result_fail(result: MergeResult, msg: str) -> MergeResult:
    """致命降级统一出口：清空 written（未发生任何写入）、记录人话警告。"""
    result.written = []
    result.ok = False
    result.warnings.append(msg)
    return result


def merge_brands_atomic(new_words: list, *, extra_forbidden=None) -> MergeResult:
    """把新词合并写入 brands.json 的 brands 键（合并写非覆盖，W1-W7）。

    流程：
    W1 写前全量重跑装载端同规格校验（非文本/空白忽略 → <2 拒绝人话警告 →
       forbidden 拒绝；集合经 write_forbidden_set 与装载点同源，含历史
       语料特例差集）——不信任展示端状态；
    W2 归一化去重（_norm_key 口径：组内 + 跨既有 brands/aliases，保留首次、
       沉默去重，略过词面进 skipped_dup 供界面汇总告知）；
    W3 合并写非覆盖：读现有原文，仅 brands 数组尾追加，aliases/domain_hints/
       org_suffixes/org_suffixes_remove 及未知键逐字原样保留；
    W4 原子写：临时文件 + os.replace，写失败旧文件原地不动；
    W5 写入目标走 resolve_config_path() 同一解析（None → 落默认路径
       brand_config_path()，与读侧「缺失是常态」衔接）；
    W6 写后 load_brand_config 回读校验（每个新词在生效 brands 列表 + 零新增
       警告——基线取写入前的同口径装载警告数）；
    W7 PermissionError/占用/只读 → ok=False + 人话警告，不半写不崩溃。

    既有文件坏 JSON/顶层非对象时拒绝写入（合并写无法在不可解析的文件上进行，
    覆盖即销毁用户数据；提示手工修复），原文件不动。全程绝不抛异常。
    """
    result = MergeResult()
    forbidden = write_forbidden_set(extra_forbidden)

    # ---- W1/W2：写前校验 + 归一化去重（先校验后去重，沿 loader 原序）----
    existing_keys: set = set()
    target = resolve_config_path()
    if target is None:  # W5：解析不到（默认路径文件不存在）→ 落默认路径
        target = brand_config_path()
    data = None
    if target.exists():
        try:
            old_text = target.read_text(encoding="utf-8-sig")  # 容忍 BOM
        except UnicodeDecodeError:
            # 读端同款编码洞（GBK 旧档穿透 OSError 拦截）
            try:
                old_text = target.read_text(encoding="gbk")
            except (UnicodeDecodeError, OSError) as exc:
                return _merge_result_fail(
                    result, f"写入失败：brands.json 编码无法识别（{exc}）。"
                            f"原配置未改动；请用记事本将其另存为 UTF-8 后重试。")
        except OSError as exc:
            return _merge_result_fail(
                result, f"写入失败：brands.json 无法读取（{exc}）。"
                        f"原配置未改动，请关闭占用程序后重试。")
        try:
            data = json.loads(old_text)
        except ValueError:
            return _merge_result_fail(
                result, "写入失败：现有 brands.json 不是有效的 JSON，"
                        "合并写入会覆盖原文件，已拒绝；请先手工修复该文件再试。")
        if not isinstance(data, dict):
            return _merge_result_fail(
                result, "写入失败：现有 brands.json 顶层不是 JSON 对象，"
                        "合并写入会覆盖原文件，已拒绝；请先手工修复该文件再试。")
        raw_brands = data.get("brands", [])
        raw_aliases = data.get("aliases", [])
        for src in (raw_brands, raw_aliases):
            if isinstance(src, list):
                existing_keys.update(_norm_key(w) for w in src
                                     if isinstance(w, str))
        existing_keys.discard("")
    else:
        # 文件缺失是常态（F7/W5）：按五键 schema 新建（与 G1 模板同形，空表）
        raw_brands = []
        data = {"brands": [], "aliases": [], "domain_hints": [],
                "org_suffixes": [], "org_suffixes_remove": []}
    brands_is_list = isinstance(raw_brands, list)

    cleaned: list = []  # [(原始词面, strip 后词, 归一化键)]
    for raw in (new_words or []):
        if not isinstance(raw, str) or not raw.strip():
            result.warnings.append("配置项存在空白或非文本条目，已忽略")
            continue
        w = raw.strip()
        if len(w) < 2:
            result.warnings.append(f"配置项「{w}」太短，遮蔽它容易误伤大片正常"
                                   f"文本，已忽略（至少 2 个字）")
            continue
        if w in forbidden:
            result.warnings.append(f"配置项「{w}」过于宽泛，遮蔽它可能误伤大量"
                                   f"正常文本，已忽略")
            continue
        norm = _norm_key(w)
        if norm in existing_keys:  # 跨既有 brands/aliases（含组内）归一化去重
            result.skipped_dup.append(raw)
            continue
        existing_keys.add(norm)
        cleaned.append((raw, w, norm))
    result.written = [w for _, w, _ in cleaned]
    if not result.written:
        result.warnings.append("没有新增词可写入（与既有登记重复或未通过写前校验）")
        return result

    # ---- W6 基线：写入前同口径装载警告数（回读时比对「零新增警告」）----
    baseline = load_brand_config(path=target, forbidden_words=forbidden)
    baseline_warn_n = len(baseline.warnings)

    # ---- W3：合并写非覆盖（仅 brands 尾追加；其余键/未知键不动）----
    if brands_is_list:
        data["brands"] = list(raw_brands) + result.written
    else:
        # 与装载端语义同源（「不是列表，已忽略该项」→ 空表）：按空表处理后写入
        data["brands"] = list(result.written)
        result.warnings.append("原「brands」键不是列表，已按空表处理并写入新词")
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    # ---- W4/W7：原子写（临时文件 + os.replace），失败旧文件原地不动 ----
    tmp_path = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".brands_", suffix=".tmp",
                                        dir=str(target.parent))
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(target))
        tmp_path = None
    except PermissionError:
        _cleanup_tmp(tmp_path)
        return _merge_result_fail(
            result, "写入失败：brands.json 可能被其他程序占用（如 Excel/WPS/"
                    "编辑器）或只读，请关闭占用程序后重试（原配置未改动）。")
    except OSError as exc:
        _cleanup_tmp(tmp_path)
        return _merge_result_fail(
            result, f"写入失败：brands.json 无法写入（{type(exc).__name__}）。"
                    f"原配置未改动。")

    # ---- W6：写后回读校验（新词在生效列表 + 零新增警告）----
    check = load_brand_config(path=target, forbidden_words=forbidden)
    missing = [w for w in result.written if w not in check.brands]
    new_warnings = check.warnings[baseline_warn_n:]  # 装载警告按序追加 → 切片即新增
    # 跨键撞车（展示端唯一必弹二次确认项）的既定后果：同名组织后缀增词
    # 条目被 P0 代理闸拒绝并警告——这正是设计要的效果（该词由放行改为
    # 遮蔽），不视为写入失败；其余任何新增警告仍按「校验未通过」响亮降级。
    unexpected = [w for w in new_warnings if "若再登记为组织后缀" not in w]
    if missing or unexpected:
        return _merge_result_fail(
            result, "写入结果校验未通过（回读缺失 "
                    f"{len(missing)} 条 / 新增警告 {len(unexpected)} 条），"
                    "请重试或手工检查 brands.json。")
    return result


def _cleanup_tmp(tmp_path) -> None:
    """原子写失败后清理临时文件（best-effort，失败不影响降级路径）。"""
    if tmp_path is None:
        return
    try:
        tmp_path.unlink(missing_ok=True)
    except OSError:
        pass
