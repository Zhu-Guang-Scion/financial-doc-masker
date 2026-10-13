# -*- coding: utf-8 -*-
"""业务调度层：批量任务编排。

职责：
- 按扩展名把文件分流到 app.handlers 中的具体处理器；
- 估算任务权重（PDF 按页数、其余按文件粒度），动态计算总进度与剩余时间；
- 单文件失败不中断批量任务，错误记录到日志，最后统一汇总；
- 为每个脱敏文件生成映射表 CSV（原文/替换为/规则/位置），支持事后回溯；
- 通过回调 on_log / on_progress 与界面层解耦（线程切换由界面层负责）。
"""
from __future__ import annotations

import csv
import os
import sys
import time
from pathlib import Path

from .handlers import HANDLER_REGISTRY
from .rules_engine import MaskOptions, RuleEngine
from .utils import MissingDependencyError

SUPPORTED_EXTS = {".docx", ".xlsx", ".xls", ".pptx", ".pdf", ".md"}  # v2.1.8：+Markdown


class _FileReport:
    """单文件处理器使用的回调包装：日志缩进、进度按权重折算到全局、收集映射记录。"""

    def __init__(self, fname: str, on_log, on_progress, base_units: float, weight: float, total_units: float):
        self.fname = fname
        self._on_log = on_log
        self._on_progress = on_progress
        self._base = base_units
        self._weight = weight
        self._total = total_units
        self.records: list = []  # (原文, 替换为, 规则, 位置)
        # v2.0.1 C1 遮蔽台账：op 级全量采集（不去重、不丢空原文行）。
        # records 供主映射表（口径零变化），op_records 供台账，record() 双写。
        self.op_records: list = []
        # v2.0.1 C1：复核轮编辑保存点重写行（pdf_handler._replay_from_plan
        # 注入 [(位置, 规则, 原文, 替换, op_id, 状态)]）；None=非编辑轮。
        self.ledger_rows_override = None
        # v2.0.1 C1：冻结重放轮旗标（pdf_handler 置位）——dispatcher 据此
        # 区分「零编辑重放（台账沿用首轮不重写）」与「首轮/回落重建轮」。
        self.ledger_replay_round = False
        # v2.0.1 C1：覆盖轮该文件 plan 未命中回落全量重检（台账等同首轮重建）。
        self.ledger_rebuilt = False

    def log(self, msg: str) -> None:
        self._on_log("  " + msg)

    def progress(self, done, total) -> None:
        frac = 0.0 if not total else max(0.0, min(1.0, float(done) / float(total)))
        self._on_progress(self._base + self._weight * frac, self._total)

    def record(self, original: str, replacement: str, rule: str, location: str) -> None:
        # v2.0.1 C1：台账先收 op 级全量（不去重；空原文也入账——「做得到的
        # 通道必须留痕」，杜绝只画不记复辟）；主映射表维持原 if original
        # 过滤，口径零变化。
        self.op_records.append((original, replacement, rule, location))
        if original:
            self.records.append((original, replacement, rule, location))


def merge_mapping_rows(per_file_rows: list) -> list:
    """按（原文,替换为,规则,文件）合并映射记录：同一替换项只显示一行。

    输入：(原文, 替换为, 规则, 文件, 位置) 列表；
    输出：(原文, 替换为, 规则, 文件, 次数, 位置汇总) 列表。
    位置汇总最多展示前 5 处，更多以「等共N处」收尾。
    """
    merged: dict = {}
    order: list = []
    for orig, repl, rule, fname, loc in per_file_rows:
        key = (orig, repl, rule, fname)
        if key not in merged:
            merged[key] = {"count": 0, "locs": []}
            order.append(key)
        item = merged[key]
        item["count"] += 1
        if loc not in item["locs"]:
            item["locs"].append(loc)
    rows = []
    for key in order:
        orig, repl, rule, fname = key
        item = merged[key]
        n = item["count"]
        locs = item["locs"]
        if len(locs) > 5:
            loc_str = "；".join(locs[:5]) + f" 等共{n}处"
        else:
            loc_str = "；".join(locs)
        rows.append((orig, repl, rule, fname, n, loc_str))
    return rows


