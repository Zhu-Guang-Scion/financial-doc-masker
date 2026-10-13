# -*- coding: utf-8 -*-
"""候选词扫描引擎（v2.1.0）。

接口面：CandidateWord / ScannerConfig /
ScanSession / scan_sources。算法：
四通道（A n-gram 级联主生成器 / B 组织后缀模式 / C 简称生成-验证闭环 /
D jieba 候选级信号）+ 频次精确回填 + 降噪闸链 DN1-DN9 + 置信分。
DN5 长词内部残片闸（实测驱动）：独立计数 eff = freq − 最长
容器频；eff≤0 ⇒ 出现全被更长候选解释 → 残片隐藏（前置 POS 执行，同时
裁剪 posseg 输入面）；打分 F 分量改用 eff（治「高频残片虚胖压制真简称」）。

2026-09-16 碎词治理批（产品反馈：候选列表碎词过多，在不影响速度的前提
下优化）：Top60 实测三族碎词（动词头缀粘连体/首尾边界残片/通用行政短语）
四根因——DN7 实体豁免自包含（e in w 命中自身）、entity_words 被高频残片
污染（无独立证据守则）、DN8 仅尾缀方向、功能字生成期滤除致真容器缺席
（「中华人民共和国」滤「中」⇒「华人民共和国」无容器可解释）。修复：
DN7 e≠w + entity MAX 容器守则；DN8 头缀对称化（GLUE_HEADS 两层剥离+功能
字清洁层）/并列连词粘连/叠尾粘连/字段尾词表，rem2 判定收窄（判例词
「示例甲家居股份有限公司」=实体+法定后缀段误杀回退案例）；DN9 边界残片
闸（新增，DN5 同遍循环顺带统计 CJK 左/右邻字分布——集中度 ≥edge_ratio
且绝对量 ≥edge_min ⇒ 长词内部残片；实测分隔带：残片族 0.91~1.00 vs
真实体带 ≤0.74）；DN6 扩「总则/附则第X条」；scan_sources 新参
configured_words（用户已登记词作外部可信容器注入 DN5/DN5b——配置全名的
内部残片不再推荐）。邻字统计复用 DN5 循环零独立扫描，耗时影响 <0.3s。

红线（设计契约，逐条对齐）：
- 零持久化：候选/频次/上下文/文件清单全程内存；本模块无 open(w)/写文件/
  日志调用；meta 只计数。
- 引擎遮蔽零改动：对 rules_engine 仅只读导入；不 import ocr_engine /
  pdf_handler 等重链路（pdf/xlsx 解析库在提取函数内惰性导入）。
- 同源工程红线：DN1 停用集 = 装载点实参「配置通道停用集权威常量 ∪
  NON_NAME_BLACKLIST」（brand_config.CONFIG_CHANNEL_FORBIDDEN_WORDS 单一
  来源派生，见 _load_brand_tables）；组织后缀生效表经
  build_org_suffix_tables(brands.org_suffixes, brands.org_suffixes_remove)
  现算（含用户 G1 增删），禁止私拷贝。
- 参数默认值固化（J1-J6）：默认值进 ScannerConfig 注释，可经环境变量
  FINMASK_G2_FREQ_MIN / FINMASK_G2_NGRAM_MAX / FINMASK_G2_SINGLE_SUFFIX_MIN /
  FINMASK_G2_DERIVE_MIN_FREQ 覆盖（沿用 FINMASK_RECHECK_BAND_H 几何 env
  先例；非法值回退默认并 warnings 告警）。

合流评审修正两条（均已落实）：
- 修正一：功能字闸语义 =「剥功能字头/尾后实体核 ≥2 CJK 字才保留」
  （防误杀「中共X」类固有字；动词短语剥后不足或词性尾验命中仍灭）。
- 修正二：扫描端自持提名证据后缀表 _NOMINATION_SUFFIX_EXTRA =
  frozenset({"委员会", "党委", "党委会"})，仅用于通道 B 提名；引擎遮蔽表
  零改动、不入 brands schema。
"""
from __future__ import annotations

import bisect
import collections
import math
import os
import re
import time
import warnings
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .generic_vocab import GENERIC_VOCAB, GENERIC_VOCAB_MAXLEN
_G_LASTCHARS = frozenset(w[-1] for w in GENERIC_VOCAB)  # DN2 DP 末字预滤
from .rules_engine import (
    _BRAND_GENERIC_TAILS,
    _COMPANY_STEM_BLACKLIST,
    _ENUM_ORG_SUFFIXES,
    _KEYWORD_BEFORE,
    _ORG_SUFFIX_TOKENS,
    _ORG_SUFFIX_TOKENS_OPTIONAL,
    _PLACE_NAMES,
    _PLACE_WORDS,
    _TITLE_AFTER1,
    _TITLE_AFTER2,
    COMMON_WORDS,
    NON_NAME_BLACKLIST,
    _norm_org_suffix_text,
    _org_suffix_full_cover,
    build_org_suffix_tables,
)

__all__ = [
    "CandidateWord",
    "ScannerConfig",
    "ScanSession",
    "scan_sources",
]

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
_CJK_FULL_RE = re.compile(r"[\u4e00-\u9fff]+$")


def _norm_candidate(w: str) -> str:
    """候选归一化（引擎 _norm_org_suffix_text 同语义）。四通道提名词均为纯
    CJK 连串（提取面保证），NFKC/剥括号/去空白对其恒等——正则全串判定短路，
    免数万次 unicodedata 调用；非纯 CJK 串（理论不出现，防御面）走原实现。"""
    return w if _CJK_FULL_RE.match(w) else _norm_org_suffix_text(w)

# 与早期探针实现同集复用（同集为契约要求）
GENERIC_PREFIX = frozenset(
    {"本", "我", "该", "各", "此", "上述", "以下", "如下", "有关", "相关", "指定", "相应"})
# 「由」「全」为终轮增补（实测判例：由技术部/本办法由财务管理中心/负责
# 全馆 类头缀粘连；判例词零含由全）
FUNC_CHARS = frozenset("的了与及或在于是为中等各本该每原现将应须需按照自至对从向以其不未已又再即将此由全")

# 合流修正二：扫描端自持「提名证据后缀表」——仅用于通道 B 的正则
# 提名（委员会头机构形态兜底召回）；引擎遮蔽表零改动，不入 brands schema。
_NOMINATION_SUFFIX_EXTRA = frozenset({"委员会", "党委", "党委会"})

# 支持格式（与探针/构建文件清单同集）
SUPPORTED_EXTS = {".docx", ".md", ".xlsx", ".xls", ".pptx", ".pdf"}

# 四通道 ⊆ {ngram, suffix, abbrev, declared, jieba_nt}
_CHANNEL_ORDER = ("ngram", "suffix", "abbrev", "declared", "jieba_nt")

# 隐藏原因（响亮优于静默；进 hidden_reason 与 meta.gate_hidden 计数）
HIDDEN_PURE = "纯组织后缀"        # v1.9.3 纯度闸同源语义：勾了也遮不掉
HIDDEN_STOP = "停用词表"          # DN1 装载点实参同源闸
HIDDEN_DEPT = "通用部门词"        # DN2 通用组织形态闸（G 表覆盖切分）
HIDDEN_FUNC = "功能字躯干"        # DN3 功能字闸（合流修正一语义）
HIDDEN_SINGLE = "单字后缀低证据"  # DN4（count≥10）
HIDDEN_POS = "词性尾验"           # 通道 B 三闸之 POS 尾验（末 token 动词/方位词/助词类）
HIDDEN_FRAG = "长词内部残片"      # DN5：独立出现计数（出现级覆盖精算）≤0
HIDDEN_DOCNO = "公文条款号"       # DN6：第X条/第X章 类条款编号形态
HIDDEN_PHRASE = "短语连串"        # DN7：≥3 词典 token 且零 OOV 的多词连串
HIDDEN_GLUED = "粘连变体"         # DN8：完整实体+功能字/头缀/G 可覆盖残段

_GATES = (HIDDEN_PURE, HIDDEN_STOP, HIDDEN_DEPT, HIDDEN_FUNC, HIDDEN_SINGLE,
          HIDDEN_FRAG, HIDDEN_DOCNO, HIDDEN_PHRASE, HIDDEN_GLUED, HIDDEN_POS)

# DN6 条款号形态（制度语料高频噪声；实测 Top50 实证：第十一条~第十八条
# 等；第二分支抓「第十/十一条」类掉头残片；第三分支「总则/附则第X条」类
# 章节条款粘连——2026-09-16 碎词治理批实测：总则第一条 15 次/10 份全形态）
_DOCNO_RE = re.compile(
    r"(?:(?:总则|附则)第[一二三四五六七八九十百零两0-9]{0,8}[条款项章]"
    r"|第[一二三四五六七八九十百零两0-9]{0,8}[条款项章]"
    r"|[一二三四五六七八九十百零两0-9]{1,8}[条款项章])")

# DN2/DN3 剥头缀动词表（实测驱动）：根据国家/负责本馆/由技术部
# 类头缀粘连——仅用于「剥字重试」判定，不进级联生成过滤（教训：中/党
# 类专名固有字不在本表；剥字只用于 G 覆盖/实体核重试，不造区分性）。
STRIP_HEADS = ("根据", "按照", "依照", "依据", "负责", "成立", "结合", "经由",
               "通过", "针对", "关于", "加强", "规范", "明确", "涉及", "会同",
               "如", "由")

# DN8 头缀粘连表（2026-09-16 碎词治理批）：剥头后 remainder=池内实体（freq
# ≥10）→ w 判粘连变体隐藏（「成立某市图书馆/根据某市财政局/报市文广新局」
# 类实测）。单字头（经/立/报/转）仅在此「remainder 须为榜内实体」的强证据下
# 使用，不做生成期过滤；真实体以这些字开头的（经贸局类）剥后 rem 不在池，
# 不受影响。组织/提交/拟订为章程语料高频引导动词（跨语料证据：某图+示例甲
# 两侧 Top60 实测）。
GLUE_HEADS = STRIP_HEADS + ("组织", "提交", "拟订", "制订", "经", "立", "报", "转",
                            "本办法", "本细则", "本规定", "本制度", "本规则", "提高",
                            "适用", "给", "有关", "支持", "并")

