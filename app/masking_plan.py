# -*- coding: utf-8 -*-
"""首轮检测冻结 plan（v1.9.4 方案二，评审 R3 决策落地）。

背景：v1.9.0 分析缓存只复用 OCR 原始输出，复核轮仍要把全部文本重新跑一遍
检测与映射键匹配——映射键全局化正是膨胀三源之首（R1 实测 97%）。本模块把
**首轮的最终遮蔽指令集**（而非 OCR 中间产物）冻结为旁路工件 plan：复核轮
（映射对照表覆盖）头部四键全匹配时零 OCR 零检测，按 plan 指令重放，编辑
（删除/改值/增行）只作用于指令集本身。

与 analysis_cache 的关系（契约 §1/蓝图 §2.4）：
- 文件旁路：`<输出根>/cache/plans/<源文件内容sha256>.json`，与 OCR 缓存
  `<输出根>/cache/<hash>.json` 同根不同目录、头部各自校验、生命周期独立；
- 头部四键 = plan_schema_version / app_version / src_content_hash /
  brands_hash（**不含 engine_name**——plan 冻结的是检测结果，换 OCR 档位
  不作废）；任一不符 → 整文件 miss（调用方回落 v1.9.3 全流程并重建）；
- analysis_cache.py 零改动（仅复用其 content_hash / brands_config_hash /
  resolve_output_root 三个纯函数）。

指令四形态（契约 §1，pdf_handler 16 处 record 的归并）：
- native_spans：原生文本页 span（区间+规则+替换+置信度+原文），重放时复算
  字符矩形，绘制与首轮共用 _apply_native_spans；
- ops[].t=="jspan"：OCR 拼接文本区间（含金额标签兜底），重放喂与首轮同一个
  行拆分绘制函数 _draw_spans_on_lines；
- ops[].t=="rect"：直画成品矩形（横幅/复检/印章/凭证/列锚/金额列），按首轮
  应用序逐条重放（kind: draw|redact_draw|stamp）；
- filename：首轮文件名替换对快照（编辑幸存过滤后回放）。

敏感级：plan 含敏感原文（native text / OCR items / 指令原文），与映射表同级，
不入 git（.gitignore 覆盖 cache/ 路径族）；「清除分析缓存」连带清除（契约
设计决策 #4）。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .utils import APP_VERSION

PLAN_SCHEMA_VERSION = 1
_PLAN_DIR_PARTS = ("cache", "plans")   # plan 目录：挂在输出根下（旁路 OCR 缓存文件体）


def _atomic_write_json(path: Path, data: dict) -> None:
    """JSON 原子写（tmp + os.replace，与 analysis_cache.save 同纪律）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(path))


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def plan_dir_for(output_root) -> Path:
    """plan 目录：`<输出根>/cache/plans/`。"""
    p = Path(output_root)
    for part in _PLAN_DIR_PARTS:
        p = p / part
    return p


def output_root(src, options) -> str:
    """输出根解析（analysis_cache.resolve_output_root 同款，口径一致）。"""
    from .analysis_cache import resolve_output_root
    return resolve_output_root(src, options)


def plan_path_for(src, output_root) -> Path:
    """plan 文件路径：`<输出根>/cache/plans/<源文件内容sha256>.json`。

    与 analysis_cache.cache_path_for 同键（content_hash）不同目录——两缓存
    文件身份一致、头部与生命周期各自独立（蓝图 §2.4）。
    """
    from .analysis_cache import content_hash
    return plan_dir_for(output_root) / f"{content_hash(src)}.json"


# ---------------------------------------------------------------------------
# plan 模型（JSON 同构 dict 的薄包装，避免序列化漂移）
# ---------------------------------------------------------------------------