def write_mapping_csv(out_dir: Path, rows: list) -> Path:
    """在 out_dir 目录下生成汇总映射表 CSV（文件名带任务时间戳，精确到分）。

    列：原文,替换为,规则,文件,次数,位置,置信度。前两列与界面“映射对照表覆盖”的
    输入格式兼容：编辑“替换为”列（或删除行）后，可将该 CSV 作为对照表
    重新运行，实现按自定义内容的批量再生。置信度列仅人名规则填写（v1.3），
    追加在末尾以保持向后兼容。
    """
    stamp = time.strftime("%Y%m%d_%H%M")
    mapping_path = Path(out_dir) / f"映射表_汇总_{stamp}.csv"
    with open(mapping_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["原文", "替换为", "规则", "文件", "次数", "位置", "置信度"])
        for row in rows:
            writer.writerow(row)
    return mapping_path


# ---------------------------------------------------------------------------
# v2.0.1 C1 遮蔽台账（红线一可追溯性落地）：op 级全量记录，任何遮蔽必可查。
# 列定义契约冻结（v201_contract.md C1）：文件/页/通道/原文/替换/op_id/状态；
# op_id=文件内 1-based 稳定序号（首轮烧入规范序，plan 重放复用首轮序）。
# ---------------------------------------------------------------------------

LEDGER_HEADER = ["文件", "页", "通道", "原文", "替换", "op_id", "状态"]
LEDGER_ACTIVE = "active"
LEDGER_EDITED = "edited"
LEDGER_DELETED = "deleted"


