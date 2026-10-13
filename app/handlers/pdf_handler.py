# -*- coding: utf-8 -*-
"""PDF 处理器：核心双链路分流。

逐页统计可提取文本字符数：
- >= NATIVE_MIN_CHARS -> 链路 A（原生文本 PDF）：
    PyMuPDF 提取字符级坐标 -> 统一规则引擎匹配 -> add_redact_annot 黑色遮罩
    -> apply_redactions（同时销毁底层文字，100% 文字准确率、毫秒级处理）。
- <  NATIVE_MIN_CHARS -> 链路 B（扫描/图片型 PDF，需勾选“图片内文字脱敏”）：
    渲染整页 -> PP-OCRv5 识别（行级 bbox；接入表格结构识别后可提供单元格级 bbox）
    -> 表格区域优先用单元格级坐标、非表格区域用行级坐标
    -> 命中内容绘制黑色遮罩；低置信度文本仅标记、不强制脱敏。

红色印章遮盖（可选）：基于渲染图的 HSV 红色区域检测（预留图像处理接口，
生产可替换为印章检测模型）。
"""
from __future__ import annotations

from pathlib import Path
import math
import os
import re

from .. import masking_plan
from .. import ocr_engine as ocr_mod
from .. import rules_engine as ocr_mod_rules
from ..utils import APP_VERSION, MissingDependencyError


def _pure_org_suffix_fn(engine):
    """G1（schema v2）：纯后缀谓词取用——优先引擎实例生效表谓词
    （brands.json org_suffixes / org_suffixes_remove 增删词在图像层
    复检/列头锚定/拆分残片通道与 find_spans 同源生效）；engine 无实例
    方法（测试 fake 引擎等引擎无关调用方）时回退模块级纯代码表谓词
    （v1.9.3 判定锚，语义不变）。"""
    return getattr(engine, "is_pure_org_suffix", None) \
        or ocr_mod_rules.is_pure_org_suffix


def _stamp_on(options) -> bool:
    """v2.1.8 words_only：印章通道随模式停用（「仅脱敏
    这些词」语义下印章块违背「正文明文原样保留」契约；默认路径零影响）。"""
    return (bool(getattr(options, "mask_stamp", False))
            and not bool(getattr(options, "words_only", False)))


def _amount_on(options) -> bool:
    """v2.1.8 words_only：金额通道（原生列/OCR 行标签/OCR 金额列/
    图内金额兜底）随模式停用（CHANGELOG 承诺「跳过全部
    自动识别（…金额…）」；文本层 AMOUNT_RE 已在 find_spans 门内）。"""
    return (bool(getattr(options, "mask_amount", False))
            and not bool(getattr(options, "words_only", False)))


def _word_repl(engine, w):
    """v2.1.8：复检通道词面替换——自定义词取批内代号（敏N），其余 ***。

    fake 引擎/异常防御：getattr 取方法、失败回退 ***（复检是兜底通道，
    不得因代号查询失败丢遮盖）。"""
    fn = getattr(engine, "custom_replacement", None)
    if fn is None:
        return "***"
    try:
        return fn(w) or "***"
    except Exception:
        return "***"


def _recheck_word_set(engine, options=None) -> set:
    """复检词集装配（视觉/条带/横幅三复检通道共用）。

    按开关分桶——人名词集（_person_conf）仅在 mask_person 开启时纳入：
    冻结重放轮 register_confidences 回注不查开关，用户关掉人名遮蔽后
    复检轮仍按人名词集扫=真实不一致路径。
    公司桶（_short_names/_company_alias）与首轮 _dict_candidates 同口径
    不门控（首轮静态词同样不查开关，单侧门控反而制造「首轮遮/复检漏」
    的新不一致）；映射/自定义词是用户显式声明，不门控。options 缺席
    （防御默认）维持全集口径。"""
    # v2.1.8 words_only：复检词集收敛为指定词集（+映射键=并集语义）——
    # 不扫任何注册实体（预研 §3.2：词集驱动基础设施，空集早退零开销）
    if options is not None and getattr(options, "words_only", False):
        words = (set(getattr(engine, "_word_keys", []) or [])
                 | set(getattr(engine, "_mapping", {}) or {}))
        words = {w for w in words if w and len(w) >= 2}
        # 纯后缀词仍过滤：图内逐处遮「公司」二字会造成灾难性过度遮挡
        words = {w for w in words if not _pure_org_suffix_fn(engine)(w)[0]}
        return words
    words = set(engine._short_names) | set(engine._mapping) \
        | set(engine._word_keys) | set(engine._company_alias)
    if options is None or getattr(options, "mask_person", True):
        words |= set(engine._person_conf)
    words = {w for w in words if w and len(w) >= 2}
    # v1.9.3：纯法定组织形式后缀词（公司/集团/有限公司…）不入复检词集
    words = {w for w in words if not _pure_org_suffix_fn(engine)(w)[0]}
    return words


# ---------------------------------------------------------------------------
# 可调参数（TODO: 可提升到配置界面）
# ---------------------------------------------------------------------------
NATIVE_MIN_CHARS = 24   # 每页可提取字符数 < 该值 => 判定为扫描/图片型页面
OCR_DPI = 200           # OCR 渲染分辨率：300 更准更慢（精度/速度权衡）
# 条带复检（_band_recheck）条带几何：band_h=条带高度(pt)、band_step=步进(pt)。
# 默认 280/240（4 条带/页，重叠仍 40pt）——v6 引擎期全量 A/C 实测 band 独占
# -169.5s/-20.1%（843.9→674.4s），9 处 band 拦截/泄漏轨/观感/碎词五判据全过
# （证据：分批回归实测报告 §9）；
# 同实验证伪降 zoom 路线（det 推理税主导、对像素弱敏感），故 zoom 固定 3 不
# 参数化。历史 200/160 可经环境变量回退；非法值回退默认，行为不变。
# band_h 下限 200：120pt 条带曾把横幅拦腰切断致 slogan 漏遮（见 _band_recheck）。
RECHECK_BAND_H = 280.0
_bh_env = os.environ.get("FINMASK_RECHECK_BAND_H", "").strip()
if _bh_env:
    try:
        RECHECK_BAND_H = float(_bh_env)
    except ValueError:
        pass  # 非法值保持默认 280
del _bh_env
# 步进与条带高差出相邻条带重叠 40pt；重叠 40pt 的语义 = 先画补丁进入后续
# 条带 OCR 输入（横幅装饰行连带遮蔽的依据），改 band_h 时 step 须同步
# 保持 step = band_h - 40。
RECHECK_BAND_STEP = 240.0
_bs_env = os.environ.get("FINMASK_RECHECK_BAND_STEP", "").strip()
if _bs_env:
    try:
        RECHECK_BAND_STEP = float(_bs_env)
    except ValueError:
        pass  # 非法值保持默认 240
del _bs_env
# 缺字假设参数（custom 用户词最强兜底）：净版条带通道
# （_custom_word_pristine_band）与视觉复检通道（_visual_recheck）对 user
# 显式配置词做「单字缺失假设」匹配（OCR det 丢框/rec 丢字使行
# 文本缺一字，连续子串不成立——通知落款行「中共市图书馆委员会」
# 缺「某」实测）。
# 词源口径（v2.0.3 二轮收窄，设计决策）：仅 UI 自定义敏感词
# _word_keys（MaskOptions.custom_words 逐字显式配置）——UI 词享受最强
# 兜底；brands.json 品牌词（_static_brand_words）不参与缺字兜底，维持
# 主链路精确匹配覆盖（首版并集词源在零 UI 词语料上对全部光栅化页
# 无条件净版条带重扫，39 份 elapsed 1174→2192s 结构性回归，故收窄）。
# CUSTOM_GAP_MIN_WORD：参与假设的最低词长（更短词去字后证据太弱）；
# CUSTOM_GAP_MIN_REST：去字后残串最低长度；
# CUSTOM_GAP_WHOLE_LINE：假设词占行文本比例 ≥ 该值时整行遮蔽，否则按残串
# 矩形向缺口侧外扩一字符宽。取 0.5 而非整行分支的 0.6：主形态
# 5 字词/9 字行=0.556 <0.6，跨度补丁盖不住分列行两端的残片「中共」「员会」
# （并列残片=用户报障形态），整行遮蔽才能闭合；查全优先（配置词风雨无阻，
# 设计决策）。行级闸：行内已含任一配置词全词=完整可读行，缺字假设不参与。
# 映射表键/批内学习简称不扩面；无 UI 词时假设通道完全关闭零开销（净版
# 文档不开、OCR 零调用，负向对照 tests/test_custom_word_recheck.py）。
CUSTOM_GAP_MIN_WORD = 4
CUSTOM_GAP_MIN_REST = 3
CUSTOM_GAP_WHOLE_LINE = 0.5
STAMP_MIN_AREA_PT2 = 80  # 印章区域最小面积（PDF 点^2），过滤噪点

# ---------------------------------------------------------------------------
# v2.1.1 红章形状闸（检测面收窄，实测红框表格/红底单元格/
# 橙红圆形 logo 大量误报「红色印章」）。阈值以「对抗三类→零检 + 真实管线
# 真章面→零漏」双面标定（完整标定表见 tests/test_stamp_shape_gate.py 头
# 注释与标定探针记录）：
#   真值面 = 全量回归批 cache/plans 全部 80 个 stamp op 矩形（9 文件，其中
#   stamp op 最多的前 3 源 67 个为套件断言面），86 个印章种子分量统计：
#   fill∈[0.0304,0.9388]、circ∈[0.019,0.8967]、长边比≤0.5187、稠密近圆域
#   （fill>0.75∧circ≥0.55）唯一成员为 14.4pt 实心红点。
#   与预设值的偏离（零漏硬约束优先，「宁留误报不可漏真章」）：
#   fill 下限 0.05→0.020（真值含截图细线红框）、上限 0.80→0.95（真值含近实心
#   红条；仍排红底实心单元格 1.0）、长边比 0.40→0.55（真值最大 0.5187；仍排
#   整页宽红色装饰带）、圆度硬保留不可行（真值 circ 低至 0.019）→ b) 按设计
#   「为辅」定位操作化为实心圆盘拒判（橙红 logo 域；五角星特征保留为豁免）。
#   闸效果实测：对抗三类 1/1/1→0/0/0；真值面覆盖修复前后位元级一致
#   （raw 72/80、fin 80/80，raw 差异系既有行合并/文本行扩展效应）。
STAMP_FILL_MIN = 0.020            # a) 填充率带通下限（红像素数/bbox 面积）
STAMP_FILL_MAX = 0.95             # a) 填充率带通上限（实心单元格=1.0 排除）
STAMP_CIRC_MIN = 0.55             # b) 圆度阈值 4πA/P²（实心圆盘判别用）
STAMP_SOLID_ROUND_FILL = 0.75     # b) 实心圆盘判别的填充率下限
STAMP_SOLID_ROUND_MIN_SIDE_PT = 20.0   # b) 短边低于此pt的实心近圆保留（真值红点14.4pt）
STAMP_LONG_EDGE_MAX_RATIO = 0.55  # c) bbox 长边/页面短边 上限（红框表格0.91排除）

# 遮罩替换内容的显示样式：白底黑字（在原文字位置显示脱敏后的文本，
# 如 110101********8515 / 138****5678 / 北京****有限公司 / ABC公司[按映射表]）
PDF_MASK_FILL = (1, 1, 1)
PDF_TEXT_COLOR = (0, 0, 0)

_STAMP_HINT_SHOWN = {"flag": False}


def _import_fitz():
    """PyMuPDF 可选导入（兼容 fitz / pymupdf 两种模块名）。"""
    try:
        import fitz  # noqa: F401  PyMuPDF
        return fitz
    except ImportError:
        try:
            import pymupdf as fitz  # PyMuPDF >= 1.24 亦可
            return fitz
        except ImportError:
            raise MissingDependencyError("处理 PDF 需要 PyMuPDF：pip install pymupdf")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def process(src: Path, dst: Path, engine, options, report) -> dict:
    fitz = _import_fitz()
    doc = fitz.open(str(src))
    try:
        # 统一坐标空间：把页面旋转“烘焙”进内容流（外观不变，rotation 归零）。
        # 扫描件常带 /Rotate 90/180/270，若不归一，绘制坐标与显示方向错位
        # （实测 rotation=270 时遮罩位置表现为 XY 颠倒）。
        normalized = 0
        for page in doc:
            if page.rotation:
                page.remove_rotation()
                normalized += 1
        if normalized:
            report.log(f"已归一化 {normalized} 个旋转页面（外观不变，坐标统一）")

        total_pages = doc.page_count
        # v1.9.4 首轮检测冻结（方案二）：复核轮（映射对照表覆盖）且 plan 头部
        # 四键全匹配（plan_schema/app_version/src_content_hash/brands_hash，
        # 不含 engine_name）→ 零 OCR 零检测按 plan 指令重放；任一不符/缺失 →
        # 回落 v1.9.3 全流程并顺带重建 plan（log 留痕）。
        plan = None
        miss_reason = ""
        if engine.mapping_only:
            plan, miss_reason = masking_plan.load_plan(src, options)
            if plan is not None and plan.page_count != total_pages:
                plan, miss_reason = None, (
                    f"plan 页数不一致（{plan.page_count} != {total_pages}）")
        if plan is not None:
            return _replay_from_plan(doc, src, dst, plan, engine, options,
                                     report, fitz)
        if engine.mapping_only:
            report.log(f"⚠ 未命中首轮冻结 plan（{miss_reason}），本次回落全量"
                       "检测（v1.9.3 语义）并重建 plan——其结果成为新冻结基线。")
            # v2.0.1 C1：回落轮=等同首轮，遮蔽台账按本轮重检重建（旗标供
            # dispatcher 聚合区留痕落盘）
            report.ledger_rebuilt = True
        report.log(f"阶段 1/2：共 {total_pages} 页，逐页判定 原生文本/扫描件 链路 …")
        # v1.9.0 分析缓存：getter 携带源文件与配置上下文（src 用于内容哈希
        # 定位缓存文件；rule_engine.mapping_only 决定 consume 态——仅映射
        # 对照表覆盖轮读缓存，auto/legacy 轮只记录不读，当轮行为零变化）。
        get_ocr = (_make_ocr_getter(report, src=src, rule_engine=engine,
                                    options=options)
                   if options.ocr_image_text else None)
        # v1.9.4：首轮冻结 plan 采集器（纯旁路记录，不影响任何检测/绘制行为）
        builder = masking_plan.PlanBuilder(src, options)
        hits = 0
        native_pages = ocr_pages = skipped_pages = 0
        error_pages = 0  # 页级异常计数（>30% 显著警告）
        low_conf_total = 0
        leaked_pages: dict = {}  # v1.6.8：redact 未销毁的顽固页 {pno: [(rect, sp)...]}
        raster_pages: set = set()  # v1.7：OCR 遮盖过的页面（销毁原图像素）
        for pno in range(total_pages):
            page = doc[pno]
            try:
                page_hits, mode, low_conf, leaked, need_raster = _process_page(
                    page, engine, options, get_ocr, report, pno, fitz,
                    plan=builder)
            except Exception as e:  # 单页失败不影响整份文件
                import traceback as _tb
                report.log("  第 " + str(pno + 1) + " 页处理异常，已跳过：["
                           + type(e).__name__ + "] " + str(e)
                           + " || " + _tb.format_exc()[-500:])
                page_hits, mode, low_conf, leaked, need_raster = 0, "error", 0, [], False
            builder.set_page(pno, mode, page_hits, low_conf)  # v1.9.4 页级元数据
            if leaked:
                leaked_pages[pno] = leaked
            if need_raster:
                raster_pages.add(pno)
            hits += page_hits
            native_pages += mode == "native"
            ocr_pages += mode == "ocr"
            skipped_pages += mode == "skipped"
            error_pages += mode == "error"
            low_conf_total += low_conf
            report.progress(pno + 1, total_pages)

        # v1.6.8 顽固页统一光栅化（迭代外执行，避免破坏页状态）：
        # 特殊内容流（填充+描边双绘等）redact 无法销毁文本层，整页转图片。
        # v1.7 扩大触发面：OCR 遮盖过的页面（链路 A 内嵌图命中 / 链路 B
        # 扫描页画块）一并光栅化——遮盖块只是视觉叠加，底层图片对象的
        # 原始像素仍可被提取还原，必须物理移除。
        raster_targets = sorted(set(leaked_pages) | raster_pages)
        for pno in raster_targets:
            builder.mark_raster(pno)  # v1.9.4：光栅化判定照搬进 plan（重放不重跑）
        if raster_targets:
            done = 0
            # 净版文档（源文件重开、未画任何遮盖）供 custom 词
            # 缺字兜底重扫——印章黑框先画会吞掉实况条带 OCR 的落款行（案例语料
            # 首跑实测）。打开失败仅降级（无净版兜底），留痕不阻塞。
            pristine_doc = None
            # 净版文档（源文件重开、未画任何遮盖）供 custom 词
            # 缺字兜底重扫——印章黑框先画会吞掉实况条带 OCR 的落款行（案例语料
            # 首跑实测）。打开失败仅降级（无净版兜底），留痕不阻塞。
            # 二轮收窄：词源=仅 UI 词（_word_keys）；零 UI 词=
            # 通道整体关闭，净版文档不开（39 份 brands 口径 elapsed 回归
            # v202 基线的关键闸）。
            if options.deep_recheck and get_ocr is not None \
                    and _custom_gap_word_keys(engine):
                try:
                    pristine_doc = fitz.open(str(src))
                except Exception as e:
                    report.log(f"⚠ 净版文档打开失败（缺字兜底降级跳过）："
                               f"[{type(e).__name__}] {e}")
            for pno in raster_targets:
                # v1.7 条带放大复检：光栅化前的最后一道兜底，检出整页 OCR
                # 漏掉的 logo 装饰小字/slogan（含横幅连带遮盖）
                if options.deep_recheck:  # v1.7.4 快速模式跳过（治横幅小字残字，耗时大头）
                    hits += _band_recheck(doc[pno], engine, get_ocr, report,
                                          pno, fitz, plan=builder,
                                          options=options)
                    # 净版条带 custom 词缺字兜底（配置词风雨无阻；
                    # 词源仅 UI 词，零 UI 词时 pristine_doc 恒
                    # None=本调用零发生）
                    if pristine_doc is not None \
                            and pno < pristine_doc.page_count:
                        hits += _custom_word_pristine_band(
                            doc[pno], pristine_doc[pno], engine, get_ocr,
                            report, pno, fitz, plan=builder)
                # v1.8.0：_mask_red_text_bands 撤除（设计决策
                # 「红字=批注不遮，按既定口径收敛」）——机制语义与该口径冲突；
                # 且红字行排除生效后实测零遮蔽产出（映射零行）
                # 而耗时 30.1s/轮（2.5%）。红章仍由 stamp 通道独立处理。
                if _rasterize_page(doc[pno]):
                    done += 1
            n_leaks = sum(len(v) for v in leaked_pages.values())
            if leaked_pages:
                report.log(f"⚠ {len(leaked_pages)} 页存在特殊内容流（文本层无法直接销毁，"
                           f"涉及 {n_leaks} 处），已整页转为图片以彻底脱敏（视觉布局不变）。")
            report.log(f"已将 {done}/{len(raster_targets)} 页整页转为图片"
                       f"（销毁被遮盖的原始图片像素，防止提取还原）。")
            if done < len(raster_targets):
                report.log(f"⚠ 其中 {len(raster_targets) - done} 页光栅化失败，"
                           f"该部分仅做了视觉遮盖，请人工复核。")
            if pristine_doc is not None:
                pristine_doc.close()
        summary = f"原生文本 {native_pages} 页 / OCR {ocr_pages} 页"
        if skipped_pages:
            summary += f" / 未启用OCR跳过 {skipped_pages} 页"
        report.log(f"阶段 2/2：保存脱敏 PDF（命中 {hits} 处；{summary}）…")
        if native_pages == 0 and skipped_pages:
            # T2：本警告仅在「未勾选 OCR」时可达（get_ocr is None ⇔ 复选框关闭，
            # 引擎初始化失败走页级异常计数而非 skipped），v2.0.0 exe 内置
            # PP-OCRv6-ONNX 引擎，旧「改用源码版/安装 Paddle」出路不实已废——
            # 正确出路是勾选 OCR（内置引擎）。
            if getattr(options, "words_only", False):
                report.log("⚠ 当前为「仅脱敏这些词」模式：扫描件文字无法识别，"
                           "如需遮盖图内指定词请勾选「图片内文字（OCR）」后重跑。")
            report.log("⚠ 本文件页面均为图片型（扫描件），文字未被脱敏。"
                       "当前未勾选“图片内文字（OCR）”：勾选后即可用内置 "
                       "PP-OCRv6-ONNX 引擎（默认，无需安装组件）识别并遮盖图片内文字。")
        if low_conf_total:
            report.log(f"低置信度标记 {low_conf_total} 条（未强制脱敏，建议人工复核）")
        doc.save(str(dst), garbage=3, deflate=True)
        _finish_analysis_cache(get_ocr, report)  # v1.9.0：文件级缓存落盘+命中统计
        # v1.9.4：首轮冻结 plan 落盘（复核轮零检测重放基线；纯旁路，失败仅留痕）
        try:
            builder.snapshot_filename(engine.filename_replacement_pairs())
            builder.snapshot_table_rows(report.records)
            plan_path = builder.save(masking_plan.output_root(src, options))
            report.log(f"已落盘首轮冻结 plan（复核轮零检测重放基线）："
                       f"cache/plans/{Path(plan_path).name}")
        except Exception as e:
            report.log(f"⚠ 冻结 plan 落盘失败（不影响本次结果）："
                       f"[{type(e).__name__}] {e}")
        # 页级异常此前被静默
        # 跳过且文件仍计「成功」——error 页零遮蔽却顶着「_脱敏后」产出，
        # 是最直接的漏遮汇报通路。>30% 页失败时显著警告（ok/fail 语义不
        # 变，避免回归口径漂移；字段+日志层修复）。
        if error_pages and total_pages and error_pages / total_pages > 0.3:
            report.log(f"⚠⚠ 警告：本文件 {error_pages}/{total_pages} 页"
                       f"（{error_pages * 100 // total_pages}%）处理异常被跳过、"
                       f"这些页面【零遮蔽】！输出文件不可作为已脱敏稿使用，"
                       f"请检查上文逐页错误信息后重试或反馈。")
        # 批内文本复用面（后台候选重扫入口；纯读 builder，
        # 失败仅留痕降级，不影响产物）
        try:
            _bt = builder.page_texts_joined()
            if _bt:
                report.extracted_text = _bt
        except Exception as e:
            report.log(f"⚠ 批内文本采集失败（候选重扫回落重新解析）："
                       f"[{type(e).__name__}] {e}")
        return {"masked": hits, "pages": total_pages,
                "error_pages": error_pages}
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# v1.9.4 冻结复核轮：按首轮 plan 零 OCR 零检测重放（契约 §2/蓝图 §3.1）
# ---------------------------------------------------------------------------