class MaskingPlan:
    """一份首轮冻结 plan。data 结构见模块 docstring 与 _new_page。"""

    def __init__(self, data: dict):
        self.data = data
        self.dirty = False  # 重放轮逐页回落修复后置 True（落盘重建基线）

    def save(self, src=None, output_root=None) -> Path:
        """原子落盘（重放轮逐页回落修复后重建冻结基线用）。

        src_content_hash 每次重算（源文件可能已被替换）；其余头部保持首轮
        装载值（app_version/brands_hash 不符本就不会进入重放）。
        """
        from .analysis_cache import content_hash
        if src is not None:
            self.data["header"]["src_content_hash"] = content_hash(src)
        path = plan_path_for(src, output_root)
        _atomic_write_json(path, self.data)
        return path

    # ---- 只读视图 --------------------------------------------------------

    @property
    def header(self) -> dict:
        return self.data.get("header") or {}

    @property
    def filename(self) -> dict:
        return self.data.get("filename") or {}

    @property
    def pages(self) -> list:
        return self.data.get("pages") or []

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def table_rows(self) -> list:
        """本文件对首轮汇总表的贡献行 [原文, 替换为, 规则]（amount 公式行除外
        ——与用户在界面上看到的表同域，编辑 diff 以此为基准）。"""
        return self.data.get("table_rows") or []

    def page_by_pno(self, pno: int):
        for pg in self.pages:
            if pg.get("pno") == pno:
                return pg
        return None

    def texts_joined(self) -> str:
        """v2.1.7 B3-3：批内文本复用面（重放轮——plan 自包含文本，纯读）。"""
        return join_page_texts(self.pages)

    # ---- 重放轮写入 ------------------------------------------------------

    def replace_page(self, pno: int, page_data: dict) -> None:
        """逐页回落实时检测后修 plan 该页（蓝图 §3.1.1；结果成为新冻结基线）。"""
        pages = self.data.setdefault("pages", [])
        for i, pg in enumerate(pages):
            if pg.get("pno") == pno:
                pages[i] = page_data
                break
        else:
            pages.append(page_data)
            pages.sort(key=lambda pg: pg.get("pno", 0))
        self.dirty = True

    # ---- 置信度回注 ------------------------------------------------------

    def register_confidences(self, engine) -> int:
        """把 plan 携带的置信度回注引擎（汇总表置信度列保持，契约 §8）。

        冻结轮无检测，engine._person_conf/_company_conf 恒空；不回注则复核轮
        汇总表置信度列整体丢失（dispatcher 从引擎表取值）。返回回注条数。
        """
        n = 0
        for pg in self.pages:
            for s in pg.get("native_spans") or []:
                # [start, end, rule, replacement, confidence, text]
                if s[4]:
                    engine.register_confidence(s[2], s[5], s[4])
                    n += 1
            for op in pg.get("ops") or []:
                conf = op.get("conf")
                if conf:
                    engine.register_confidence(op.get("rule", ""), op.get("orig", ""), conf)
                    n += 1
        return n


def join_page_texts(pages) -> str:
    """v2.1.7 B3-3：批内文本复用面——按序拼接 原生 text + OCR 行文本。

    纯读旁路（入口①后台候选重扫复用批内文本——扫描页的 OCR 成果不再
    丢弃）；pages=可迭代页 dict（text/ocr.items 键）。空页跳过。
    """
    parts = []
    for pg in pages:
        t = (pg.get("text") or "").strip()
        node = pg.get("ocr") or {}
        lines = [r[0] for r in (node.get("items") or []) if r and r[0]]
        seg = "\n".join([t] + lines)
        if seg.strip():
            parts.append(seg)
    return "\n".join(parts)


def _new_page(pno: int, mode: str = "native") -> dict:
    return {"pno": int(pno), "mode": mode, "hits": 0, "low_conf": 0,
            "raster": False, "native_spans": [], "ops": []}


# ---------------------------------------------------------------------------
# 首轮采集器（pdf_handler 各通道装配点埋点写入）
# ---------------------------------------------------------------------------