# DN8 字段尾词表（同批）：残段以通用字段名收尾（规章文件+编号/资产变动+信息
# 类）→ 视同功能余段判粘连。仅 4 词+限定 $ 锚，证据=两侧语料 Top60 同形态
# ≥4 项（规章文件编号/规章文件名称/文件审批流程流水号/资产变动信息）。
_FIELD_TAIL_RE = re.compile(r"(?:编号|名称|流水号|信息)$")

# R-C1 声明式（以下简称/简称/下称）两条正则：括号式 与 无括号带引号式。
_DECL_BRACKET_RE = re.compile(
    r"[（(]\s*(?:以下简称|简称|下称)[之为]?[：:]?\s*[“”\"‘’'「『【]?\s*"
    r"([\u4e00-\u9fff]{2,6})[””\"‘’'」』】]?\s*[）)]")
_DECL_QUOTED_RE = re.compile(
    r"(?:以下简称|简称|下称)[之为]?[：:]?\s*[“”\"‘’'「『【]([\u4e00-\u9fff]{2,6})[””\"‘’'」』】]")
_DECL_LOOKBACK_STRIP_RE = re.compile(r"[》）)、，,；;：:\s”’」』】》。．.]+$")
_DECL_FULLNAME_RE = re.compile(r"([\u4e00-\u9fff]{4,16})$")
# 上下文数字打码（统一口径）：≥7 位数字串 →〔数字×N〕
_MASK_DIGITS_RE = re.compile(r"[0-9]{7,}")
# 地名形态减分尾字（S 分量）
_PLACE_TAIL_RE = re.compile(r"(?:市|省|县|区|路|街)$")


# ---------------------------------------------------------------------------
# 配置：默认值依据 J1-J6 定值，环境变量覆盖（非法值回退默认+warnings 告警）
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        warnings.warn(f"环境变量 {name}={raw!r} 不是整数，回退默认值 {default}")
        return default
    if val < minimum:
        warnings.warn(f"环境变量 {name}={val} 低于下限 {minimum}，回退默认值 {default}")
        return default
    return val


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError:
        warnings.warn(f"环境变量 {name}={raw!r} 不是数值，回退默认值 {default}")
        return default
    if not (lo <= val <= hi):
        warnings.warn(f"环境变量 {name}={val} 超出 [{lo}, {hi}]，回退默认值 {default}")
        return default
    return val


@dataclass
class ScannerConfig:
    """扫描参数。默认值依据 J1-J6 定值；env 调节位见模块头。"""

    freq_min: int = field(default_factory=lambda: _env_int("FINMASK_G2_FREQ_MIN", 3, 1))          # J1 召回频次门限
    ngram_max: int = field(default_factory=lambda: _env_int("FINMASK_G2_NGRAM_MAX", 9, 2))        # n-gram 长度上限（中共某市图书馆委员会=9 字）
    single_suffix_min: int = field(default_factory=lambda: _env_int("FINMASK_G2_SINGLE_SUFFIX_MIN", 10, 1))  # J3 单字后缀证据闸 count≥10
    derive_min_freq: int = field(default_factory=lambda: _env_int("FINMASK_G2_DERIVE_MIN_FREQ", 10, 1))     # J4 R-C2/R-C3 全称资格线（且 4≤len≤10）
    weights: tuple = (0.35, 0.20, 0.30, 0.15)   # J6 置信分权重 (F, D, S, C)
    top_page: int = 50                           # UI 分页位（扫描器不截断，Top50 语义在展示端）
    # DN9 边界残片闸（2026-09-16 碎词治理批）：出现点 CJK 左/右邻字 Top-1
    # 集中度 ≥edge_ratio（分母=全部出现数，BOUND 稀释防误杀）且绝对量 ≥edge_min
    # → 判为更长词内部残片。实测分隔带：残片族 0.91~1.00（华人民共和国 0.95/
    # 收账款 0.97/编制及解释部门 1.00），真实体带 ≤0.74（示例乙-直 0.74/示例甲
    # -家 0.61/示例丁 0.38/三重一大 CJK 0）——0.90 居中留裕量。
    edge_ratio: float = field(default_factory=lambda: _env_float("FINMASK_G2_EDGE_RATIO", 0.90, 0.5, 1.0))
    edge_min: int = field(default_factory=lambda: _env_int("FINMASK_G2_EDGE_MIN", 5, 1))


@dataclass(slots=True)
class CandidateWord:
    """内存态候选词（schema 见下；不落盘，会话关闭即弃）。"""

    word: str                       # 候选词原串
    freq: int = 0                   # 全语料精确回填计数（occurrence 级）
    files_n: int = 0                # 命中文档去重数
    channels: list[str] = field(default_factory=list)  # ⊆ {ngram, suffix, abbrev, declared, jieba_nt}
    org_form: bool = False          # 机构形态（后缀/委员会头收尾）
    declared: bool = False          # 来自「以下简称」声明配对（R-C1）
    already_configured: str | None = None  # 扫描器恒置 None，流程层按 _norm_key 五面比对回填
    score: float = 0.0              # 0-100 置信分
    hidden_reason: str | None = None  # None=正常；否则拦截原因（响亮优于静默）
    person_likely: bool = False     # 人名信号（整组不出现在 visible()）
    first_loc: dict = field(default_factory=dict)  # {"file_idx": int, "start": int, "end": int}


class _Nom:
    """提名期证据（回填/闸链前的中间态）。"""

    __slots__ = ("channels", "org_form", "declared")

    def __init__(self) -> None:
        self.channels: set = set()
        self.org_form = False
        self.declared = False


class _Tally:
    """回填计数器：词 → (freq, files, first_loc) + 通道 D 信号位。"""

    __slots__ = ("freq", "files", "first", "jieba_oov", "jieba_common", "nr_flag", "person")

    def __init__(self) -> None:
        self.freq = 0
        self.files: list = []
        self.first: tuple | None = None
        self.jieba_oov = False
        self.jieba_common = False
        self.nr_flag = False
        self.person = False

    def add(self, fi: int, start: int, end: int) -> None:
        self.freq += 1
        if not self.files or self.files[-1] != fi:
            self.files.append(fi)
        if self.first is None:
            self.first = (fi, start, end)


class ScanSession:
    """扫描会话：候选榜 + meta + 按需切片的内存文本。

    零持久化闭环：context_text 从会话内存文本按需切片（≥7 位数字打码），
    close() 清空全部文本与候选。
    """

    def __init__(self, candidates: list | None = None, meta: dict | None = None):
        self.candidates: list = candidates if candidates is not None else []
        self.meta: dict = meta if meta is not None else {}
        self._texts: list = []
        self._file_names: list = []
        self._files_by_word: dict = {}

    # -- 展示面 -------------------------------------------------------------
    def visible(self) -> list:
        """正常展示组：hidden_reason 为空且非 person_likely。"""
        return [c for c in self.candidates
                if c.hidden_reason is None and not c.person_likely]

    def context_text(self, cand: CandidateWord, span: int = 15) -> str:
        """首现 ±span 字切片渲染；≥7 位数字打码「〔数字×N〕。"""
        loc = cand.first_loc or {}
        fi = loc.get("file_idx")
        if fi is None or fi >= len(self._texts):
            return ""
        text = self._texts[fi]
        s = max(0, loc["start"] - span)
        e = min(len(text), loc["end"] + span)
        frag = _MASK_DIGITS_RE.sub(lambda m: f"〔数字×{len(m.group())}〕", text[s:e])
        return frag.replace("\r", "").replace("\n", "⏎")

    def source_files(self, cand: CandidateWord, limit: int = 5) -> list:
        """前 N 个来源文件名（详情弹窗用；「等共 N 份」由展示端拼接）。"""
        idxs = self._files_by_word.get(cand.word, [])
        return [self._file_names[i] for i in idxs[:limit]]

    def rank(self, cand: CandidateWord) -> int:
        """全榜名次（score 降序位次，1 起；丙档「全榜在榜」口径）。"""
        return self.candidates.index(cand) + 1

    # -- 生命周期 ------------------------------------------------------------
    def close(self) -> None:
        """清空全部内存文本与候选（零持久化闭环）。"""
        self.candidates = []
        self._texts = []
        self._file_names = []
        self._files_by_word = {}


# ---------------------------------------------------------------------------
# 文本提取层（探针四格式提取器升级为正式层：docx 全 part w:t / pdf 文字层
# 页判稀 / xlsx 只读值 / pptx slide XML；第三方库惰性导入保持轻依赖）
# ---------------------------------------------------------------------------
_W_T = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
_A_T = "{http://schemas.openxmlformats.org/drawingml/2006/main}t"
_TEXT_PAGE_MIN_CHARS = 20  # 探针口径：有效字符少于 20 视为扫描件页

# 超大文件闸——单文件 >64 MiB 时跳过提取（降级
# 哲学：跳过不阻塞批），防单个巨型文件拖垮整批候选扫描；跳过数计入
# meta.oversize_files（新键，展示端不强求展示）。阈值取 64 MiB：远超
# 常规财税文档上限、又足以拦截异常巨型档（如误选的视频/安装包改后缀）。
_MAX_EXTRACT_BYTES = 64 * 1024 * 1024


def _is_oversize(p: Path) -> bool:
    """超大文件判定（>64 MiB）。stat 失败（文件不存在/无权限）按非超大
    处理——不在闸口二次报错，交由后续提取/降级路径按原口径处置。"""
    try:
        return p.stat().st_size > _MAX_EXTRACT_BYTES
    except OSError:
        return False