def _plan_ocr_ctx(pg) -> dict:
    """页 OCR 节点 → {items, offsets, joined}；无 items 返回 None。

    items 为 plan 存储的 good items 快照（页坐标、阅读序），重放据此走与
    首轮同一个行拆分绘制函数——零渲染零 OCR（蓝图 D1 自包含设计）。
    """
    node = pg.get("ocr") or {}
    raw = node.get("items") or []
    if not raw:
        return None
    from ..ocr_engine import OCRLine
    items = [OCRLine(text=r[0], bbox=(r[1], r[2], r[3], r[4]),
                     confidence=float(r[5])) for r in raw]
    offsets = []
    pos = 0
    for idx, it in enumerate(items):
        offsets.append((it, pos, idx))
        pos += len(it.text)
    return {"items": items, "offsets": offsets,
            "joined": "".join(it.text for it in items)}


def _op_counted(op) -> bool:
    """重放 op 是否计入 masked（V-C 缺陷 2 修复点）。

    口径对齐首轮 v1.9.3：banner（横幅补扫，返回值在 _mask_embedded_images
    调用点被弃用）与 visual_recheck（视觉复检，返回值仅作 leaked 清单）两
    通道首轮本就不计入 hits，重放同规则（masking_plan.PLAN_UNCOUNTED_RULES）。
    """
    return op.get("rule", "") not in masking_plan.PLAN_UNCOUNTED_RULES


def _is_raster_stage_op(op) -> bool:
    """op 是否属于首轮光栅化阶段（全部页处理完之后）才应用的指令
    （band_recheck；V-C 缺陷 3 修复点——页内重放会使其映射行插队）。"""
    return (op.get("t") == "rect"
            and op.get("rule", "") in masking_plan.PLAN_RASTER_STAGE_RULES)


def _apply_plan_ops(page, ops, report, pno, fitz, ocr_ctx,
                    deferred=None) -> int:
    """按首轮应用序重放 ops 指令（direct.rect / ocr.joined_spans）。

    连续同 kind 的 redact 批合并为一次 apply_redactions——与首轮「同通道
    一次 apply」语义一致（stamp=redact+纯黑块；redact_draw=redact+画块）。
    deferred 非 None 时，光栅化阶段指令（band_recheck，存在页 ops 尾部）
    不随页内重放，按页归集交调用方在光栅化阶段应用（与首轮 process()
    尾部「先 band_recheck 后光栅化」结构同构，重放映射行序与首轮全等）；
    此时传 ops 列表而非整页节点，光栅化阶段以 deferred=None 回放归集指令。
    返回计入 masked 的指令数（banner/visual_recheck 不计，口径同首轮）。
    """
    if deferred is not None:
        held = [o for o in ops if _is_raster_stage_op(o)]
        if held:
            deferred.setdefault(pno, []).extend(held)
            ops = [o for o in ops if not _is_raster_stage_op(o)]
    n = len(ops)
    hits = 0
    i = 0
    while i < n:
        op = ops[i]
        if op.get("t") == "jspan":
            if ocr_ctx is None:
                i += 1
                continue  # OCR 节点缺失（不应发生），跳过并留痕于计数差
            spans = [(int(op.get("s", 0)), int(op.get("e", 0)),
                      op.get("rule", ""), op.get("repl", ""),
                      op.get("conf", ""), op.get("orig", ""))]
            n_hit = _draw_spans_on_lines(
                page, ocr_ctx["items"], ocr_ctx["offsets"], spans, None,
                report, pno, op.get("tag", "(OCR)"), plan=None,
                preset=op.get("p", "m"))
            if _op_counted(op):
                hits += n_hit
            i += 1
            continue
        # —— rect 族：收集连续同 kind 成组（同通道一次 apply）——
        kind = op.get("k", "draw")
        group = []
        while i < n and ops[i].get("t") == "rect" \
                and ops[i].get("k", "draw") == kind:
            group.append(ops[i])
            i += 1
        if kind in ("stamp", "redact_draw"):
            try:
                for o in group:
                    page.add_redact_annot(fitz.Rect(o["rect"]))
                if group:
                    page.apply_redactions()
            except Exception as e:
                report.log(f"  第 {pno + 1} 页重放 redaction 失败"
                           f"（退化为仅画块）：[{type(e).__name__}] {e}")
            for o in group:
                if kind == "stamp":
                    _draw_black(page, o["rect"])
                else:
                    _draw_replacement(page, o["rect"], o.get("repl", ""))
                report.record(original=o.get("orig", ""),
                              replacement=o.get("rec", ""),
                              rule=o.get("rule", ""),
                              location=o.get("loc", ""))
        else:  # draw
            for o in group:
                _draw_replacement(page, o["rect"], o.get("repl", ""))
                report.record(original=o.get("orig", ""),
                              replacement=o.get("rec", ""),
                              rule=o.get("rule", ""),
                              location=o.get("loc", ""))
        hits += sum(1 for o in group if _op_counted(o))
    return hits


def _replay_native_page(page, pg, engine, report, pno, fitz, adds,
                        add_stat, deferred=None) -> int:
    """重放原生文本页：文本层校验 → native span 绘制（与首轮同函数）→
    ops 指令 → 增行键定位。文本层与冻结 plan 不一致返回 None（调用方
    逐页回落 v1.9.3 现行分支，蓝图 §3.1.1）。"""
    text, char_boxes = _extract_chars(page)
    if text != pg.get("text"):
        return None
    spans = []
    for s, e, rule, repl, conf, orig in pg.get("native_spans") or []:
        spans.append(ocr_mod_rules.SensitiveSpan(int(s), int(e), rule, orig,
                                                 repl, conf))
    occupied = [(sp.start, sp.end) for sp in spans]
    for key, repl, rule in adds:
        for s0, e0 in masking_plan.find_add_occurrences(text, key, occupied):
            occupied.append((s0, e0))
            spans.append(ocr_mod_rules.SensitiveSpan(s0, e0, rule, key, repl,
                                                     ""))
            add_stat[key] = add_stat.get(key, 0) + 1
    spans.sort(key=lambda sp: sp.start)
    hits = len(_apply_native_spans(page, text, char_boxes, spans, report, pno))
    # 原生页内嵌图的 OCR 节点（items + joined_spans 指令）：与首轮同一行拆分绘制
    ocr_ctx = _plan_ocr_ctx(pg)
    hits += _apply_plan_ops(page, pg.get("ops") or [], report, pno, fitz,
                            ocr_ctx, deferred)
    # 原生页内嵌图的 OCR joined 增行定位（图内文字通道）
    if ocr_ctx is not None and adds:
        occupied = [(op.get("s", 0), op.get("e", 0))
                    for op in pg.get("ops") or [] if op.get("t") == "jspan"]
        new_spans = []
        for key, repl, rule in adds:
            for s0, e0 in masking_plan.find_add_occurrences(
                    ocr_ctx["joined"], key, occupied):
                occupied.append((s0, e0))
                new_spans.append((s0, e0, rule, repl, "", key))
                add_stat[key] = add_stat.get(key, 0) + 1
        if new_spans:
            hits += _draw_spans_on_lines(page, ocr_ctx["items"],
                                         ocr_ctx["offsets"], new_spans, None,
                                         report, pno, "(图内文字)")
    return hits


def _replay_ocr_page(page, pg, engine, report, pno, fitz, adds,
                     add_stat, deferred=None) -> int:
    """重放扫描/图片页：零渲染零 OCR——items+ops 全部来自 plan 快照。"""
    ocr_ctx = _plan_ocr_ctx(pg)
    hits = _apply_plan_ops(page, pg.get("ops") or [], report, pno, fitz,
                           ocr_ctx, deferred)
    if ocr_ctx is not None and adds:
        # 增行键在 OCR joined 文本上定位（契约 §3：plan 每页文本定位）
        occupied = [(op.get("s", 0), op.get("e", 0))
                    for op in pg.get("ops") or [] if op.get("t") == "jspan"]
        new_spans = []
        for key, repl, rule in adds:
            for s0, e0 in masking_plan.find_add_occurrences(
                    ocr_ctx["joined"], key, occupied):
                occupied.append((s0, e0))
                new_spans.append((s0, e0, rule, repl, "", key))
                add_stat[key] = add_stat.get(key, 0) + 1
        if new_spans:
            hits += _draw_spans_on_lines(page, ocr_ctx["items"],
                                         ocr_ctx["offsets"], new_spans, None,
                                         report, pno, "(OCR)")
    return hits


def _replay_page_fallback(doc, pno, engine, options, report, fitz, get_ocr,
                          plan, src):
    """单页回落 v1.9.3 现行分支并修 plan 该页（蓝图 §3.1.1/§5.6：PyMuPDF
    渲染漂移等导致的文本失配页，重放期即时检测修复）。返回命中数或 None。"""
    try:
        builder = masking_plan.PlanBuilder(src, options)
        page = doc[pno]
        page_hits, mode, low_conf, leaked, need_raster = _process_page(
            page, engine, options, get_ocr, report, pno, fitz, plan=builder)
        builder.set_page(pno, mode, page_hits, low_conf)
        pg = builder.export_page(pno)
        pg["raster"] = need_raster or bool(leaked)
        plan.replace_page(pno, pg)
        if pg["raster"] and options.deep_recheck and get_ocr is not None:
            # 与首轮光栅化阶段同序：条带复检兜底（回落页按现行分支补跑）
            try:
                _band_recheck(doc[pno], engine, get_ocr, report, pno, fitz,
                              options=options)

            except Exception:
                pass
            # 净版条带 custom 词缺字兜底同域补跑（与首轮光栅化
            # 阶段同构；净版打开失败仅降级）。收窄口径：词源仅 UI 词，
            # 零 UI 词时不开净版文档（与首轮调用点同一关闭闸）。
            try:
                if _custom_gap_word_keys(engine):
                    pdoc = fitz.open(str(src))
                    try:
                        if pno < pdoc.page_count:
                            _custom_word_pristine_band(
                                doc[pno], pdoc[pno], engine, get_ocr, report,
                                pno, fitz)
                    finally:
                        pdoc.close()
            except Exception:
                pass
        return int(page_hits or 0)
    except Exception as e:
        report.log(f"  第 {pno + 1} 页回落实时检测也失败"
                   f"（保留该页首轮冻结指令状态）：[{type(e).__name__}] {e}")
        return None


def _op_mark(report) -> int:
    """台账 op 采集游标：report 无 op_records 时返回 0（纯旁路，
    兼容测试桩 report）。"""
    recs = getattr(report, "op_records", None)
    return len(recs) if recs else 0


def _plan_ledger_snapshot(plan) -> list:
    """pre-edit 指令快照 → 遮蔽台账基准行（首轮烧入规范序）。

    行序=首轮 record 应用序：逐页（native_spans → 页内 ops 存储序），尾部
    光栅阶段指令（band_recheck，页升序）——与首轮「全部页→光栅化阶段」两段
    结构、与重放应用序（_replay_from_plan）一致；plan op ↔ record 一一对应
    （T1 归因报告 §5 三方对账结论）。行=(pno, stage, 位置, 规则, 原文, 替换)。
    """
    rows = []
    for pg in plan.pages:
        pno = int(pg.get("pno", 0))
        for s in pg.get("native_spans") or []:
            # [start, end, rule, replacement, confidence, text]
            rows.append((pno, "page", f"第{pno + 1}页", s[2], s[5], s[3]))
        for op in pg.get("ops") or []:
            if _is_raster_stage_op(op):
                continue
            if op.get("t") == "jspan":
                rows.append((pno, "page",
                             f"第{pno + 1}页{op.get('tag', '(OCR)')}",
                             op.get("rule", ""), op.get("orig", ""),
                             op.get("repl", "")))
            else:  # rect：映射记录替换文本取 rec（凭证/印章行与绘制 repl 不同）
                rows.append((pno, "page", op.get("loc", ""), op.get("rule", ""),
                             op.get("orig", ""), op.get("rec", "")))
    for pg in plan.pages:
        pno = int(pg.get("pno", 0))
        for op in pg.get("ops") or []:
            if _is_raster_stage_op(op):
                rows.append((pno, "raster", op.get("loc", ""), op.get("rule", ""),
                             op.get("orig", ""), op.get("rec", "")))
    return rows


def _ledger_rows_after_edits(plan_ledger, deletes, changes, repaired_windows,
                             replay_op_base, replay_records) -> tuple:
    """编辑保存点的台账三态流转（v2.0.1 C1）：删除→deleted/改值→edited/
    增行新画块→新增 active 行。

    - plan_ledger：pre-edit 指令快照行（_plan_ledger_snapshot，首轮烧入规范
      序，幸存行 op_id 复用首轮序）；
    - deletes/changes：按原文全局编辑算子（apply_plan_edits 同源口径）；
    - repaired_windows：回落重检页 {pno: (起, 止) op_records 绝对下标窗}——
      该页首轮行弃用，按当轮重检记录重建（回落语义按页顺延）；
    - replay_records：本轮 op_records 切片——未匹配幸存快照行（多重集对账）
      的记录即增行产生的新画块；「仅原位」行（防呆拒绝键）非画块不入账。

    返回 (rows, stats)：rows=[(位置, 规则, 原文, 替换, op_id, 状态)]（1-based
    连续 op_id）；stats={deleted, edited, added, re_pages}。
    """
    from ..dispatcher import LEDGER_ACTIVE, LEDGER_DELETED, LEDGER_EDITED
    deletes = set(deletes or ())
    changes = dict(changes or {})
    repaired_pnos = set(repaired_windows or {})
    # 回落重检页：标记当轮记录窗并按页归集（换算为 replay_records 相对下标）
    in_window = [False] * len(replay_records)
    re_rows_by_pno: dict = {}
    for pno, (s, e) in (repaired_windows or {}).items():
        for i in range(max(0, s - replay_op_base),
                       min(len(replay_records), e - replay_op_base)):
            in_window[i] = True
            r = replay_records[i]
            if r[3] != "仅原位":
                re_rows_by_pno.setdefault(pno, []).append(r)
    # 幸存行三态标注（回落重检页的首轮行弃用）
    page_rows: dict = {}  # pno -> [位置, 规则, 原文, 替换, 状态]
    raster_rows: list = []
    surviving_multiset: dict = {}
    for pno, stage, loc, rule, orig, repl in plan_ledger:
        if pno in repaired_pnos:
            continue
        if orig in deletes:
            status = LEDGER_DELETED
        elif orig in changes:
            repl, status = changes[orig], LEDGER_EDITED
        else:
            status = LEDGER_ACTIVE
        (raster_rows if stage == "raster" else page_rows.setdefault(pno, [])) \
            .append([loc, rule, orig, repl, status])
        if status != LEDGER_DELETED:
            key = (loc, rule, orig, repl)
            surviving_multiset[key] = surviving_multiset.get(key, 0) + 1
    # 增行新画块 = 非回落页重放记录 − 幸存快照行（多重集扣减，容忍同矩形
    # 重复绘制行）；余量即本轮新增
    add_records = []
    for i, r in enumerate(replay_records):
        if in_window[i] or r[3] == "仅原位":
            continue
        key = (r[3], r[2], r[0], r[1])  # (位置, 规则, 原文, 替换)
        if surviving_multiset.get(key, 0) > 0:
            surviving_multiset[key] -= 1
        else:
            add_records.append(r)
    # 组装：页升序（幸存三态行 / 回落页当轮重检行）→ 光栅阶段行 → 增行行，
    # op_id 全表 1-based 连续重编（幸存行相对序=首轮规范序）。
    # 注意 record 元组序=(原文,替换,规则,位置)，入账须重排为台账列序。
    def _rec_to_row(r, status):
        return [r[3], r[2], r[0], r[1], status]

    ordered: list = []
    for pno in sorted(set(page_rows) | set(re_rows_by_pno)):
        ordered.extend(page_rows.get(pno) or [])
        ordered.extend([_rec_to_row(r, LEDGER_ACTIVE)
                        for r in re_rows_by_pno.get(pno) or []])
    ordered.extend(raster_rows)
    ordered.extend([_rec_to_row(r, LEDGER_ACTIVE) for r in add_records])
    rows = [(loc, rule, orig, repl, k, status)
            for k, (loc, rule, orig, repl, status) in enumerate(ordered, 1)]
    stats = {"deleted": sum(1 for r in rows if r[5] == LEDGER_DELETED),
             "edited": sum(1 for r in rows if r[5] == LEDGER_EDITED),
             "added": len(add_records),
             "re_pages": len(repaired_windows or {})}
    return rows, stats