class PlanBuilder:
    """首轮遮蔽指令采集器：按各通道「数据装配点」逐条写入（蓝图 §2.2）。

    report.record 签名不动；埋点与 record 同分支，保证「进了映射表的就是
    进了 plan 的」（单一事实源）。全部方法 best-effort 契约由调用方保证
    （plan 落盘失败仅留痕，不影响当轮输出）。
    """

    def __init__(self, src, options):
        self._src = src
        self._options = options
        from .analysis_cache import brands_config_hash
        self.data = {
            "header": {
                "plan_schema_version": PLAN_SCHEMA_VERSION,
                "app_version": str(APP_VERSION),
                "src_content_hash": "",     # save() 时补算（与大文件流式哈希一次）
                "src_name": Path(src).name,
                "brands_hash": brands_config_hash(),
                "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "page_count": 0,
                # v2.1.8 仅指定词模式：头部模式维度（load_plan 同键强校验，
                # 防跨模式串台重放；旧 plan 无该键按 False 对齐默认路径）
                "words_only": bool(getattr(options, "words_only", False)),
                # 仅记录首轮口径，重放不读（选项漂移策略见蓝图 §4.2）。
                # v2.0.3 F5：first_round_options 由 5 键扩为 MaskOptions 全部
                # 类别开关（16 标量键）——D3 取证实证：现场 plan 不记录
                # person 字段致「人名开关盘上不可证」，台账/审计要求开关态
                # 可追溯。向后兼容：本键族为纯附加记录，load_plan 头部强校验
                # 只看四键、重放读取点（pdf_handler 选项漂移提示）按
                # `key in fropts` 容忍缺失 → 旧 plan（无新键）装载/重放均
                # 不受影响；新增键不参与 plan 缓存键（路径=src_content_hash，
                # 头部校验=四键）→ 存量 plan 零失效。内容/路径类字段
                # （custom_words/whitelist/mapping_csv/output_dir）非类别开关
                # 且含敏感清单，不进快照（其检测语义已冻结进指令本身）。
                "first_round_options": {
                    # ---- 旧 5 键（v2.0.1 起既有，键名/语义原样保留）----
                    "mask_amount": bool(getattr(options, "mask_amount", False)),
                    "mask_stamp": bool(getattr(options, "mask_stamp", False)),
                    "deep_recheck": bool(getattr(options, "deep_recheck", True)),
                    "ocr_image_text": bool(getattr(options, "ocr_image_text", False)),
                    "ocr_min_confidence": float(getattr(options, "ocr_min_confidence", 0.6)),
                    # ---- v2.0.3 F5 新增：全量类别开关与行为标量 ----
                    "mask_company": bool(getattr(options, "mask_company", True)),
                    "mask_person": bool(getattr(options, "mask_person", True)),
                    "mask_contact": bool(getattr(options, "mask_contact", True)),
                    "mask_address": bool(getattr(options, "mask_address", True)),
                    "person_loose": bool(getattr(options, "person_loose", True)),
                    "amount_mode": str(getattr(options, "amount_mode", "mask")),
                    "amount_partial": bool(getattr(options, "amount_partial", False)),
                    "amount_threshold": float(getattr(options, "amount_threshold", 0.0)),
                    "keep_excel_formula": bool(getattr(options, "keep_excel_formula", True)),
                    "include_institutions": bool(getattr(options, "include_institutions", False)),
                    "xls_to_xlsx": bool(getattr(options, "xls_to_xlsx", False)),
                    # v2.1.8 仅指定词模式：plan 头部模式维度（防跨模式串台，
                    # load_plan 同键强校验）
                    "words_only": bool(getattr(options, "words_only", False)),
                },
            },
            "filename": {"src_stem": Path(src).stem, "pairs": []},
            "table_rows": [],
            "pages": [],
        }
        self._pages: dict = {}

    # ---- 页级元数据 ------------------------------------------------------

    def _page(self, pno: int) -> dict:
        pg = self._pages.get(pno)
        if pg is None:
            pg = self._pages[pno] = _new_page(pno)
        return pg

    def set_page(self, pno: int, mode: str, hits: int, low_conf: int) -> None:
        pg = self._page(pno)
        pg["mode"] = mode
        pg["hits"] = int(hits)
        pg["low_conf"] = int(low_conf)

    def mark_raster(self, pno: int) -> None:
        self._page(pno)["raster"] = True

    # ---- 通道指令 --------------------------------------------------------

    def set_native_text(self, pno: int, text: str) -> None:
        """原生页整页文本（重放校验 + 增行定位双重身份）。"""
        self._page(pno)["text"] = text

    def add_native_spans(self, pno: int, spans) -> None:
        """原生页 span 批（须与实际绘制集合一致，调用点在绘制之后）。

        spans：(start, end, rule, replacement, confidence, text) 元组序列。
        """
        pg = self._page(pno)
        for s, e, rule, repl, conf, text in spans:
            pg["native_spans"].append([int(s), int(e), str(rule), str(repl),
                                       str(conf or ""), str(text)])

    def ensure_ocr(self, pno: int) -> dict:
        pg = self._page(pno)
        if "ocr" not in pg:
            pg["ocr"] = {"items": [], "low_conf": 0}
        return pg["ocr"]

    def set_ocr_items(self, pno: int, items) -> None:
        """OCR good items 快照（text/bbox/置信度；bbox 统一为页坐标）。

        items：(text, (x0, y0, x1, y1), confidence) 序列，阅读序。重放零 OCR
        的行来源（蓝图 D1：items 自包含，不回引 OCR 缓存）。
        """
        node = self.ensure_ocr(pno)
        node["items"] = [[str(t), float(b[0]), float(b[1]), float(b[2]), float(b[3]),
                          float(c)] for (t, b, c) in items]

    def page_texts_joined(self) -> str:
        """v2.1.7 B3-3：批内文本复用面（首轮——builder 页存储纯读拼接）。"""
        return join_page_texts(self._pages[p] for p in sorted(self._pages))

    def add_op_rect(self, pno: int, kind: str, rect, repl: str, rec: str,
                    orig: str, rule: str, loc: str, conf: str = "") -> None:
        """直画成品矩形（横幅/复检/印章/凭证/列锚/金额列）。

        kind：draw=仅画替换块；redact_draw=先 redaction 销毁底层文字再画
        （文本层直画通道）；stamp=redaction+纯黑块。repl=绘制文本；
        rec=映射表记录替换文本（凭证/印章行两者不同，如实分记）。
        """
        self._page(pno)["ops"].append({
            "t": "rect", "k": kind,
            "rect": [float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3])],
            "repl": str(repl), "rec": str(rec), "orig": str(orig),
            "rule": str(rule), "loc": str(loc), "conf": str(conf or ""),
        })

    def add_op_jspan(self, pno: int, s: int, e: int, rule: str, repl: str,
                     conf: str, orig: str, tag: str, preset: str = "m") -> None:
        """OCR 拼接文本区间指令（含金额标签兜底）。

        preset：m=实体命中绘制参数（外扩 12%+6pt/±3pt、≥60% 整行）；
        a=金额兜底参数（12%+2pt/±1.5pt、无整行分支）——与首轮逐值一致。
        """
        self._page(pno)["ops"].append({
            "t": "jspan", "s": int(s), "e": int(e), "rule": str(rule),
            "repl": str(repl), "conf": str(conf or ""), "orig": str(orig),
            "tag": str(tag), "p": preset,
        })

    # ---- 文件级快照 ------------------------------------------------------

    def snapshot_filename(self, pairs) -> None:
        """首轮文件名替换对快照（engine.filename_replacement_pairs()）。"""
        self.data["filename"]["pairs"] = [[str(o), str(r)] for o, r in pairs]

    def snapshot_table_rows(self, records) -> None:
        """本文件对汇总表的贡献行快照（编辑 diff 基准）。

        records：(原文, 替换为, 规则, 位置)。amount 规则行不入汇总表
        （dispatcher 过滤口径），同步剔除保证 diff 域=用户看到的表。
        """
        seen = set()
        rows = []
        for orig, repl, rule, _loc in records:
            if rule == "amount" or not orig:
                continue
            key = (orig, repl, rule)
            if key in seen:
                continue
            seen.add(key)
            rows.append([str(orig), str(repl), str(rule)])
        self.data["table_rows"] = rows

    # ---- 装配与落盘 ------------------------------------------------------

    def export_page(self, pno: int) -> dict:
        """导出单页数据（重放轮逐页回落修复用）。"""
        import copy
        return copy.deepcopy(self._page(pno))

    def build(self) -> dict:
        self.data["pages"] = [self._pages[p] for p in sorted(self._pages)]
        self.data["header"]["page_count"] = len(self.data["pages"])
        return self.data

    def save(self, output_root) -> Path:
        """原子落盘（tmp + os.replace，与 analysis_cache.save 同纪律）。"""
        from .analysis_cache import content_hash
        data = self.build()
        data["header"]["src_content_hash"] = content_hash(self._src)
        path = plan_path_for(self._src, output_root)
        _atomic_write_json(path, data)
        return path