def _extract_docx(path: Path) -> str:
    chunks: list = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not (name.startswith("word/") and name.endswith(".xml")):
                continue
            try:
                root = ET.fromstring(z.read(name))
            except Exception:
                continue
            for t in root.iter(_W_T):
                if t.text:
                    chunks.append(t.text)
    return "".join(chunks)


def _extract_pdf(path: Path) -> tuple[str, int]:
    """返回 (text, ocr_needed_pages)。文字层稀疏页不计入文本、计页数。"""
    import fitz  # 惰性导入（PyMuPDF）

    doc = fitz.open(path)
    pages = doc.page_count
    texts: list = []
    text_pages = 0
    for page in doc:
        t = page.get_text("text")
        if len(re.sub(r"\s", "", t)) < _TEXT_PAGE_MIN_CHARS:
            texts.append("")
        else:
            texts.append(t)
            text_pages += 1
    doc.close()
    return "".join(texts), pages - text_pages


def _extract_xlsx(path: Path) -> str:
    from openpyxl import load_workbook  # 惰性导入

    wb = load_workbook(path, read_only=True, data_only=True)
    chunks: list = []
    try:
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                for v in row:
                    if isinstance(v, str):
                        chunks.append(v)
    finally:
        wb.close()
    return "".join(chunks)


def _extract_pptx(path: Path) -> str:
    chunks: list = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not (name.startswith("ppt/slides/slide") and name.endswith(".xml")):
                continue
            try:
                root = ET.fromstring(z.read(name))
            except Exception:
                continue
            for t in root.iter(_A_T):
                if t.text:
                    chunks.append(t.text)
    return "".join(chunks)


def _extract_xls_xlrd(path: Path) -> str:
    """.xls 备用读取（xlrd）——稳定体验补项。

    openpyxl 不支持老版 .xls（InvalidFileException 被 F8 降级哲学吞成空
    文本——此前扫一个纯 .xls 文件夹必然「未发现候选词」）。xlrd 与
    xls_handler 同款口径：全部 sheet 串接文本。
    """
    import xlrd
    parts = []
    try:
        book = xlrd.open_workbook(str(path))
        for sheet in book.sheets():
            for row in sheet.get_rows():
                for cell in row:
                    v = cell.value
                    if isinstance(v, str) and v.strip():
                        parts.append(v.strip())
                    elif isinstance(v, float) and v == int(v):
                        parts.append(str(int(v)))
    except Exception:
        return ""
    return "\n".join(parts)


def _extract_file(p: Path) -> tuple[str, int]:
    """单文件提取 → (text, ocr_pages_skipped)。不支持的扩展名回空。

    超大文件在闸口直接回空（>64 MiB，跳过提取，降级
    降级哲学不抛异常不阻塞批）；scan_sources 侧对同一判定计数
    meta.oversize_files。
    """
    if _is_oversize(p):
        return "", 0
    ext = p.suffix.lower()
    if ext == ".docx":
        return _extract_docx(p), 0
    if ext == ".pdf":
        return _extract_pdf(p)
    if ext == ".xlsx":
        return _extract_xlsx(p), 0
    if ext == ".xls":
        # ⑧：.xls 走 xlrd 备用（openpyxl 不支持该格式）；xlrd 缺失时
        # 回空文本（F8 降级哲学：单文件失败不阻塞批）
        try:
            import xlrd  # noqa: F401
        except ImportError:
            return "", 0
        return _extract_xls_xlrd(p), 0
    if ext == ".pptx":
        return _extract_pptx(p), 0
    if ext == ".md":
        # .md 纯文本：与 md_handler 遮蔽读入同一编码策略（复用单一实现，
        # 两处口径零漂移）；函数内导入保持本模块「不 import handlers 重
        # 链路」的顶层轻依赖契约。OSError 由 scan_sources 按 F8 哲学吞掉。
        from .handlers.md_handler import read_text_detect
        return read_text_detect(p)[0], 0
    return "", 0


# ---------------------------------------------------------------------------
# 同源装载：停用集=装载点实参；后缀生效表=build_org_suffix_tables
# ---------------------------------------------------------------------------
def _load_brand_tables() -> tuple[frozenset, frozenset, int, int]:
    """返回 (dn1 停用集, 后缀生效 vocab, 生效 maxlen, 配置警告条数)。

    与 rules_engine.__init__ 装载点同源：load_brand_config(forbidden_words=
    write_forbidden_set()) → build_org_suffix_tables(cfg.org_suffixes,
    cfg.org_suffixes_remove)。停用集统一经 brand_config.write_forbidden_set()
    派生（含历史语料特例差集；同源由构造保证，禁止私拷表达式）。配置警告
    只计数不落日志（零持久化红线：警告文案含用户词面）。
    """
    from .brand_config import load_brand_config, write_forbidden_set  # 轻依赖，函数内导入同源

    cfg = load_brand_config(forbidden_words=write_forbidden_set())
    _core, allv, maxlen = build_org_suffix_tables(cfg.org_suffixes, cfg.org_suffixes_remove)
    stop = write_forbidden_set()
    return frozenset(stop), frozenset(allv), maxlen, len(cfg.warnings)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def _func_core(s: str) -> str:
    """合流修正一：剥功能字头/尾后的实体核。"""
    i, j = 0, len(s)
    while i < j and s[i] in FUNC_CHARS:
        i += 1
    while j > i and s[j - 1] in FUNC_CHARS:
        j -= 1
    return s[i:j]


def _func_core_ok(s: str) -> bool:
    """实体核 ≥2 CJK 字才保留（中共X 存活；动词短语剥后不足仍灭）。"""
    return len(_func_core(s)) >= 2


def _longest_tail(s: str, heads_by_lastchar: dict) -> str:
    """s 末尾命中的最长 head（按末字索引短路，heads 组内按长度降序）。"""
    bucket = heads_by_lastchar.get(s[-1]) if s else None
    if not bucket:
        return ""
    for h in bucket:
        if s.endswith(h):
            return h
    return ""


def _index_heads_by_lastchar(heads) -> dict:
    """部门头集合按末字建索引（索引化 _longest_tail 的短路预计算）。"""
    idx: dict = {}
    for h in heads:
        idx.setdefault(h[-1], []).append(h)
    for bucket in idx.values():
        bucket.sort(key=len, reverse=True)  # heads 已有序时稳定保持
    return idx


_JIEBA_READY = False


def _ensure_jieba() -> None:
    global _JIEBA_READY
    if _JIEBA_READY:
        return
    import logging as _logging
    import jieba
    jieba.setLogLevel(_logging.ERROR)  # 静默词典加载日志（与词面无关）
    _JIEBA_READY = True


# ---------------------------------------------------------------------------
# 主流水线
# ---------------------------------------------------------------------------
def scan_sources(paths, on_progress=None, config: ScannerConfig | None = None,
                 reuse_texts: dict | None = None,
                 configured_words=None) -> ScanSession:
    """扫描一组文档 → ScanSession（全量入口）。

    paths: 文件路径可迭代（五扩展名外的文件按空文本计入 files_n，不抛异常）；
    on_progress: callable(done, total)，逐文件回调（UI 进度节流由调用方负责）；
    reuse_texts: {Path: text} piggyback——命中键的文件不再提取，直接复用批内
      已抽取/已 OCR 文本（复用方案）；
    configured_words: 用户已登记词（brands/别名/③区自定义/对照表键，2026-09-16
      碎词治理批）——作为外部可信容器注入 DN5 覆盖精算与 DN5b 包含实体集：
      其内部残片（配置全名的截断碎片）按残片隐藏，不再推荐。仅内存使用，
      不入 meta、不落盘；None=零行为（考场零配置口径不受影响）。
    """
    cfg = config if config is not None else ScannerConfig()
    paths = [Path(p) for p in paths]
    reuse: dict = {}
    if reuse_texts:
        reuse = {Path(k): v for k, v in reuse_texts.items()}
    ext_words = None
    if configured_words is not None:
        ext_words = sorted({w.strip() for w in configured_words if w and w.strip()})

    t0 = time.perf_counter()
    texts: list = []
    names: list = []
    ocr_skipped = 0
    failed_files = 0    # 解析失败文件计数（meta.failed_files）
    oversize_files = 0  # 超大文件跳过计数（meta.oversize_files）
    interrupted = False
    for i, p in enumerate(paths, 1):
        if p in reuse:
            text, ocr = reuse[p], 0
        elif _is_oversize(p):
            # 超大文件不进解析器（>64 MiB；跳过不阻塞批）。
            # 先闸后提取，与 _extract_file 头部同判定、同阈值常量。
            text, ocr = "", 0
            oversize_files += 1
        else:
            try:
                text, ocr = _extract_file(p)
            except Exception:  # 单文件失败不阻塞批（F8 降级哲学）
                text, ocr = "", 0
                failed_files += 1
        texts.append(text)
        names.append(p.name)
        ocr_skipped += ocr
        if callable(on_progress) and on_progress(i, len(paths)) is False:
            # 停止协议（v2.1.4 修复）：UI 请求停止（「取消扫描」按钮）→
            # 已提取部分照常进管线出部分结果；interrupted 旗标入 meta 供
            # 展示端诚实标注。此前回调返回值被忽略，「正在停止/部分结果」
            # 文案不实（评审实锤）。
            interrupted = True
            break

    session = _run_pipeline(texts, names, cfg, ext_words)
    session.meta["files_n"] = len(texts)
    session.meta["files_total"] = len(paths)
    session.meta["interrupted"] = interrupted
    session.meta["ocr_pages_skipped"] = ocr_skipped
    # failed_files 契约补全——UI 侧（候选词确认窗黄条）
    # 早已消费 meta.failed_files，扫描器此前从未产出该键（int(None or 0)
    # 恒 0，黄条永不出现）。现按 ocr_pages_skipped 同模式计数产出。
    session.meta["failed_files"] = failed_files
    # 超大文件跳过计数（新键；降级哲学只留痕不阻塞）。
    session.meta["oversize_files"] = oversize_files
    session.meta["elapsed_s"] = round(time.perf_counter() - t0, 2)
    return session