def _replay_from_plan(doc, src, dst, plan, engine, options, report, fitz) -> dict:
    """冻结复核轮主流程（契约 §2）：头部四键已由 process() 校验通过。

    - 零 OCR 零检测：native 页文本校验后走 _apply_native_spans（与首轮
      同码），OCR 页喂 plan 自包含 items；direct/jspan 指令按首轮应用序
      重放；raster 集合照搬首轮（不重跑复检）；
    - 编辑算子（dispatcher 预计算 engine.plan_edits）：删除/改值在 plan 上
      按原文全局生效；增行过防呆闸后对每页文本定位产生新指令；
    - 逐页回落：native 文本失配/重放异常 → 该页 v1.9.3 现行分支+修 plan；
    - 文件名收尾经 report.filename_pairs_override 交给 dispatcher
      （finalize_output_name 原样应用+去重碰撞）。
    """
    edits = getattr(engine, "plan_edits", None) or {}
    deletes = list(edits.get("deletes") or ())
    changes = dict(edits.get("changes") or {})
    adds = [(k, r, "mapping" if k in engine._mapping else "custom")
            for k, r in (edits.get("adds") or ())]
    rejected = list(edits.get("rejected") or ())
    # v2.0.1 C1：冻结重放轮旗标——dispatcher 据此把「零编辑重放」判为台账
    # 沿用首轮不重写（零新增）。
    report.ledger_replay_round = True
    # v2.0.1 C1 编辑保存点（apply_plan_edits 调用点）：编辑指令集（删/改/增
    # 任一非空）才建 pre-edit 快照（首轮烧入规范序，幸存行 op_id 复用首轮序
    # 的基准）；是否真重写台账由编辑「落地结果」判定（见函数尾部）——存量
    # v1.9.4 语义下映射表文件名行会以零命中增行键形态进入指令集（add_stat
    # 恒空），按 outcomes 判定才能保「零编辑重放零新增不重写」。
    ledger_edit_candidate = bool(deletes or changes or adds)
    plan_ledger = _plan_ledger_snapshot(plan) if ledger_edit_candidate else None
    replay_op_base = _op_mark(report)
    repaired_op_windows: dict = {}  # pno -> (起, 止)：回落重检页当轮记录窗
    report.log(f"冻结复核：命中首轮冻结 plan（{plan.page_count} 页），"
               "零 OCR 零检测，按首轮冻结指令重放。")
    # 选项漂移提示（蓝图 §4.2：指令按首轮口径重放，当轮选项仅作用 plan 外页）
    fropts = plan.header.get("first_round_options") or {}
    # 人名/公司开关入漂移提示（复检词集已按开关分桶，
    # 关闭后复检轮语义变化须让用户可见）
    drift = [name for key, name in (
        ("mask_amount", "金额脱敏"), ("mask_stamp", "印章遮盖"),
        ("deep_recheck", "深度复检"), ("ocr_image_text", "图片内文字脱敏"),
        ("mask_person", "人名脱敏"), ("mask_company", "公司脱敏"),
        ("words_only", "仅指定词模式"))  # v2.1.8 模式维度入漂移提示
        if key in fropts
        and bool(fropts[key]) != bool(getattr(options, key, None))]
    if "ocr_min_confidence" in fropts and options.ocr_image_text \
            and float(fropts.get("ocr_min_confidence") or 0) \
            != float(getattr(options, "ocr_min_confidence", 0) or 0):
        drift.append("OCR 置信度阈值")
    if drift:
        report.log(f"提示：本轮选项（{'、'.join(drift)}）与首轮不同，"
                   "不改变已冻结的首轮内容（按首轮口径重放）。")
    try:
        plan.register_confidences(engine)  # 置信度回注（契约 §8）
    except Exception:
        pass
    masking_plan.apply_plan_edits(plan, deletes, changes, report_log=report.log)

    hits = 0
    add_stat = {}
    deferred: dict = {}  # pno → [op]：首轮光栅化阶段才应用的指令（band_recheck）
    raster_targets = []
    repaired_pages = 0
    # 惰性 getter：仅在发生逐页回落且需要 OCR 时才会真正初始化引擎
    get_ocr = (_make_ocr_getter(report, src=src, rule_engine=engine,
                                options=options)
               if options.ocr_image_text else None)
    for pg in plan.pages:
        pno = int(pg.get("pno", 0))
        if pno >= doc.page_count:
            report.log(f"⚠ 冻结 plan 页码 {pno} 超出文档页数"
                       f"（{doc.page_count}），跳过该页。")
            continue
        page = doc[pno]
        mode = pg.get("mode")
        n = 0
        page_op_mark = _op_mark(report)  # v2.0.1 C1：页级记录游标（回落窗起点）
        page_repaired = False
        try:
            if mode == "native":
                n = _replay_native_page(page, pg, engine, report, pno, fitz,
                                        adds, add_stat, deferred)
                if n is None:
                    report.log(f"  第 {pno + 1} 页文本层与冻结 plan 不一致"
                               "（渲染器差异？），该页回落实时检测并修复 plan。")
                    deferred.pop(pno, None)  # 回落页整体重检，弃用已归集指令
                    page_repaired = True
                    n = _replay_page_fallback(doc, pno, engine, options,
                                              report, fitz, get_ocr, plan, src)
                    if n is not None:
                        repaired_pages += 1
            elif mode == "ocr":
                n = _replay_ocr_page(page, pg, engine, report, pno, fitz,
                                     adds, add_stat, deferred)
            elif mode == "error":
                # 稳定体验补项：
                # 首轮 error 页此前在重放轮「零工作」静默跳过——复核轮输出
                # 与首轮同样顶着「_脱敏后」却少遮了这些页。至少显式留痕，
                # 让复核方知道该页从未被处理（真 fallback=逐页实时检测，
                # 需 OCR 上下文重构，登记待处理）
                report.log(f"  第 {pno + 1} 页首轮处理异常（error），重放轮"
                           f"无冻结指令可回放——该页【零遮蔽】，如需覆盖"
                           f"请以该页所在文件重跑完整脱敏。")
        except Exception as e:
            import traceback as _tb
            report.log(f"  第 {pno + 1} 页冻结重放异常（[{type(e).__name__}] "
                       f"{e}），该页回落实时检测（v1.9.3 语义）并修复 plan。"
                       " || " + _tb.format_exc()[-300:])
            deferred.pop(pno, None)  # 回落页整体重检（含页内即时条带复检），
            page_repaired = True
            n = _replay_page_fallback(doc, pno, engine, options, report,
                                      fitz, get_ocr, plan, src)
            if n is not None:
                repaired_pages += 1
        if page_repaired:  # v2.0.1 C1：回落页当轮记录窗（含重放半程残留记录）
            repaired_op_windows[pno] = (page_op_mark, _op_mark(report))
        hits += int(n or 0)
        cur = plan.page_by_pno(pno) or pg
        if cur.get("raster"):
            raster_targets.append(pno)
        report.progress(pno + 1, plan.page_count)

    if repaired_pages:
        # v2.0.1 C1：存在回落重检页——当轮实际画块已偏离首轮指令（重检页新
        # 画块），台账不得再视为「纯重放沿用首轮」，按当轮实际画块落盘
        # （零编辑时 dispatcher 走 op_records 默认口径，行=当轮真实遮蔽）。
        report.ledger_replay_round = False

    if raster_targets or deferred:
        done = 0
        raster_set = set(raster_targets)
        # 首轮同构（process() 尾部）：光栅化阶段逐页「先 band_recheck 指令、
        # 后整页光栅化」——band 通道映射行落位与首轮全等（V-C 缺陷 3 修复点；
        # deferred 页序=raster 页序=首轮光栅化序，均按 pno 升序）
        for pno in sorted(raster_set | set(deferred.keys())):
            dops = deferred.get(pno) or []
            if dops:
                hits += _apply_plan_ops(doc[pno], dops, report, pno, fitz, None)
            if pno in raster_set and _rasterize_page(doc[pno]):
                done += 1
        report.log(f"已将 {done}/{len(raster_targets)} 页整页转为图片"
                   "（照搬首轮冻结判定，销毁底层内容防提取还原）。")
        if done < len(raster_targets):
            report.log(f"⚠ 其中 {len(raster_targets) - done} 页光栅化失败，"
                       "该部分仅做了视觉遮盖，请人工复核。")
    # 防呆拒绝键：不产生任何自动指令，仅表内留痕
    if rejected:
        for k, repl, reason in rejected:
            report.record(original=k, replacement=repl, rule="mapping",
                          location="仅原位")
        report.log("该键过于通用，仅应用于当前位置（该键仅记录于映射表，"
                   "不产生自动遮蔽指令）："
                   + "；".join(f"「{k}」（{reason}）" for k, _r, reason in rejected))
    if add_stat:
        report.log("增行键已按冻结文本全局定位遮蔽："
                   + "、".join(f"「{k}」{cnt} 处" for k, cnt
                               in sorted(add_stat.items(),
                                         key=lambda kv: (-len(kv[0]), kv[0]))))
    # 文件名收尾：首轮快照 ⊕ 编辑（幸存过滤/改值烤入快照，增行/无关编辑行
    # 不进文件名对——M4 缺陷 1 修复口径），由 dispatcher.finalize_output_name
    # 按同一应用序落名（蓝图 §3.3）
    merged = masking_plan.merge_filename_pairs(plan.filename.get("pairs"))
    rej_set = {k for k, _r, _w in rejected}
    report.filename_pairs_override = [(o, r) for o, r in merged
                                      if o not in rej_set]
    # v2.0.1 C1 编辑保存点：按编辑后指令集重写台账行（三态流转：删除→
    # deleted、改值→edited、增行新画块→新增 active；回落重检页按当轮记录
    # 重建）。重写判定看编辑落地结果（删除/改值指令在集，或增行确有新画块
    # add_stat 非空）——零编辑重放零新增不重写，台账沿用首轮产物；重写行经
    # report.ledger_rows_override 交 dispatcher 聚合区落盘。
    if ledger_edit_candidate and (deletes or changes or add_stat):
        try:
            ledger_rows, led_stats = _ledger_rows_after_edits(
                plan_ledger, deletes, changes, repaired_op_windows,
                replay_op_base,
                (getattr(report, "op_records", None) or [])[replay_op_base:])
            report.ledger_rows_override = ledger_rows
            report.log("遮蔽台账已按编辑后指令集更新（编辑保存点重写）："
                       f"删除 {led_stats['deleted']} 行→deleted、"
                       f"改值 {led_stats['edited']} 行→edited、"
                       f"增行 {led_stats['added']} 行→新增 active"
                       + (f"；{led_stats['re_pages']} 页回落重检按当轮记录重建"
                          if led_stats["re_pages"] else "") + "。")
        except Exception as e:
            report.log(f"⚠ 遮蔽台账编辑重写失败（不影响本次脱敏结果）："
                       f"[{type(e).__name__}] {e}")
    report.log(f"阶段 2/2：保存脱敏 PDF（重放遮蔽 {hits} 处；零 OCR 零检测）…")
    doc.save(str(dst), garbage=3, deflate=True)
    _finish_analysis_cache(get_ocr, report)
    if repaired_pages:
        try:
            plan.save(src, masking_plan.output_root(src, options))
            report.log(f"冻结 plan 已因 {repaired_pages} 页回落修复而更新"
                       "（成为新冻结基线）。")
        except Exception as e:
            report.log(f"⚠ 冻结 plan 更新落盘失败（不影响本次结果）："
                       f"[{type(e).__name__}] {e}")
    # v2.1.7 B3-3：批内文本复用面（重放轮——plan 自包含文本，纯读旁路）
    try:
        _bt = plan.texts_joined()
        if _bt:
            report.extracted_text = _bt
    except Exception:
        pass
    return {"masked": hits, "pages": plan.page_count}


# ---------------------------------------------------------------------------
# 链路分流
# ---------------------------------------------------------------------------

def _process_page(page, engine, options, get_ocr, report, pno, fitz,
                  plan=None) -> tuple:
    """返回 (命中数, 链路模式, 低置信度条数, redact未销毁清单, 待光栅化标记)。

    plan（v1.9.4）：首轮冻结 plan 采集器；None 时不采集（重放轮逐页回落
    修复传入局部采集器）。埋点与 report.record 同分支，纯旁路零行为变化。
    """
    text, char_boxes = _extract_chars(page)
    need_raster = False

    if len(text) >= NATIVE_MIN_CHARS:
        # —— 链路 A：原生文本 PDF ——
        hits, leaked = _mask_native(page, text, char_boxes, engine, report, pno,
                                    plan=plan)
        # 金额口径统一为遮——文本层列对齐金额兜底：
        # 表头含「标准/金额/费用…（元…）」的表格，其下同列裸数字（550/450 类
        # 制度参数值）常规金额规则因无单位不命中，造成同文档口径不一致
        # v2.0.1 金额闸：调用点传 options——mask_amount=False 时本通道零 op
        hits += _mask_text_amount_columns(page, fitz, report, pno, options,
                                          plan=plan)
        if _stamp_on(options):
            hits += _mask_stamps_on_page(page, fitz, report, pno, plan=plan)
        # v1.6.7：原生页内嵌截图/配图 OCR 遮盖（页内图片不随文本
        # redact 销毁，收款截图/附件表头等图片文字此前从不识别）
        if get_ocr is not None:
            emb_hits = _mask_embedded_images(page, engine, options, get_ocr,
                                             report, pno, fitz, plan=plan)
            hits += emb_hits
            # v1.7 数据级销毁：OCR 遮盖块只是页面上的视觉叠加，嵌入图片
            # 对象的原始像素仍含原文（提取图片即可还原）。页面含嵌入图且
            # 该页有任何命中（文本/图内 OCR/表头列关联）即整页光栅化，
            # 物理移除原图对象——整页快照型 PDF 的图片层原文残字只有
            # 光栅化才能根除。
            if emb_hits or (hits and page.get_images()):
                need_raster = True
            # v1.6.9 视觉复检兜底：矢量转曲文字（文本层提取不到、无图对象、
            # redact 无效）与图内遮盖坐标偏移，靠整页 OCR 复查注册实体
            # 兜住——命中处画块并标记该页需光栅化
            if options.deep_recheck:  # v1.7.4 快速模式跳过（治转曲文字/坐标偏移残留）
                leaked += _visual_recheck(page, engine, get_ocr, report, pno,
                                          fitz, plan=plan, options=options)
        return hits, "native", 0, leaked, need_raster

    # —— 链路 B：扫描/图片型 PDF ——
    if get_ocr is None:
        return 0, "skipped", 0, [], False

    ocr = get_ocr()
    zoom = OCR_DPI / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    png = pix.tobytes("png")
    result = ocr.recognize(png, OCR_DPI)
    items = _select_items(result)  # 表格区域优先单元格级坐标，其余用行级坐标
    # v1.6.9：低置信行不再整行跳过——截图小字（DMS 界面/申请单）OCR 置信度
    # 常低于阈值，但识别文本若能精确命中实体规则（品牌/公司/人名/自定义词），
    # 匹配本身即正确性证明。无命中的低置信行仍不处理（维持计数供复核）。
    good, low_conf = [], 0
    for it in items:
        if not it.text.strip():
            continue
        if it.confidence >= options.ocr_min_confidence:
            good.append(it)
        else:
            low_conf += 1
            # 低置信救捞改
            # 无副作用判定——find_spans 非纯函数（品牌学习/简称注册/
            # _person_conf 回写），OCR 噪声行经此注册后批内全文档被打补丁
            # （复检污染闭环的根因）。此处
            # 仅需「能否命中」的布尔答案：三表快照回滚——救捞判定后恢复
            # 注册表面，学习只留给已过置信闸的 good 行（_mask_joined_ocr_
            # lines 正路）。
            _snap = (dict(engine._short_names), dict(engine._person_conf),
                     dict(engine._company_alias))
            _hit = bool(engine.find_spans(it.text))
            engine._short_names, engine._person_conf, engine._company_alias \
                = _snap
            if _hit:
                good.append(it)
    if plan is not None:
        # v1.9.4 埋点：OCR good items 快照（已过首轮置信闸、阅读序）——
        # 重放零 OCR 的行来源 + 增行定位的「每页文本」结构（蓝图 D1：
        # items 自包含，不回引 OCR 缓存）。
        plan.ensure_ocr(pno)["low_conf"] = low_conf
        plan.set_ocr_items(pno, [(it.text, it.bbox, it.confidence)
                                 for it in good])

    # v1.7 表头列纵向关联（OCR 行版）：扫描件变更记录表「批准人」列头与
    # 数据值隔着整行表头文本，行内前缀窗口够不到。表头词正下方 ≤60pt、
    # 横向重叠的 2~4 字纯 CJK 行（每表头只取最近一行）按 force_person
    # 校验后并入处理列表（黑名单/虚词闸门保留）。
    hdr_hits = 0
    try:
        import re as _re
        # 关键词表改用模块顶已有的 ocr_mod_rules 引用
        # （原此处为绝对包名 __import__ 动态导入 rules_engine，是打包器
        # 重定位隐患；sys.modules 中本就是同一对象，零行为变化）。
        _cjk_row = _re.compile(r"[一-龥]{2,4}$")
        _dbg_kw_hits = 0
        for hdr in list(items):
            # v1.9.1 渲染确定性：key=(-len, 词) 完全决定序——原 key=len,
            # reverse=True 在同长度关键词组内回落到 set 迭代序（随
            # PYTHONHASHSEED 漂移），同表头行命中多个等长关键词时先者胜的
            # 取舍不稳定
            for kw in sorted(set(list(ocr_mod_rules._KEYWORD_BEFORE)
                                 + list(ocr_mod_rules._FORM_FIELD_KEYS)),
                             key=lambda k: (-len(k), k)):
                ki = hdr.text.find(kw)
                if ki < 0:
                    continue
                _dbg_kw_hits += 1
                kx0, _, kx1, _ = _span_rect_in_line(hdr.bbox, hdr.text,
                                                    ki, ki + len(kw))
                cands = []
                for it2 in items:
                    if it2 is hdr or not it2.text.strip():
                        continue
                    gap = it2.bbox[1] - hdr.bbox[3]
                    if not (0 < gap <= 100):
                        continue
                    if not _cjk_row.fullmatch(it2.text.strip()):
                        continue
                    cx2 = (it2.bbox[0] + it2.bbox[2]) / 2
                    if kx0 - 8 <= cx2 <= kx1 + 8:
                        cands.append((gap, it2))
                for _, nearest in sorted(cands, key=lambda x: x[0]):
                    # 该行大概率已在 good 中（高置信），但拼接文本里孤立方块
                    # 无上下文证据不会命中——直接按行画遮盖块（变更记录表
                    # 同一列有多行数据，全部关联）
                    # 职务/职等行否决（二阶组织架构 p6「高级总监/
                    # 高级经理/工程师」数据列被当人名列整列遮蔽）
                    _nt = nearest.text.strip()
                    if any(k in _nt for k in _JOB_TITLE_WORDS):
                        continue
                    ok = engine.find_spans(nearest.text.strip(),
                                           force_person=True)
                    if not ok:
                        continue
                    rep = nearest.text.strip()[0] + "*" * (len(nearest.text.strip()) - 1)
                    rect = fitz.Rect(nearest.bbox[0] - 2, nearest.bbox[1] - 1,
                                     nearest.bbox[2] + 2, nearest.bbox[3] + 1)
                    _draw_replacement(page, rect, rep)
                    hdr_hits += 1
                    report.record(original=nearest.text.strip(), replacement=rep,
                                  rule="person",
                                  location=f"第{pno + 1}页(OCR·表头列)")
                    if plan is not None:  # v1.9.4 埋点：直画成品矩形
                        plan.add_op_rect(pno, "draw", rect, rep, rep,
                                         nearest.text.strip(), "person",
                                         f"第{pno + 1}页(OCR·表头列)",
                                         conf=engine.get_confidence(
                                             "person", nearest.text.strip()))
                break  # 每个表头行只按最长命中关键词关联一次
    except Exception as e:
        # 表头列关联失败留痕：静默失败=该页表头列人名可能漏遮且无解释（失败必须留痕）
        try:
            report.log(f"  第 {pno + 1} 页表头列关联遮盖失败：[{type(e).__name__}] {e}")
        except Exception:
            pass

    # v1.6.6：整页拼接识别——OCR 逐行跑引擎会把跨行名称拆断
    # （“示例丁厨柜有限/公司”分属两行，前半行无公司后缀即漏检）。
    # 拼接后统一 find_spans，跨行 span 再按行拆分遮盖。
    hits = _mask_joined_ocr_lines(page, good, engine, options, report, pno,
                                  "(OCR)", plan=plan)
    # v1.7.3：扫描表格金额列兜底（OCR 行 bbox 为页坐标，base 传 None 即页坐标）
    hits += _mask_ocr_amount_columns(page, good, [None] * len(good), engine,
                                     options, report, pno, fitz, "(OCR金额)",
                                     plan=plan)
    hits += hdr_hits  # v1.7：表头列关联遮盖计入命中（驱动 need_raster）
    if _stamp_on(options):
        hits += _mask_stamps_from_png(page, png, report, pno, plan=plan)
    # v1.7 数据级销毁：扫描页遮盖块同样只是视觉叠加，画过块的页面整页
    # 光栅化以移除原始扫描图对象（无命中页保留原样，不做无谓重建）
    return hits, "ocr", low_conf, [], hits > 0


def _custom_gap_word_keys(engine) -> set:
    """缺字兜底词源（收窄口径）：仅 UI 自定义敏感词
    （engine._word_keys，MaskOptions.custom_words 用户逐字显式配置）。

    口径边界（设计决策 v2.0.3 二轮，性能回归修复）：brands.json 品牌
    词（_static_brand_words）不参与缺字兜底——首版并集词源实测在零
    UI 词语料（brands_test 11 词）上对全部光栅化页无条件净版条带重扫，
    39 份口径 elapsed 1174→2192s（+87%）结构性回归。理由：UI 词=用户逐字
    显式配置，享受最强兜底（配置词风雨无阻）；brands 词由主链路精确匹配
    覆盖（v2.0.x 现状），用户如需品牌词兜底可经 UI 自定义词通道逐字配置
    （发布说明已注明）。映射表键/批内学习简称不扩面（同首版口径）。
    零 UI 词时返回空集=通道完全关闭：调用点不开净版文档，函数内零 OCR。
    """
    return set(getattr(engine, "_word_keys", ()) or ())


def _custom_word_gap_matches(text, gap_words, occupied):
    """缺字假设匹配（band/visual 复检通道共用）。

    对 gap_words（custom 词，调用方已按 (-len, 词) 排序）逐一做「单字缺失
    假设」：词去掉任一位置的一个字后，若剩余残串仍是行文本的连续子串，
    且该子串与 occupied（该行已有全词命中区间）及已收假设互不重叠，则
    产出一个缺字假设。返回 (词, 残串起, 残串止, 是否整行, 缺字位置) 元组
    列表；每词只取最左一处假设（确定性）。

    适用根因（案例语料实测）：OCR det 丢框（行左端截断）或 rec 丢字
    （行框完整但识别序列缺字）使行文本比真实版面少一字，custom 词连续
    子串不成立、全词通道零候选。缺字假设以「残串连续命中 + 恰缺一字」
    为证据恢复候选；假设本质是概率性的，误报面经回归评估留档
    （配置词风雨无阻——查全优先，设计决策）。
    """
    out = []
    taken = list(occupied or [])
    n = len(text)
    # 行级闸：行内已含任一配置词全词 ⇒ 该行是「完整可读行」（首轮精确
    # 匹配通道已按全词处理），缺字假设不再参与——否则更长短词的去字残串
    # 会在已遮蔽区间上重复出补丁（台账噪声）。残行才走缺字假设。
    if any(w in text for w in gap_words):
        return out
    for w in gap_words:
        if len(w) < CUSTOM_GAP_MIN_WORD or w in text:
            continue  # 词太短，或全词本就命中（全词通道已处理）
        for gi in range(len(w)):
            rest = w[:gi] + w[gi + 1:]
            if len(rest) < CUSTOM_GAP_MIN_REST:
                continue
            i = text.find(rest)
            if i < 0:
                continue
            s, e = i, i + len(rest)
            if any(s < pe and ps < e for ps, pe in taken):
                continue
            # 整行判定：假设跨度（残串+缺字 1 字=len(w)）占行文本比例
            whole = len(w) >= CUSTOM_GAP_WHOLE_LINE * max(1, n)
            out.append((w, s, e, whole, gi))
            taken.append((s, e))
            break  # 每词只取最左一处假设
    return out


def _gap_patch_rect(ln, word, s, e, whole, gi):
    """缺字假设的补丁矩形（band 局部坐标 / visual 页坐标均适用，调用方
    自行做 y0 平移）。whole=True 整行框（左右各外扩半字符宽，覆盖 det 框
    边缘裁切的字形残迹——det 框 x0=318 而真实行起 ≈312 实测）；
    否则残串矩形向缺口侧外扩一字符宽（gi=0 缺字在词首→左扩；gi=词尾→
    右扩；词中→两侧各半字符宽：缺字字形被 rec 挤出序列，其物理位置在
    残串框内或接缝附近，比例折算存在漂移）。"""
    fitz = _import_fitz()
    bbox = ln.bbox
    cw = (bbox[2] - bbox[0]) / max(1, len(ln.text))
    if whole:
        return fitz.Rect(bbox[0] - cw * 0.5, bbox[1] - 1.0,
                         bbox[2] + cw * 0.5, bbox[3] + 1.0)
    lx = _span_rect_in_line(bbox, ln.text, s, e)
    if gi == 0:
        pad_l, pad_r = cw, 2.0
    elif gi == len(word) - 1:
        pad_l, pad_r = 2.0, cw
    else:
        pad_l, pad_r = cw * 0.5, cw * 0.5
    return fitz.Rect(lx[0] - pad_l, lx[1], lx[2] + pad_r, lx[3])