# ---------------------------------------------------------------------------
# 装载与头部校验（复核轮入口；任一不符 → miss + 人话原因）
# ---------------------------------------------------------------------------

def load_plan(src, options, app_version: str = None):
    """装载 plan 并做头部四键强校验。返回 (MaskingPlan|None, miss_reason)。

    四键：plan_schema_version / app_version / src_content_hash / brands_hash
    （契约 §1：不含 engine_name——plan 冻结的是检测结果，换 OCR 档位不作废）。
    """
    from .analysis_cache import brands_config_hash, content_hash, resolve_output_root
    app_version = str(app_version or APP_VERSION)
    path = plan_path_for(src, resolve_output_root(src, options))
    if not path.exists():
        return None, "plan 不存在（首次处理该文件或已被清除）"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"plan 文件损坏不可读（{type(e).__name__}）"
    if not isinstance(data, dict):
        return None, "plan 文件结构异常（非对象）"
    header = data.get("header")
    if not isinstance(header, dict):
        return None, "plan 头部缺失"
    expect = {
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "app_version": app_version,
        "src_content_hash": content_hash(src),
        "brands_hash": brands_config_hash(),
    }
    for field, want in expect.items():
        got = header.get(field)
        if got != want:
            return None, (f"头部不匹配：{field} 不一致"
                          f"（{got!r} != {want!r}）")
    # v2.1.8 P0：模式维度强校验——仅指定词 plan 与全量 plan 不得互相重放
    # （旧 plan 无该键按 False 对齐默认路径，存量缓存零失效；仅跨模式拒绝）。
    wo_cur = bool(getattr(options, "words_only", False))
    wo_plan = bool(header.get("words_only", False))
    if wo_cur != wo_plan:
        return None, (f"头部不匹配：words_only 模式不一致"
                      f"（plan={wo_plan!r} != 当前={wo_cur!r}，跨模式不得重放）")
    pages = data.get("pages")
    if not isinstance(pages, list) or not pages:
        return None, "plan 页表结构异常"
    return MaskingPlan(data), ""


