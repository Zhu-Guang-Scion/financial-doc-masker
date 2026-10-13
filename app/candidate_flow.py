# -*- coding: utf-8 -*-
"""候选词推荐流程层（v2.1.0）：展示端防呆 S1-S6 的纯函数化 + W 写入编排。

定位：本模块是扫描器（app/candidate_scanner.py）与界面
（CandidateReviewDialog）之间的纯逻辑层，不 import tkinter，可脱离 GUI 单测。

职责：
- already_configured 五登记面比对回填（brands / aliases / ③区
  自定义词 / 对照表覆盖 CSV 键 / 组织后缀增词；_norm_key 归一化）——扫描器
  恒置 None，回填归属本层；
- 展示端防呆 S1-S6：长度预过滤 / 泛词同源预过滤（集合=装载点同源，含
  历史记账特例差集，见 brand_config）/ 纯组织后缀预标记（rules_engine.is_pure_org_suffix 同源）/
  五登记面已配置预检 / 跨键撞车预检（唯一必弹二次确认项）/ 非专名形态过滤；
- 清单支撑：默认排序（置信分降序、同分频次降序）/ 分页切片
  （50/页）/ 搜索过滤 / 徽标文案；
- 入口①推荐闸：本批候选 ≥3 且存在未登记候选才展示；批文件
  集指纹（内存 set 去重，程序退出即弃——零持久化红线）；
- W 流程编排：勾选集合 → brand_config.merge_brands_atomic → 成功弹窗/日志
  汇总文案（建议文案见常量区，发布周期判定=条件展示）；
- 扫描组件装载降级：模块缺失/加载失败 → 人话提示，不得崩溃。

零持久化红线：候选词/频次/上下文/来源文件全程内存；本层不落盘、不缓存、
不打日志；日志与弹窗文案只允许计数（{N}/{M}/{K}），不允许候选词明文。
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量（依据见行内注释）
# ---------------------------------------------------------------------------

TOP_PAGE = 50            # Top50/页（ScannerConfig.top_page 同值）
RECOMMEND_MIN = 3        # 入口①推荐闸：本批候选 ≥3 才展示（降频防扰）
DIGIT_MASK_MIN = 7       # 上下文连续 ≥7 位数字串打码
DIGIT_MASK_REPL = "〔数字×{n}〕"

# 已配置徽标文案（五种登记面；键=RegistrationFaces 字段名）
FACE_LABELS = {
    "brands": "已在品牌表",
    "aliases": "已在别名",
    "custom_words": "已在自定义敏感词",
    "mapping_keys": "已在对照表键",
    "org_suffix_adds": "⚠写入后该组织后缀增词将失效",
}
BADGE_NONE = "—"
BADGE_PURE_SUFFIX = "⚠纯组织后缀"

# 入口①完成弹窗推荐区文案（只出现计数，不出现词面）
RECOMMEND_TEXT = "本批检测到 {n} 个高频专名候选（已按出现次数排序）。"
RECOMMEND_BUTTON = "查看并确认候选词..."
RECOMMEND_SCANNING_TEXT = "正在检测本批文档中的高频专名候选……"

# ③区常驻入口（含 ⓘ 悬浮说明原文）
DISCOVER_BUTTON = "从文档发现候选词..."
DISCOVER_INFO = ("扫描上方文件列表，按出现频次列出机构/品牌候选词，"
                 "勾选后写入 brands.json，跨会话生效。")

# 扫描组件未就绪降级（模块缺失不崩溃）
SCANNER_NOT_READY_TITLE = "扫描组件未就绪"
SCANNER_NOT_READY_BODY = ("候选词扫描组件（candidate_scanner）尚未就绪，暂无法从文档发现候选词。"
                          "可直接在③「自定义敏感词」手动填入词表，或重新安装/更新程序后再试。")

# 对话框底部提示
REVIEW_HINT = "勾选词将写入 brands.json，下次处理自动生效"
S5_CONFIRM_TITLE = "跨键撞车确认"
S5_CONFIRM_BODY = ("候选词「{word}」与现有组织后缀增词同名：写入 brands.json 后，"
                   "该增词条目将在下次加载时失效（该词由放行改为遮蔽）。\n\n确定勾选吗？")
WRITE_BUTTON = "写入 brands.json({k})"
WRITE_BUTTON_DISABLED = "写入 brands.json(0)"

# 异常降级文案（只出现计数）
ZERO_CANDIDATE_BODY = ("未在 {m} 份文件中发现高频专名候选。常见原因：\n"
                       "① 文档以扫描件为主，文字层缺失；\n"
                       "② 内容不含重复出现的机构/品牌名。\n"
                       "可勾选『图片内文字（OCR）』后重新跑批再试，"
                       "或直接在③『自定义敏感词』手动填入。")
# v2.1.7：零候选文案条件化——无跳页版（meta.ocr_pages_skipped==0
# 时用）。本批各页文字层完整、无扫描页被跳过，则「扫描件为主」不成立、
# 「勾选 OCR 重跑」不会有增量发现——两条都撤，只保留自定义敏感词手动
# 填入指引（{m} 占位与原版同口径，供 UI 统一 .format(m=文件数)）。
ZERO_CANDIDATE_BODY_NO_OCR = ("未在 {m} 份文件中发现高频专名候选"
                              "（本批各页文字层完整，无扫描页被跳过，"
                              "勾选『图片内文字（OCR）』重跑不会有新发现）。"
                              "可能原因：内容不含重复出现的机构/品牌名。\n"
                              "可直接在③『自定义敏感词』手动填入。")
ZERO_CANDIDATE_TITLE = "未发现候选词"
ALL_FAILED_BODY = "{m} 份文件均无法读取（可能已损坏或被占用）。"
ALL_FAILED_TITLE = "扫描失败"
# v2.1.9：文案可兑现化——扫描器零持久化（候选全程内存、不落
# 任何日志），「详见处理日志」无日志可详；改为面向用户的自查指引
# （被占用/损坏是解析失败的主因）。计数来源 meta.failed_files（v2.1.9
# 起扫描器真实产出，此前该键从未生成、黄条永不出现）。
PARTIAL_BANNER = "{k} 份文件解析失败，未计入统计（请检查文件是否被占用或损坏）"
PARTIAL_RESULT_NOTE = "部分结果（{m}/{total} 份）"
PARTIAL_RESULT_TITLE = " - 部分结果"
REVIEW_DIALOG_TITLE = "候选词确认 - 来自 {m} 份文件"

# 建议文案（第 2 条仅本地工作痕迹存在时展示）
SUCCESS_TITLE = "配置已更新"
SUCCESS_BODY = ("已写入 brands.json：新增 {n} 个词（品牌表现共 {m} 个词）。\n"
                "新配置将在下一次「开始处理」时自动生效，无需重启程序。")
SUCCESS_DUP_NOTE = "\n另有 {n} 条与既有登记重复，已略。"
SUCCESS_REJECT_NOTE = "\n另有 {n} 条未通过写前校验被拒绝。"
SUCCESS_ADVICE_1 = ("\n\n建议：\n"
                    "1. 用本次扫描的文档重新执行一次脱敏，确认上述词已全部遮盖"
                    "（勾选时看到的残留应不再出现）；")
SUCCESS_ADVICE_2 = ("2. brands.json 变更属配置词变更，若脱敏产物已对外交付，"
                    "请按贵司既定流程复验。")
IMA_MARKER_DIR = "_goal_work"  # 本地工作痕迹标记目录（仅源码开发环境可能存在；
                               # 探测不到时静默跳过建议第 2 条，不报错不提示）
LOG_LINE = ("品牌配置已更新：+{n} 词（总计 {m}，写入 {file}）。"
            "下次批处理生效；已交付产物请按贵司流程复验。")
WRITE_FAIL_TITLE = "写入失败"


# ---------------------------------------------------------------------------
# 基础纯函数：上下文数字打码 / 非专名形态 / 归一化
# ---------------------------------------------------------------------------

def mask_long_digits(text: str, min_len: int = DIGIT_MASK_MIN) -> str:
    """上下文数字打码：连续 ≥min_len 位数字串替换为
    「〔数字×{位数}〕」（含详情弹窗）；全角数字同口径。候选清单会在屏上
    停留整个勾选过程，明文数字串（手机号/身份证/账号）长期暴露违背工具
    自身的敏感面哲学。min_len 缺省 7：覆盖手机号(11)/证件(18)/银行账号，
    6 位以下常见金额与编号放过，保留判断语境。"""
    if not text:
        return text or ""
    out: list = []
    run: list = []
    for ch in text:
        if ch.isdigit():  # 全角数字（０-９）同判
            run.append(ch)
            continue
        if run:
            out.append(DIGIT_MASK_REPL.format(n=len(run)) if len(run) >= min_len
                       else "".join(run))
            run = []
        out.append(ch)
    if run:
        out.append(DIGIT_MASK_REPL.format(n=len(run)) if len(run) >= min_len
                   else "".join(run))
    return "".join(out)


def is_non_entity_form(word: str) -> bool:
    """S6 非专名形态过滤：纯数字串、纯符号、纯单字英文等不入候选清单
    （数字串不是遮蔽目标对象名——金额/编号走既有类别规则；防清单被噪声淹没）。
    剥去数字/符号/空白后无剩余实体字 → True；剩余为单个 ASCII 字母 → True。"""
    w = (word or "").strip()
    if not w:
        return True
    core = [ch for ch in w
            if not (ch.isspace() or unicodedata.category(ch)[0] in ("P", "S", "N"))]
    if not core:
        return True  # 纯数字/符号/空白
    if len(core) == 1 and core[0].isascii() and core[0].isalpha():
        return True  # 纯单字英文
    return False


def norm_word(word: str) -> str:
    """清单比较键：brand_config._norm_key 同口径（NFKC + 剥括号引号空白）。"""
    from .brand_config import _norm_key
    return _norm_key(word)


# ---------------------------------------------------------------------------
# v2.1.7 UI 支撑纯函数（零行为变更小项的可测半区）
# ---------------------------------------------------------------------------

def zero_candidate_body(meta: dict) -> str:
    """零候选弹窗正文按 meta 条件化：ocr_pages_skipped > 0 →
    原版 BODY（扫描件原因 + OCR 重跑建议成立）；否则 NO_OCR 版（本批
    无跳页，OCR 重跑无增量，撤掉该话术）。返回值仍含 {m} 占位，由调用方
    .format(m=文件数)。meta 缺键 / None / 非数值一律按 0 容错——异常 meta
    不得阻塞降级弹窗本身。"""
    try:
        skipped = int((meta or {}).get("ocr_pages_skipped", 0) or 0)
    except (TypeError, ValueError):
        skipped = 0
    return ZERO_CANDIDATE_BODY if skipped > 0 else ZERO_CANDIDATE_BODY_NO_OCR


def collect_supported_files(folder, exts=None) -> list:
    """入口②文件夹本地收集：递归收集支持格式的文件，按路径
    排序、仅文件不计目录；纯函数——不触碰 UI、不写入主处理列表（「选择
    文件夹只作扫描语料，误点『开始处理』不至全 folder 跑脱敏」）。
    exts 缺省与主列表装载点同源（dispatcher.SUPPORTED_EXTS，惰性导入
    防循环依赖）；目录不可读返回空列表（上游已有空结果提示流兜底）。"""
    from .dispatcher import SUPPORTED_EXTS
    exts = SUPPORTED_EXTS if exts is None else set(exts)
    try:
        entries = sorted(Path(folder).rglob("*"))
    except OSError:
        return []
    return [f for f in entries if f.is_file() and f.suffix.lower() in exts]


def to_reuse_texts(d: dict) -> dict | None:
    """批内文本复用键转换：{str(源路径): 文本} → {Path(源路径):
    文本}（scan_sources.reuse_texts 的键契约）。空/None → None（=不传，
    行为与现状一致）；防御式——生产端 summary 缺 "batch_texts" 键时零影响。"""
    if not d:
        return None
    return {Path(k): v for k, v in d.items()}


# ---------------------------------------------------------------------------
# 五登记面（S4/S5 比对数据）与回填
# ---------------------------------------------------------------------------

@dataclass
class RegistrationFaces:
    """五个登记面的当前词表快照（对话框打开时采集一次；W1 写前仍会全量
    重校验——扫描与写入之间配置可能已被手工改动，不信任展示端状态）。"""

    brands: tuple = ()
    aliases: tuple = ()
    custom_words: tuple = ()
    mapping_keys: tuple = ()
    org_suffix_adds: tuple = ()

    def face_members(self):
        """(字段名, 成员序列) 逐面枚举（比对顺序=徽标优先级顺序）。"""
        return (("brands", self.brands),
                ("aliases", self.aliases),
                ("custom_words", self.custom_words),
                ("mapping_keys", self.mapping_keys),
                ("org_suffix_adds", self.org_suffix_adds))


def match_registration_face(word: str, faces: RegistrationFaces) -> str | None:
    """S4 五登记面比对（_norm_key 归一化口径）：命中返回面字段名，否则 None。
    扫描器恒置 already_configured=None，由本函数在流程层回填。"""
    key = norm_word(word)
    if not key:
        return None
    for name, members in faces.face_members():
        for member in members:
            if isinstance(member, str) and norm_word(member) == key:
                return name
    return None


def collides_org_suffix_adds(word: str, faces: RegistrationFaces) -> bool:
    """S5 跨键撞车检测：候选 ∩ 现有 org_suffixes 增词 ≠ ∅（归一化键）。
    写入 brands 后该增词条目在下次加载时被 P0 代理闸拒绝（brand_config 装载
    端「同文件 brands 会员进入增词碰撞集」语义），即放行词变遮蔽词——
    展示端唯一必弹二次确认的项。"""
    key = norm_word(word)
    return bool(key) and any(isinstance(m, str) and norm_word(m) == key
                             for m in faces.org_suffix_adds)


def covered_by_configured(word: str, faces: RegistrationFaces) -> bool:
    """S4b 已配置覆盖筛选（2026-09-16 碎词治理批，需求「结合用户已
    输入的自定义敏感词对候选词进行筛选」）：候选（归一化键）包含任一已
    登记词为真子串 ⇒ 筛除。依据：遮蔽引擎 _dict_candidates 对品牌/自定义词
    做 text.find 无边界子串匹配——已配置短词（如「示例甲」）的命中区间
    覆盖长候选（「示例甲集团」）的全部出现处，勾选长候选无增量遮蔽价值，
    徒占清单位。反向（候选 ⊂ 配置词）不筛：全名已配置时其简称仍可能独立
    出现（全名 find 命不中独立简称），独立简称是真实推荐需求——该方向的
    残片清理由扫描器 configured_words 外部容器闸（DN5/DN5b）承担。
    撞车面 org_suffix_adds 不在本判定内（S5 保留勾选+二次确认）。"""
    key = norm_word(word)
    if len(key) < 2:
        return False
    for name, members in faces.face_members():
        if name == "org_suffix_adds":
            continue
        for m in members:
            if isinstance(m, str):
                mk = norm_word(m)
                if len(mk) >= 2 and mk != key and mk in key:
                    return True
    return False


def generic_forbidden_set(extra_forbidden=None) -> frozenset:
    """S2 泛词预过滤集合：与实际装载点同源（brand_config.write_forbidden_set
    同一来源，含历史记账特例差集；禁止私拷——镜像漂移防线）。"""
    from .brand_config import write_forbidden_set
    return write_forbidden_set(extra_forbidden)


def is_pure_org_suffix_word(word: str) -> bool:
    """S3 纯组织后缀预标记：rules_engine.is_pure_org_suffix 同源判定
    （遮这类词=大范围误遮；异常时放行不拦——不阻塞展示）。"""
    try:
        from .rules_engine import is_pure_org_suffix
        return bool(is_pure_org_suffix(word)[0])
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 清单条目（展示端防呆落地后的渲染模型）
# ---------------------------------------------------------------------------

@dataclass
class ReviewItem:
    """候选清单行模型：扫描器 CandidateWord 的展示态投影 + S1-S6 判定结果。"""

    word: str
    freq: int = 0
    files_n: int = 0
    score: float = 0.0
    channels: list = field(default_factory=list)
    org_form: bool = False
    declared: bool = False
    already_configured: str | None = None   # S4 回填：五面字段名或 None
    collision_org_suffix: bool = False      # S5：写入将使同名组织后缀增词失效
    hidden_reason: str | None = None        # None=正常行；否则=拦截原因（可查区）
    selectable: bool = True                 # 禁选行：已配置(1-4面)/隐藏/纯后缀
    requires_confirm: bool = False          # S5 勾选时必弹二次确认
    badge: str = BADGE_NONE
    source_ref: object = None               # 原候选对象（上下文/来源文件按需取）

    @property
    def is_hidden(self) -> bool:
        return self.hidden_reason is not None


def badge_for(item: ReviewItem) -> str:
    """徽标文案（已配置标记列；悬浮明细由 UI 用 _ToolTip 补）。"""
    if item.is_hidden:
        if item.hidden_reason == "纯组织后缀":
            return BADGE_PURE_SUFFIX  # S3：灰显禁选 + 「⚠纯组织后缀」徽标
        return f"已隐藏：{item.hidden_reason}"
    if item.already_configured:
        return FACE_LABELS.get(item.already_configured, item.already_configured)
    if item.collision_org_suffix:
        return FACE_LABELS["org_suffix_adds"]
    return BADGE_NONE


def sort_review_items(items: list) -> list:
    """默认排序：置信分降序，同分频次降序（稳定再按词面保证
    渲染确定性）。返回新列表，不改入参。"""
    return sorted(items, key=lambda it: (-it.score, -it.freq, it.word))


def build_review_items(candidates: list, faces: RegistrationFaces) -> tuple:
    """扫描器 CandidateWord 序列 → (ReviewItem 列表[默认排序], 统计 dict)。

    展示端防呆 S1-S6 逐条落地（应用顺序=先 S1/S2/S6 拦截，再 S3 纯后缀，
    再 S4 五面回填与禁选，再 S5 撞车标记——撞车行保持可勾选，用户选择优先）：
    - person_likely 组整组不进展示列表（计数入 stats["person_hidden"]）；
    - S4 已配置筛选（2026-09-16 碎词治理批，原为灰显禁选占位——按需求
      改为筛除）：五面精确命中 或 被已配置词包含（S4b 覆盖判定）→ 不进
      清单，计数 stats["configured_filtered"] / stats["covered_filtered"]；
    - 扫描器自带 hidden_reason（DN 闸链）原样透传为隐藏行（进 {H} 可查区）；
    - stats：total/person_hidden/hidden_n(H)/visible_n/unregistered_n/
      configured_filtered/covered_filtered。
    """
    stats = {"total": 0, "person_hidden": 0, "hidden_n": 0,
             "visible_n": 0, "unregistered_n": 0,
             "configured_filtered": 0, "covered_filtered": 0}
    items: list = []
    forbidden = generic_forbidden_set()
    for cand in candidates or []:
        stats["total"] += 1
        if getattr(cand, "person_likely", False):
            stats["person_hidden"] += 1  # 人名组不出现在展示列表
            continue
        word = (getattr(cand, "word", "") or "")
        item = ReviewItem(
            word=word,
            freq=int(getattr(cand, "freq", 0) or 0),
            files_n=int(getattr(cand, "files_n", 0) or 0),
            score=float(getattr(cand, "score", 0.0) or 0.0),
            channels=list(getattr(cand, "channels", []) or []),
            org_form=bool(getattr(cand, "org_form", False)),
            declared=bool(getattr(cand, "declared", False)),
            hidden_reason=getattr(cand, "hidden_reason", None),
            source_ref=cand,
        )
        stripped = word.strip()
        if item.hidden_reason:
            item.selectable = False  # 扫描器拦截（DN1-DN4/纯后缀等）→ 灰显可查
        elif len(stripped) < 2:  # S1 长度预过滤（装载端 <2 拒绝同语义前移）
            item.hidden_reason = "候选词过短"
            item.selectable = False
        elif stripped in forbidden:  # S2 泛词同源预过滤（历史记账特例照旧放行）
            item.hidden_reason = "命中通用词停用表"
            item.selectable = False
        elif is_non_entity_form(word):  # S6 非专名形态
            item.hidden_reason = "非专名形态"
            item.selectable = False
        elif is_pure_org_suffix_word(word):  # S3 纯组织后缀预标记
            item.hidden_reason = "纯组织后缀"
            item.selectable = False
            item.badge = BADGE_PURE_SUFFIX
        else:
            # S4 五登记面比对回填（流程层职责）
            face = match_registration_face(word, faces)
            if face and face != "org_suffix_adds":
                # 已在品牌表/别名/自定义词/对照表键 → 筛除（2026-09-16 起，
                # 原为灰显禁选占位——不再占清单位，只计 stats）
                stats["configured_filtered"] += 1
                continue
            if covered_by_configured(word, faces):  # S4b 已配置词 ⊂ 候选
                stats["covered_filtered"] += 1
                continue
            if collides_org_suffix_adds(word, faces):  # S5 跨键撞车
                item.collision_org_suffix = True
                item.requires_confirm = True  # 唯一必弹二次确认项（仍允许勾选）
        if item.is_hidden:
            stats["hidden_n"] += 1
        else:
            stats["visible_n"] += 1
            if item.selectable:
                stats["unregistered_n"] += 1
        item.badge = badge_for(item)  # 徽标文案统一归一（渲染模型自洽）
        items.append(item)
    return sort_review_items(items), stats


# ---------------------------------------------------------------------------
# 清单支撑：过滤 / 分页 / 选择
# ---------------------------------------------------------------------------

def filter_items(items: list, query: str) -> list:
    """搜索过滤：按候选词子串实时过滤（归一化键包含 + 原串
    包含双口径，大小写折叠），跨页生效；空查询原样返回。"""
    q = (query or "").strip().lower()
    if not q:
        return list(items)
    out = []
    for it in items:
        w = (it.word or "").lower()
        if q in w or q in norm_word(it.word).lower():
            out.append(it)
    return out


def page_count(n_items: int, per_page: int = TOP_PAGE) -> int:
    """页数（Top 50/页；分页两重理由=渲染效率+人工审查带宽）。"""
    if per_page <= 0 or n_items <= 0:
        return 1
    return (n_items + per_page - 1) // per_page


def page_slice(items: list, page: int, per_page: int = TOP_PAGE) -> list:
    """分页切片：page 从 1 计，越界自动夹取（返回当前页可见行）。"""
    pages = page_count(len(items), per_page)
    page = max(1, min(int(page or 1), pages))
    start = (page - 1) * per_page
    return list(items[start:start + per_page])


def selectable_of(items: list) -> list:
    """当前行集合中可勾选行（禁选行不参与——全选/反选/清空的作用域口径）。"""
    return [it for it in items if it.selectable and not it.is_hidden]


# ---------------------------------------------------------------------------
# 入口①推荐闸（完成弹窗推荐区）
# ---------------------------------------------------------------------------

def batch_fingerprint(paths) -> str:
    """批文件集指纹：排序后的绝对路径串 SHA-256（存内存 set，程序退出即弃；
    只存指纹不存路径清单——零持久化红线下的最小痕迹）。"""
    joined = "\n".join(sorted(os.path.abspath(str(p)) for p in (paths or [])))
    return hashlib.sha256(joined.encode("utf-8", "surrogatepass")).hexdigest()


def should_recommend(n_candidates: int, unregistered_n: int,
                     fingerprint: str, seen: set | None) -> bool:
    """入口①展示闸（降频防扰）：本批候选 ≥3（RECOMMEND_MIN）
    且存在未登记候选，且同批文件集未提示过（避免完成弹窗变成广告位）。"""
    if n_candidates < RECOMMEND_MIN or unregistered_n <= 0:
        return False
    if seen is not None and fingerprint in seen:
        return False
    return True


def mark_batch_seen(fingerprint: str, seen: set) -> None:
    """记录批指纹（点过「查看并确认」或推荐已展示后调用；内存 set）。"""
    seen.add(fingerprint)


# ---------------------------------------------------------------------------
# 扫描组件装载（对扫描器签名编程 + 友好降级）
# ---------------------------------------------------------------------------

def load_scanner_module() -> tuple:
    """尝试装载 app.candidate_scanner。返回 (模块, None) 或
    (None, 人话提示)——模块缺失/加载失败一律降级不崩溃。"""
    try:
        from . import candidate_scanner
        return candidate_scanner, None
    except Exception as exc:  # ImportError 及任何加载期异常（不吞栈到用户面）
        detail = f"{type(exc).__name__}" if exc is not None else ""
        body = SCANNER_NOT_READY_BODY
        if detail:
            body = f"{body}（{detail}）"
        return None, body


# ---------------------------------------------------------------------------
# W 流程编排：勾选集合 → merge_brands_atomic → 结果汇总文案
# ---------------------------------------------------------------------------

def ima_context_present() -> bool:
    """发布周期提示的本地工作痕迹判定：仅当源码运行且本地标记目录存在时为
    True（普通终端用户/exe 环境无此目录 → 静默跳过、不报错不提示，只见
    建议第 1 条）。"""
    try:
        if getattr(sys, "frozen", False):
            return False
        from .utils import program_data_dir
        return (Path(program_data_dir()) / IMA_MARKER_DIR).is_dir()
    except Exception:
        return False


def compose_success_dialog(result, total_after: int | None = None,
                           ima_hint: bool | None = None) -> dict:
    """写入成功弹窗文案（只出现计数，不出现候选词面）。"""
    n = len(result.written or [])
    body = SUCCESS_BODY.format(n=n, m=(total_after if total_after is not None else n))
    if result.skipped_dup:
        body += SUCCESS_DUP_NOTE.format(n=len(result.skipped_dup))
    rejected = [w for w in (result.warnings or []) if "已忽略" in w]
    if rejected:
        body += SUCCESS_REJECT_NOTE.format(n=len(rejected))
    body += SUCCESS_ADVICE_1
    if ima_hint is None:
        ima_hint = ima_context_present()
    if ima_hint:
        body += SUCCESS_ADVICE_2
    return {"title": SUCCESS_TITLE, "body": body}


def compose_log_line(result, total_after: int | None = None) -> str:
    """处理日志行（无条件写全——但只写计数与文件名，零词面）。"""
    n = len(result.written or [])
    m = total_after if total_after is not None else n
    return LOG_LINE.format(n=n, m=m, file="brands.json")


def commit_selected(selected_words: list, *,
                    extra_forbidden=None,
                    ima_hint: bool | None = None) -> dict:
    """W 编排：勾选集合 → merge_brands_atomic（W1-W7 全实现）→ 汇总文案。

    返回 {"result": MergeResult,
          "dialog": {title, body}（成功弹窗文案，ok=False 时为错误弹窗文案）,
          "log_line": str,
          "total_after": int|None}。
    ok=False 时 dialog 为 {title: WRITE_FAIL_TITLE, body: 首条致命警告}，
    调用方保持清单打开供重试（原配置未改动）。"""
    from .brand_config import (load_brand_config, merge_brands_atomic,
                               resolve_config_path)
    from .app_config import brand_config_path
    result = merge_brands_atomic(selected_words, extra_forbidden=extra_forbidden)
    total_after = None
    if result.ok:
        try:
            p = resolve_config_path() or brand_config_path()
            total_after = len(load_brand_config(path=p).brands)
        except Exception:
            total_after = None
    if result.ok:
        dialog = compose_success_dialog(result, total_after, ima_hint=ima_hint)
    else:
        first = (result.warnings or ["写入失败，请重试。"])[0]
        dialog = {"title": WRITE_FAIL_TITLE, "body": first}
    return {"result": result, "dialog": dialog,
            "log_line": compose_log_line(result, total_after),
            "total_after": total_after}