def _visual_recheck(page, engine, get_ocr, report, pno, fitz, plan=None,
                    options=None) -> list:
    """视觉复检兜底（v1.6.9）：含图页处理完后整页 OCR 复查已注册实体。

    覆盖两类无法由文本链路发现的残留：矢量转曲文字（get_text 提取不到
    且无图对象，redact 无效）与图内遮盖坐标偏移。命中处画遮盖块并返回
    泄漏清单（触发该页光栅化，彻底销毁所有底层内容）。
    """
    try:
        if not page.get_image_info():
            return []
        words = _recheck_word_set(engine, options)  # 复检词集按开关分桶
        if not words:
            return []
        ocr = get_ocr()
        leaked = []
        # v1.8.0：2x 二次复检轮撤除（审计结论）——
        # v1.7.4 性能诊断已实测快速模式（仅 1x）漏报 0；2x 轮独立命中仅 11
        # 处/全语料（命中率 ≈3.5%）却占整页二次 OCR 开销（实测 201.9s/轮、
        # 占全量 16.5%），P1 速度优先。原 1x/2x 双轮结构见 git 历史。
        pix = page.get_pixmap(
            matrix=fitz.Matrix(OCR_DPI / 72.0, OCR_DPI / 72.0), alpha=False)
        # v1.9.1 渲染确定性：命中补丁先收集、后按 (y, x, w, h, 词, 缺字标记)
        # 规范序烧入。原 for w in words 直接迭代 set——烧入序随 PYTHONHASHSEED
        # 漂移，重叠补丁的 z 序翻转即扫描件叠写星/星形形态漂移（台账族根因，
        # 双种子探针实测）。收集序不影响补丁集合，烧入序由此处排序唯一决定。
        patches = []  # (rect, word, 缺字假设标记)
        # 缺字假设词集（收窄口径：仅 UI 自定义词，与
        # _custom_word_pristine_band 同源 _custom_gap_word_keys——brands.json
        # 品牌词不参与缺字兜底；排序定序）
        _custom_keys = _custom_gap_word_keys(engine)
        gap_words = sorted((w for w in words if w in _custom_keys),
                           key=lambda k: (-len(k), k))
        for ln in ocr.recognize(pix.tobytes("png"), OCR_DPI).lines:
            if not ln.text.strip():
                continue
            occ = []  # 本行全词命中区间（缺字假设不与之重叠）
            for w in words:
                i = ln.text.find(w)
                while i >= 0:
                    lx = _span_rect_in_line(ln.bbox, ln.text, i, i + len(w),
                                            extra_pad=(ln.bbox[2] - ln.bbox[0]) * 0.02 + 2.0)
                    patches.append((fitz.Rect(*lx), w, False))
                    occ.append((i, i + len(w)))
                    i = ln.text.find(w, i + 1)
            # 缺字假设兜底（配置词风雨无阻）：det 丢框/rec 丢字
            # 行的单字缺失恢复，整行或缺口侧外扩补遮
            for (gw, s, e, whole, gi) in _custom_word_gap_matches(
                    ln.text, gap_words, occ):
                patches.append((_gap_patch_rect(ln, gw, s, e, whole, gi),
                                gw, True))
        patches.sort(key=lambda p: (p[0].y0, p[0].x0, p[0].width, p[0].height,
                                    p[1], p[2]))
        for rect, w, is_gap in patches:
            loc = (f"第{pno + 1}页(视觉复检·缺字兜底)" if is_gap
                   else f"第{pno + 1}页(视觉复检)")
            _repl = _word_repl(engine, w)  # v2.1.8：自定义词走 敏N 代号
            _draw_replacement(page, rect, _repl)
            report.record(original=w, replacement=_repl, rule="visual_recheck",
                          location=loc)
            if plan is not None:  # v1.9.4 埋点：复检补丁成品矩形
                plan.add_op_rect(pno, "draw", rect, _repl, _repl, w,
                                 "visual_recheck", loc)
            leaked.append((rect, w))
        return leaked
    except Exception as e:
        # 复检失败不阻塞主流程（保守：不触发光栅化），但必须留痕（失败必须留痕）
        try:
            report.log(f"  第 {pno + 1} 页视觉复检失败（保守不触发光栅化）："
                       f"[{type(e).__name__}] {e}")
        except Exception:
            pass
        return []


def _band_recheck(page, engine, get_ocr, report, pno, fitz, plan=None,
                   options=None) -> int:
    """条带放大复检（v1.7）：整页 OCR 对 logo 横幅装饰小字（slogan）、
    低对比小字的检测召回极差（det 忽略小目标，实测整页 1x/2x/3x 均检不出，
    局部裁剪 3x 可检出），按水平条带局部 3x 放大逐一识别。

    命中品牌实体的行正常遮盖；其垂直邻近行（横幅装饰字/ slogan 小字，
    OCR 常误读为"量生活·会空间"等无法精确匹配）按版式连带遮盖——
    品牌横幅是一个整体视觉资产。仅对已进入光栅化名单的页面执行
    （这些页原图将被销毁，此处遮盖只影响最终位图观感）。返回新增遮盖数。
    """
    if get_ocr is None:
        return 0
    try:
        words = _recheck_word_set(engine, options)  # 复检词集按开关分桶
        if not words:
            return 0
        ocr = get_ocr()
        page_h = page.rect.height
        # 高条带 + 重叠步进：logo 横幅与其装饰小字（slogan）必须落入同一条带，
        # 否则跨带的无命中装饰行没有品牌行可作连带依据（实测 120pt 条带把
        # 横幅拦腰切断导致 slogan 漏遮，故 band_h 下限 200）；重叠区重复遮盖
        # 视觉等价，无害。几何两值已参数化（RECHECK_BAND_H / RECHECK_BAND_STEP，
        # 可 env 覆盖见模块头；280/240 为实测默认，step = band_h - 40）
        band_h, step = RECHECK_BAND_H, RECHECK_BAND_STEP
        bands = []
        y = 0.0
        while y < page_h - 1e-6:
            bands.append(y)
            y += step
        zoom = 3 * OCR_DPI / 72.0
        hits = 0
        for y0 in bands:
            clip = fitz.Rect(0, y0, page.rect.width, min(y0 + band_h, page_h))
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip,
                                  alpha=False)
            result = ocr.recognize(pix.tobytes("png"), OCR_DPI * 3)
            line_info = []  # (line, 命中实体词列表)
            for ln in result.lines:
                t = ln.text.strip()
                if not t:
                    continue
                line_info.append((ln, [w for w in words if w in t]))

            def _to_page_rect(bbox):
                return fitz.Rect(bbox[0], bbox[1] + y0, bbox[2], bbox[3] + y0)

            band_patches = []  # v1.9.1 渲染确定性：本条带命中补丁收集器

            for ln, hit_words in line_info:
                if not hit_words:
                    continue
                for w in hit_words:
                    i = ln.text.find(w)
                    while i >= 0:
                        lx = _span_rect_in_line(ln.bbox, ln.text, i, i + len(w))
                        rect = fitz.Rect(lx[0], lx[1] + y0, lx[2], lx[3] + y0)
                        # v1.9.1 渲染确定性：补丁收集后规范序烧入（原在词迭代中
                        # 即时画，set 序随种子漂移→重叠补丁 z 序翻转，J 台账族根因）
                        band_patches.append((rect, w))
                        i = ln.text.find(w, i + 1)
            # v1.9.1：本条带补丁按 (y, x, w, h, 词) 规范序统一烧入。仍按条带
            # 分批画（条带间 40pt 重叠，先画补丁会进入后续条带的 OCR 输入，
            # 该耦合语义保持不变）；条带内画序不再依赖 set 迭代序。
            band_patches.sort(key=lambda p: (p[0].y0, p[0].x0,
                                             p[0].width, p[0].height, p[1]))
            for rect, w in band_patches:
                _repl = _word_repl(engine, w)  # v2.1.8：自定义词走 敏N 代号
                _draw_replacement(page, rect, _repl)
                report.record(original=w, replacement=_repl,
                              rule="band_recheck",
                              location=f"第{pno + 1}页(条带复检)")
                if plan is not None:  # v1.9.4 埋点：条带复检补丁成品矩形
                    plan.add_op_rect(pno, "draw", rect, _repl, _repl, w,
                                     "band_recheck", f"第{pno + 1}页(条带复检)")
                hits += 1
            # v1.8.0：band 连带遮蔽撤除（审计结论）——保护对象 slogan 已被
            # goal_rules 白名单豁免，连带
            # 实测仅剩观感级误遮（职务词邻行/文件控制表标签）；命中行
            # 本身的实体遮盖（上方 find 循环）完整保留。回归用例：
            # tests/test_band_neighbor_regression.py
        return hits
    except Exception as e:
        # 复检失败不阻塞主流程，但必须留痕——静默失败=泄漏无解释（失败必须留痕）
        try:
            report.log(f"  第 {pno + 1} 页条带复检失败：[{type(e).__name__}] {e}")
        except Exception:
            pass
        return 0


def _custom_word_pristine_band(live_page, pristine_page, engine, get_ocr,
                               report, pno, fitz, plan=None) -> int:
    """custom 词净版条带缺字兜底（用户配置词最强兜底，设计决策
    「配置了的词必须风雨无阻」）。

    为什么需要净版：现行 _band_recheck 渲染的是页处理后的实况——页内印章
    通道黑框已先画（印章框恰压落款行中部实测），条带 OCR 看到
    的落款行被黑框截成碎片，det/rec 均无法还原整行 ⇒ 缺字假设无从谈起
    （首跑实测：实况 band 零命中、输出残片仍见「中共」
    「员会」）。本通道改从源文件重开文档（净版页，未画任何遮盖）按同款
    条带几何（RECHECK_BAND_H/STEP、3x）重扫，custom 词做单字缺失假设匹配
    （OCR det 丢框/rec 丢字使行文本缺一字、连续子串不成立——落款行
    「中共市图书馆委员会」缺「某」实测），
    命中补丁画在实况页上（该页随即整页光栅化，补丁只影响最终位图观感）。

    范围声明（二轮收窄口径）：仅 user 显式配置词（engine
    ._word_keys UI 自定义敏感词，经纯后缀过滤）；brands.json 品牌词
    （_static_brand_words）不参与缺字兜底——主链路精确匹配维持覆盖，
    且首版并集词源实测零 UI 词语料上无条件净版重扫构成 +87% 结构性回归
    （39 份 elapsed 1174→2192s）。批内学习/映射派生简称与映射表键不扩面；
    仅缺字假设（全词行仍由首轮 joined OCR 与既有 band 通道负责）；仅对
    已入光栅化名单的页执行（与 band 同域）。op 记 rule="band_recheck"
    （光栅化阶段语义复用：重放推迟+计数口径不变，位置串「缺字兜底」区分
    来源），台账/plan 逐笔可溯。无 UI 词时零开销（词集空直接返回，且
    调用点不开净版文档；负向对照 tests/test_custom_word_recheck.py）。
    返回新增遮盖数。
    """
    if get_ocr is None:
        return 0
    # 用户显式配置词（信任地基）：UI 自定义敏感词（_word_keys，D1 现场口径；
    # 收窄=唯一词源）。映射表键不入本通道（映射=精确替换契约，缺字
    # 假设与其语义不匹配）；brands 词同不入（见 _custom_gap_word_keys）。
    custom_keys = _custom_gap_word_keys(engine)
    if not custom_keys:
        return 0
    try:
        words = {w for w in custom_keys if w and len(w) >= 2}
        # 纯法定组织形式后缀词不入兜底词集（同 _band_recheck v1.9.3 口径）
        words = {w for w in words if not _pure_org_suffix_fn(engine)(w)[0]}
        # 排序定序（v1.9.1 渲染确定性口径）：(-len, 词) 全决定序
        gap_words = sorted(words, key=lambda k: (-len(k), k))
        if not gap_words:
            return 0
        ocr = get_ocr()
        # 净版页与实况页同一源文件同一页序；净版页保留 /Rotate 由 get_pixmap
        # 按显示方向渲染（实况页已 remove_rotation 烘焙，两者显示空间逐点
        # 同一——条带几何/补丁坐标直接可比）。
        page_h = pristine_page.rect.height
        band_h, step = RECHECK_BAND_H, RECHECK_BAND_STEP
        zoom = 3 * OCR_DPI / 72.0
        hits = 0
        y = 0.0
        bands = []
        while y < page_h - 1e-6:
            bands.append(y)
            y += step
        for y0 in bands:
            clip = fitz.Rect(0, y0, pristine_page.rect.width,
                             min(y0 + band_h, page_h))
            pix = pristine_page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                                           clip=clip, alpha=False)
            result = ocr.recognize(pix.tobytes("png"), OCR_DPI * 3)
            band_patches = []  # (rect 页坐标, 词)——v1.9.1 规范序烧入口径
            for ln in result.lines:
                if not ln.text.strip():
                    continue
                for (gw, s, e, whole, gi) in _custom_word_gap_matches(
                        ln.text, gap_words, []):
                    lrect = _gap_patch_rect(ln, gw, s, e, whole, gi)
                    band_patches.append((fitz.Rect(lrect[0], lrect[1] + y0,
                                                   lrect[2], lrect[3] + y0),
                                         gw))
            band_patches.sort(key=lambda p: (p[0].y0, p[0].x0,
                                             p[0].width, p[0].height, p[1]))
            for rect, gw in band_patches:
                _repl = _word_repl(engine, gw)  # v2.1.8：自定义词走 敏N 代号
                _draw_replacement(live_page, rect, _repl)
                report.record(original=gw, replacement=_repl,
                              rule="band_recheck",
                              location=f"第{pno + 1}页(条带复检·缺字兜底)")
                if plan is not None:  # v1.9.4 埋点：成品矩形（重放推迟同 band）
                    plan.add_op_rect(pno, "draw", rect, _repl, _repl, gw,
                                     "band_recheck",
                                     f"第{pno + 1}页(条带复检·缺字兜底)")
                hits += 1
        return hits
    except Exception as e:
        # 兜底失败不阻塞主流程，但必须留痕（失败必须留痕）
        try:
            report.log(f"  第 {pno + 1} 页净版条带缺字兜底失败"
                       f"（不影响主流程）：[{type(e).__name__}] {e}")
        except Exception:
            pass
        return 0


def _mask_joined_ocr_lines(page, items, engine, options, report, pno, loc_tag,
                           base_rects=None, plan=None) -> int:
    """OCR 行拼接遮盖（链路 B 与内嵌图片共用）：

    把全部 OCR 行拼接后统一 find_spans（跨行名称/上下文证据恢复），
    命中 span 按行拆分画遮盖块。base_rects 非 None 时为内嵌图片模式：
    与 items 等长的矩形列表（各行所在图片的页面矩形，行 bbox 为图内
    相对坐标，需平移到页面坐标）；None 时行 bbox 即页面坐标（整页 OCR）。

    v1.9.4：绘制段抽出为 _draw_spans_on_lines / _draw_amount_label_spans
    （检测路径与冻结重放共用同一行拆分绘制；本函数保留检测职责，
    find_spans 的两次调用语义原样保留）。plan 为首轮冻结采集器。
    options（v2.0.1 金额闸）：行标签金额兜底的类别开关在调用点检查，
    冻结重放不走本函数（plan 快照固化首轮闸结果），故仅需检测路径传入。
    """
    offsets = []  # (item, 全文起始偏移)
    joined_parts = []
    pos = 0
    for idx, it in enumerate(items):
        offsets.append((it, pos, idx))
        joined_parts.append(it.text)
        pos += len(it.text)
    joined = "".join(joined_parts)

    # v1.9.4：绘制段抽出为 _draw_spans_on_lines——检测与冻结重放共用同一
    # 行拆分绘制（重放零 OCR 时喂 plan 存储的 items+spans，逐值同参）。
    spans = [(sp.start, sp.end, sp.rule, sp.replacement, sp.confidence,
              joined[sp.start:sp.end]) for sp in engine.find_spans(joined)]
    hits = _draw_spans_on_lines(page, items, offsets, spans, base_rects,
                                report, pno, loc_tag, plan=plan, preset="m")
    # 行标签金额兜底（OA p6 报销截图行级金额 81.00/74.31 实例）——
    # 「…费/税/补贴/金额/合计」等行标签紧邻的小额两位小数/千分位数字，常规
    # 金额规则因无单位且无千分位不命中。仅在 OCR 拼接文本上启用（原生文本层
    # 维持既有口径），命中不与引擎 span 重叠。
    # v2.0.1 金额闸：调用点以 options.mask_amount 包裹——
    # 金额关时零 op；实体 span 绘制（上方 _draw_spans_on_lines）不受影响。
    _img_amt = re.findall(r"(?:费|税|补贴|补助|金额|合计|小计|单价|总价|价款)"
                          r"[ 	：:为是]{0,6}(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+\.\d{2})(?!\d)",
                          joined)
    if _img_amt and _amount_on(options):
        # 复用首次 find_spans 结果（原第二次全量重跑）——
        # find_spans 非纯函数（简称注册副作用），二次调用在「同块内先声明
        # 简称后又出现」的角落会多出未绘制 span 反向压制金额兜底；covered
        # 语义改为「实际绘制过的区间」更自洽（角落差方向=宁多遮不漏）。
        covered = [(sp[0], sp[1]) for sp in spans]
        hits += _draw_amount_label_spans(page, items, offsets, joined, covered,
                                         base_rects, report, pno, loc_tag,
                                         plan=plan)
    return hits


def _draw_spans_on_lines(page, items, offsets, spans, base_rects, report,
                         pno, loc_tag, plan=None, preset="m") -> int:
    """拼接 span 的行拆分绘制（v1.9.4 自 _mask_joined_ocr_lines 抽出）。

    检测路径与冻结重放共用同一路径（蓝图 D1：重放零 OCR 时喂 plan 存储
    的 items+spans，行级矩形折算/外扩/整行判定与首轮逐值一致）。
    spans：(s, e, rule, repl, conf, orig) 元组序列（joined 坐标）；
    preset：m=实体命中绘制参数（外扩 12%+6pt/±3pt、≥60% 整行分支）；
            a=金额兜底绘制参数（12%+2pt/±1.5pt、无整行分支）。
    返回命中数（口径与原实现一致：实体 span 逐 span 计数）。
    """
    fitz = _import_fitz()
    hits = 0
    entity_preset = preset == "m"
    for (s, e, rule, repl, conf, orig) in spans:
        hits += 1
        report.record(original=orig, replacement=repl,
                      rule=rule, location=f"第{pno + 1}页{loc_tag}")
        if plan is not None:  # v1.9.4 埋点：OCR 拼接区间指令
            plan.add_op_jspan(pno, s, e, rule, repl, conf, orig, loc_tag,
                              "m" if entity_preset else "a")
        first_line = True
        for it, base, idx in offsets:
            i_s, i_e = base, base + len(it.text)
            if i_s >= e:
                break  # 后续行都在 span 之后（offsets 按拼接顺序排列）
            if i_e <= s:
                continue
            sub_s = max(s, i_s) - i_s
            sub_e = min(e, i_e) - i_s
            if sub_e <= sub_s:
                continue
            if first_line:
                line_repl = repl
                first_line = False
            else:
                line_repl = "×" * (sub_e - sub_s)  # 跨行后续行：遮蔽条
            # 跨行 span 的行级遮蔽——span 覆盖该行 ≥60% 字符时
            # 直接整行矩形（det 碎行「示例甲家 | 居股份有限公司…」场景，
            # 字符级折算矩形配合外扩仍会漏行尾/行首残字）
            whole = entity_preset and (sub_e - sub_s) >= 0.6 * max(1, len(it.text))
            if whole:
                rect = fitz.Rect(it.bbox[0], it.bbox[1], it.bbox[2], it.bbox[3])
            else:
                rect = _span_rect_in_line(it.bbox, it.text, sub_s, sub_e)
            # det 分行前缀续接机制移除——
            # 唯一实测场景「某市海」已按证据门槛存疑放行（主检+独立校验
            # 双确认，无再识别锚点）；该机制曾致误报半径 27→40。
            if base_rects is not None and base_rects[idx] is not None:
                br = base_rects[idx]
                rect = fitz.Rect(br.x0 + rect[0], br.y0 + rect[1],
                                 br.x0 + rect[2], br.y0 + rect[3])
            # v1.7 残字扩展：OCR 行按字符数等宽折算的矩形与真实字形存在边缘
            # 偏差（黑体强调、字距不均时尤甚），实体类命中的遮盖块四周扩
            # 12%+2pt，防止"公司""雅"等原字边缘残迹露出（截断/残字≠脱敏）
            # 外扩加强——扫描/图内 OCR 的 det 框与真实字形存在
            # 1 字级漂移（标题格「▓居」、直营样本「某市海」残迹实测），
            # 水平外扩 12%+6pt、垂直 ±3pt（原 12%+2pt/±1.5pt）
            # v2.1.5 观感收窄：外扩补偿的是绝对量级漂移（1 字级），
            # 但 12% 比例项随 span 宽度放大——长实体名（15 字公司名）每侧
            # 可外扩 ~2.5 邻字宽；整行分支（≥60% 行）叠加 12%+6pt 后块
            # 面积翻倍（样本审计报告实测：外扩前 413k pt²→外扩后
            # 861k pt²，pad/字宽 p50=2.6），遮蔽块频繁压住邻字与上下行。
            # 收窄分径（残字补偿语义保留，锚定字符宽 cw）：
            #   整行分支：框内已含全行字符，外扩仅补偿 det 框对行首尾字形
            #   的边缘裁切——半字宽/±1pt（与 _gap_patch_rect whole 同口径，
            #   det x0=318 vs 真实行起 ≈312 实测）；
            #   折算分支：min(12%+6pt, 1.4×cw+2pt)——窄 span 保持外扩加强口径
            #   （2 字词 ≈8.9pt 不变），宽 span 封顶 ~1.4 字宽；
            #   vpad 3pt→1.5pt：加强前安全口径（det 框自身含纵向余白，
            #   加强案例均为水平漂移）；a 预设（金额）本就 ±1.5pt。
            cw = (it.bbox[2] - it.bbox[0]) / max(1, len(it.text))
            if whole:
                pad = cw * 0.5
                vpad = 1.0
            else:
                pad = min((rect[2] - rect[0]) * 0.12
                          + (6.0 if entity_preset else 2.0), cw * 1.4 + 2.0)
                vpad = 1.5
            rect = fitz.Rect(rect[0] - pad, rect[1] - vpad,
                             rect[2] + pad, rect[3] + vpad)
            _draw_replacement(page, rect, line_repl)
    return hits