# ---------------------------------------------------------------------------
# 编辑算子（契约 §3：删除=按原文全局、改值=按原文全局、增行=防呆闸+文本定位）
# ---------------------------------------------------------------------------

def add_key_allowed(key: str, engine=None) -> tuple:
    """增行键防呆闸（设计决策 #3）。返回 (是否放行, 拒绝原因)。

    四道闸（任一命中即拒）：
    1. strip 后长度 < 2（堵「关」类单字键）；
    2. 归一化键（NFKC+剥括号引号空白，复用 _norm_org_suffix_text 口径）命中
       _BRAND_STOP_ALL ∪ 组织后缀生效表（G1：engine 存在时取实例生效表
       _org_suffix_stop_tokens = _BRAND_STOP_ALL ∪ 生效主干∪可选∪用户增，
       含用户增词、不含用户删词——否则出现「用户删了 X 却仍不能注册键 X」
       的语义分裂；engine 为 None 回退模块级
       _BRAND_STOP_ALL ∪ _ORG_SUFFIX_TOKENS ∪ _ORG_SUFFIX_TOKENS_OPTIONAL）
       （堵「公司/集团/店/局」类 220 处事故源）；
    3. is_pure_org_suffix 纯度判定为真（同 G1：engine 感知实例生效表，
       None 回退模块级）；
    4. jieba 词典频次 ≥20 且词性非 nr（堵「条款/应链」类两字碎片，与映射
       派生品牌闸同源同参；jieba 异常时该子闸放行不阻塞）。
    """
    # 稳定体验补项：字符串包名硬导入改相对导入——
    # 与本模块其余函数同源（打包器重定位/单测导入形态下零风险）
    from . import rules_engine as rules
    k = (key or "").strip()
    if len(k) < 2:
        return False, "长度不足 2 个字"
    norm = rules._norm_org_suffix_text(k)
    stop = getattr(engine, "_org_suffix_stop_tokens", None)
    if stop is None:
        stop = (rules._BRAND_STOP_ALL | rules._ORG_SUFFIX_TOKENS
                | rules._ORG_SUFFIX_TOKENS_OPTIONAL)
    if norm in stop or k in stop:
        return False, "命中通用词/组织后缀停用表"
    try:
        pure_fn = getattr(engine, "is_pure_org_suffix", None)
        if pure_fn is None:
            pure_fn = rules.is_pure_org_suffix
        if pure_fn(k)[0]:
            return False, "纯法定组织形式后缀组合"
    except Exception:
        pass
    # 频次闸（子闸异常放行，设计决策 #3）
    try:
        jieba_mod = None
        getter = getattr(engine, "_get_jieba", None)
        if callable(getter):
            try:
                jieba_mod = getter()
            except Exception:
                jieba_mod = None
        if jieba_mod is None:
            import jieba as jieba_mod
        jieba_mod.initialize()
        freq = jieba_mod.dt.FREQ.get(norm) or jieba_mod.dt.FREQ.get(k) or 0
        if freq >= 20:
            from jieba import posseg
            tag = posseg.dt.word_tag_tab.get(norm) or posseg.dt.word_tag_tab.get(k)
            if tag != "nr":
                return False, f"jieba 常用词典高频词（频次 {freq}）"
    except Exception:
        pass
    return True, ""