def _run_pipeline(texts: list, names: list, cfg: ScannerConfig,
                  configured_words=None) -> ScanSession:
    """生成 → 验证 → 降噪 → 排序。全程内存。
    configured_words：用户已登记词（外部可信容器，见 scan_sources 文档）。"""
    meta: dict = {
        "total_chars": sum(len(t) for t in texts),
        "nominees_n": 0,
        "low_freq_dropped": 0,
        "person_hidden_n": 0,
        "declared_pairs": 0,
        "suffix_stem_dropped": 0,
        "gate_hidden": {g: 0 for g in _GATES},
    }
    _t0 = time.perf_counter()
    stop_set, suffix_vocab, suffix_maxlen, warn_n = _load_brand_tables()
    meta["phase_ms"] = {"load": round((time.perf_counter() - _t0) * 1000, 1)}
    meta["brand_cfg_warnings"] = warn_n  # 只计数（文案含词面，不落日志）

    # DN2 部门头种子 = _ENUM_ORG_SUFFIXES ∪ 后缀生效表 ∪ 党务群团头
    dept_heads = tuple(sorted(
        set(_ENUM_ORG_SUFFIXES) | set(suffix_vocab)
        | {"党委", "党组", "党委会", "党支部", "党总支", "工会"},
        key=len, reverse=True))
    heads_by_lastchar = _index_heads_by_lastchar(dept_heads)
    suffix_lastchars = frozenset(t[-1] for t in suffix_vocab)
    g_lastchars = frozenset(t[-1] for t in GENERIC_VOCAB)
    # DN4 单字后缀集（_ORG_SUFFIX_TOKENS ∪ _OPTIONAL 派生，长度=1 的成员）
    single_suffix = {t for t in (set(_ORG_SUFFIX_TOKENS) | set(_ORG_SUFFIX_TOKENS_OPTIONAL))
                     if len(t) == 1}
    # 通道 B 提名后缀 = 引擎双表 ∪ _NOMINATION_SUFFIX_EXTRA（合流修正二）
    nom_suffix = tuple(sorted((set(_ORG_SUFFIX_TOKENS) | set(_ORG_SUFFIX_TOKENS_OPTIONAL)
                               | _NOMINATION_SUFFIX_EXTRA), key=len, reverse=True))
    suffix_alt = "|".join(re.escape(t) for t in nom_suffix)
    suffix_re = re.compile(rf"([\u4e00-\u9fff]{{2,16}})(?P<sf>{suffix_alt})")

    # ---- 提取连串 + 文档级 2-gram 计数（回填索引底座） --------------------
    runs_all: list = []       # per file: [(offset, run), ...]
    c2_file: list = []        # per file: Counter{2-gram: n}
    count2: collections.Counter = collections.Counter()
    for text in texts:
        fr = [(m.start(), m[0]) for m in _CJK_RUN_RE.finditer(text)]
        runs_all.append(fr)
        fc: collections.Counter = collections.Counter()
        for _off, run in fr:
            for a, b in zip(run, run[1:]):
                fc[a + b] += 1
        c2_file.append(fc)
        count2.update(fc)

    # ---- 通道 A：CJK n-gram 级联计数（主生成器；J1/J2 设计依据） --
    # 级联生成面按设计伪代码做功能字「含即弃」（把动词短语类噪声灭绝在生成期、
    # 控制级联池规模）；合流修正一的功能字闸语义由 DN3 闸与通道 B 躯干判定
    # （func_core_ok 剥头/尾实体核 ≥2）承载——「中共X」类机构词经通道 B
    # （_NOMINATION_SUFFIX_EXTRA 提名）+频次回填存活，判例词零损失。
    # 实现注记：keep[n-1] 成员已不含功能字 → 第 n 层只需检查新尾字（O(1)）。
    meta["phase_ms"]["runs_c2"] = round((time.perf_counter() - _t0) * 1000, 1)
    keep: dict = {}
    keep[2] = {g: c for g, c in count2.items()
               if c >= cfg.freq_min and g[0] not in FUNC_CHARS
               and g[1] not in FUNC_CHARS}
    for n in range(3, cfg.ngram_max + 1):
        prev = keep[n - 1]
        if not prev:
            keep[n] = {}
            continue
        cur: dict = {}
        for fr in runs_all:
            for _off, run in fr:
                L = len(run)
                for i in range(L - n + 1):
                    if run[i:i + n - 1] in prev and run[i + n - 1] not in FUNC_CHARS:
                        g = run[i:i + n]
                        cur[g] = cur.get(g, 0) + 1
        keep[n] = {g: c for g, c in cur.items() if c >= cfg.freq_min}

    meta["phase_ms"]["cascade"] = round((time.perf_counter() - _t0) * 1000, 1)
    nominees: dict = {}

    def nominate(word: str, channel: str, org_form: bool = False,
                 declared: bool = False) -> None:
        nom = nominees.get(word)
        if nom is None:
            nom = _Nom()
            nominees[word] = nom
        nom.channels.add(channel)
        nom.org_form = nom.org_form or org_form
        nom.declared = nom.declared or declared

    for n in sorted(keep):
        for g in keep[n]:
            nominate(g, "ngram")

    # ---- 通道 B：组织后缀模式（贪婪配对 + 纠偏前移） ----------------------
    for text in texts:
        for m in suffix_re.finditer(text):
            word = m.group(0)
            stem = word[:-len(m.group("sf"))]
            if not stem or stem in GENERIC_PREFIX or not _func_core_ok(stem):
                meta["suffix_stem_dropped"] += 1  # 探针 denoise_drop 同风格：只计数
                continue
            nominate(word, "suffix", org_form=True)

    # ---- 通道 C / R-C1：声明式提取（简称入候选，最高置信） ----------------
    for text in texts:
        taken: list = []  # 已采信的声明匹配跨度（双正则去重，只影响配对计数）
        for rex in (_DECL_BRACKET_RE, _DECL_QUOTED_RE):
            for m in rex.finditer(text):
                if any(a <= m.start() < b for a, b in taken):
                    continue
                if _nominate_declared(m, text, nominate):
                    taken.append((m.start(), m.end()))
                    meta["declared_pairs"] += 1

    # ---- 通道 C / R-C2 + R-C3：全称派生简称（生成-验证闭环，J4） ----------
    eligible = _eligible_fullnames(nominees, keep, cfg, texts, c2_file)
    _channel_abbrev_r2(eligible, count2, cfg, nominate)
    _channel_abbrev_r3(eligible, suffix_vocab, keep, count2, cfg, nominate)

    # org_form 一次性判定（后缀通道提名已置位；其余提名按机构形态后缀收尾判定，
    # 装配面直接读 _Nom，避免打分/隐藏阶段重复 endswith 扫描）
    nom_suffix_lastchars = {t[-1]: [] for t in nom_suffix}
    for t in nom_suffix:  # nom_suffix 已按长度降序，组内保持有序
        nom_suffix_lastchars[t[-1]].append(t)
    for w, nom in nominees.items():
        if not nom.org_form:
            for t in nom_suffix_lastchars.get(w[-1], ()):
                if w.endswith(t):
                    nom.org_form = True
                    break

    meta["nominees_n"] = len(nominees)

    meta["phase_ms"]["nominate_suffix_abbrev"] = round((time.perf_counter() - _t0) * 1000, 1)

    # ---- 预回填闸链（集合查表/切分类，闸序即代价序） ----------------------
    tally: dict = {w: _Tally() for w in nominees}
    surv: list = []
    pre_hidden: dict = {}   # 词 → 预回填闸原因（纯组织后缀/停用词表/通用部门词/功能字躯干）
    post_hidden: dict = {}  # 词 → 回填后闸原因（单字后缀低证据/词性尾验）
    for w in nominees:
        norm = _norm_candidate(w)
        # 纯度闸（v1.9.3 同源语义：整词被后缀生效表完整切分 → 勾了也遮不掉；
        # 末字预滤：全覆盖必经末字命中词表末字集，未命中免 DP）
        if (norm[-1] in suffix_lastchars
                and _org_suffix_full_cover(norm, suffix_vocab, suffix_maxlen)):
            pre_hidden[w] = HIDDEN_PURE
            continue
        # DN1 停用集同源闸（装载点实参 ∪ 后缀生效表，整词相等）
        if norm in stop_set or w in suffix_vocab:
            pre_hidden[w] = HIDDEN_STOP
            continue
        # DN2 通用组织形态闸（G 表=generic_vocab.GENERIC_VOCAB，地名/品牌字
        # 是区分性成分 → 覆盖切分失败 → 存活）
        if _dn2_generic(w, norm, heads_by_lastchar):
            pre_hidden[w] = HIDDEN_DEPT
            continue
        # DN6 条款号形态（第X条/第X章 类；实测 Top50 实证）
        if _DOCNO_RE.fullmatch(norm):
            pre_hidden[w] = HIDDEN_DOCNO
            continue
        # DN3 功能字躯干闸（合流修正一语义；通道 A/B 提名面已过滤，此处兜底留痕）
        if not _func_core_ok(w):
            pre_hidden[w] = HIDDEN_FUNC
            continue
        surv.append(w)
    for reason in pre_hidden.values():
        meta["gate_hidden"][reason] += 1

    # ---- 频次精确回填（occurrence 级精确计数 + 命中文档去重 + 首现定位） --
    # 全部提名词都回填（含预闸拦下者——已隐藏区也展示频次）；2 字词走文档级
    # 2-gram 计数器（零扫描），≥3 字词走连串位置索引（前缀预滤，索引计数
    # 口径）。
    # 预剪枝（上界定理）：词频 ≤ 其任一内部 2-gram 的全语料计数——内部
    # 2-gram 计数 < freq_min 的词不可能过频次门限，直接计放弃、不入回填走查
    # （declared 例外仍走全量回填）。剪枝与回填后放弃的出榜结果逐词一致。
    by_len: dict = {}
    pruned: set = set()
    for w in nominees:
        if len(w) >= 3 and not nominees[w].declared:
            for i in range(len(w) - 1):
                if count2.get(w[i:i + 2], 0) < cfg.freq_min:
                    pruned.add(w)
                    break
            if w in pruned:
                continue
        by_len.setdefault(len(w), {})[w] = tally[w]
    for w, rec in by_len.get(2, {}).items():
        for fi, fc in enumerate(c2_file):
            n = fc.get(w, 0)
            if n:
                pos = texts[fi].find(w)
                for _ in range(n):
                    rec.add(fi, pos, pos + 2)
    for n in sorted(k for k in by_len if k >= 3):
        table = by_len[n]
        prefix = {w[:-1] for w in table}
        for fi, fr in enumerate(runs_all):
            for off, run in fr:
                L = len(run)
                for i in range(L - n + 1):
                    if run[i:i + n - 1] not in prefix:
                        continue
                    rec = table.get(run[i:i + n])
                    if rec is not None:
                        s = off + i
                        rec.add(fi, s, s + n)

    meta["phase_ms"]["gates_refill"] = round((time.perf_counter() - _t0) * 1000, 1)

    # ---- 容器频索引（MAX 口径·全池）：池 = 回填后达频次门
    # 限或声明对的全部提名词——含将被各闸隐藏的更长词（隐藏词的出现同样解
    # 释其内部残片：理办法 ⊂ 管理办法(G 隐藏)、定资产 ⊂ 固定资产(停用)，
    # 实测二轮判例）。eff = freq − MAX 容器频：MAX 对残片集群互叠天然免疫
    # （不会像 SUM/覆盖精算那样把真词解释到 0——某市图书馆实测回退案例）；
    # 多容器分摊场景（城图书馆 = 某市图书馆 502 + 某市市图书馆 3）由 eff 打
    # 分沉底兜住，不依赖精确隐藏。
    pool = [w for w in nominees
            if nominees[w].declared or tally[w].freq >= cfg.freq_min]
    cont_max = _build_container_freq(pool, tally, mode="max")
    pre_hidden_set = set(pre_hidden)

    # ---- 频次诚实边界：freq<freq_min 且非 declared → 放弃（非隐藏，
    #      不入榜；声明对例外——语境证据替代频次证据） ------------------------
    kept: list = []
    for w in surv:
        if w in pruned or (tally[w].freq < cfg.freq_min
                           and not nominees[w].declared):
            meta["low_freq_dropped"] += 1
            continue
        kept.append(w)

    # ---- DN4 单字后缀低证据闸（J3：末字单字后缀 且 len≤4 且 count<10） ----
    stage2: list = []
    for w in kept:
        if (w[-1] in single_suffix and len(w) <= 4
                and tally[w].freq < cfg.single_suffix_min):
            post_hidden[w] = HIDDEN_SINGLE
            meta["gate_hidden"][HIDDEN_SINGLE] += 1
            continue
        stage2.append(w)

    # ---- 通道 D 前置：批量 posseg + 人名标记（S1 第三刀重排） -------------
    # 人名标记必须先于 DN5：整名人可能被残串容器「解释到 0」而残片反而存留
    # （实测判例），先标 person 再做残片闸，传播才有种子、整名不丢旗。
    # 实现注记：候选逐个 posseg 与「\n 分隔批量 posseg」结果逐 token 相等
    # （jieba 按非汉字块切句），批量一次调用压掉数十万次 cut 调用开销；
    # 信号（末 token 旗标/any-nr/any-nt/token 数）在遍历中增量统计。
    flags: dict = {}
    if stage2:
        _ensure_jieba()
        import jieba
        import jieba.posseg as pseg
        rows: list = []         # 每词 (末 token 旗标, any-nr, any-nt, token 数, oov 字符数)
        big = "\n".join(stage2)
        _last, _nr, _nt, _n, _oo = "", False, False, 0, 0
        for t in pseg.cut(big):
            if t.word == "\n":
                rows.append((_last, _nr, _nt, _n, _oo))
                _last, _nr, _nt, _n, _oo = "", False, False, 0, 0
                continue
            _last = t.flag
            _n += 1
            if jieba.dt.FREQ.get(t.word, 0) == 0:
                _oo += len(t.word)
            if t.flag == "nr":
                _nr = True
            elif t.flag == "nt":
                _nt = True
        rows.append((_last, _nr, _nt, _n, _oo))
        flags = dict(zip(stage2, rows))
        for w in stage2:
            last_flag, nr_flag, nt_flag, _ntok, _oovc = flags[w]
            rec = tally[w]
            rec.nr_flag = nr_flag
            if nt_flag:
                nominees[w].channels.add("jieba_nt")  # 机构形态旁证
            dict_freq = jieba.dt.FREQ.get(w, 0)
            rec.jieba_oov = dict_freq == 0          # 未登录（打分加分面）
            rec.jieba_common = dict_freq >= 200     # 词典高频常用词（降权面）

    # ---- person_likely（nr 旗标 / 职务上下文；机构形态与声明对豁免） --
    kw_sorted = tuple(sorted(_KEYWORD_BEFORE, key=len, reverse=True))
    title_sorted = tuple(sorted(_TITLE_AFTER2, key=len, reverse=True))
    person_hidden = 0
    for w in stage2:
        rec = tally[w]
        nom = nominees[w]
        if (len(w) <= 4 and len(rec.files) <= 2
                and not nom.org_form and not nom.declared
                and (rec.nr_flag or _title_context_hit(w, rec, texts,
                                                       kw_sorted, title_sorted))):
            rec.person = True
            person_hidden += 1
    # 人名传播（防残片逃逸）：人名词的内部子串同标 person 组——
    # 「怀瑾」类残片无论残片闸结果如何都不可达用户榜（实测归因）。
    person_hidden += _propagate_person(stage2, tally)
    meta["person_hidden_n"] = person_hidden

    # ---- DN7 短语连串闸前置判定（phrase 命中） -----------------------------
    # entity_words 资格守则（2026-09-16 碎词治理批）：freq−MAX容器频 达
    # 「比例 ≥50% 或绝对独立 ≥3」双通道——治「务管理/收账款」类高频残片的
    # 豁免污染（残片独立数≈1 出局），同时保「示例乙」类被自身变体（直营
    # 专卖店 15/23）大幅解释的真品牌（绝对独立 8≥3 在集——二轮实测回退
    # 案例：单比例通道曾致「示例乙事业部」被短语闸误杀）。MAX 口径零额外
    # 扫描（cont_max 已有）。
    phrase_hits: set = set()
    entity_words = [e for e in pool if len(e) >= 3 and tally[e].freq >= 10
                    and not _g_cover(e)
                    and (tally[e].freq - cont_max.get(e, 0) >= 0.5 * tally[e].freq
                         or tally[e].freq - cont_max.get(e, 0) >= 3)]
    entity_words.sort(key=len, reverse=True)
    place_tokens = tuple(sorted(
        (p for p in (_PLACE_NAMES | _PLACE_WORDS) if len(p) >= 2),
        key=len, reverse=True))
    for w in stage2:
        last_flag, nr_flag, nt_flag, ntok, oovc = flags.get(w, ("", False, False, 0, 0))
        if len(w) >= 4 and ntok >= 3 and oovc == 0:
            if nominees[w].org_form and tally[w].freq >= 10 and not _dup2gram(w):
                continue  # 成形态机构词豁免（某市财政局/中共某市图书馆委员会 类；
                #  乱串形态不豁免——「程管控…管控部」类 suffix 错切，同批实测）
            head = _longest_tail(w, heads_by_lastchar)
            stem = w[:len(w) - len(head)] if head else w
            if any(p in stem for p in place_tokens):
                continue  # 词干含地名成分（某市文库研究中心 类）豁免
            # 含实体级词豁免：e≠w（自包含修复——「规章文件编号/收账款」类
            # 高频噪声曾以自身为豁免实体存活，2026-09-16 碎词治理批实测）
            if any(e != w and e in w for e in entity_words):
                continue  # 含实体级词（规范某市图书馆/示例乙事业部 类）豁免
            phrase_hits.add(w)

    # ---- DN5 长词内部残片闸（出现级覆盖精算） ----------------
    # 可信容器 = 非 0-OOV 短语连串 且（被闸隐藏 / 声明对 / MAX 非残片）的池
    # 内词——短语闸前置纯化可信集（实测案例：某市图书馆曾被 某市图书馆财务
    # 管理 类连串残片解释到 0）。覆盖按长度自长至短单遍合并扫：先查本长度
    # 词的独立出现（>n 容器区间已就位），再并入本长度可信容器区间——X收账款
    # 族词（应收/预收/暂收账款 各自仅部分覆盖）由区间并集正确解释，MAX 的
    # 族内分摊盲区就此消除。configured_words（用户已登记词）作为外部可信容器
    # 并入 cred_by_len（2026-09-16 碎词治理批）：配置全名在文本中的出现区间
    # 同样解释其内部残片——容器词本身不必在候选池（「应收账款」类被功能字
    # 生成期滤除的容器由此补位）。
    credible = [w for w in pool
                if w in pre_hidden_set or nominees[w].declared
                or (w not in phrase_hits
                    and tally[w].freq - cont_max.get(w, 0) > 0)]
    unc: dict = {w: 0 for w in stage2}
    stage2_by_len: dict = {}
    for w in stage2:
        stage2_by_len.setdefault(len(w), set()).add(w)
    cred_by_len: dict = {}
    for w in credible:
        cred_by_len.setdefault(len(w), set()).add(w)
    for w in (configured_words or ()):
        cred_by_len.setdefault(len(w), set()).add(w)  # 外部可信容器（不去重候选词面——e≠w 判定在 DN5b/筛除面各自把关）
    # DN9 邻字分布底座（2026-09-16 碎词治理批）：候选的 CJK 左/右邻字
    # Counter——与 DN5 独立出现扫同一遍循环顺带统计（命中点 i>0 ⇒ pre=
    # run[i-1]，i+n<L ⇒ next=run[i+n]；run 边界=BOUND 不计），零独立扫描。
    # 长度帽=ngram_max（suffix 通道错切体可达 9 字「在执行过程中如上级部」，
    # 其 next=门 集中是唯一杀招）。非对称规则（实测「税收账款+了(了结)」
    # 句法助词误杀回退判例）：pre 侧功能字邻字保留（中/应=被滤容器词首
    # 遗痕，「中华人民共和国/应收账款」实证）；next 侧功能字邻字排除
    # （了/的/是/为 高频黏着一切名词，属句法非词内）。代价=「X采购行+为
    # (行为)」类截断漏杀，登记观察。
    edge_pre: dict = {}
    edge_next: dict = {}
    edge_len_max = cfg.ngram_max
    merged_by_fi: dict = {}
    for n in sorted(set(stage2_by_len) | set(cred_by_len), reverse=True):
        table = stage2_by_len.get(n)
        want_edge = table is not None and n <= edge_len_max
        if want_edge:
            edge_pre.update({w: collections.Counter() for w in table})
            edge_next.update({w: collections.Counter() for w in table})
        if table:
            for fi, fr in enumerate(runs_all):
                iv, starts = merged_by_fi.get(fi, ((), []))
                for off, run in fr:
                    L = len(run)
                    for i in range(L - n + 1):
                        g = run[i:i + n]
                        if g in table:
                            if want_edge:
                                if i > 0:
                                    edge_pre[g][run[i - 1]] += 1
                                if i + n < L and run[i + n] not in FUNC_CHARS:
                                    edge_next[g][run[i + n]] += 1  # 功能字排除（非对称规则）
                            j = bisect.bisect_right(starts, off + i) - 1
                            if not (j >= 0 and iv[j][1] >= off + i + n):
                                unc[g] += 1
        ct = cred_by_len.get(n)
        if ct:
            newiv: dict = {}
            for fi, fr in enumerate(runs_all):
                for off, run in fr:
                    L = len(run)
                    for i in range(L - n + 1):
                        if run[i:i + n] in ct:
                            newiv.setdefault(fi, []).append((off + i, off + i + n))
            for fi, ivs in newiv.items():
                old = merged_by_fi[fi][0] if fi in merged_by_fi else []
                allv = sorted(list(old) + ivs)
                out: list = []
                cs_, ce_ = allv[0]
                for s, e in allv[1:]:
                    if s <= ce_:
                        if e > ce_:
                            ce_ = e
                    else:
                        out.append((cs_, ce_))
                        cs_, ce_ = s, e
                out.append((cs_, ce_))
                merged_by_fi[fi] = (out, [a for a, _b in out])

    frag_hidden: dict = {}
    uncovered: dict = unc
    stage3: list = []
    for w in stage2:
        if unc[w] <= 0:
            frag_hidden[w] = HIDDEN_FRAG
            meta["gate_hidden"][HIDDEN_FRAG] += 1
        else:
            stage3.append(w)

    # ---- DN5b 实体包含式残片（前置闸） ---------------------
    # 被池内实体词（freq≥10、非 G 可覆盖、≥3 字）包含、且独立出现「双门槛
    # ≤10% 且 ≤10 次」的候选 → 残片隐藏——实体内部残片家族（城图书馆 7/512、
    # 示例甲家 5/135 类）按频次打分必然霸榜；示例乙 独立率 35% 的真品牌不
    # 误杀（实测案例）。某市图书馆/某图 类无包含实体者不受影响。
    # configured_words（用户已登记词）并入包含实体集（2026-09-16 碎词治理批）：
    # 配置全名的外部残片（「示例甲家居股份有限公司」已配置 ⇒「甲家居股」
    # 类内部碎片）不再推荐——u_w 已含外部容器区间扣减（DN5 并扫）。
    pool_entities = [e for e in pool if len(e) >= 3 and tally[e].freq >= 10
                     and not _g_cover(e)]
    ext_entities = [e for e in (configured_words or ())
                    if len(e) >= 3 and e not in tally]
    frag2: set = set()
    for w in stage3:
        f_w = tally[w].freq
        u_w = uncovered.get(w, f_w)
        # 双门槛：独立 ≤10 次 且 ≤10%——城图书馆 7/512 类残片在网内，
        # 示例乙 8/23=35% 类真品牌不误杀（实测案例）
        if not (u_w <= 10 and u_w <= 0.10 * f_w):
            continue
        if any(w != e and w in e for e in pool_entities):
            frag2.add(w)
            continue
        if any(w != e and w in e for e in ext_entities):
            frag2.add(w)
    if frag2:
        kept2: list = []
        for w in stage3:
            if w in frag2:
                frag_hidden[w] = HIDDEN_FRAG
                meta["gate_hidden"][HIDDEN_FRAG] += 1
            else:
                kept2.append(w)
        stage3 = kept2

    # ---- DN9 边界残片闸（2026-09-16 碎词治理批） -----------------------------
    # 功能字生成期滤除使真容器缺席（「中华人民共和国」被滤「中」⇒「华人民
    # 共和国」无容器可解释），此类残片的通用证据=出现点邻字分布：某 CJK 邻
    # 字以 ≥edge_ratio 集中度伴随（分母=全部出现数，BOUND 出现稀释防误杀）
    # 且绝对量 ≥edge_min ⇒ 这些出现实为「邻字+w」更长串的内部。实测分隔带：
    # 残片 0.91~1.00（华人民共和国 0.95/收账款 0.97/编制及解释部 next-门
    # 1.00/市本级行 next-政 1.00）vs 真实体 ≤0.74（示例乙-直 0.74/示例甲-
    # 家 0.61/示例丁 0.38/三重一大 CJK 0）；低频真实体（某市图书馆 n=3）被
    # edge_min=5 绝对量豁免。declared（声明对，三重一大类）整类豁免。
    edge_hits: set = set()
    for w in stage3:
        if len(w) > edge_len_max or nominees[w].declared:
            continue
        f_w = tally[w].freq
        for cnt in (edge_pre.get(w), edge_next.get(w)):
            if not cnt:
                continue
            ch, k = cnt.most_common(1)[0]
            if k >= cfg.edge_min and k >= cfg.edge_ratio * f_w:
                edge_hits.add(w)
                break
    if edge_hits:
        kept3: list = []
        for w in stage3:
            if w in edge_hits:
                frag_hidden[w] = HIDDEN_FRAG
                meta["gate_hidden"][HIDDEN_FRAG] += 1
            else:
                kept3.append(w)
        stage3 = kept3

    # ---- POS 尾验（末 token 动词/方位词/助词/介连副/叹 → 弃） --------------
    # 严格执行/日起施行 类动词短语；词典 nr 整词对尾部 2-3 字复核词性，防
    # 「严格执行」类词典 nr 误标漏网。
    if stage3:
        tail_jobs: list = []
        for w in stage3:
            last_flag, _nr, _nt, ntok, _oovc = flags[w]
            # ntok≤2（2026-09-16 碎词治理批放宽，原 ntok==1）：词典 nr 误标
            # 双 token 形态（「必须/严格执行(nr)」类）同样走尾 2-3 字词性复核
            if ntok <= 2 and last_flag == "nr" and len(w) >= 2:
                for tail in {w[-2:], w[-3:]}:
                    if len(tail) >= 2 and tail != w:
                        tail_jobs.append(tail)
        tail_bad_words: set = set()
        if tail_jobs:
            unique_tails = sorted(set(tail_jobs))
            tail_verdict = {}
            for t, blocks in zip(unique_tails, _iter_posseg_blocks(unique_tails, pseg)):
                tail_verdict[t] = bool(blocks) and blocks[-1][1].startswith("v")
            for w in stage3:
                last_flag, _nr, _nt, ntok, _oovc = flags[w]
                if ntok <= 2 and last_flag == "nr" and any(
                        tail_verdict.get(t, False)
                        for t in ({w[-2:], w[-3:]} - {w}) if len(t) >= 2):
                    tail_bad_words.add(w)
        kept_pos: list = []
        for w in stage3:
            if flags[w][0].startswith(("v", "f", "u", "y", "c", "p", "d", "e")) \
                    or w in tail_bad_words:
                post_hidden[w] = HIDDEN_POS
                meta["gate_hidden"][HIDDEN_POS] += 1
                continue
            kept_pos.append(w)
        stage3 = kept_pos

    # ---- DN7 短语连串闸 + DN8 粘连变体闸（短语命中判定已随可信集纯化前置，
    # 此处对存活者落账；粘连实体须自身独立证据达半，防残片链互证——实测判例：
    # 示例甲家居股份有限公司曾被「示例甲家居股份有限(unc=1)+公司」粘连误杀）
    # entities 守则换 MAX 容器口径（2026-09-16 碎词治理批）：unc≥0.5×freq 的
    # 区间精算守则曾把真容器「某市图书馆」(unc 101/502=0.20) 整个排除，致
    # 「某市图书馆馆」类粘连体无实体可用；MAX 口径（freq−MAX容器频≥0.5×freq）
    # 对残片链仍免疫（链成员 freq≈容器频 → 出局），真容器多容器分摊场景回池。
    entities = [e for e in stage3 if len(e) >= 4 and tally[e].freq >= 10
                and tally[e].freq - cont_max.get(e, 0) >= 0.5 * tally[e].freq]
    entity_set = set(entities)
    pool_freq10 = {w for w in pool if tally[w].freq >= 10}
    # 尾缀方向实体索引（按首字——pool_long 千级 × stage3 千级的全对子串扫
    # 会破耗时线；索引化后每词 ≤len(w) 个起点 × 小桶）
    pool_long_by_first: dict = {}
    for e in pool_freq10:
        if len(e) >= 4:
            pool_long_by_first.setdefault(e[0], []).append(e)
    kept_final: list = []
    for w in stage3:
        if w in phrase_hits:
            post_hidden[w] = HIDDEN_PHRASE
            meta["gate_hidden"][HIDDEN_PHRASE] += 1
            continue
        glued = False
        # DN8 头缀方向（2026-09-16 碎词治理批对称化）：w 剥引导动词/规章自称
        # 头（至多两层，「本办法由财务管理中心」=本办法+由 双层）后 remainder
        # =池内实体（freq≥10）⇒ 头缀粘连体（成立某市图书馆/本办法由财务管理
        # 中心 类）。remainder 须自身达实体证据线，防「头+弱串」误杀。
        core = w
        stripped = False
        # 多字头优先、功能字次之，交替剥至多四层（「本办法适用于X」=
        # 本办法+适用+于 三层；「并结合公司」=并+结合 两层——「本/并」∈
        # FUNC_CHARS，若功能字预剥先行会拆散「本办法」多字头致匹配失效）
        for _ in range(4):
            hit = next((h for h in GLUE_HEADS
                        if core.startswith(h) and len(core) - len(h) >= 2), None)
            if hit is not None:
                core = core[len(hit):]
                stripped = True
                continue
            if core and core[0] in FUNC_CHARS and len(core) >= 3:
                core = core[1:]
                stripped = True
                continue
            break
        if stripped:
            while core and core[0] in FUNC_CHARS and len(core) >= 2:
                core = core[1:]  # 功能字清洁层（本办法适用于X → 剥头后「于X」→X）
            if core in entity_set or core in pool_freq10:
                glued = True
        if not glued and len(w) <= 8:
            # DN8 并列连词粘连（同批）：w 含功能连词（和/与/及/或）且切开的
            # 各段均为池内实体（freq≥10）⇒「审计局和主管局」类并列错切。
            # 真机构名以功能连词分段后各段整词均在池的形态罕见（防误杀线）。
            for sep in ("和", "与", "及", "或"):
                if sep in w:
                    parts = w.split(sep)
                    if len(parts) == 2 and all(
                            len(p) >= 2 and p in pool_freq10 for p in parts):
                        glued = True
                        break
        if not glued:
            # DN8 尾缀方向（2026-09-16 收窄版）：w=池内实体(≥4字, freq≥10)+
            # 纯功能余段 或 字段尾（编号/名称/流水号/信息）⇒ 粘连。「某市图
            # 书馆馆」类叠尾单判。rem2 判定收窄为「剥功能字后为空 或 字段尾」
            # ——不再用 _g_cover(rem2)（「示例甲家居股份有限公司」=示例甲
            # 家居+股份有限公司(G覆盖) 曾被判例误杀，实测回退；机构法定后缀
            # 段是全名的组成部分，非功能余段）。freq≥10 线防残片链互证。
            L = len(w)
            for i in range(L - 3):
                for e in pool_long_by_first.get(w[i], ()):
                    el = len(e)
                    if el >= L - i or e == w or not w.startswith(e, i):
                        continue
                    rem2 = "".join(ch for ch in w[i + el:] if ch not in FUNC_CHARS)
                    head2 = "".join(ch for ch in w[:i] if ch not in FUNC_CHARS)
                    if head2 == "" and (rem2 == "" or _FIELD_TAIL_RE.search(rem2)):
                        glued = True
                        break
                if glued:
                    break
            if not glued and w[-1] == w[-2] and w[:-1] in pool_freq10:
                glued = True  # 叠尾粘连（某市图书馆+馆 类重叠形态）
        if glued:
            post_hidden[w] = HIDDEN_GLUED
            meta["gate_hidden"][HIDDEN_GLUED] += 1
            continue
        kept_final.append(w)
    stage3 = kept_final

    meta["phase_ms"]["posseg"] = round((time.perf_counter() - _t0) * 1000, 1)

    # ---- 置信分（权重 J6） ------------------------------------
    # F 分量用 freq（出现级覆盖已在 DN5 源头隐藏残片，freq 不再被碎片虚胖；
    # 实测判例：eff 计分把真实体一同压低而中段垃圾相对上浮，评审翻红
    # ——频次口径回归稳定锚）。D 分量保留独立率折扣（eff/freq）。
    candidates: list = []
    eff_of = {w: tally[w].freq for w in stage3}
    eff_top = max(eff_of.values(), default=1) or 1
    files_max = max((len(tally[w].files) for w in stage3), default=1) or 1
    # 变体继承（排序质量手段）：实体+组织尾的复合形态
    # （示例乙事业部/示例甲直营店 类）继承实体的频次/广度证据——复合词的推
    # 荐价值即实体在组织形态下的呈现（「完整实体变体计命中」的打分面）。
    var_tails = frozenset(set(nom_suffix) | set(_BRAND_GENERIC_TAILS))
    ent_by_first: dict = {}
    for e in pool_entities:
        ent_by_first.setdefault(e[0], []).append(e)
    var_src: dict = {}
    for w in stage3:
        for e in ent_by_first.get(w[0], ()):
            if len(e) < len(w) and w.startswith(e) and tally[e].freq >= 10:
                tail = w[len(e):]
                if 2 <= len(tail) <= 4 and tail in var_tails:
                    var_src[w] = e
                    break
    w0, w1, w2, w3 = cfg.weights
    for w in stage3:
        rec = tally[w]
        nom = nominees[w]
        e_src = var_src.get(w)
        f_src = tally[e_src].freq if e_src else rec.freq
        d_files = len(tally[e_src].files) if e_src else len(rec.files)
        # 独立率折扣（实测判例「限公司 149>有限公司 148」）：残片的
        # 跨文档广度（D）同样被容器虚胖——D 乘 eff/freq 后残片彻底沉底。
        ratio = min(1.0, uncovered.get(w, rec.freq) / rec.freq) if rec.freq else 0.0
        f = min(1.0, math.log2(1 + f_src) / math.log2(1 + eff_top))
        d = ratio * math.log2(1 + d_files) / math.log2(1 + files_max)
        len_factor = 0.3 if len(w) == 2 else (0.6 if len(w) == 3 else 1.0)
        s = 0.4 * len_factor + 0.3 * (1 if nom.org_form else 0) \
            + 0.3 * (1 if rec.jieba_oov else 0)
        if rec.jieba_common:
            # 词典高频常用词降权（设计的「降权面」，2026-09-16
            # 接线）：「个月/政部/账龄」类高频通用词在碎词闸清场后上浮挤占
            # 低频真品牌（示例丁 13 次/5 份被挤出 Top50——二轮实测回退案例）。
            # 真实体多未登录（jieba_oov 与 common 互斥），不受此压。
            s -= 0.15
        if _PLACE_TAIL_RE.search(w) or w in _PLACE_NAMES or w in _PLACE_WORDS:
            s -= 0.2  # 地名形态减分
        s = max(0.0, min(1.0, s))
        ch = nom.channels
        if nom.declared:
            c = 1.0
        elif "ngram" in ch and "suffix" in ch:
            c = 0.9
        elif "ngram" in ch and "jieba_nt" in ch:
            c = 0.8
        elif "abbrev" in ch:
            c = 0.7
        elif "ngram" in ch:
            c = 0.6
        else:
            c = 0.5
        if e_src:
            c = max(c, 0.9)  # 变体继承：实体+组织尾复合形态按双通道置信计
        candidates.append(CandidateWord(
            word=w,
            freq=rec.freq,
            files_n=len(rec.files),
            channels=[x for x in _CHANNEL_ORDER if x in ch],
            org_form=nom.org_form,
            declared=nom.declared,
            already_configured=None,  # 恒 None（流程层回填）
            score=round(100 * (w0 * f + w1 * d + w2 * s + w3 * c), 2),
            hidden_reason=None,
            person_likely=rec.person,
            first_loc={"file_idx": rec.first[0], "start": rec.first[1],
                       "end": rec.first[2]} if rec.first else {},
        ))

    # 预回填闸与回填后闸（DN4/POS）拦下的候选保留在榜（hidden_reason 留痕，
    # 「已隐藏 {H} 项」数据源）；低频放弃者不入榜。
    def _hidden_cand(w: str, reason: str) -> CandidateWord:
        rec = tally[w]
        return CandidateWord(
            word=w, freq=rec.freq, files_n=len(rec.files),
            channels=[x for x in _CHANNEL_ORDER if x in nominees[w].channels],
            org_form=nominees[w].org_form,
            declared=nominees[w].declared,
            already_configured=None,
            score=0.0,
            hidden_reason=reason,
            person_likely=rec.person,
            first_loc={"file_idx": rec.first[0], "start": rec.first[1],
                       "end": rec.first[2]} if rec.first else {},
        )

    for w, reason in pre_hidden.items():
        candidates.append(_hidden_cand(w, reason))
    for w, reason in post_hidden.items():
        candidates.append(_hidden_cand(w, reason))
    for w, reason in frag_hidden.items():
        candidates.append(_hidden_cand(w, reason))

    meta["phase_ms"]["score_person"] = round((time.perf_counter() - _t0) * 1000, 1)

    # score 降序，同分频次降序
    candidates.sort(key=lambda c: (-c.score, -c.freq))

    session = ScanSession(candidates=candidates, meta=meta)
    session._texts = texts
    session._file_names = names
    session._files_by_word = {w: tally[w].files for w in tally}
    return session