def _draw_amount_label_spans(page, items, offsets, joined, covered,
                             base_rects, report, pno, loc_tag,
                             plan=None) -> int:
    """行标签金额兜底的行拆分绘制（v1.9.4 自 _mask_joined_ocr_lines
    抽出，检测/冻结重放共用）。covered：引擎 span 区间（重叠豁免）。
    返回命中数（口径与原实现一致：逐行块计数）。"""
    fitz = _import_fitz()
    hits = 0
    for m in re.finditer(r"(?:费|税|补贴|补助|金额|合计|小计|单价|总价|价款)"
                         r"[ \t：:为是]{0,6}(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+\.\d{2})(?!\d)",
                         joined):
        gs, ge = m.start(1), m.end(1)
        if any(gs < pe and ps < ge for ps, pe in covered):
            continue
        report.record(original=m.group(1), replacement="***",
                      rule="amount", location=f"第{pno + 1}页{loc_tag}")
        if plan is not None:  # v1.9.4 埋点：金额兜底拼接区间指令
            plan.add_op_jspan(pno, gs, ge, "amount", "***", "", m.group(1),
                              loc_tag, "a")
        first_line = True
        for it, base, idx in offsets:
            i_s, i_e = base, base + len(it.text)
            if i_s >= ge:
                break
            if i_e <= gs:
                continue
            sub_s = max(gs, i_s) - i_s
            sub_e = min(ge, i_e) - i_s
            if sub_e <= sub_s:
                continue
            rect = _span_rect_in_line(it.bbox, it.text, sub_s, sub_e)
            if base_rects is not None and base_rects[idx] is not None:
                br = base_rects[idx]
                rect = fitz.Rect(br.x0 + rect[0], br.y0 + rect[1],
                                 br.x0 + rect[2], br.y0 + rect[3])
            pad = (rect[2] - rect[0]) * 0.12 + 2.0
            rect = fitz.Rect(rect[0] - pad, rect[1] - 1.5,
                             rect[2] + pad, rect[3] + 1.5)
            repl2 = "***" if first_line else "×" * (sub_e - sub_s)
            first_line = False
            _draw_replacement(page, rect, repl2)
            hits += 1
    return hits


def _mask_text_amount_columns(page, fitz, report, pno: int, options,
                              plan=None) -> int:
    """文本层列对齐金额兜底（金额口径统一为遮）。

    表头词含金额类词且带单位（住宿标准（元/天/人）/金额（元）等）的表格，
    其下方同 x 区间的 2~6 位纯数字为制度参数/交易金额值——常规金额规则因
    无货币单位不命中（防版本号误判的既有闸），造成同文档「有单位遮、无单位
    不遮」口径不一致。按表头 x 区间±40pt、表头下方 220pt 内的纯数字 word
    逐个画遮块并计入命中（驱动光栅化销毁）。仅原生文本页调用。

    v2.0.1 金额闸（契约 C2 #1）：本通道是 amount 直画通道，首行检查
    options.mask_amount——金额类别关闭时零 op（不画块、不记映射行）。

    表头谓词（回炉版；第一版判定树全量 39 份暴露 23 处新误遮
    已回退，见共享验证记录）。判定树（对一切表头候选先施
    「干净文本」负约束，再按语义词分路径）：
      通用否决（全分支）：
        · len>24（沿用旧上限）；
        · 页脚带（词底距页底 <90pt，或右缘 130pt×底部 160pt 右下角象限）
          ——页码列不立表头（OA 23/25、直营店 4/18、非现金 5/9 实测）；
        · 句读点（，。；：！？…,.;:!?）——正文残片不立表头（顿号、不收：
          「港、澳（港元）单程交通费/」真列头含顿号）；
        · 字数混排（CJK∧数字同词，如「错金额500」「表1：」）——数据行
          碎片/表标题不立表头；
        · 尾随点（含顿号，如「注明厂商、品名、单价、数量、」列表续行残片
          ——redact 断行产物不还原原文、按残片形态直接否定）。
      正向路径：
        A1 强金额语义词（金额/限额/单价/合计…17 词）∧ ≤14 字 ∧ 列首
          〔带内上方无「表头状」词——句读点行/字数混排行不阻断列首，
          放行「表1：…」标题下的真表头〕
          → 表头（放行不含元字的正表头「金额（人民币）」）；
        B1 元/￥/¥ ∧ 弱语义词（标准/费用/补贴/补助）→ 表头（位置不限
          ——「住宿标准（元/天/人）」类单位化列头，页中段实测）；
        B2 元/￥/¥ ∧ 列首〔±40pt 同带上方无非数字文字词；语义词不阻断
          （多级表头堆叠「报销标准→港、澳…」）〕→ 表头；
        弱词无元：不立表头（第一版 A2 漏洞封闭——「操作标准」真列头非
          金额列，其下「48 小时内」时限曾被遮，库存办法 p23 实测）。
    数字侧否定（命中数字词逐个检查）：
        · 页脚带/右下角（同上，页码 6 处实测）；
        · 「N/M」斜杠分式（两段各 ≤2 位=页码形态；「300/400」类 3 位
          并列值保留，费用 p11 实测）；
        · 计量/计时单位邻接（±15pt 同行）：右邻词首字/左邻词尾字 ∈
          分次时天日月年页人号项名个折点周刻秒单双倍%％——「2 分/次」
          扣分值、「48 小时内」时限、「21 页」页数、「2024 年」年份实测；
          元/￥/¥ 不收（真金额列邻接单位词照常遮）。
    另加同矩形跨表头去重：同一数字词只归先到表头、只画一次（第一版
    唯一正确部分，保留；hits/映射行/plan 指令按唯一矩形计）。
    """
    if not _amount_on(options):  # v2.0.1 金额闸：关=零 op（v2.1.8 含 words_only 停用）
        return 0
    try:
        words = page.get_text("words")
    except Exception:
        return 0
    if not words:
        return 0

    # ---- v2.0.2-R2 表头谓词（B2 回炉）----
    AMT_STRONG = ("金额", "限额", "单价", "总价", "价款", "合计", "小计",
                  "总额", "余额", "税额", "税费", "货款", "定金", "订金",
                  "押金", "罚款", "运费")
    AMT_WEAK = ("标准", "费用", "补贴", "补助")
    AMT_ALL = AMT_STRONG + AMT_WEAK
    _SENT_PUNCT = "，。；：！？…,.;:!?"     # 顿号、不收（真列头「港、澳…」实测）
    _TRAIL_PUNCT = "，。；：！？…、,.;:!?"  # 尾随顿号=列表续行残片（OA p23 实测）
    _UNIT_CHARS = "分次时天日月年页人号项名个折点周刻秒单双倍%％"
    page_h = float(page.rect.height)
    page_w = float(page.rect.width)

    def _pure_digit(t):
        return bool(t) and re.fullmatch(r"\d{1,6}(?:/\d{1,6})?", t) is not None

    def _mixed_digit_cjk(t):
        return (any("0" <= ch <= "9" or "０" <= ch <= "９" for ch in t)
                and any("\u4e00" <= ch <= "\u9fa5" for ch in t))

    def _in_footer_band(w):
        """页码防御：词底距页底 <90pt，或右下角象限（右缘 130pt×底 160pt）。
        全量轮 6 处页码 rect y1∈[759,783]、x∈[460,540]（A4 842 高）实测。"""
        return (w[3] >= page_h - 90.0
                or (w[0] >= page_w - 130.0 and w[3] >= page_h - 160.0))

    def _bad_header_text(t):
        """表头候选文本负约束（全分支通用）：正文残片在此排除。"""
        if any(ch in t for ch in _SENT_PUNCT):
            return True
        if _mixed_digit_cjk(t):
            return True
        return t[-1] in _TRAIL_PUNCT

    def _text_above(w, x_lo, x_hi):
        """B2 列首判定：w 上方（y1 ≤ w.y0+2）中心 x 落于带内的非数字文字
        词。纯数字（他表数据/页码）与语义词（多级表头合法堆叠）不阻断。"""
        for w2 in words:
            if w2 is w:
                continue
            t2 = w2[4]
            if not t2 or _pure_digit(t2):
                continue
            if any(k in t2 for k in AMT_ALL):
                continue
            if w2[3] <= w[1] + 2.0 and x_lo <= (w2[0] + w2[2]) / 2.0 <= x_hi:
                return True
        return False

    def _headerlike_above(w):
        """A1 列首判定：仅「表头状」词阻断——句读点行/字数混排行（正文段
        落、表标题「表1：…」类）不阻断列首（真表头常位于表标题之下）。"""
        for w2 in words:
            if w2 is w:
                continue
            t2 = w2[4]
            if not t2 or _pure_digit(t2) or _bad_header_text(t2):
                continue
            if any(k in t2 for k in AMT_ALL):
                continue
            if w2[3] <= w[1] + 2.0 \
                    and w[0] - 40.0 <= (w2[0] + w2[2]) / 2.0 <= w[2] + 40.0:
                return True
        return False

    def _digit_negated(w):
        """数字侧否定：页码/页码分式/计量单位邻接（全量轮 23 处误遮的反向
        防线——即使残片漏立表头，其下非金额数字也不再遮）。"""
        if _in_footer_band(w):
            return True
        if re.fullmatch(r"\d{1,2}/\d{1,2}", w[4]):  # N/M 页码分式
            return True
        y0, y1 = w[1], w[3]
        h = max(y1 - y0, 1.0)
        nxt = prv = None
        for w2 in words:
            if w2 is w or not w2[4]:
                continue
            ov = min(y1, w2[3]) - max(y0, w2[1])
            if ov <= 0 or ov < 0.5 * min(h, max(w2[3] - w2[1], 1.0)):
                continue
            gap_r = w2[0] - w[2]
            if -2.0 <= gap_r <= 15.0 and (nxt is None or w2[0] < nxt[0]):
                nxt = w2
            gap_l = w[0] - w2[2]
            if -2.0 <= gap_l <= 15.0 and (prv is None or w2[2] > prv[2]):
                prv = w2
        if nxt is not None and nxt[4][0] in _UNIT_CHARS:
            return True
        if prv is not None and prv[4][-1] in _UNIT_CHARS:
            return True
        return False

    headers = []
    for w in words:
        txt = w[4]
        if not txt or len(txt) > 24:
            continue
        if _in_footer_band(w):        # 页脚带不立表头
            continue
        if _bad_header_text(txt):     # 句读点/字数混排/尾随点残片
            continue
        has_cur = ("元" in txt or "￥" in txt or "¥" in txt)
        if has_cur:
            if any(k in txt for k in AMT_WEAK):
                headers.append(w)      # B1：元∧弱语义，位置不限
            elif not _text_above(w, w[0] - 40.0, w[2] + 40.0):
                headers.append(w)      # B2：元∧列首
        elif any(k in txt for k in AMT_STRONG) and len(txt) <= 14 \
                and not _headerlike_above(w):
            headers.append(w)          # A1：强词∧短文本∧列首
        # 弱词无元：不立表头（第一版 A2 漏洞封闭）

    if not headers:
        return 0
    hits = 0
    rects = []
    seen = set()  # v2.0.2 跨表头去重：同一数字词（同矩形）只画一次
    for hw in headers:
        if hits >= 80:
            break
        hx0, hy0, hx1 = hw[0], hw[1], hw[2]
        for w in words:
            if not (hy0 + 4 < w[1] < hy0 + 220):
                continue
            txt = w[4]
            # 允许 300/400 类斜杠并列值（费用办法 p11 港元表实例）
            if not re.fullmatch(r"\d{1,6}(?:/\d{1,6})?", txt) or len(txt) > 13:
                continue
            if _digit_negated(w):  # v2.0.2-R2 页码/分式/单位邻接否定
                continue
            cx = (w[0] + w[2]) / 2
            if not (hx0 - 40 <= cx <= hx1 + 40):
                continue
            key = (w[0], w[1], w[2], w[3], txt)
            if key in seen:
                continue
            seen.add(key)
            rects.append((fitz.Rect(w[0] - 1.5, w[1] - 1.5,
                                    w[2] + 1.5, w[3] + 1.5), txt))
            hits += 1
            if hits >= 80:  # 单页上限防失控（按去重后唯一矩形计）
                break
    if rects:
        # 原生文本必须 redaction 移除原字（画框盖不住文本层，
        # 提取仍可还原=数据级残留），再补画 ***
        try:
            for r, _ in rects:
                page.add_redact_annot(r)
            page.apply_redactions()
        except Exception as e:
            report.log(f"  第 {pno + 1} 页列对齐金额 redaction 失败：[{type(e).__name__}] {e}")
        for r, txt in rects:
            _draw_replacement(page, r, "***")
            report.record(original=txt, replacement="***", rule="amount",
                          location=f"第 {pno + 1} 页(列对齐)")
            if plan is not None:  # v1.9.4 埋点：直画成品矩形（redact 批+画块）
                plan.add_op_rect(pno, "redact_draw", r, "***", "***", txt,
                                 "amount", f"第 {pno + 1} 页(列对齐)")
    return hits


# ---------------------------------------------------------------------------
# 链路 A：原生文本遮罩
# ---------------------------------------------------------------------------

# 原生页内嵌图片 OCR 的尺寸阈值与每页上限（控制耗时）
_EMBED_IMG_MIN_W = 60.0    # pt
_EMBED_IMG_MIN_H = 40.0
_EMBED_IMG_MAX_PER_PAGE = 8


def _embed_img_cap_note(total: int, limit: int):
    """每页内嵌图截断留痕消息（纯函数零副作用）。

    _mask_embedded_images 按面积降序只识别前 limit 张（耗时控制），第
    limit+1 张起图内文字不经识别直接保留（漏遮面）。此前该截断完全静默，
    v2.1.7 起切片前由本函数生成单行中文消息，调用方拼页号后经 report.log
    留痕（先例：同函数 OCR 失败日志），控制流零变化。

    total <= limit（无截断）返回 None；页号不在此拼（由调用方按
    「第 X 页」前缀拼，故消息以「内嵌图共…」起头，避免「页本页」重复）。
    """
    if total <= limit:
        return None
    return (f"内嵌图共 {total} 张，按面积取大识别前 {limit} 张，"
            f"其余 {total - limit} 张未脱敏，请人工复核")

# v1.7 单据特征词：命中即整图遮盖（发票/回单/OA 用款报销单截图整体为
# 敏感凭证——申请人/供应商/行明细小字 OCR 无法逐字段覆盖）
_INVOICE_MARKS = ("发票", "纳税人识别号", "增值税", "开户行",
                  "用款申请单", "报销单", "付款申请单", "付款申请流程",
                  "签购单", "商户存根", "应收金额", "持卡人签名")  # 补刷卡凭条类
# v1.7.3：图内/扫描表格金额列头词（配合 _mask_ocr_amount_columns）
_MONEY_COL_LABELS = ("金额", "价款", "单价", "总价", "合计", "小计", "余额",
                     "货款", "定金", "订金", "押金", "罚款", "运费")
# 职务/职等词——表头列关联与孤立人名兜底不放行这些行（组织
# 架构表的职级/职务列是人名误报重灾区；真名与职务同列时由主遮盖流程覆盖）
_JOB_TITLE_WORDS = ("总监", "总裁", "总经理", "经理", "主任", "主管", "专员",
                    "工程师", "董事长", "董事", "监事", "会计", "出纳", "科员",
                    "职员", "员工", "助理", "秘书", "合伙人", "律师", "医师",
                    "设计师", "建筑师", "级", "等",
                    "明细", "分类", "类别", "名称", "编号", "合计", "金额",
                    "数量", "单价", "栏目", "项目", "摘要", "日期")  # 含字段标签

# 表格截图语境特征词（仅用于图内孤立人名兜底阈值放宽，
# 不与 _JOB_TITLE_WORDS 混用——后者另作表头列关联，扩词会加大其触发面）
_TABLE_HEADER_HINTS = ("姓名", "导购", "设计师", "联系人", "负责人", "员工",
                       "审核", "复核", "制表", "签字", "销售", "客服")

# 字段标签后缀否定表（列头锚定的值行排除用）——中文表格
# 的字段标签几乎必然以这些字结尾（申请单号/退款类型/收款单位/重量/产品…），
# 而中文姓名极少以此结尾；否定方向比肯定白名单安全。定点误遮 104 条全部命中
# 此表（验证报告）。
_FIELD_LABEL_SUFFIX = ("号", "单", "类型", "型", "日期", "时间", "方式", "单位",
                       "金额", "数量", "地址", "电话", "凭证", "原因", "状态",
                       "名称", "编号", "费用", "标准", "部门", "事项", "说明",
                       "比例", "期限", "限额", "结果", "意见", "要求", "情况",
                       "内容", "备注", "附件", "量", "品", "征", "销", "款",
                       "位", "证", "额", "人", "员", "商", "厂", "价", "率",
                        "期", "址", "话", "因", "称", "账",
                       "述", "税", "务", "折", "成", "批")  # 补：工程描述/是否含税/售后服务/单打折/盘点完成/审批
# （批次2 2026-09 清理：原 59 项含重复「款」，去重留一，行为无变化）


def _banner_recheck(page, img_rect, ocr, engine, report, pno, zoom,
                    plan=None, options=None) -> int:
    """内嵌图顶部横幅连带补扫（v1.7）。

    品牌表头横幅 = 主 logo 行 + 装饰小字（slogan「懂生活·会空间」等），
    装饰小字 det 召回差且 OCR 误读（懂→量/重）无法按注册词精确匹配。
    本函数在主遮盖流程**之前**对图片顶部区域做 3x 窄条裁剪识别：
    命中已注册实体的行作为锚点正常遮盖，与锚点垂直相邻（≤1.5 倍锚点
    行高）的未命中行按横幅整体语义连带遮盖。返回新增遮盖数。
    """
    words = _recheck_word_set(engine, options)  # 复检词集按开关分桶
    if not words:
        return 0
    fitz = _import_fitz()
    top_h = min(img_rect.height * 0.35, 120.0)
    clip = fitz.Rect(img_rect.x0, img_rect.y0, img_rect.x1, img_rect.y0 + top_h)
    mul = 1.5  # zoom*1.5 ≈ 300dpi，页宽图 → ~2480px，不触发 4000px 降采样
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom * mul, zoom * mul), clip=clip,
                          alpha=False)
    result = ocr.recognize(pix.tobytes("png"), int(OCR_DPI * mul))
    line_info = []
    for ln in result.lines:
        t = ln.text.strip()
        if t:
            line_info.append((ln, [w for w in words if w in t]))
    if not line_info:
        return 0
    hits = 0
    anchors = [ln for ln, ws in line_info if ws]
    if not anchors:
        return 0
    # 横幅遮蔽改逐行制（此前 = 整带制，新建工程实例——
    # 图片内部大标题「供应商比价表」「单一供应源申请单」被 ±0.8 行高整带抹除，
    # 页眉 slogan「—创新·分享—」连带消失）：
    #   覆盖 = 锚点行按命中词 x 范围（同 OCR 行的非敏感文字保留——实例
    #   大标题「供应商比价表」与品牌同处一行）＋锚点邻近（≤1.0 倍行高）的
    #   ≤4 字装饰碎片行整宽遮；≥5 字的非锚行（表单标题/制度文字）一律保留。
    # v1.9.1 渲染确定性：补丁先收集、后按 (y, x, w, h, 词) 规范序烧入。
    # 原逐词即时画——for w in words 迭代 set，烧入序随 PYTHONHASHSEED 漂移，
    # 同一锚行的窄/宽两块补丁 z 序翻转 = 叠写星/星间距漂移（双种子
    # 探针实测：同两矩形两 seed 相反序绘制，443 diffpx）。OCR 已全部完成，
    # 收集-再画不改变任何后续 OCR 输入。
    patches = []  # (rect, word)
    for anc, hit_words in line_info:
        if not hit_words:
            continue
        anc_h = max(anc.bbox[3] - anc.bbox[1], 10.0)
        anc_cy = (anc.bbox[1] + anc.bbox[3]) / 2 + img_rect.y0
        for w in hit_words:
            i = anc.text.find(w)
            while i >= 0:
                lx = _span_rect_in_line(anc.bbox, anc.text, i, i + len(w))
                # pad 回调 18%+3pt→12%+2pt、纵向 ±0.15 行高
                # （p33 页眉回归：外扩吞掉相邻白名单「全」与 slogan 前半）
                pad = (lx[2] - lx[0]) * 0.12 + 2.0
                rect = fitz.Rect(lx[0] - pad, lx[1] + img_rect.y0 - anc_h * 0.15,
                                 lx[2] + pad, lx[3] + img_rect.y0 + anc_h * 0.15)
                patches.append((rect, w))
                i = anc.text.find(w, i + 1)
        # ≤4 字装饰碎片整宽遮撤除（UI 截图的
        # 导航栏目/字段标签同处锚点邻近行，被整宽连带；品牌命中词 x 范围
        # 遮蔽 + slogan 白名单已覆盖原防漏目标）
    patches.sort(key=lambda p: (p[0].y0, p[0].x0, p[0].width, p[0].height, p[1]))
    for rect, w in patches:
        _repl = _word_repl(engine, w)  # v2.1.8：自定义词走 敏N 代号
        _draw_replacement(page, rect, _repl)
        report.record(original=w, replacement=_repl, rule="banner",
                      location=f"第 {pno + 1} 页(横幅整体遮盖)")
        if plan is not None:  # v1.9.4 埋点：横幅补丁成品矩形
            plan.add_op_rect(pno, "draw", rect, _repl, _repl, w, "banner",
                             f"第 {pno + 1} 页(横幅整体遮盖)")
        hits += 1
    return hits