def apply_plan_edits(plan: MaskingPlan, deletes, changes, report_log=None) -> dict:
    """把编辑算子作用到 plan 指令集（原地修改；重放前调用）。

    - 删除（契约 D4）：剔除 original（用户可见原文）== 删除键的**一切指令**
      （native/jspan/rect 全通道，含印章/凭证/横幅）与文件名替换对；
    - 改值：按原文全局更新替换文本（native/jspan 更新绘制+记录替换文本；
      rect 仅更新记录替换文本——凭证/印章行的绘制语义是整图遮/黑块，改值
      只改表内呈现）。

    增行不在本函数（plan 恒为首轮基线，增行指令每次现算、不入 plan）。
    返回统计 {"deleted": [...], "changed": [...]}。
    """
    deletes = set(deletes or ())
    changes = dict(changes or {})
    deleted_seen = set()
    changed_seen = set()

    def _drop(orig: str) -> bool:
        if orig in deletes:
            deleted_seen.add(orig)
            return True
        return False

    def _reval(orig: str, repl: str) -> str:
        if orig in changes and changes[orig] != repl:
            changed_seen.add(orig)
            return changes[orig]
        return repl

    for pg in plan.pages:
        kept_spans = []
        for s in pg.get("native_spans") or []:
            # [start, end, rule, replacement, confidence, text]
            if _drop(s[5]):
                continue
            s[3] = _reval(s[5], s[3])
            kept_spans.append(s)
        if len(kept_spans) != len(pg.get("native_spans") or []):
            pg["native_spans"] = kept_spans
        kept_ops = []
        for op in pg.get("ops") or []:
            if _drop(op.get("orig", "")):
                continue
            if op.get("t") == "jspan":
                op["repl"] = _reval(op.get("orig", ""), op.get("repl", ""))
            else:  # rect：仅更新记录替换文本（绘制语义不变）
                op["rec"] = _reval(op.get("orig", ""), op.get("rec", ""))
            kept_ops.append(op)
        if len(kept_ops) != len(pg.get("ops") or []):
            pg["ops"] = kept_ops

    fn = plan.data.get("filename") or {}
    if fn.get("pairs"):
        kept_pairs = []
        for o, r in fn["pairs"]:
            if _drop(o):
                continue
            kept_pairs.append([o, _reval(o, r)])
        fn["pairs"] = kept_pairs

    # table_rows 是编辑 diff 基准（首轮快照），保持原样不随编辑改动——
    # 重放轮汇总表由重放 record 自然重组（被删行无指令即无记录）。
    if (deleted_seen or changed_seen) and callable(report_log):
        try:
            if deleted_seen:
                report_log("冻结复核：已按原文全局删除 "
                           f"{len(deleted_seen)} 项（含印章/凭证/横幅通道）："
                           + "、".join(sorted(deleted_seen)))
            if changed_seen:
                report_log(f"冻结复核：已按原文全局改值 {len(changed_seen)} 项")
        except Exception:
            pass
    plan.dirty = plan.dirty or bool(deleted_seen or changed_seen)
    return {"deleted": sorted(deleted_seen), "changed": sorted(changed_seen)}