def _dn2_generic(w: str, norm: str, heads_by_lastchar: dict) -> bool:
    """DN2 双判据（多轮变体重试）：① 整词（≤8 字）被 G 完整切分；② 以部门
    头收尾且去头躯干被 G 完整切分（财务管理中心类）。变体序列=原串 → 剥
    STRIP_HEADS 头缀（至多两层）→ 剥功能字串（实测驱动：根据
    国家/负责本馆/由技术部/资产与物业管理部 类头缀与功能字粘连的部门词躯
    干）。头缀/功能字不是区分性成分，剥字不造出区分性——中共某市图书馆委
    员会剥中后躯干仍含 某市图书馆 专名成分 → 切分失败存活。"""
    variants: list = []
    cand = norm
    for _ in range(2):  # 头缀至多剥两层（根据+国家/负责+本馆类）
        stripped = cand
        for h in STRIP_HEADS:
            if stripped.startswith(h) and len(stripped) > len(h):
                stripped = stripped[len(h):]
                break
        if stripped != cand:
            variants.append(stripped)
            cand = stripped
        else:
            break
    core = "".join(ch for ch in norm if ch not in FUNC_CHARS)
    if core != norm and len(core) >= 2:
        variants.append(core)
    for cand in [norm] + variants:
        if (len(w) <= 8 and cand[-1] in _G_LASTCHARS
                and _org_suffix_full_cover(cand, GENERIC_VOCAB, GENERIC_VOCAB_MAXLEN)):
            return True
        head = _longest_tail(cand, heads_by_lastchar)
        if head:
            stem = cand[:len(cand) - len(head)]
            if not stem:
                return True  # 整词即部门头（办公室/党委会类）：无区分性成分 → 隐藏
            if stem[-1] in _G_LASTCHARS and _org_suffix_full_cover(
                    stem, GENERIC_VOCAB, GENERIC_VOCAB_MAXLEN):
                return True
    return False