def _mask_ocr_amount_columns(page, items, base_rects, engine, options, report,
                             pno, fitz, tag, plan=None) -> int:
    """图内/扫描表格金额列兜底（v1.7.3）。

    表格里的裸数字金额（无货币单位/小数）常规金额规则不命中；列头行含金额类
    词、且数字行与其列 x 对齐（水平重叠 ≥40%）时按金额遮盖。仅处理 ≥3 位
    纯数字（排除行号/序号），带字母的编号（订单号等）天然不匹配。
    设计结论 2026-09-06：图片表单内金额须遮。返回命中数（驱动光栅化销毁原图）。
    """
    if not _amount_on(options) or not items:
        return 0
    hits = 0
    # 按所属图片分组（链路A 嵌入图 bbox 为图内相对坐标，需配 base rect 换算）
    groups = {}
    for i, it in enumerate(items):
        t = it.text.strip()
        if t:
            groups.setdefault(base_rects[i] if base_rects else None,
                              []).append(it)
    for base, group in groups.items():
        bx = base.x0 if base is not None else 0.0
        by = base.y0 if base is not None else 0.0
        labels = [it for it in group
                  if any(k in it.text.replace(" ", "") for k in _MONEY_COL_LABELS)]
        if not labels:
            continue
        for it in group:
            t = it.text.replace(" ", "").replace("，", ",")
            if not re.fullmatch(r"[\d,]{3,}(?:\.\d{1,2})?", t):
                continue  # 仅纯数字（含逗号分节/小数），字母编号/日期不匹配
            x0, y0, x1, y1 = it.bbox
            # 与文本层列对齐通道
            # 口径对齐——此前 label 只要求「在上方」（无距离上限，跨表任意
            # 远的 label 也能锚定），且无年份/日期否定（「合计」列下方的
            # 年份 2024、单号 20250111 类纯数字被误遮，OCR 页与文本页
            # 口径不一的用户感知根因）。补两闸：
            # ① 纵向窗口：label 底到数字行顶 ≤220pt（对齐文本层同款常量）；
            # ② 年份/日期形态否定：1900-2100 纯四位、YYYYMMDD 八位拒。
            # 八位带月/日合法性校验（与 rules_engine
            # 标签通道镜像——「合计 20500000」类 8 位真金额不误拒）
            if re.fullmatch(r"(?:19|20)\d\d", t) or \
                    re.fullmatch(
                        r"(?:19|20)\d\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])", t):
                continue
            aligned = False
            for lb in labels:
                if lb is it or lb.bbox[1] > y0:
                    continue  # 列头须在数字行上方（同列）
                if y0 - lb.bbox[3] > 220.0:
                    continue  # 纵向距离上限（跨表 label 不锚定）
                ov = min(lb.bbox[2], x1) - max(lb.bbox[0], x0)
                if ov >= 0.4 * max(x1 - x0, 1.0):
                    aligned = True
                    break
            if not aligned:
                continue
            # parse_amount 改用模块顶 ocr_mod_rules 引用
            # （原此处为绝对包名 __import__ 动态导入 rules_engine 取
            # parse_amount，零必要——模块顶已 import 同一对象，且绝对包名
            # 是打包器重定位隐患）。
            value = ocr_mod_rules.parse_amount(t)
            if value is None or abs(value) <= options.amount_threshold:
                continue
            rect = fitz.Rect(bx + x0 - 1, by + y0 - 1, bx + x1 + 1, by + y1 + 1)
            repl = engine._default_replacement("amount", t)
            _draw_replacement(page, rect, repl)
            report.record(original=t, replacement=repl, rule="amount",
                          location=f"第 {pno + 1} 页{tag}")
            if plan is not None:  # v1.9.4 埋点：金额列成品矩形
                plan.add_op_rect(pno, "draw", rect, repl, repl, t, "amount",
                                 f"第 {pno + 1} 页{tag}")
            hits += 1
    return hits


def _ocr_clip_tiled(page, r, ocr, zoom, factor, fitz, tile_px=1900.0, overlap=0.12):
    """大图分块 OCR。

    RapidOCR 全局 max_side_len≈2000 会把超限图整体缩回同一输入，放大重试
    在大图上数学性失效（诊断实锤：2x/3x 渲染 2191/3287px 输入均为 2000px）。
    按 ≤tile_px 像素边长 + overlap 重叠切块逐块识别，重叠区按 (text, 粗粒度
    坐标) 去重，行 bbox（block 原点坐标，PDF 点）平移回整图坐标系后合并。
    图尺寸不超限时退化为整图识别。
    """
    from app.ocr_engine import OCRLine, OCRPageResult

    f_zoom = zoom * factor
    f_dpi = OCR_DPI * factor
    w_px = r.width * f_zoom
    h_px = r.height * f_zoom

    def _render(clip):
        pix = page.get_pixmap(matrix=fitz.Matrix(f_zoom, f_zoom), clip=clip,
                              alpha=False)
        return ocr.recognize(pix.tobytes("png"), f_dpi)

    if max(w_px, h_px) <= tile_px:
        return _render(r)

    def _starts(total):
        if total <= tile_px:
            return [0.0]
        step = tile_px * (1.0 - overlap)
        s = []
        v = 0.0
        while v + tile_px < total:
            s.append(v)
            v += step
        s.append(total - tile_px)
        return s

    out = OCRPageResult()
    seen = set()
    for y0 in _starts(h_px):
        for x0 in _starts(w_px):
            clip = fitz.Rect(r.x0 + x0 / f_zoom, r.y0 + y0 / f_zoom,
                             min(r.x1, r.x0 + (x0 + tile_px) / f_zoom),
                             min(r.y1, r.y0 + (y0 + tile_px) / f_zoom))
            sub = _render(clip)
            dx = clip.x0 - r.x0
            dy = clip.y0 - r.y0
            for ln in sub.lines:
                key = (ln.text, round(ln.bbox[0] / 8), round(ln.bbox[1] / 8))
                if key in seen:  # 重叠区同行两块各识别一次，去重
                    continue
                seen.add(key)
                out.lines.append(OCRLine(
                    text=ln.text, confidence=ln.confidence,
                    bbox=(ln.bbox[0] + dx, ln.bbox[1] + dy,
                          ln.bbox[2] + dx, ln.bbox[3] + dy)))
    return out


def _mask_embedded_images(page, engine, options, get_ocr, report, pno, fitz,
                          plan=None) -> int:
    """原生页内嵌截图/配图的 OCR 遮盖（v1.6.7 Fix B）。

    页面文本层正常但内嵌截图（收款截图/附件表头等）时，截图内文字从不被
    文本链路识别。对每张足够大的内嵌图片按区域渲染→OCR→命中处绘制白底
    替换块（覆盖式遮蔽，与整页 OCR 链路同语义）。返回命中数。
    """
    page_area = page.rect.width * page.rect.height
    rects = []
    seen_xref = set()
    for info in page.get_images(full=True):
        xref = info[0]
        if xref in seen_xref:
            continue
        seen_xref.add(xref)
        try:
            for r in page.get_image_rects(xref):
                if r.width < _EMBED_IMG_MIN_W or r.height < _EMBED_IMG_MIN_H:
                    continue
                if r.width * r.height < 0.004 * page_area:
                    continue
                rects.append(fitz.Rect(r))
        except Exception:
            continue
    if not rects:
        return 0
    rects.sort(key=lambda r: r.width * r.height, reverse=True)
    _cap_note = _embed_img_cap_note(len(rects), _EMBED_IMG_MAX_PER_PAGE)
    if _cap_note:  # 截断不再静默——第 9 张起图内文字不经
        # 识别直接保留（漏遮面），镜像同函数 OCR 失败留痕先例拼页号走 report.log
        report.log(f"  第 {pno + 1} 页{_cap_note}")
    rects = rects[:_EMBED_IMG_MAX_PER_PAGE]

    ocr = get_ocr()
    zoom = OCR_DPI / 72.0
    # v1.6.9：页面级全图行合并——同一界面截图常被 PDF 拆成多个图片对象
    # （"部门负责人签字："与"张某"分属两图），逐图识别会切断证据。
    # 全部图 OCR 行统一拼接识别，命中按行所在图坐标平移遮盖。
    all_items, all_rects = [], []
    invoice_hits = 0
    _extra_hits = 0  # 孤立人名/列锚遮盖计数（并入返回值驱动 need_raster）
    try:
        _page_text_ctx = page.get_text()
    except Exception:
        _page_text_ctx = ""
    _page_credential_ctx = any(k in _page_text_ctx for k in
                               ("刷卡纸", "签购单", "POS机", "POS 机", "回单", "凭证"))
    _eng_hits0 = 0  # 每图重算（下方重试逻辑内赋值）
    for r in rects:
        try:
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=r, alpha=False)
            result = ocr.recognize(pix.tobytes("png"), OCR_DPI)
        except Exception as e:  # v1.6.9：不再静默——OCR 失败会让该截图整体漏遮
            report.log(f"  第 {pno + 1} 页内嵌图片识别失败（{type(e).__name__}），"
                       f"该图文字未脱敏，请人工复核")
            continue  # 单图异常不中断整页
        # v1.7 闭环加固：小号艺术字 logo/低对比小字的检测召回不稳（实测同一
        # logo 时检时不检）。1x 识别拼不出任何实体命中时放大 2 倍重试一次，
        # 取检出行较多者；两次行 bbox 均由 recognize 按 dpi 换算为 PDF 点，
        # 坐标系一致，无需二次换算。
        try:
            _result_1x = result  # 1x 快照（凭证判定口径，不被分块重试改写）
            hits1 = len(engine.find_spans("".join(it.text for it in result.lines)))
            _lines1 = len(result.lines)
            # 修复（图像层漏报）：原门
            # hits1<=2 对内容丰富的截图永远关闭——示例乙店 p5 hits1=5/36 行
            # （13.9%，公司行被 v6 读缺尾字）、p11 hits1=13/111 行（11.7%，
            # 人名表 7 格只读出 3 格），2x/3x 重试从未执行。放宽："行多命中少"
            # （行数≥12 且实体命中率 <1/7≈14.3%，大量可见文字未被利用）也进入
            # 重试。定点实测 p5/p11 命中率 13.9%/11.7% 均落入触发区。
            if hits1 <= 2 or (_lines1 >= 12 and hits1 * 7 < _lines1):
                # v1.8.0：2x 重试改分块 OCR——RapidOCR 全局 max_side_len≈2000
                # 会把超限图整体缩回同一输入（升档数学性失效，诊断实锤）
                result2 = _ocr_clip_tiled(page, r, ocr, zoom, 2, fitz)
                hits2 = len(engine.find_spans("".join(it.text for it in result2.lines)))
                if hits2 > hits1 or (hits2 == hits1
                                     and len(result2.lines) > len(result.lines)):
                    result = result2
        except Exception:
            pass
        # 小图零命中凭证兜底——页面语境含凭证词（刷卡纸/签购单/
        # POS/回单/凭证）且该图为小尺寸照片、多档重试后引擎仍零命中时整图遮
        # （示例乙 p3 刷卡纸实测：4x 人眼可读 示例金额 RMB1,234.56 而 OCR 全档不可读）
        try:
            if r.width < 300 and _page_credential_ctx and _eng_hits0 == 0:
                _draw_replacement(page, fitz.Rect(r), "***")
                report.record(original="凭证照片", replacement="整图遮盖",
                              rule="invoice", location=f"第{pno + 1}页(图内文字)")
                if plan is not None:  # v1.9.4 埋点：凭证整图遮成品矩形
                    plan.add_op_rect(pno, "draw", fitz.Rect(r), "***",
                                     "整图遮盖", "凭证照片", "invoice",
                                     f"第{pno + 1}页(图内文字)")
                invoice_hits += 1
                continue
        except Exception as e:
            # 凭证语境整图遮判定失败留痕（失败必须留痕：静默失败=小图凭证可能漏遮）
            try:
                report.log(f"  第 {pno + 1} 页凭证语境整图遮判定失败："
                           f"[{type(e).__name__}] {e}")
            except Exception:
                pass
        # 凭证照片金额平衡重试——OCR 文本中 ¥/￥ 前缀或千分位
        # 裸金额形态 ≥2 处、而引擎实际遮盖数更少时，该图整体按凭证遮盖
        # （报销场景单据整体即敏感凭证；拍照小字逐字段遮蔽不可靠——
        # 示例乙 p3 POS「示例金额 ¥1,234.56」漏出黑框上沿实测）
        try:
            # 凭证判定用 1x 快照——分块 OCR 高召回会同时
            # 抬高 _raw_amt 与 _eng_hits，抵消「金额多而遮盖少」的失衡判据
            # （实测：OA 付款报销 p7/p13 凭证整图遮丢失，卡号/金额漏出）
            _joined_txt = "".join(it.text for it in _result_1x.lines)
            _raw_amt = len(re.findall(r"[￥¥]\s*\d[\d,]*(?:\.\d{1,2})?|\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?", _joined_txt))
            _eng_hits = len(engine.find_spans(_joined_txt))
            _eng_hits0 = _eng_hits
            # v2.0.1 金额闸（契约 C2 #3）：两处金额词（_raw_amt）触发的整图遮
            # 追加 mask_amount 检查——金额关时凭证判定不消费金额形态证据；
            # _eng_hits0 更新保留（后续图的凭证语境分支语义不变）。
            if _raw_amt >= 1 and _eng_hits == 0 and r.width < 300 \
                    and _amount_on(options):
                # 小图含裸金额而实体零命中 → 整图按凭证遮（POS 屏金额行实测）
                _draw_replacement(page, fitz.Rect(r), "***")
                report.record(original="凭证截图", replacement="整图遮盖",
                              rule="invoice", location=f"第{pno + 1}页(图内文字)")
                if plan is not None:  # v1.9.4 埋点：凭证整图遮成品矩形
                    plan.add_op_rect(pno, "draw", fitz.Rect(r), "***",
                                     "整图遮盖", "凭证截图", "invoice",
                                     f"第{pno + 1}页(图内文字)")
                invoice_hits += 1
                continue
            if _raw_amt >= 2 and _eng_hits < _raw_amt and _amount_on(options):
                _draw_replacement(page, fitz.Rect(r), "***")
                report.record(original="凭证截图", replacement="整图遮盖",
                              rule="invoice", location=f"第{pno + 1}页(图内文字)")
                if plan is not None:  # v1.9.4 埋点：凭证整图遮成品矩形
                    plan.add_op_rect(pno, "draw", fitz.Rect(r), "***",
                                     "整图遮盖", "凭证截图", "invoice",
                                     f"第{pno + 1}页(图内文字)")
                invoice_hits += 1
                continue
        except Exception as e:
            # 凭证金额平衡重试失败留痕（失败必须留痕：静默失败=POS 凭证小图可能漏遮）
            try:
                report.log(f"  第 {pno + 1} 页凭证金额平衡重试失败："
                           f"[{type(e).__name__}] {e}")
            except Exception:
                pass
        # v1.7 发票特征整图遮盖：发票/回单类截图（拍照发票透视+印章+小字）
        # OCR 无法可靠全覆盖，而报销场景中该图整体即敏感凭证——文本含
        # 发票特征词即整图打码（宁可过度遮盖，不漏字段）
        # 1x 与分块文本任一命中即触发（双查防漏）
        try:
            _inv_txt_tiled = "".join(it.text for it in result.lines).replace(" ", "")
            _inv_txt_1x = "".join(it.text for it in _result_1x.lines).replace(" ", "")
            if any(k in _inv_txt_tiled or k in _inv_txt_1x
                   for k in _INVOICE_MARKS):
                _draw_replacement(page, fitz.Rect(r), "***")
                report.record(original="发票/回单截图", replacement="整图遮盖",
                              rule="invoice", location=f"第{pno + 1}页(图内文字)")
                if plan is not None:  # v1.9.4 埋点：发票整图遮成品矩形
                    plan.add_op_rect(pno, "draw", fitz.Rect(r), "***",
                                     "整图遮盖", "发票/回单截图", "invoice",
                                     f"第{pno + 1}页(图内文字)")
                invoice_hits += 1  # v1.7：计入命中驱动 need_raster（销毁原图）
                continue
        except Exception as e:
            # 发票特征整图遮判定失败留痕（失败必须留痕：静默失败=发票截图可能漏遮）
            try:
                report.log(f"  第 {pno + 1} 页发票特征整图遮判定失败："
                           f"[{type(e).__name__}] {e}")
            except Exception:
                pass
        # v1.7 横幅连带补扫：logo 装饰小字（slogan）在整图 OCR 中 det 召回
        # 极差，且 OCR 常误读（懂→量/重）无法按词匹配，必须在品牌行未被
        # 遮盖前以本图顶部窄条 3x 裁剪补扫（窄条不触发 4000px 降采样），
        # 以命中实体行为锚点，把垂直相邻的装饰行按横幅整体连带遮盖。
        # 纳入 deep_recheck 开关（原为唯一
        # 不在开关内的无条件额外 OCR 项；快速模式跳过，与 band/visual 同口径）
        if options.deep_recheck:
            try:
                _banner_recheck(page, r, ocr, engine, report, pno, zoom,
                                plan=plan, options=options)
            except Exception:
                pass
        # 表格截图语境判定——图内含姓名/导购/设计师等表头
        # 特征词时，孤立人名兜底阈值 0.85→0.70（v6 对低清灰字人名常读成形近
        # 名「某悦天→某俊灵」@0.73，0.85 接不住；语境限定控制误遮面）
        try:
            _img_txt = "".join(it.text for it in result.lines)
            _tbl_ctx = any(k in _img_txt for k in _TABLE_HEADER_HINTS)
        except Exception:
            _tbl_ctx = False
        _lone_name_min_conf = 0.70 if _tbl_ctx else 0.85
        _lone_masked = set()  # 孤立人名兜底已遮行（供列头锚定去重）
        for item in _select_items(result):
            if not item.text.strip():
                continue
            if item.confidence >= options.ocr_min_confidence \
                    or engine.find_spans(item.text):  # 低置信但命中实体也参与
                all_items.append(item)
                all_rects.append(r)
                # v1.6.9 截图孤立人名兜底：表单单元格人名独占一行，行序交错
                # 会切断前缀证据；孤立 2~3 字姓氏行按疑似人名遮盖（阈值见上）
                if item.confidence >= _lone_name_min_conf and options.mask_person \
                        and engine.is_lone_ocr_name(item.text):
                    report.record(original=item.text, replacement="×" * len(item.text),
                                  rule="person", location=f"第{pno + 1}页(图内文字)")
                    _draw_replacement(page, fitz.Rect(
                        r.x0 + item.bbox[0], r.y0 + item.bbox[1],
                        r.x0 + item.bbox[2], r.y0 + item.bbox[3]),
                        item.text[0] + "*" * (len(item.text) - 1))
                    if plan is not None:  # v1.9.4 埋点：孤立人名成品矩形
                        plan.add_op_rect(
                            pno, "draw",
                            fitz.Rect(r.x0 + item.bbox[0], r.y0 + item.bbox[1],
                                      r.x0 + item.bbox[2], r.y0 + item.bbox[3]),
                            item.text[0] + "*" * (len(item.text) - 1),
                            "×" * len(item.text), item.text, "person",
                            f"第{pno + 1}页(图内文字)",
                            conf=engine.get_confidence("person", item.text))
                    _lone_masked.add((round(item.bbox[0]), round(item.bbox[1])))
                    _extra_hits += 1
        # 图内表头列锚定整格遮——表格语境图中人名格灰字被
        # v6 读成形近名（某悦天→某传灵/某俊灵 @0.61-0.69，首字非真实姓氏，
        # 置信阈值与 is_lone_ocr_name 均接不住）。改为以表头特征词行锚定列
        # 位置：其下方同列的 2~4 字纯 CJK 短行按疑似人名列值整格遮（首字+**）。
        # 精准度优于 band 连带（限同列+长度闸+标签后缀否定+否决表）；
        # 仅 _tbl_ctx 图启用。
        if _tbl_ctx and options.mask_person:
            try:
                _hdr_rows = [it for it in result.lines
                             if any(k in it.text for k in _TABLE_HEADER_HINTS)
                             and len(it.text.strip()) <= 6]
                _cell_stop = {"合计", "小计", "共计", "备注", "暂无", "无",
                              "以下", "以上", "日期", "姓名", "性别", "页数",
                              "审批", "盘点完成", "售后服务", "工程描述",
                              "是否含税", "单打折", "已完成", "未完成",
                              "审核中", "已审批"}  # 补流程图/清单标签；
                            # 批次2(2026-09) 补列头「性别」（示例乙 p11 性别→性* 误遮）
                for h in _hdr_rows:
                    hx = (h.bbox[0] + h.bbox[2]) / 2
                    hw = max(h.bbox[2] - h.bbox[0], 30.0)
                    for it2 in result.lines:
                        t2 = it2.text.strip()
                        if not (2 <= len(t2) <= 4):
                            continue
                        if not all("一" <= c <= "龥" for c in t2):
                            continue
                        if any(k in t2 for k in _TABLE_HEADER_HINTS) or t2 in _cell_stop:
                            continue
                        if _pure_org_suffix_fn(engine)(t2)[0]:
                            continue  # v1.9.3：纯组织后缀碎片（公司/集团/有限公司…）无脱敏意义
                        if t2.endswith(_FIELD_LABEL_SUFFIX):
                            continue  # 字段标签（申请单号/收款单位/重量…），非人名
                        if (round(it2.bbox[0]), round(it2.bbox[1])) in _lone_masked:
                            continue  # 孤立人名兜底已遮
                        cx2 = (it2.bbox[0] + it2.bbox[2]) / 2
                        if abs(cx2 - hx) > hw * 1.5:
                            continue  # 不同列
                        if not (h.bbox[3] - 2 <= it2.bbox[1] <= h.bbox[3] + 200):
                            continue  # 仅表头下方 200pt 内
                        report.record(original=t2,
                                      replacement=t2[0] + "*" * (len(t2) - 1),
                                      rule="person",
                                      location=f"第{pno + 1}页(图内表格人名)")
                        _draw_replacement(page, fitz.Rect(
                            r.x0 + it2.bbox[0], r.y0 + it2.bbox[1],
                            r.x0 + it2.bbox[2], r.y0 + it2.bbox[3]),
                            t2[0] + "*" * (len(t2) - 1))
                        if plan is not None:  # v1.9.4 埋点：列锚定成品矩形
                            plan.add_op_rect(
                                pno, "draw",
                                fitz.Rect(r.x0 + it2.bbox[0], r.y0 + it2.bbox[1],
                                          r.x0 + it2.bbox[2], r.y0 + it2.bbox[3]),
                                t2[0] + "*" * (len(t2) - 1),
                                t2[0] + "*" * (len(t2) - 1), t2, "person",
                                f"第{pno + 1}页(图内表格人名)",
                                conf=engine.get_confidence("person", t2))
                        _extra_hits += 1
            except Exception as e:
                # 列头锚定失败留痕（失败必须留痕：静默失败=表格人名格可能漏遮）
                try:
                    report.log(f"  第 {pno + 1} 页图内表格列锚定失败："
                               f"[{type(e).__name__}] {e}")
                except Exception:
                    pass
    # 按视觉阅读序（先上后下、先左后右）拼接——图片对象顺序与版面无关，
    # "签字："图与姓名图只有按阅读序拼接才能恢复前缀证据
    order = sorted(range(len(all_items)),
                   key=lambda i: (round(all_rects[i].y0), all_rects[i].x0))
    all_items = [all_items[i] for i in order]
    all_rects = [all_rects[i] for i in order]
    if plan is not None and all_items:
        # v1.9.4 埋点：图内 OCR good items（行 bbox 平移为页坐标——重放
        # 按 base_rects=None 走同一行拆分绘制，数学等价，蓝图 D1）
        plan.set_ocr_items(pno, [
            (it.text,
             (all_rects[i].x0 + it.bbox[0], all_rects[i].y0 + it.bbox[1],
              all_rects[i].x0 + it.bbox[2], all_rects[i].y0 + it.bbox[3]),
             it.confidence)
            for i, it in enumerate(all_items)])
    joined = _mask_joined_ocr_lines(page, all_items, engine, options, report,
                                    pno, "(图内文字)", base_rects=all_rects,
                                    plan=plan)
    # v1.7.3：图内表格金额列兜底（列头对齐裸数字；设计结论 2026-09-06）
    joined += _mask_ocr_amount_columns(page, all_items, all_rects, engine,
                                       options, report, pno, fitz, "(图内金额)",
                                       plan=plan)
    return joined + invoice_hits + _extra_hits