def find_add_occurrences(text: str, key: str, occupied) -> list:
    """增行键在页文本中的全部出现区间（契约 §3 增行算子定位步）。

    与 occupied（已幸存指令的 (start,end) 区间序列）任一重叠的出现点跳过
    （蓝图 §3.2 约束①：不与既有遮蔽叠加）。调用方按长键优先序传入多个键。
    """
    out = []
    if not key:
        return out
    start = 0
    n = len(text)
    while True:
        i = text.find(key, start)
        if i < 0:
            break
        start = i + 1
        s, e = i, i + len(key)
        if e > n:
            break
        if any(s < oe and os_ < e for os_, oe in occupied):
            continue
        out.append((s, e))
    return out


# ---------------------------------------------------------------------------
# 重放统计/次序口径常量（v1.9.4 M4 修复：重放与首轮逐通道对齐）
# ---------------------------------------------------------------------------

# 首轮 masked 不计入的通道（v1.9.3 既有口径：横幅补扫 `_banner_recheck` 的
# 返回值在调用点被弃用、视觉复检 `_visual_recheck` 的返回值仅作 leaked 清单，
# 两通道只记映射表不进 hits）——重放统计同规则，杜绝「重放按 op 计数」的
# 假膨胀（V-C 实测 masked 1137→1211，+74=banner 63+visual 11；绘制像素不受
# 影响）。按规则映射计数表实现，与首轮计数点同源同表、不依赖 plan 新字段
# （存量 plan 零失效）。
PLAN_UNCOUNTED_RULES = frozenset({"banner", "visual_recheck"})

# 首轮在光栅化阶段（全部页处理完之后，process() 尾部
# `for pno in raster_targets: _band_recheck(...)`）才产生指令与映射记录的
# 通道——其指令存于页 ops 尾部，重放若随页内 ops 应用，该通道的映射行会
# 插到后续页的行之前，行序对不上首轮（V-C 实测 1 行次序位移：
# 计中心|band_recheck|家具家品）。重放必须同样推迟到光栅化阶段按页序应用。
PLAN_RASTER_STAGE_RULES = frozenset({"band_recheck"})


def merge_filename_pairs(plan_pairs) -> list:
    """复核轮文件名替换对：首轮快照（已经删改过滤）——**不再并入当轮引擎键**。

    语义（v1.9.4 M4 缺陷 1 修复）：文件名替换对 = 首轮 plan 快照中的
    filename 规则对 ⊕ 编辑表中针对这些同名字行的改值/删除——后者已由
    apply_plan_edits 烤入快照（删除剔对、改值更新值），本函数即「快照原样
    按原文长度降序返回」。增行/无关编辑行不进文件名对：覆盖轮引擎
    `_mapping`=整张编辑表（183 键），若把引擎派生对并入，碎片键
    （「限公司→×××」等）会命中「××有限公司」类文件名造成错误改名
    （V-C 实测 18/39 文件改名、映射 md5 漂移）；首轮 auto 轮引擎注册表即
    快照唯一来源，重放轮零检测、不存在合法的「引擎新键」。
    """
    merged: dict = {}
    for o, r in plan_pairs or []:
        merged[o] = r
    return sorted(merged.items(), key=lambda pr: len(pr[0]), reverse=True)