# ---------------------------------------------------------------------------
# 通道 C 支件
# ---------------------------------------------------------------------------
def _nominate_declared(m: re.Match, text: str, nominate) -> bool:
    """R-C1：声明式简称提名。向前回溯最近全称串（≥4 字 CJK，书名号/括号外）；
    简称过功能字实体核闸后入候选（channel=declared，最高置信）。"""
    abbr = m.group(1)
    if not _func_core_ok(abbr):
        return False
    lookback = text[max(0, m.start() - 48):m.start()]
    stripped = _DECL_LOOKBACK_STRIP_RE.sub("", lookback)
    fm = _DECL_FULLNAME_RE.search(stripped)
    if fm is None:
        return False
    if fm.group(1) == abbr:
        return False
    nominate(abbr, "declared", declared=True)
    return True


def _eligible_fullnames(nominees, keep, cfg, texts, c2_file) -> list:
    """R-C2/R-C3 全称资格池：freq≥derive_min_freq 且 4≤len≤10（J4）。
    len≤ngram_max 直读级联计数（精确出现计数）；更长全称（后缀通道提名的
    法定全称形态，如 10 字公司名）经文档级 2-gram 预滤后 str.count 精确计数。"""
    out: list = []
    for w, nom in nominees.items():
        if not (4 <= len(w) <= 10):
            continue
        if len(w) <= cfg.ngram_max:
            if keep.get(len(w), {}).get(w, 0) >= cfg.derive_min_freq:
                out.append(w)
        elif "suffix" in nom.channels:
            bigrams = {w[i:i + 2] for i in range(len(w) - 1)}
            total = 0
            for fi, fc in enumerate(c2_file):
                if any(b not in fc for b in bigrams):
                    continue
                total += texts[fi].count(w)
            if total >= cfg.derive_min_freq:
                out.append(w)
    return out