def _extract_chars(page) -> tuple:
    """提取字符级坐标：返回 (全文文本, [字符bbox...])，两者按下标一一对应。

    v1.6.6：行/块之间插入换行符——此前无分隔符拼接会让表格单元格边界
    消失（“新增某浩-副总裁”的 CJK 邻接把人名判为词碎片而漏检）。
    换行符不参与遮罩矩形计算（isspace 过滤），仅还原文本行结构。
    """
    raw = page.get_text("rawdict")
    chars = []
    boxes = []
    for block in raw.get("blocks", []):
        if block.get("type") != 0:  # 非文本块（图片等）
            continue
        for line in block.get("lines", []):
            line_start = len(chars)
            for span in line.get("spans", []):
                for ch in span.get("chars", []):
                    c = ch.get("c", "")
                    box = ch.get("bbox")
                    if c and box:
                        chars.append(c)
                        boxes.append(box)
            if len(chars) > line_start:  # 本行有内容 → 行尾补换行
                if chars and chars[-1] != "\n":
                    prev = boxes[-1] if boxes else (0, 0, 0, 0)
                    chars.append("\n")
                    boxes.append((prev[2], prev[1], prev[2], prev[3]))
        if chars and chars[-1] != "\n":
            prev = boxes[-1] if boxes else (0, 0, 0, 0)
            chars.append("\n")
            boxes.append((prev[2], prev[1], prev[2], prev[3]))
    return "".join(chars), boxes


def _header_column_person_spans(text, char_boxes, engine, spans):
    """表头列纵向关联人名（v1.7）。

    变更记录/审批表「批准人」列头与其数据值隔着整行表头文本（
    "批准人 变更描述
A0 2021-12-30 某兵 新增"），行内前缀窗口够不到。
    取列头词 bbox 正下方 ≤90pt、横向重叠的最近一行 2~4 字 CJK 块，
    经 force_person 档（黑名单/虚词闸门保留、上下文豁免）校验后作为
    人名 span 返回；与既有 span 重叠的块跳过。
    """
    out = []
    # 关键词表改用模块顶 ocr_mod_rules 引用（原此处为
    # 绝对包名 __import__ 动态导入 rules_engine，是打包器重定位隐患；
    # sys.modules 中本就是同一对象，零行为变化）。
    # v1.9.1 渲染确定性：key=(-len, 词) 完全决定序（原同长度组内回落 set
    # 迭代序，随 PYTHONHASHSEED 漂移；先命中先占的 existing 去重取舍随之
    # 不稳定）
    kws = sorted(set(list(ocr_mod_rules._KEYWORD_BEFORE)
                     + list(ocr_mod_rules._FORM_FIELD_KEYS)),
                 key=lambda k: (-len(k), k))
    if not kws:
        return out
    existing = [(sp.start, sp.end) for sp in spans]
    for kw in kws:
        start = 0
        while True:
            i = text.find(kw, start)
            if i < 0:
                break
            start = i + 1
            j_end = i + len(kw)
            if j_end >= len(char_boxes):
                continue
            kb = [char_boxes[k] for k in range(i, j_end)
                  if not text[k].isspace()]
            if not kb:
                continue
            kx0 = min(b[0] for b in kb)
            kx1 = max(b[2] for b in kb)
            ky1 = max(b[3] for b in kb)
            cx = (kx0 + kx1) / 2
            half = max((kx1 - kx0) * 0.75, 14.0)
            rows = {}
            for j2 in range(j_end, min(len(text), j_end + 400)):
                b = char_boxes[j2]
                if b[1] > ky1 + 100:
                    break
                ch = text[j2]
                if ch.isspace() or not ("一" <= ch <= "龥"):
                    continue
                bcx = (b[0] + b[2]) / 2
                if abs(bcx - cx) <= half:
                    rows.setdefault(round(b[1], 1), []).append((j2, ch))
            if not rows:
                continue
            # 全部同列行（变更记录/比价表同一列可有多行数据值）
            for y_key in sorted(rows):
                cells = sorted(rows[y_key])
                # v1.7.2 对齐轮：横向孤立性校验——块边不足 1 字宽的同行 CJK
                # 邻字说明捕获的是行内正文片段（如“与公司经济业务”被列范围
                # 截成“司经济”），不是孤立表单值，跳过
                s0p = cells[0][0]
                s1p = cells[-1][0]
                bw = max(char_boxes[k][2] - char_boxes[k][0]
                         for k, _ in cells) or 1.0
                row_y = char_boxes[s0p][1]
                inline = False
                for nb in (s0p - 1, s1p + 1):
                    if 0 <= nb < len(text) and "\u4e00" <= text[nb] <= "\u9fa5":
                        nbx = char_boxes[nb]
                        if abs(nbx[1] - row_y) < 5 and (
                                (nb < s0p and char_boxes[s0p][0] - nbx[2] < bw)
                                or (nb > s1p and nbx[0] - char_boxes[s1p][2] < bw)):
                            inline = True
                            break
                if inline:
                    continue
                block = "".join(ch for _, ch in cells)
                if not (2 <= len(block) <= 4):
                    continue
                s0 = cells[0][0]
                s1 = s0 + len(block)
                if any(s0 < e and s1 > st for st, e in existing):
                    continue
                for sp2 in engine.find_spans(block, force_person=True):
                    if sp2.text == block:
                        out.append(type(sp2)(s0, s1, sp2.rule, block,
                                             sp2.replacement, sp2.confidence))
                        existing.append((s0, s1))
                        break
    return out


def _mask_native(page, text: str, char_boxes: list, engine, report, pno: int,
                 plan=None) -> tuple:
    """原生文本链路：redact 销毁原文，再在原位写入脱敏替换内容。

    v1.6.8：
    - 跨行实体二次匹配（"示例甲家居股份\\n有限公司"类）：对去换行副本
      再跑一遍引擎，命中区间经索引映射拆回原文本的连续字符段分别遮盖；
    - redact 失效兜底：部分页面（填充+描边双绘/特殊内容流）apply 后
      原文仍在文本层，此时整页光栅化重建（视觉不变，文本层彻底销毁）。

    v1.9.4：绘制段抽出为 _apply_native_spans（检测与冻结重放共用同一
    redact 批→record→绘制路径）；plan 为首轮冻结采集器（纯旁路记录）。
    """
    spans = list(engine.find_spans(text))

    # ---- 跨行实体二次匹配（相邻行边界窗口拼接，避免整页拼接的 O(n²) 枚举）----
    if "\n" in text:
        WIN = 12  # 行尾/行首取多少字参与拼接
        lines = text.split("\n")
        offsets = []
        pos = 0
        for ln in lines:
            offsets.append(pos)
            pos += len(ln) + 1
        existing = {(s.start, s.end) for s in spans}
        extra_spans = []
        for k in range(len(lines) - 1):
            tail = lines[k][-WIN:]
            head = lines[k + 1][:WIN]
            if not tail or not head:
                continue
            window = tail + head
            split_at = len(tail)
            for sp2 in engine.find_spans(window):
                if sp2.start >= split_at or sp2.end <= split_at:
                    continue  # 未跨越拼接点（行内可命中的已由首轮覆盖）
                # 映射回原文本区间
                a0 = offsets[k] + len(lines[k]) - len(tail) + sp2.start
                a1 = offsets[k] + len(lines[k])
                b0 = offsets[k + 1]
                b1 = offsets[k + 1] + sp2.end - split_at
                if (a0, b1) in existing:
                    continue
                existing.add((a0, b1))
                # v1.9.3：拆分残片同过「纯组织后缀」硬阻拦（实测通道：行尾
                # 「…股份有限」+行首「公司…」残片行=映射表碎词行来源；区别性
                # 成分残片照常遮蔽，纯后缀残片不遮不记——残留后缀不构成泄漏）
                _parts = [(a0, a1, text[a0:a1], sp2.replacement),
                          (b0, b1, text[b0:b1], "×" * (b1 - b0))]
                for _s0, _s1, _seg, _repl in _parts:
                    if _s1 - _s0 < 1 or _pure_org_suffix_fn(engine)(_seg)[0]:
                        continue
                    extra_spans.append(type(sp2)(_s0, _s1, sp2.rule,
                                                 _seg, _repl,
                                                 sp2.confidence))
        spans.extend(extra_spans)

    # v1.7 表头列纵向关联人名（变更记录表「批准人」列 → 数据值）
    if char_boxes and spans:
        try:
            spans.extend(_header_column_person_spans(text, char_boxes, engine,
                                                     spans))
        except Exception:
            pass
    spans.sort(key=lambda s: s.start)

    if plan is not None:
        # v1.9.4 埋点：整页文本快照（重放逐页校验 + 增行定位双重身份）。
        # 在 spans 早退判定之前——零命中页也要有文本供增行定位。
        plan.set_native_text(pno, text)
    if not spans:
        return 0, []  # v1.6.9：修复早退分支返回 int 导致整页被跳过（TypeError）
    hit_rects = _apply_native_spans(page, text, char_boxes, spans, report, pno,
                                    plan=plan)
    if not hit_rects:
        return 0, []  # v1.6.9：同上，早退返回与签名一致的 2 元组

    # ---- redact 失效检测：部分页面（填充+描边双绘等特殊内容流）apply 后
    # 原文仍在文本层。返回泄漏清单，由调用方在全部页处理完后统一光栅化
    # （迭代中 insert/delete 页会破坏文档页状态）。
    after = page.get_text("text").replace("\n", "")
    leaked = [(rect, sp) for rect, sp in hit_rects
              if sp.text.replace("\n", "") in after]
    return len(hit_rects), leaked


def _apply_native_spans(page, text: str, char_boxes: list, spans: list,
                        report, pno: int, plan=None) -> list:
    """原生 span 集合的统一绘制路径（v1.9.4 自 _mask_native 抽出）。

    首轮检测与冻结重放共用：span→char_boxes 折算矩形（±0.5pt 外扩）→
    add_redact_annot 批（单次 apply）→ record → 原位白底黑字绘制。
    spans：带 start/end/rule/text/replacement/confidence 的 span 序列
    （已按 start 排序）。返回 (rect, span) 列表（泄漏检测沿用）。
    """
    fitz = _import_fitz()
    hit_rects = []
    for sp in spans:
        boxes = [char_boxes[i] for i in range(sp.start, sp.end) if not text[i].isspace()]
        if not boxes:
            continue
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[2] for b in boxes)
        y1 = max(b[3] for b in boxes)
        rect = fitz.Rect(x0 - 0.5, y0 - 0.5, x1 + 0.5, y1 + 0.5)
        page.add_redact_annot(rect, fill=False)  # 仅销毁底层文字；替换内容随后绘制
        hit_rects.append((rect, sp))
    if not hit_rects:
        return []
    page.apply_redactions(images=getattr(fitz, "PDF_REDACT_IMAGE_NONE", 0))
    for rect, sp in hit_rects:
        report.record(sp.text, sp.replacement, sp.rule, f"第{pno + 1}页")
        _draw_replacement(page, rect, sp.replacement)
    if plan is not None:
        # v1.9.4 埋点：native span（与实际绘制集合一致，调用点在绘制之后）
        plan.add_native_spans(pno, [
            (sp.start, sp.end, sp.rule, sp.replacement, sp.confidence, sp.text)
            for _rect, sp in hit_rects])
    return hit_rects


def _rasterize_page(page) -> bool:
    """整页光栅化重建（v1.6.8 redact 失效兜底）。

    渲染当前页面（含已绘制的替换块）为位图，插入新页替换旧页——旧内容流
    整体废弃，文本层彻底销毁；视觉布局保持（变为图像页）。
    ⚠ 必须在页面迭代循环之外调用（迭代中 insert/delete 会破坏页状态）。

    v1.7 修复多页光栅化：insert/delete 后 PyMuPDF 的 Page 对象缓存失效，
    旧实现 insert_page(旧页位) 后经 doc[new_pno] 取到的可能是过期对象，
    贴图会落到错误页面（实测一页出现双图、另一页全白）。改为先删旧页、
    在原位插入新页后重新取页对象再贴图，每一步都基于重建后的页树。
    """
    fitz = _import_fitz()
    try:
        doc = page.parent
        pno = page.number  # 先取页码：delete/insert 后旧 page 对象失效
        zoom = 200 / 72.0
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        w, h = page.rect.width, page.rect.height
        doc.delete_page(pno)
        doc.insert_page(pno, width=w, height=h)
        newp = doc[pno]  # 删除后页树重建，重新取页对象
        newp.insert_image(newp.rect, pixmap=pix)
        return True
    except Exception:
        return False  # 兜底失败时至少保留视觉遮盖结果


def _draw_replacement(page, rect, rep: str) -> None:
    """在矩形区域以白底黑字显示脱敏替换文本。

    坐标空间：rect 为“显示方向”坐标；draw_rect/insert_text 绘制在
    “未旋转页面空间”——旋转页（rotation != 0）必须乘 derotation_matrix。
    """
    fitz = _import_fitz()
    r = fitz.Rect(rect)
    if page.rotation:
        r = r * page.derotation_matrix
    if r.is_empty or r.is_infinite:
        return
    page.draw_rect(r, color=None, fill=PDF_MASK_FILL, overlay=True)
    fontsize = _fit_fontsize(r, rep)
    baseline = r.y1 - (r.y1 - r.y0) * 0.22
    page.insert_text(fitz.Point(r.x0 + 1, baseline), rep, fontname="china-s",
                     fontsize=fontsize, color=PDF_TEXT_COLOR)


def _fit_fontsize(rect, text: str) -> float:
    """按矩形高宽与替换文本长度估算字号（CJK 全宽 / ASCII 半宽加权）。

    观感补项：巨矩形（发票截图区
    整图遮蔽 rect 高数百 pt）此前 h*0.72 分支直接放大星号到半页——
    「巨大星形黑块」观感缺陷。补绝对上限 _REPL_FONT_MAX（24pt≈大标题
    级；正常文本行 h≈12pt→h*0.72≈8.6 远不触上限，行为零变化）。
    """
    h = max(1.0, rect.y1 - rect.y0)
    w = max(1.0, rect.x1 - rect.x0)
    length = sum(1.0 if ord(c) > 0x2E7F else 0.55 for c in text) or 1.0
    return max(4.0, min(h * 0.72, w / (length * 0.92), _REPL_FONT_MAX))


# 替换文本字号绝对上限（pt）：仅巨 rect（截图整图遮蔽类）触发钳制
_REPL_FONT_MAX = 24.0


def _draw_black(page, bbox) -> None:
    """纯黑遮盖（用于红色印章等无替换文本语义的区域）。

    坐标空间同 _draw_replacement：旋转页需乘 derotation_matrix。
    """
    fitz = _import_fitz()
    r = fitz.Rect(bbox)
    if page.rotation:
        r = r * page.derotation_matrix
    if r.is_empty or r.is_infinite:
        return
    page.draw_rect(r, color=None, fill=(0, 0, 0), overlay=True)


def _span_rect_in_line(line_bbox, line_text: str, start: int, end: int,
                       extra_pad: float = 0.0) -> tuple:
    """在 OCR 行框内按字符宽度比例估计命中片段的子矩形。

    OCR 只有行级坐标：以 CJK 全宽 / ASCII 半宽加权估算字符水平位置，
    y 方向取整行高度。近似定位。extra_pad：按行宽比例的额外左右扩展
    （v1.7 残字扩展：等宽折算与真实字形存在边缘偏差，实体命中处外扩，
    防止"公司""雅"等原字边缘残迹露出——截断/残字≠脱敏）。
    """
    x0, y0, x1, y1 = line_bbox

    def weight(ch) -> float:
        return 1.0 if ord(ch) > 0x2E7F else 0.55

    total = sum(weight(c) for c in line_text) or 1.0
    before = sum(weight(c) for c in line_text[:start])
    span = sum(weight(c) for c in line_text[start:end])
    width = x1 - x0
    fx0 = before / total
    fx1 = (before + span) / total
    pad = width * 0.01 + extra_pad
    return (x0 + width * fx0 - pad, y0 - 1, x0 + width * fx1 + pad, y1 + 1)


# ---------------------------------------------------------------------------
# 链路 B：OCR 结果坐标选择
# ---------------------------------------------------------------------------

def _select_items(result) -> list:
    """表格区域优先使用单元格级坐标，非表格区域使用行级坐标。"""
    cells = result.table_cells
    if not cells:
        return list(result.lines)
    items = list(cells)
    for line in result.lines:
        if not any(_overlap_ratio(line.bbox, c.bbox) > 0.5 for c in cells):
            items.append(line)
    return items


def _overlap_ratio(a, b) -> float:
    """bbox a 与 b 的相交面积占 a 面积的比例。"""
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    area_a = max(1e-6, (ax1 - ax0) * (ay1 - ay0))
    return (ix * iy) / area_a


# ---------------------------------------------------------------------------
# OCR 引擎懒加载
# ---------------------------------------------------------------------------

def _ocr_engine_cached(name):
    """只读探测：进程级引擎缓存（ocr_engine._ENGINE_CACHE）是否已有该引擎实例。

    仅用于日志措辞分流（v2.0.1 措辞修正，依据引擎驻留评估记录：
    auto/legacy_v5 引擎本就进程级常驻，「首次使用…初始化」逐文件弹出系每文件
    getter 局部 holder 恒空所致的误报）。不构造、不写入、不改动任何缓存与
    构造生命周期；经 getattr 防御性读取，测试以假 ocr_mod 整体替换时自然返回
    False（走完整初始化日志路径，既有断言语义不变）。
    """
    cache = getattr(ocr_mod, "_ENGINE_CACHE", None)
    if not isinstance(cache, dict):
        return False
    return any(isinstance(k, tuple) and len(k) >= 1 and k[0] == name
               for k in cache)