def write_ledger_csv(out_dir, rows: list) -> Path:
    """在 out_dir 生成 op 级遮蔽台账 CSV（与映射表同目录、同 utf-8-sig 编码）。

    与主映射表的分工：映射表按（原文,替换为,规则,文件）合并去重且剥除
    amount 家族（v2 口径零变化）；台账逐 op 全量不去重，全部通道无一例外
    （含 amount 家族/banner/visual/band/invoice 等）——「做不到记录
    的通道禁止画块」由此成立。含敏感原文，与映射表同规格保管（入库/外发
    禁区），UI「清除分析缓存」联动清除。
    """
    stamp = time.strftime("%Y%m%d_%H%M")
    ledger_path = Path(out_dir) / f"遮蔽台账_{stamp}.csv"
    with open(ledger_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(LEDGER_HEADER)
        for row in rows:
            writer.writerow(row)
    return ledger_path


def build_ledger_rows(op_records: list, fname: str, start_id: int = 1) -> list:
    """op 记录 → 台账行（首轮/回落重建口径）：状态全 active，op_id 连续编号。

    op_records：(原文, 替换, 规则, 位置) 全量序列（_FileReport.record 双写
    采集）。「仅原位」行（防呆拒绝键——设计决策 #2 不产生自动遮蔽指令）
    非画块，不入台账，保「画块总数=台账行数」对账恒等；主映射表仍收（原
    口径零变化）。
    """
    rows = []
    op_id = start_id
    for orig, repl, rule, loc in op_records:
        if loc == "仅原位":
            continue
        rows.append([fname, loc, rule, orig, repl, op_id, LEDGER_ACTIVE])
        op_id += 1
    return rows


# ---------------------------------------------------------------------------
# v2.1.7 A1① 决策留痕旁挂（PoC-1 连续评分数据面）：接受/否决双侧证据，
# 与遮蔽行为完全解耦——只读不回灌，绝不作为任何通道输入（映射表才是
# 对照行为契约）。行由 RuleEngine._trace 采集（FINMASK_DECISION_TRACE=0 关）。
# ---------------------------------------------------------------------------

DECISION_TRACE_HEADER = ["文件", "类型", "类别", "词", "证据", "置信度"]


def write_decision_trace_csv(out_dir, rows: list) -> Path:
    """在 out_dir 生成决策留痕 CSV（与映射表/台账同目录、同 utf-8-sig）。

    行=（文件, accept/reject, 类别, 词, 证据串, 置信度）。含敏感词面与
    否决词面，与映射表同规格保管（入库/外发禁区），UI「清除分析缓存」
    联动清除。
    """
    stamp = time.strftime("%Y%m%d_%H%M")
    trace_path = Path(out_dir) / f"决策留痕_{stamp}.csv"
    with open(trace_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(DECISION_TRACE_HEADER)
        for row in rows:
            writer.writerow(row)
    return trace_path


# Windows 文件名非法字符 -> 全角替代（脱敏替换文本含 * 等字符时保证文件名合法）
_FILENAME_CHAR_MAP = str.maketrans({
    "*": "＊", "?": "？", ":": "：", "|": "｜", "/": "／", "\\": "＼",
    "<": "《", ">": "》", '"': "＂",
})


def _sanitize_filename(name: str) -> str:
    return name.translate(_FILENAME_CHAR_MAP)


def build_output_path(src: Path, options: MaskOptions, engine,
                      taken: set | None = None) -> tuple:
    """输出占位路径：原名 + 「_脱敏后」后缀；重名时自动追加序号。

    v1.4 起文件名脱敏改为【两阶段】：内容处理完成（引擎已登记全部实体与
    别名）后，由 finalize_output_name() 按实体表重命名——这样双段文件名、
    无后缀简称、跨文件别名一致性都能正确处理。
    v1.6.5：taken 为本批次已分配的占位文件名集合——所有占位路径在处理开始
    前分配，磁盘上尚无文件，仅靠 dst.exists() 无法发现同 stem 碰撞
    （同批次两个"年度报告.docx"会互相覆盖，第一个结果静默丢失）。
    """
    out_dir = Path(options.output_dir) if options.output_dir else src.parent
    if options.output_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".xlsx" if (src.suffix.lower() == ".xls" and options.xls_to_xlsx) else src.suffix
    taken = taken if taken is not None else set()
    dst = out_dir / f"{src.stem}_脱敏后{suffix}"
    n = 1
    while dst.exists() or dst.name in taken:
        dst = out_dir / f"{src.stem}_脱敏后({n}){suffix}"
        n += 1
    taken.add(dst.name)
    return dst, None


def finalize_output_name(tmp_dst: Path, src_name: str, src_stem: str, options: MaskOptions, engine,
                         pairs_override=None) -> tuple:
    """内容处理完成后，按引擎已登记实体对文件名做最终脱敏并重命名。

    pairs_override（v1.9.4）：冻结复核轮传入「首轮文件名替换对快照 ⊕ 编辑」
    （pdf_handler._replay_from_plan 经 report 注入）——冻结轮无检测，引擎
    运行期注册表不再自然形成，快照回放是唯一等价路径；None = 现行引擎派生。

    返回 (最终路径, 文件名映射记录或 None)。记录格式：
    (原文件名, 最终文件名, "filename", "文件名")。
    """
    pairs = list(pairs_override) if pairs_override is not None \
        else engine.filename_replacement_pairs()
    final_stem = src_stem
    for orig, repl in pairs:
        # 跳过文件名行/超长键（那些是整文件名，不用于文件名内替换）
        if len(orig) > 40 or "." in orig:
            continue
        if orig and orig in final_stem:
            final_stem = final_stem.replace(orig, repl)
    final_stem = _sanitize_filename(final_stem)
    suffix = tmp_dst.suffix
    final_dst = tmp_dst.with_name(final_stem + "_脱敏后" + suffix)
    n = 1
    while final_dst.exists() and final_dst != tmp_dst:  # 占位文件自身不算冲突
        final_dst = tmp_dst.with_name(f"{final_stem}_脱敏后({n}){suffix}")
        n += 1
    os.replace(tmp_dst, final_dst)
    name_record = None
    # v2.1.9 B12：比较基准改用词干形态（原 src_name 含扩展名 → 比较串
    # "报告.pdf_脱敏后.pdf" 恒不等 → 每文件必产一条纯改名 filename 噪音行，
    # 54 行/批；v2.1.8 words_only 评估批发现）。仅词干真实替换或碰撞加序
    # （最终名 != 词干+_脱敏后+后缀）才记账。
    if final_dst.name != src_stem + "_脱敏后" + suffix:
        name_record = (src_name, final_dst.name, "filename", "文件名")
    return final_dst, name_record


def _cleanup_dst(dst, on_log=None) -> None:
    """失败时删除占位输出文件：防止半成品/未脱敏副本顶着 _脱敏后 名字留存
    （.xls 的 COM 链路会先复制原件到占位路径，中途异常即原样敏感副本）。
    v2.1.9：删除失败必须留痕——静默残留=顶着已脱敏名的原文
    副本无任何解释（.xls 被 Excel 进程锁定时 unlink 会 PermissionError）。"""
    try:
        Path(dst).unlink(missing_ok=True)
    except OSError as e:
        msg = ("清理失败输出文件未成功，内容为未脱敏原文，请手动删除："
               f" {Path(dst).name} —— [{type(e).__name__}] {e}")
        if on_log is not None:
            on_log(msg)
        else:
            sys.stderr.write(msg + "\n")


def probe_weight(path: Path, ext: str) -> int:
    """任务权重（进度单元）：PDF 按页数、其余按 1（TODO: 可再按文件大小细化）。"""
    if ext == ".pdf":
        try:
            try:
                import fitz
            except ImportError:
                import pymupdf as fitz
            doc = fitz.open(str(path))
            try:
                return max(1, doc.page_count)
            finally:
                doc.close()
        except Exception as e:
            # 页数探测失败按 1 页计（仅影响进度估算）；留痕防静默（工程红线一）
            sys.stderr.write(f"[probe_weight] PDF 页数探测失败，按 1 页计："
                             f"{Path(path).name} —— [{type(e).__name__}] {e}\n")
            return 1
    return 1


def _fmt_seconds(secs: float) -> str:
    m, s = divmod(int(secs), 60)
    return f"{m}分{s:02d}秒" if m else f"{s}秒"


def _compute_plan_edits(files, options: MaskOptions, engine, on_log) -> dict | None:
    """v1.9.4 冻结复核编辑 diff（契约 §3）：编辑表 vs 各文件首轮 plan 表快照。

    - 删除 = plan 表原文并集 - 编辑表键：按原文全局生效（跨文件、跨通道，
      含印章/凭证/横幅行——设计决策 #1 不设二次确认，删除清单落日志）；
    - 改值 = 交集中替换文本不同者：按原文全局更新指令替换文本；
    - 增行 = 编辑表键 - plan 并集（含新自定义敏感词）：过防呆闸
      （masking_plan.add_key_allowed）后由冻结重放轮逐页定位产生新指令；
      不过闸 → 仅记录 + 设计决策 #2 人话文案（原位留痕，不自动遮蔽）。
    返回 None = 非覆盖轮或批内无任何可用 plan（全部回落全流程，无需 diff）。
    """
    from . import masking_plan
    if not engine.mapping_only:
        return None
    union = set()
    repls_by_orig: dict = {}
    n_plans = 0
    for f in files:
        p = Path(f)
        if p.suffix.lower() != ".pdf":
            continue
        try:
            plan, _reason = masking_plan.load_plan(p, options)
        except Exception:
            continue
        if plan is None:
            continue
        n_plans += 1
        for row in plan.table_rows():
            union.add(row[0])
            repls_by_orig.setdefault(row[0], set()).add(row[1])
    if not n_plans:
        return None
    edited = engine._mapping  # 编辑表（RuleEngine 载入，原文为键，v1.9.3 同口径）
    deletes = sorted(union - set(edited))
    # 改值只认「真编辑」：edited 值与该原文的全部 plan 行替换文本都不同才
    # 生效（同原文多替换多行经 load_mapping_csv 原文为键塌缩取末行是 CSV
    # 载入工件而非编辑意图——零编辑重放必须与首轮逐行等价，M1 判据）。
    changes = {k: edited[k] for k in (union & set(edited))
               if edited[k] not in repls_by_orig.get(k, ())}
    add_src = {k: edited[k] for k in (set(edited) - union)}
    for w in engine._word_keys:  # 新自定义敏感词与增行键同构，同过防呆闸
        if w and w not in union and w not in add_src:
            add_src[w] = engine.custom_replacement(w)  # v2.1.8：敏N 代号（与首轮同源派生）
    adds, rejected = [], []
    for k in sorted(add_src, key=lambda x: (-len(x), x)):
        ok, reason = masking_plan.add_key_allowed(k, engine)
        if ok:
            adds.append((k, add_src[k]))
        else:
            rejected.append((k, add_src[k], reason))
    if deletes:
        shown = "、".join(deletes[:20]) + ("…" if len(deletes) > 20 else "")
        on_log(f"冻结复核：检测到删除 {len(deletes)} 项，按原文全局生效"
               f"（跨文件、跨通道，含印章/凭证/横幅行）：{shown}")
    for k, _repl, reason in rejected:
        on_log(f"该键过于通用，仅应用于当前位置（该键仅记录于映射表，"
               f"不产生自动遮蔽指令）：「{k}」（{reason}）")
    return {"deletes": deletes, "changes": changes, "adds": adds,
            "rejected": rejected}


def _eta_text(done_units: float, total_units: float, t0: float) -> str:
    if done_units <= 0 or total_units <= done_units:
        return ""
    remain = (time.time() - t0) / done_units * (total_units - done_units)
    return _fmt_seconds(remain)


def run_batch(files, options: MaskOptions, on_log, on_progress) -> dict:
    """批量处理入口（应在子线程中调用，禁止在本函数内直接操作 tkinter 控件）。

    on_log(msg)                    ：日志回调；
    on_progress(done_units, total) ：进度回调（浮点进度单元）。
    """
    t0 = time.time()
    out_desc = options.output_dir if options.output_dir else "与源文件相同目录"
    on_log(f"输出目录：{out_desc}")

    # v1.6.3：每批次在输出路径下创建独立子文件夹（任务时间精确到分钟），
    # 脱敏文件与汇总映射表统一存入，避免多次任务互相混杂。
    batch_dir = None
    if files:
        base = Path(options.output_dir) if options.output_dir else Path(files[0]).parent
        batch_dir = base / f"脱敏输出_{time.strftime('%Y%m%d_%H%M')}"
        n = 2
        while batch_dir.exists():  # 同分钟多批次：追加序号防映射表静默覆盖（v1.6.5）
            batch_dir = base / f"脱敏输出_{time.strftime('%Y%m%d_%H%M')}({n})"
            n += 1
        batch_dir.mkdir(parents=True, exist_ok=True)
        options.output_dir = str(batch_dir)
        on_log(f"本批次输出文件夹：{batch_dir}")

    engine = RuleEngine(options)
    if engine.mapping_count:
        on_log(f"已加载映射对照表：{engine.mapping_count} 条（命中时优先按对照表替换）")
        on_log("对照表覆盖模式：本次仅按映射表与自定义敏感词脱敏，自动规则已停用"
               "（在映射表中删除的行将不再遮盖）。")
        on_log("首轮检测冻结（v1.9.4）：命中首轮冻结结果的 PDF 将零 OCR 零检测"
               "重放，仅应用映射表增删改；未命中的文件回落全量检测（v1.9.3 语义）。")
    # v1.9.4 冻结复核编辑 diff（契约 §3）：删除/改值/增行三算子在此预计算，
    # 经引擎属性穿线给 pdf_handler 冻结重放轮消费（其他处理器不受影响）。
    engine.plan_edits = None
    if engine.mapping_only:
        try:
            engine.plan_edits = _compute_plan_edits(files, options, engine, on_log)
        except Exception as e:
            on_log(f"⚠ 冻结复核编辑差异计算失败（按 v1.9.3 覆盖语义继续）："
                   f"[{type(e).__name__}] {e}")
    if getattr(engine, "_words_only", False):
        on_log("仅按自定义敏感词脱敏：自动识别已停用（品牌预设词与批内学习不生效，"
               "指定词豁免纯后缀/白名单全局闸）；复检词集已收敛为指定词集。")
    if engine.word_count:
        on_log(f"已启用自定义敏感词：{engine.word_count} 个")

    # v1.6.7：文件名实体预注册——正文中只出现品牌词（"示例甲木门"）
    # 而全名仅在文件名的文档，也能派生品牌跟随脱敏。仅利用注册副作用，
    # 文件名本身的脱敏仍由两阶段 finalize_output_name 完成。
    # v1.6.9 扩展：PDF 首页预扫描——简称声明多在封面/首页（"某市示例贸易
    # 有限公司以下简称为'示例贸易'"），此前按文件名排序处理时，简称消费方
    # （施工办法/OA规范）可能排在声明方（直营店细则）之前导致漏检。
    # words_only 同样跳过预扫描——
    # 注册副作用通道在 find_spans 内已被模式位门控，跳过=纯省时零行为
    if not engine.mapping_count and not getattr(engine, "words_only", False):
        for f in files:
            # v2.1.7 A1①：决策留痕文件盖章（预扫描段）
            engine._trace_file = f"{Path(f).name}(预扫描)"
            stem = Path(f).stem
            if stem:
                engine.find_spans(stem)
            if Path(f).suffix.lower() == ".pdf":
                try:
                    import fitz
                    with fitz.open(f) as _doc:
                        if len(_doc):
                            _t = _doc[0].get_text("text")
                            if len(_t.strip()) >= 24:  # 扫描件首页跳过（OCR 成本高）
                                engine.find_spans(_t)
                except Exception as e:
                    # 预扫描失败不影响正常处理，但留痕（工程红线一：静默失败=简称
                    # 声明未注册，后续文档的品牌跟随可能漏遮且无解释）
                    on_log(f"⚠ 首页预扫描失败（不影响正常处理）：{Path(f).name}"
                           f" —— [{type(e).__name__}] {e}")

    # 预估总进度单元，构建任务清单（文件名应用规则引擎同步脱敏）
    jobs = []
    taken_names: set = set()  # v1.6.5：本批次已分配的占位输出名，防同 stem 互相覆盖
    total_units = 0
    for f in files:
        p = Path(f)
        ext = p.suffix.lower()
        if ext not in SUPPORTED_EXTS:
            on_log(f"✘ 跳过不支持的文件格式：{p.name}")
            continue
        weight = probe_weight(p, ext)
        dst, name_record = build_output_path(p, options, engine, taken_names)
        jobs.append((p, ext, dst, weight, name_record))
        total_units += weight

    summary = {"ok": 0, "fail": 0, "masked": 0,
               "skipped": len(files) - len(jobs), "outputs": [], "seconds": 0.0,
               "output_dir": str(batch_dir) if batch_dir else ""}

    done_units = 0.0
    mapping_hint_shown = False
    per_file_rows: list = []  # (原文, 替换为, 规则, 文件, 位置)
    # v2.0.1 C1：遮蔽台账行采集区（7 列）+ 轮次旗标（见聚合区写出判定）
    ledger_rows: list = []
    ledger_edited = False   # 本批存在编辑保存点重写行（pdf_handler 注入）
    ledger_fresh = False    # 本批存在全量检测文件（首轮/非 PDF/回落重建）
    ledger_rebuilt = False  # 本批存在覆盖轮 plan 未命中回落重建文件（留痕用）
    mapping_dir = Path(options.output_dir) if options.output_dir else None
    batch_texts: dict = {}  # v2.1.7 B3-3：批内文本复用面（入口①后台重扫消费）
    batch_texts_chars = 0  # v2.1.9：总量闸计数
    for idx, (src, ext, dst, weight, name_record) in enumerate(jobs, 1):
        eta = _eta_text(done_units, total_units, t0)
        on_log(f"—— [{idx}/{len(jobs)}] {src.name}" + (f"（剩余约 {eta}）" if eta else ""))
        report = _FileReport(src.name, on_log, on_progress, done_units, weight, total_units)
        engine._trace_file = src.name  # v2.1.7 A1①：决策留痕文件盖章
        file_t0 = time.time()
        try:
            handler = HANDLER_REGISTRY[ext]
            stats = handler(src, dst, engine, options, report)
            # v1.4 文件名两阶段收尾：内容完成后按引擎实体表重命名占位文件
            # v1.9.4：冻结重放轮经 report 注入「首轮文件名快照 ⊕ 编辑」
            final_dst, name_record = finalize_output_name(
                Path(dst), src.name, src.stem, options, engine,
                pairs_override=getattr(report, "filename_pairs_override", None))
            if name_record:
                report.records.append(name_record)
                report.op_records.append(name_record)  # v2.0.1 C1：台账同收
            masked = int(stats.get("masked", 0))
            out_path = str(final_dst)
            # v2.1.7 B3-3：批内文本采集（PDF 原生+OCR 拼接；非 PDF 未设即跳过）。
            # v2.1.9：总量上限闸——超 50M 字符停止采集（入口①重扫
            # 退化为直接重扫），防大批次全量文本无界驻留内存。
            _bt = getattr(report, "extracted_text", None)
            if _bt and batch_texts_chars < 50_000_000:
                batch_texts[str(src)] = _bt
                batch_texts_chars += len(_bt)
            elif _bt:
                summary["batch_texts_capped"] = True
            summary["ok"] += 1
            summary["masked"] += masked
            summary["outputs"].append(out_path)
            on_log(f"✔ 完成：{Path(out_path).name}"
                   f"（脱敏 {masked} 处，用时 {_fmt_seconds(time.time() - file_t0)}）")
            if name_record:
                on_log(f"文件名已同步脱敏：{src.name} → {Path(out_path).name}")
            per_file_rows.extend(
                (orig, repl, rule, src.name, loc) for orig, repl, rule, loc in report.records)
            # v2.0.1 C1：遮蔽台账行采集——编辑保存点重写行（复核轮编辑轮）
            # 优先注入；其余文件按 op 级全量建行（首轮/零编辑重放/回落重建
            # 共用此默认口径，重放行与首轮逐行等价=零新增）。
            override = getattr(report, "ledger_rows_override", None)
            if override is not None:
                ledger_edited = True
                file_max_id = 0
                for loc, rule, orig, repl, op_id, status in override:
                    ledger_rows.append([src.name, loc, rule, orig, repl,
                                        op_id, status])
                    file_max_id = max(file_max_id, int(op_id))
                if name_record:  # 重命名发生在台账重写点之后，续号补行
                    ledger_rows.append([src.name, name_record[3], name_record[2],
                                        name_record[0], name_record[1],
                                        file_max_id + 1, LEDGER_ACTIVE])
            else:
                ledger_rows.extend(build_ledger_rows(report.op_records, src.name))
            if not getattr(report, "ledger_replay_round", False):
                ledger_fresh = True  # 首轮/非 PDF/回落重建：台账当轮新产
            if getattr(report, "ledger_rebuilt", False):
                ledger_rebuilt = True
            if not mapping_dir:
                mapping_dir = final_dst.parent
        except MissingDependencyError as e:
            summary["fail"] += 1
            _cleanup_dst(dst, on_log)
            on_log(f"✘ 失败（缺少可选组件）：{src.name} —— {e}")
        except Exception as e:  # 单文件异常不中断批量任务
            summary["fail"] += 1
            _cleanup_dst(dst, on_log)
            on_log(f"✘ 失败：{src.name} —— [{type(e).__name__}] {e}"
                   f"（已清理未完成的输出文件）")
            # TODO: 可选——将完整 traceback 写入本地错误日志文件，便于排查
        finally:
            done_units += weight
            on_progress(done_units, total_units)

    # 汇总映射表：整个批次只输出一份，同一替换项合并为一行，便于批量修改再生
    if per_file_rows or (options.mask_amount and options.amount_mode == "scale"):
        # v2：金额明细行不入映射表，仅保留金额缩放公式行（系数已在首行）
        all_rows = [(o, r, rule, f, loc) for (o, r, rule, f, loc) in per_file_rows
                    if rule != "amount"]
        if options.mask_amount and options.amount_mode == "scale" and engine.amount_scale_K:
            all_rows.insert(0, ("[金额缩放公式]", f"显示值 = 原值 × {engine.amount_scale_K}",
                                "amount_formula", "全部", "-"))
        merged = merge_mapping_rows(all_rows)
        if merged:
            # v1.3：人名行附带置信度（严格/强制/改造三=高，高危交叉/nr交叉=中，宽松=低）
            enriched = [
                row + ((engine.get_person_confidence(row[0]) if row[2] == "person" else ""),)
                for row in merged
            ]
            mapping_path = write_mapping_csv(mapping_dir or Path.cwd(), enriched)
            summary["mapping"] = str(mapping_path)
            on_log(f"已生成汇总映射表：{mapping_path.name}"
                   f"（{len(merged)} 个不同替换项；含敏感原文，请妥善保管）")
            if not mapping_hint_shown:
                mapping_hint_shown = True
                on_log("提示：编辑映射表“替换为”列（或删除行）后，将其选入“映射对照表覆盖”"
                       "重新处理，输出将严格按新映射批量生成。")

    # v2.0.1 C1：遮蔽台账落盘——op 级全量（含主表剥除的 amount 家族；上面
    # :410 剥除口径原样保留，主映射表零变化，台账接住金额行）。与 plan 的
    # 关系（契约定死）：首轮/回落重建=台账当轮落盘；复核轮零编辑重放=零新增
    # 不重写（台账是首轮产物，原样沿用）；编辑保存点重写行已在循环内注入。
    if ledger_rows and (ledger_fresh or ledger_edited):
        ledger_path = write_ledger_csv(mapping_dir or Path.cwd(), ledger_rows)
        summary["ledger"] = str(ledger_path)
        note = "（plan 未命中，按本轮重检重建）" if ledger_rebuilt else ""
        on_log(f"已生成遮蔽台账{note}：{ledger_path.name}"
               f"（{len(ledger_rows)} 个遮蔽 op 逐条留痕，含敏感原文，请妥善保管）")
    elif ledger_rows and engine.mapping_only:
        on_log("遮蔽台账：本轮为冻结重放（画块=首轮指令重放），零新增台账行，"
               "台账沿用首轮产物原样沿用。")

    # v2.1.7 A1①：决策留痕旁挂落盘——接受/否决双侧证据面（PoC-1 连续评分
    # 标定数据）。与映射表/台账同目录同敏感级；默认开启，引擎侧
    # FINMASK_DECISION_TRACE=0 可关；对映射表 md5/像素基线/台账 glob 零影响。
    _trace_rows = list(getattr(engine, "_decision_trace", ()) or ())
    if _trace_rows:
        _dropped = int(getattr(engine, "_trace_dropped", 0) or 0)
        trace_path = write_decision_trace_csv(mapping_dir or Path.cwd(), _trace_rows)
        summary["decision_trace"] = str(trace_path)
        summary["decision_trace_rows"] = len(_trace_rows)
        _drop_note = f"，超封顶丢弃 {_dropped} 行" if _dropped else ""
        on_log(f"已生成决策留痕：{trace_path.name}"
               f"（接受/否决共 {len(_trace_rows)} 行{_drop_note}；"
               "含敏感词面，与映射表同规格保管）")
    summary["batch_texts"] = batch_texts  # v2.1.7 B3-3：批内文本复用面

    summary["seconds"] = time.time() - t0
    on_log(
        f"===== 汇总：成功 {summary['ok']} 个，失败 {summary['fail']} 个，"
        f"共脱敏 {summary['masked']} 处，用时 {_fmt_seconds(summary['seconds'])} ====="
    )
    return summary