def _channel_abbrev_r2(eligible, count2, cfg, nominate) -> None:
    """R-C2：全称保序 2 字组合假设 + 2-gram 索引验证（某图之路）。
    假设须过功能字实体核闸；词典常用词降权不硬拒（打分面处理）。"""
    seen: set = set()
    for full in eligible:
        for i in range(len(full)):
            for j in range(i + 1, len(full)):
                hyp = full[i] + full[j]
                if hyp in seen:
                    continue
                seen.add(hyp)
                if count2.get(hyp, 0) >= cfg.freq_min and _func_core_ok(hyp):
                    nominate(hyp, "abbrev")


def _channel_abbrev_r3(eligible, suffix_vocab, keep, count2, cfg, nominate) -> None:
    """R-C3：剥离式派生（法定后缀→行业尾词；v1.6.6 _derive_brand 先例）。
    派生串须非常用词/地名/黑名单，且回索引验证 count≥freq_min（R-C1 除外
    的独立计数证据约束）。"""
    suffix_eff = tuple(sorted(suffix_vocab, key=len, reverse=True))
    tails = tuple(sorted(_BRAND_GENERIC_TAILS, key=len, reverse=True))
    veto = (COMMON_WORDS | frozenset(NON_NAME_BLACKLIST) | _PLACE_NAMES
            | _PLACE_WORDS | _COMPANY_STEM_BLACKLIST)
    for full in eligible:
        base = None
        for suf in suffix_eff:
            if full.endswith(suf) and len(full) - len(suf) >= 2:
                base = full[:-len(suf)]
                break
        if base is None:
            continue
        for g in tails:
            if base.endswith(g) and len(base) - len(g) >= 2:
                base = base[:len(base) - len(g)]
                break
        if not (2 <= len(base) <= 4) or base in veto:
            continue
        cnt = count2.get(base, 0) if len(base) == 2 else keep.get(len(base), {}).get(base, 0)
        if cnt >= cfg.freq_min:
            nominate(base, "abbrev")