def _ocr_mode_engine_cached(mode):
    """只读探测：指定 OCR 模式的引擎在进程内是否已常驻（日志措辞分流用）。

    - gpu_v6：ExternalV6GpuEngine 进程级单例（_V6_GPU_ENGINE_SINGLETON，worker
      常驻，T3 实测唯一 PID 贯穿全批）；不入 _ENGINE_CACHE，单独探测。
    - cpu_only / gpu_v5 / 其余模式：以 getattr 防御性探测 ocr_engine 的
      per-mode 常驻缓存（_MODE_ENGINE_CACHE，C3 接口落地后即自动生效）；
      未落地前探测恒 False → 按真实冷启动打印完整初始化日志（cpu_only/gpu_v5
      在无 per-mode 缓存时确为每文件重建，如实报）。
    """
    mode_cache = getattr(ocr_mod, "_MODE_ENGINE_CACHE", None)
    if isinstance(mode_cache, dict) and mode_cache.get(mode) is not None:
        return True
    from .. import app_config as _app_config
    if mode == _app_config.OCR_MODE_GPU_V6:
        return getattr(ocr_mod, "_V6_GPU_ENGINE_SINGLETON", None) is not None
    return False


def _make_ocr_getter(report, src=None, rule_engine=None, options=None):
    """首次用到时才初始化 OCR 引擎（模型加载耗时较长，且无扫描页时不该报错）。

    v1.8.2 起模式感知（config.json 的 ocr_mode 键，经 app_config.resolve_ocr_mode）：
    - legacy_v5（存量 ocr_engine=ppocrv5 且未写过 ocr_mode）：完整保留 v1.8.1
      逻辑（get_ocr_engine_name 路径 + v6 失败回退 v5 分支原样），存量用户语义不变；
    - auto：与 v1.8.1 默认路径逐行等价（PP-OCRv6-ONNX，档位自动 CPU=small/GPU=medium）；
    - cpu_only / gpu_v5 / gpu_v6：经 create_engine_for_mode() 创建，任何
      MissingDependencyError 统一回退 CPU（v6-small）并 report.log 人话提示；
      回退发生在任何文档产出之前，兜底也失败才抛（禁止崩溃、禁止半成品输出）。

    v2.0.1 日志措辞（T3 误报修正，仅文案与打印条件，构造路径零变化）：引擎
    真冷启动（进程内无常驻实例）才打「首次使用 OCR…正在初始化引擎」；引擎已
    进程内常驻（缓存/单例命中）时只打一行「OCR 引擎复用（进程内常驻）」，不再
    逐文件重弹初始化日志（探测口径见 _ocr_engine_cached/_ocr_mode_engine_cached）。

    v1.9.0 分析缓存：src/options 齐备时（process() 主链路）引擎初始化后包一层
    CachedOcrEngine（app/analysis_cache.py）——consume=rule_engine.mapping_only
    （仅映射对照表覆盖轮读缓存），auto/legacy 轮只记录新站点不读（当轮输出零
    变化，顺带为复核轮建缓存）。缓存文件 `<输出根>/cache/<源文件sha256>.json`。
    包装失败/缓存任何异常只留痕降级为直连引擎（纯性能层，绝不影响脱敏结果）。
    单测直呼 `_make_ocr_getter(report)`（src=None）保持无缓存直连语义。
    """
    from .. import app_config
    from ..utils import APP_VERSION

    mode = app_config.resolve_ocr_mode()
    holder = {}
    state = {"reused": False}  # 本轮初始化是否命中进程内常驻（日志措辞分流）

    def _init_engine():
        state["reused"] = False
        if mode == app_config.OCR_MODE_LEGACY_V5:
            # —— 存量 v5 用户：v1.8.1 逻辑原样 ——
            name = app_config.get_ocr_engine_name()
            if _ocr_engine_cached(name):
                state["reused"] = True
                return ocr_mod.get_ocr_engine(name)
            report.log(f"首次使用 OCR：正在初始化 {name} 引擎（加载本地模型，请稍候）…")
            try:
                return ocr_mod.get_ocr_engine(name)
            except MissingDependencyError as e:
                if name == app_config.OCR_ENGINE_V5:
                    raise
                # v1.8.0：v6 初始化失败（缺组件/模型/GPU 异常）→ 明确回退 v5
                # （get_ocr_engine() 默认档同为 v6，重试无意义）
                report.log(f"OCR 引擎 {name} 初始化失败（{e}），回退 PP-OCRv5。")
                return ocr_mod.get_ocr_engine(app_config.OCR_ENGINE_V5)
        if mode == app_config.OCR_MODE_AUTO:
            # —— 自动：与 v1.8.1 默认路径逐行等价（选 v5 的存量用户在上方
            #    legacy_v5 分支处理，走到这里的配置恒为 v6）——
            name = app_config.OCR_ENGINE_V6_ONNX
            if _ocr_engine_cached(name):
                state["reused"] = True
                return ocr_mod.get_ocr_engine(name)
            report.log(f"首次使用 OCR：正在初始化 {name} 引擎（加载本地模型，请稍候）…")
            try:
                return ocr_mod.get_ocr_engine(name)
            except MissingDependencyError as e:
                report.log(f"OCR 引擎 {name} 初始化失败（{e}），回退 PP-OCRv5。")
                return ocr_mod.get_ocr_engine(app_config.OCR_ENGINE_V5)
        # —— cpu_only / gpu_v5 / gpu_v6：统一入口 + per-mode 缓存 + CPU 兜底 ——
        if _ocr_mode_engine_cached(mode):
            state["reused"] = True
            # v2.0.1 C3：gpu_v6 仍经健康守卫接线（worker 中途失效自愈），
            # 守卫为轻包装、worker 进程常驻；cpu/gpu_v5 直取缓存实例
            if mode == app_config.OCR_MODE_GPU_V6:
                return ocr_mod.GpuV6ResilientEngine(mode, report.log)
            return ocr_mod.get_engine_for_mode(mode, report_log=report.log)
        report.log(f"首次使用 OCR（模式：{mode}）：正在初始化引擎（加载本地模型，请稍候）…")
        try:
            if mode == app_config.OCR_MODE_GPU_V6:
                # v2.0.1 C3：gpu_v6 经健康守卫接线（worker 识别中途失效 →
                # invalidate+重建一次 → 仍失败走下方既有 CPU 兜底，不崩）
                return ocr_mod.GpuV6ResilientEngine(mode, report.log)
            return ocr_mod.get_engine_for_mode(mode, report_log=report.log)
        except MissingDependencyError as e:
            report.log(f"所选 OCR 模式不可用（{e}），已回退 CPU（v6-small）。")
            return ocr_mod.create_cpu_fallback_engine()

    def _wrap_with_cache(eng):
        """v1.9.0：按源文件上下文包分析缓存代理；任何异常降级为直连引擎。"""
        try:
            from .. import analysis_cache
            # v2.1.8 words_only 同享缓存消费（曾全量跑过的文件零 OCR）
            consume = bool(getattr(rule_engine, "mapping_only", False)) \
                or bool(getattr(rule_engine, "_words_only", False))
            output_root = analysis_cache.resolve_output_root(src, options)
            cache_path = analysis_cache.cache_path_for(src, output_root)
            return analysis_cache.CachedOcrEngine(
                eng, cache_path, app_version=APP_VERSION,
                report_log=report.log, consume=consume)
        except Exception as e:
            try:
                report.log(f"分析缓存初始化失败（不影响处理）："
                           f"[{type(e).__name__}] {e}")
            except Exception:
                pass
            return None

    def get_ocr():
        if holder.get("engine") is None:
            eng = _init_engine()
            cache = _wrap_with_cache(eng) if src is not None else None
            holder["engine"] = cache if cache is not None else eng
            holder["cache"] = cache
            get_ocr.analysis_cache = cache  # process 末尾 save/统计取用
            if state["reused"]:
                # v2.0.1 T3 措辞修正：引擎进程内常驻（缓存/单例命中），不再
                # 逐文件重弹「初始化/已就绪」，一行如实说明复用即可。
                report.log(f"OCR 引擎复用（进程内常驻）：{holder['engine'].name}")
            else:
                report.log(f"OCR 引擎已就绪：{holder['engine'].name}")
        return holder["engine"]

    return get_ocr


def _finish_analysis_cache(get_ocr, report) -> None:
    """v1.9.0：单文件完成时落盘分析缓存并统计命中（纯性能层，失败只留痕）。

    - save 合并写回 `<输出根>/cache/<源文件sha256>.json`；
    - 映射覆盖轮（consume）命中时输出「命中 N/M 项，跳过 OCR」；
      未命中原因已在缓存打开时记录；
    - 引擎本轮从未初始化（无 OCR 消费页/快速直通）时无事可做。
    """
    if get_ocr is None:
        return
    cache = getattr(get_ocr, "analysis_cache", None)
    if cache is None:
        return
    try:
        cache.save()
        st = cache.stats()
        if st.get("consume") and st.get("hits"):
            total = st["hits"] + st["misses"]
            report.log(f"分析缓存：命中 {st['hits']}/{total} 项，跳过 OCR")
    except Exception as e:
        try:
            report.log(f"分析缓存落盘失败（不影响处理）：[{type(e).__name__}] {e}")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 红色印章遮盖（可选，预留图像处理接口）
# ---------------------------------------------------------------------------

def _mask_stamps_on_page(page, fitz, report, pno: int, plan=None) -> int:
    zoom = OCR_DPI / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return _mask_stamps_from_png(page, pix.tobytes("png"), report, pno,
                                 plan=plan)


# 合并前组件追溯（终闸回退用）——键=矩形元组值
_STAMP_MERGE_TRACE: dict = {}


def _merge_same_line_rects(rects, gap_factor=3.0):
    """同一文字行上的相邻红色组件合并为整行矩形。

    逐组件遮盖会在字符间隙处留下可读残字（碎词方向）；红色批注/红字行为
    连续文本，按行合并后整行遮盖。y 中心距 < 0.6×高 且 x 间隙 < 3×行高
    视为同行。
    合并时把「合并前组件清单」记入 _STAMP_MERGE_TRACE（多轮
    合并累积），供 _stamp_final_gate 超限时回退到逐个已过形状闸的组件。
    """
    if len(rects) <= 1:
        return rects
    changed = True
    cur = list(rects)
    while changed:
        changed = False
        out = []
        while cur:
            r = cur.pop()
            merged = False
            for i, o in enumerate(out):
                cy_r = (r[1] + r[3]) / 2
                cy_o = (o[1] + o[3]) / 2
                h = max(r[3] - r[1], o[3] - o[1], 1.0)
                gap = max(r[0] - o[2], o[0] - r[2], 0.0)
                if abs(cy_r - cy_o) < 0.6 * h and gap < gap_factor * h:
                    new = (min(o[0], r[0]), min(o[1], r[1]),
                           max(o[2], r[2]), max(o[3], r[3]))
                    out[i] = new
                    comps = list(_STAMP_MERGE_TRACE.get(tuple(o), [o])) \
                        + list(_STAMP_MERGE_TRACE.get(tuple(r), [r]))
                    _STAMP_MERGE_TRACE.pop(tuple(o), None)
                    _STAMP_MERGE_TRACE.pop(tuple(r), None)
                    _STAMP_MERGE_TRACE[new] = comps
                    merged = True
                    changed = True
                    break
            if not merged:
                out.append(r)
        cur = out
    return cur


def _extend_rects_to_text_lines(page, rects):
    """把红色矩形在同行上扩展到完整文字行。

    面积阈值滤掉的小号红字组件会留下可读残段（碎词方向）；利用文本层把
    与矩形垂直重叠、水平邻近的文字一并纳入遮盖带。图片页文本层为空时保持原矩形。
    """
    if not rects:
        return rects
    try:
        words = page.get_text("words")  # (x0, y0, x1, y1, word, ...)
    except Exception:
        return rects
    if not words:
        return rects
    out = []
    for (x0, y0, x1, y1) in rects:
        h = max(y1 - y0, 1.0)
        cy = (y0 + y1) / 2
        nx0, nx1 = x0, x1
        for w in words:
            wy0, wy1 = w[1], w[3]
            wcy = (wy0 + wy1) / 2
            if abs(wcy - cy) > 0.7 * max(h, wy1 - wy0):
                continue
            gap = max(x0 - w[2], w[0] - x1, 0.0)
            # v2.1.5 观感收窄：吞行半径原锚 5×印章框高——h≈100pt 时半径
            # ~500pt，同行几乎全吞（审计报告实测：118pt 印章框被扩到
            # 446pt 横条，吞入的竟是已画好的替换文本「ABC股份有限公司」，
            # 纯重复遮蔽无安全收益）。防残段的真实尺度是「小红字碎片
            # 紧贴印章主体」（防残段动机），改锚 2×邻字高（保底 8pt）：
            # 碎片照吞，远距邻字不再连带。
            if gap <= 2.0 * max(w[3] - w[1], 8.0) \
                    or (w[0] >= x0 - 1 and w[2] <= x1 + 1):
                nx0 = min(nx0, w[0])
                nx1 = max(nx1, w[2])
        out.append((nx0, y0, nx1, y1))
        # extend 改变了矩形值——迁移组件追溯（键随值走）
        if (nx0, y0, nx1, y1) != (x0, y0, x1, y1):
            _STAMP_MERGE_TRACE[(nx0, y0, nx1, y1)] = \
                _STAMP_MERGE_TRACE.pop((x0, y0, x1, y1), [(x0, y0, x1, y1)])
    return out


def _stamp_final_gate(page, rects, report, pno: int) -> list:
    """印章成品矩形终闸。

    形状闸（STAMP_LONG_EDGE_MAX_RATIO 等）此前作用于**单个连通域 bbox**，
    而 merge/extend 在闸后执行——一排过闸小红组件（红框表格/红字批注）
    可被无界合并成整页黑条（v2.1.5 前实测 118pt 印章被扩成 446pt 横条）。
    终闸在合并/扩行之后以同口径复检：成品矩形长边 >55% 页短边 → 回退为
    该矩形**合并前的原始组件**（逐个已过形状闸，安全）；仍超限的极端
    组件（单个连通域即超）保留（宁多遮不漏）并留痕。
    """
    pw = min(page.rect.width, page.rect.height) or 1.0
    out = []
    for r in rects:
        long_edge = max(r[2] - r[0], r[3] - r[1])
        if long_edge <= STAMP_LONG_EDGE_MAX_RATIO * pw:
            out.append(r)
            continue
        # 超限：尝试回退该矩形到合并前组件（值键取 trace——merge/extend
        # 均按矩形元组值维护）
        src = _STAMP_MERGE_TRACE.pop(tuple(r), None)
        if src:
            n_ok = 0
            for c in src:
                if max(c[2] - c[0], c[3] - c[1]) <= STAMP_LONG_EDGE_MAX_RATIO * pw:
                    out.append(c)
                    n_ok += 1
            if report is not None:
                report.log(f"  第 {pno + 1} 页：印章合并矩形长边 {long_edge:.0f}pt "
                           f"超终闸（>{STAMP_LONG_EDGE_MAX_RATIO:.0%} 页短边），"
                           f"回退为 {n_ok}/{len(src)} 个原始组件")
        else:
            out.append(r)
            if report is not None:
                report.log(f"  第 {pno + 1} 页：印章矩形长边 {long_edge:.0f}pt "
                           f"超终闸但无组件记录可回退，保守保留（宁多遮不漏）")
    return out


def _mask_stamps_from_png(page, png_bytes: bytes, report, pno: int,
                          plan=None) -> int:
    rects = detect_red_regions(png_bytes, OCR_DPI)
    if rects is None:
        if not _STAMP_HINT_SHOWN["flag"]:
            _STAMP_HINT_SHOWN["flag"] = True
            report.log("提示：未安装 OpenCV，无法检测红色印章（pip install opencv-python numpy）")
        return 0
    _STAMP_MERGE_TRACE.clear()  # id 键仅批内有效，防跨页陈旧引用
    rects = _merge_same_line_rects(rects)
    rects = _extend_rects_to_text_lines(page, rects)
    rects = _stamp_final_gate(page, rects, report, pno)
    # 先 redaction 移除区域内的底层文本对象（合成样本实测——
    # 环形印章文字是矢量文本，仅画黑块时文本层仍可提取还原），再画遮块。
    # images=IMAGE_NONE——印章误报区域
    # 的内嵌照片像素不再被物理抹除（文本通道同款保护；图片销毁由光栅化
    # 机制统一承担），误报后果从「数据破坏」降回「观感」。
    try:
        for r in rects:
            page.add_redact_annot(r)
        if rects:
            page.apply_redactions(
                images=getattr(_import_fitz(), "PDF_REDACT_IMAGE_NONE", 0))
    except Exception as e:
        report.log(f"  第 {pno + 1} 页红章 redaction 失败（退化为仅画块）：[{type(e).__name__}] {e}")
    for i, r in enumerate(rects, 1):
        _draw_black(page, r)
        # 红章命中入映射表（此前仅日志，复核方在映射表中不可见）
        try:
            report.record(original=f"红色印章区域#{i}", replacement="██",
                          rule="stamp", location=f"第{pno + 1}页")
        except Exception:
            pass
        if plan is not None:  # v1.9.4 埋点：印章成品矩形（redact 批+纯黑块）
            plan.add_op_rect(pno, "stamp", r, "██", "██",
                             f"红色印章区域#{i}", "stamp", f"第{pno + 1}页")
    if rects:
        report.log(f"  第 {pno + 1} 页：遮盖红色印章区域 {len(rects)} 处")
    return len(rects)


def _stamp_star_like(cnt) -> bool:
    """五角星顶点特征（v2.1.1 S2 契约 §二 b）：approx 顶点 8~14（五角星 10 个
    顶点的近似容差带）且凸实度 ≤0.8（星形凹陷使轮廓面积明显低于凸包）。
    满足者按星形保留，不落入实心圆盘拒判。"""
    try:
        import cv2
    except ImportError:  # 与 detect_red_regions 同口径：无 OpenCV 不启用
        return False
    peri = cv2.arcLength(cnt, True)
    approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)
    if not (8 <= len(approx) <= 14):
        return False
    hull_area = cv2.contourArea(cv2.convexHull(cnt))
    return cv2.contourArea(cnt) / max(hull_area, 1e-6) <= 0.8


def detect_red_regions(png_bytes: bytes, dpi: int, min_area_pt2: float = STAMP_MIN_AREA_PT2):
    """检测渲染图中的红色区域（印章），返回 PDF 点坐标 bbox 列表。

    OpenCV 不可用时返回 None（上层据此给出提示）。
    v2.1.1 S2：HSV/面积/细长线闸之后新增形状校验（契约 §二 a 填充率带通/
    b 圆度或星形/c 尺寸域上限；阈值双面标定见模块头 STAMP_* 常量注释与
    tests/test_stamp_shape_gate.py），红框表格、红底实心单元格、橙红圆形
    logo 等非印章红域不再检出；真章零漏以 v210 真实管线 stamp 面逐矩形
    复检锁定（真值面覆盖与修复前位元级一致）。
    TODO: 当前为 HSV 阈值 + 连通域的简单实现，生产可替换为印章检测模型；
          圆形印章建议按环形区域遮盖而非整体矩形，以保留印文下的正文。
    """
    if not png_bytes:  # 空输入防御（cv2.imdecode 空 buf 断言炸）
        return []
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    arr = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        return []
    hsv = cv2.cvtColor(arr, cv2.COLOR_BGR2HSV)
    m1 = cv2.inRange(hsv, (0, 80, 60), (10, 255, 255))
    m2 = cv2.inRange(hsv, (170, 80, 60), (180, 255, 255))
    mask = cv2.morphologyEx(m1 | m2, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    scale = 72.0 / float(dpi)
    page_short_pt = min(mask.shape[0], mask.shape[1]) * scale
    rects = []
    for cnt in contours:
        cnt_area = cv2.contourArea(cnt)
        if cnt_area * scale * scale < min_area_pt2:
            continue
        x, y, w, h = cv2.boundingRect(cnt)
        # 细长红字行排除——宽高比 >5 且行高 <14pt 的红色连通域
        # 是红色批注/提示文字行（直营店 p15「备注项…12 个字」、OA p4「收款
        # 银行需明细到开卡支行」实例），非印章印迹；印章为近圆形/椭圆团块
        if w * scale / max(h * scale, 0.1) > 5.0 and h * scale < 14.0:
            continue
        # ---- v2.1.1 S2 形状闸（契约 §二 a/b/c；阈值标定见模块头注释）----
        # a) 填充率带通：分量红像素数/bbox 面积 ∈ [0.020, 0.95]。红像素数取
        #    外轮廓多边形填充 ∩ 红掩码（含被包围子分量，如环内五角星）。
        crop = mask[y:y + h, x:x + w]
        poly = np.zeros((h, w), np.uint8)
        cv2.drawContours(poly, [cnt], -1, 1, thickness=-1, offset=(-x, -y))
        red_px = int(np.count_nonzero(crop & poly))
        fill = red_px / float(w * h)
        if fill < STAMP_FILL_MIN or fill > STAMP_FILL_MAX:
            continue
        # c) 尺寸域上限：bbox 长边 ≤ 页面短边 55%（整页红装饰带/整页红框排除）
        if max(w, h) * scale > STAMP_LONG_EDGE_MAX_RATIO * page_short_pt:
            continue
        # b) 圆度或星形（辅闸，标定操作化）：实心（fill>0.75）近圆
        #    （4πA/P²≥0.55）且短边>20pt 判为圆盘 logo 拒判——橙红圆形 logo
        #    对抗类（fill 0.785/圆度 0.90/短边 60pt）；真值面唯一稠密近圆
        #    成员为 14.4pt 实心红点（≤20pt 保留），真章环带 fill≈0.2 不入域；
        #    五角星形态（顶点数+凸实度）豁免。
        if (fill > STAMP_SOLID_ROUND_FILL
                and 4 * math.pi * cnt_area / max(cv2.arcLength(cnt, True) ** 2, 1e-6) >= STAMP_CIRC_MIN
                and min(w, h) * scale > STAMP_SOLID_ROUND_MIN_SIDE_PT
                and not _stamp_star_like(cnt)):
            continue
        rects.append((x * scale, y * scale, (x + w) * scale, (y + h) * scale))
    return rects