def _iter_posseg_blocks(words: list, pseg):
    """批量词性标注的逐块迭代器：\\n 分隔一次 cut，按分隔符切回与 words 等长
    的 [(token, flag), ...] 块（jieba 按非汉字块切句，结果与逐词 cut 相等）。"""
    if not words:
        return
    big = "\n".join(words)
    cur: list = []
    for t in pseg.cut(big):
        if t.word == "\n":
            yield cur
            cur = []
        else:
            cur.append((t.word, t.flag))
    yield cur


def _build_container_freq(words: list, tally: dict, mode: str = "sum") -> dict:
    """容器频索引：word → 含 w 的更长候选频次聚合（mode="sum" 求和 / "max" 取最大）。

    两遍可信 SUM 口径（实测驱动，调用方编排）：
    第一遍 MAX 全池预判——残串链嵌套容器（某市图书馆发布 ⊂ 某市图书馆
    发布服务）对同一批出现重复计数，直接 SUM 会过杀真词（实测回退判例）；
    第二遍只对「可信容器」求和——可信=被闸隐藏者（隐藏词的出现同样解释其
    内部残片，理办法 ⊂ 管理办法(G 隐藏)）/ 声明对 / MAX 预判自身非残片者。
    SUM 是「被解释次数」上界 → eff=freq−SUM 为独立出现数下界（隐藏面保守）。"""
    container: dict = {}
    for w2 in words:
        fw2 = tally[w2].freq
        L = len(w2)
        if fw2 <= 0 or L < 3:
            continue
        for i in range(L - 1):
            for j in range(i + 2, L + 1):
                sub = w2[i:j]
                if sub == w2:
                    continue
                if mode == "max":
                    cur = container.get(sub)
                    if cur is None or fw2 > cur:
                        container[sub] = fw2
                else:
                    container[sub] = container.get(sub, 0) + fw2
    return container


def _g_cover(s: str) -> bool:
    """G 表完整覆盖判定（DN8 粘连变体剩余段与短语豁免的共用谓词）。"""
    return (len(s) >= 2 and s[-1] in _G_LASTCHARS
            and _org_suffix_full_cover(s, GENERIC_VOCAB, GENERIC_VOCAB_MAXLEN))


def _dup2gram(s: str) -> bool:
    """乱串判定（2026-09-16 碎词治理批）：串内任一 2-gram 出现 ≥3 次——
    「程管控目标风险与影响管控措施管控部」（管控×3）类 suffix 通道错切乱串。
    真实体名含同段三重复的形态罕见（实测判例词零命中）。"""
    if len(s) < 6:
        return False
    seen: dict = {}
    for i in range(len(s) - 1):
        g2 = s[i:i + 2]
        seen[g2] = seen.get(g2, 0) + 1
        if seen[g2] >= 3:
            return True
    return False


def _propagate_person(stage3: list, tally: dict) -> int:
    """人名词的内部子串同标 person 组，返回新标记者数（残片防逃逸）。"""
    stage3_set = set(stage3)
    extra: set = set()
    for pw in (w for w in stage3 if tally[w].person):
        L = len(pw)
        for i in range(L - 1):
            for j in range(i + 2, L + 1):
                sub = pw[i:j]
                # 传播守则（实测案例「示例甲家」nr 误标污染真品牌）：
                # 只向自身符合 person 准入画像（≤4 字且 files≤2）的子串扩散，
                # 独立存在面更广的词（真品牌/真机构）不被残片反向污染。
                if (sub in stage3_set and not tally[sub].person
                        and len(sub) <= 4 and len(tally[sub].files) <= 2):
                    extra.add(sub)
    for w in extra:
        tally[w].person = True
    return len(extra)


def _title_context_hit(w: str, rec, texts, kw_sorted, title_sorted) -> bool:
    """职务上下文信号：前贴 _KEYWORD_BEFORE / 后贴 _TITLE_AFTER*（复用
    rules_engine 人名上下文表；抽样前 3 份文件的前 2 处出现）。"""
    for fi in rec.files[:3]:
        text = texts[fi]
        start = 0
        for _ in range(2):
            i = text.find(w, start)
            if i < 0:
                break
            s, e = i, i + len(w)
            prev = text[max(0, s - 10):s].rstrip("：:，, 　\t\n\r")
            if any(prev.endswith(kw) for kw in kw_sorted):
                return True
            nxt = text[e:e + 4]
            if any(nxt.startswith(t) for t in title_sorted) \
                    or (nxt[:1] and nxt[:1] in _TITLE_AFTER1):
                return True
            start = i + 1
    return False
