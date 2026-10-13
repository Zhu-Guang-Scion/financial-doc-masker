# -*- coding: utf-8 -*-
"""分析层缓存（v1.9.0）：复核轮 OCR 结果复用，纯性能优化，行为零变化。

背景（复核轮工作流）：首轮按自动规则全量脱敏 → 用户在汇总映射表上复核
（删误报行/改替换文本）→ 把该 CSV 选入“映射对照表覆盖”严格重跑。映射
覆盖模式下引擎状态在 __init__ 后冻结（学习通道全关、预扫描跳过，
find_spans 是 (映射表, 自定义词, 文本) 的纯函数），同一源文件两轮之间
唯一昂贵的重复计算是 OCR。本模块把逐站点的 OCR 结果缓存到
`<输出根>/cache/<源文件sha256>.json`，映射态重跑直接回放。

生命线：缓存回放与全量重跑逐字节一致。达成方式——
- 站点键 = sha256(dpi + image_bytes)：渲染是常量参数的确定性纯函数
  （同一源文件 + 同一 PyMuPDF 版本下 PNG 字节完全确定），键精确绑定
  一切输入；键命中即意味着图像与分辨率逐字节相同；
- OCRPageResult 字段级序列化（text/bbox/confidence），JSON float 往返
  在 Python 中精确（repr 最短表示），回放对象与真 OCR 对象字段级一致，
  bbox 还原为 tuple（与 BaseOCREngine 惯例同形）；
- 缓存体不含任何规则语义：映射表内容/自定义词不入键（复核轮改映射表
  正是复用前提），匹配仍由活动引擎逐次重算；
- 头部强校验（schema_version / app_version / engine_name / brands_hash
  任一不符 → 整文件 miss）：engine_name 取 OCR 后端实际值（含档位），
  brands 按从严口径纳入键（brands.json 一字之变即全量失效）。

线程约束：dispatcher.run_batch 为单线程逐文件处理（app/dispatcher.py），
同一 CachedOcrEngine 实例只被单线程串行调用，无需加锁；GPU-v6 模式的
worker 读线程只服务 recognize() 内部通信，不构成本类并发。

持久化策略：进程结束不自动写盘——由 pdf_handler.process 在单文件完成时
显式调 save()（合并写回，tmp + os.replace 原子替换）；单文件中途异常
不落盘（缓存可重建，宁缺毋半）。

敏感面声明：缓存体含 OCR 识别全文——即文档原文的
机器可读副本，敏感级与汇总映射表 / 冻结 plan 同级，勿随项目外发或提交
版本库；输出目录需移交他人时请先清除。UI「清除分析缓存」按钮联动清除：
该按钮删除 <输出目录>/cache/ 全部 *.json（本缓存）及 cache/plans/
（v1.9.4 起连带首轮冻结 plan），两者同根同清，不存在只清其一的路径。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .ocr_engine import OCRLine, OCRPageResult

CACHE_SCHEMA_VERSION = 1
_CACHE_DIRNAME = "cache"          # 缓存目录名（挂在输出根下）
_BATCH_DIR_PREFIX = "脱敏输出_"   # dispatcher.run_batch 批目录名前缀（v1.6.3 起）


# ---------------------------------------------------------------------------
# 键与哈希
# ---------------------------------------------------------------------------

def content_hash(path) -> str:
    """源文件内容 sha256（流式 1MB 块，支持大文件）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def brands_config_hash() -> str:
    """brands.json 内容 sha256；无配置（或缺属常态）→ "none"。

    经 brand_config.resolve_config_path()（FINMASK_BRANDS 环境变量优先 >
    程序目录 brands.json）；与引擎加载品牌预设的解析口径完全一致。
    读取失败按无配置处理（与 load_brand_config 的降级哲学一致）。
    """
    from . import brand_config
    try:
        p = brand_config.resolve_config_path()
        if p is None or not Path(p).exists():
            return "none"
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except OSError:
        return "none"


def site_key(image_bytes: bytes, dpi: int) -> str:
    """单次 OCR 调用的站点键：sha256(dpi + image_bytes)。

    dpi 与图像字节共同精确绑定一次 recognize 调用的全部输入。
    """
    h = hashlib.sha256()
    h.update(f"{int(dpi)}|".encode("ascii"))
    h.update(image_bytes)
    return h.hexdigest()


def resolve_output_root(src, options) -> str:
    """解析输出根（缓存目录挂载点）：`<输出根>/cache/`。

    - 显式 options.output_root（调用方在 dispatcher 改写前捕获）优先；
    - 否则按批目录名模式反推：run_batch 会把 options.output_dir 改写为
      `<输出根>/脱敏输出_YYYYMMDD_HHMM`（v1.6.3 起），本函数在 process()
      内执行时改写已发生——目录名以「脱敏输出_」开头即取其父目录；
    - 普通目录 → 原样作为输出根；未设置 → 源文件父目录。

    两轮任务选同一输出目录即共享缓存（用户无感知）。
    """
    explicit = getattr(options, "output_root", None) if options is not None else None
    if explicit:
        return str(explicit)
    outdir = getattr(options, "output_dir", "") if options is not None else ""
    if outdir:
        p = Path(outdir)
        if p.name.startswith(_BATCH_DIR_PREFIX):
            return str(p.parent)
        return str(p)
    return str(Path(src).parent)


def cache_path_for(src, output_root: str) -> Path:
    """缓存文件路径：`<输出根>/cache/<源文件内容sha256>.json`。"""
    return Path(output_root) / _CACHE_DIRNAME / f"{content_hash(src)}.json"


# ---------------------------------------------------------------------------
# 序列化（OCRPageResult <-> JSON dict，字段级一致）
# ---------------------------------------------------------------------------

def _line_to_dict(ln: OCRLine) -> dict:
    bbox = ln.bbox
    return {"text": str(ln.text),
            "bbox": [float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])],
            "confidence": float(ln.confidence)}


def _dict_to_line(d: dict) -> OCRLine:
    b = d["bbox"]
    return OCRLine(text=str(d["text"]),
                   bbox=(float(b[0]), float(b[1]), float(b[2]), float(b[3])),
                   confidence=float(d.get("confidence", 1.0)))


def _result_to_dicts(result: OCRPageResult) -> dict:
    """OCRPageResult -> 可 JSON 化条目。table_cells 当前恒空，按同构序列化。"""
    return {"dpi": None,  # 写入时由调用方补 int(dpi)
            "lines": [_line_to_dict(ln) for ln in result.lines],
            "table_cells": [_line_to_dict(ln) for ln in getattr(result, "table_cells", [])]}


def _dicts_to_result(entry: dict):
    """条目 -> OCRPageResult；结构不合法返回 None（调用方回退真 OCR）。"""
    try:
        if not isinstance(entry, dict):
            return None
        out = OCRPageResult()
        for d in entry["lines"]:
            out.lines.append(_dict_to_line(d))
        for d in entry.get("table_cells") or []:
            out.table_cells.append(_dict_to_line(d))
        return out
    except (KeyError, IndexError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 缓存代理引擎
# ---------------------------------------------------------------------------

class CachedOcrEngine:
    """BaseOCREngine 同形代理：recognize 先查缓存（consume 态），未命中透传真引擎并记录。

    - .name 透传真引擎实际名（就绪日志/状态栏语义不变）；
    - consume=True 且缓存文件存在且头部全匹配 → 载入回放；任一不符 →
      miss_all 静默回退（report_log 留一行原因），本次全量 OCR 并重建缓存；
    - consume=False（auto/legacy 态）只写不读：记录新站点顺带建缓存，
      为后续映射复核轮做铺垫，绝不改变当轮输出；
    - save() 合并写回：磁盘文件头部匹配（同引擎/同版本/同 brands）时并入
      其站点（在内存新结果优先），否则整体重建——绝不混入其他引擎产出。
    """

    def __init__(self, engine, cache_path, app_version, report_log=None,
                 consume: bool = False):
        self._engine = engine
        self._cache_path = Path(cache_path)
        self._app_version = str(app_version)
        self._report_log = report_log
        self._consume = bool(consume)
        self._hits = 0
        self._misses = 0
        self._recorded = 0
        self._loaded = False
        self._miss_reason = ""
        self._header = {
            "schema_version": CACHE_SCHEMA_VERSION,
            "app_version": self._app_version,
            "engine_name": str(getattr(engine, "name", "")),
            "brands_hash": brands_config_hash(),
        }
        self._created = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._sites: dict = {}
        # v1.9.0 分析快照（仅存不用）：预留后续轮把主 find_spans 结果一并
        # 缓存；本轮接线降级为空节（采集侵入面大、消费面为零），见实现报告。
        self._analysis: dict = {"spans": [], "learn_words": []}
        if self._consume:
            self._open()

    # ---- 属性透传 -------------------------------------------------------

    @property
    def name(self) -> str:
        """真引擎实际名（就绪日志/诊断不变）。"""
        return getattr(self._engine, "name", "")

    @property
    def wrapped_engine(self):
        """被代理的真 OCR 引擎（诊断/测试用）。"""
        return self._engine

    # ---- 打开 / 载入 -----------------------------------------------------

    def _log(self, msg: str) -> None:
        if self._report_log:
            try:
                self._report_log(msg)
            except Exception:
                pass

    def _miss_all(self, reason: str) -> None:
        self._loaded = False
        self._miss_reason = reason
        self._sites = {}
        self._log(f"分析缓存：未命中（{reason}），本次全量流程并重建缓存")

    def _open(self) -> None:
        """consume 态打开缓存文件：头部强校验，任一不符 → miss_all。"""
        if not self._cache_path.exists():
            self._miss_all("缓存不存在（首次处理该文件）")
            return
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            self._miss_all(f"缓存文件损坏不可读（{type(e).__name__}）")
            return
        if not isinstance(data, dict):
            self._miss_all("缓存文件结构异常（非对象）")
            return
        header = data.get("header")
        if not isinstance(header, dict):
            self._miss_all("缓存头部缺失")
            return
        for field in ("schema_version", "app_version", "engine_name", "brands_hash"):
            if header.get(field) != self._header[field]:
                self._miss_all(f"头部不匹配：{field} 不一致"
                               f"（{header.get(field)!r} != {self._header[field]!r}）")
                return
        sites = data.get("sites")
        if not isinstance(sites, dict):
            self._miss_all("缓存站点表结构异常")
            return
        analysis = data.get("analysis")
        if isinstance(analysis, dict):
            self._analysis = {
                "spans": analysis.get("spans") if isinstance(analysis.get("spans"), list) else [],
                "learn_words": analysis.get("learn_words")
                if isinstance(analysis.get("learn_words"), list) else [],
            }
        if isinstance(header.get("created"), str) and header["created"]:
            self._created = header["created"]  # 保留首次建档时间
        self._sites = sites
        self._loaded = True

    # ---- 对外接口（BaseOCREngine 同形） -----------------------------------

    def recognize(self, image_bytes: bytes, dpi: int) -> OCRPageResult:
        key = site_key(image_bytes, dpi)
        if self._consume:
            result = _dicts_to_result(self._sites.get(key))
            if result is not None:
                entry = self._sites.get(key)
                if isinstance(entry, dict) and entry.get("dpi") == int(dpi):
                    self._hits += 1
                    return result
        self._misses += 1
        result = self._engine.recognize(image_bytes, dpi)
        entry = _result_to_dicts(result)
        entry["dpi"] = int(dpi)
        self._sites[key] = entry
        self._recorded += 1
        return result

    def note_analysis_spans(self, text: str, spans) -> None:
        """v1.9.0 预留接口：记录模式下快照一次主 find_spans 调用（仅存不用）。

        本轮接线未调用（采集需把缓存对象穿入逐页遮盖链路，侵入面大且
        v1.9.0 消费面为零）；接口就位供后续轮启用。
        """
        try:
            self._analysis.setdefault("spans", []).append({
                "text": str(text),
                "spans": [[int(s.start), int(s.end), str(s.rule), str(s.replacement)]
                          for s in (spans or [])],
            })
        except Exception:
            pass  # 快照属best-effort，绝不影响处理

    def stats(self) -> dict:
        return {"consume": self._consume,
                "hits": self._hits,
                "misses": self._misses,
                "recorded": self._recorded,
                "loaded": self._loaded,
                "sites": len(self._sites),
                "miss_reason": self._miss_reason}

    # ---- 落盘 -------------------------------------------------------------

    def save(self) -> None:
        """合并写回（tmp + os.replace 原子替换）。目录按需创建。

        磁盘文件头部与本实例完全匹配时并入其站点（内存新结果优先——
        同键必同值，覆盖只为防御手改）；头部不匹配（旧版本/其他引擎/
        其他 brands）则整体重建，绝不混入异源产出。
        """
        sites = self._sites
        created = self._created
        if not self._loaded and self._cache_path.exists():
            try:
                disk = json.loads(self._cache_path.read_text(encoding="utf-8"))
                header = disk.get("header") if isinstance(disk, dict) else None
                disk_sites = disk.get("sites") if isinstance(disk, dict) else None
                if isinstance(header, dict) and isinstance(disk_sites, dict) and all(
                        header.get(k) == self._header[k]
                        for k in ("schema_version", "app_version", "engine_name",
                                  "brands_hash")):
                    merged = dict(disk_sites)
                    merged.update(sites)
                    sites = merged
                    if isinstance(header.get("created"), str) and header["created"]:
                        created = header["created"]
            except (OSError, ValueError):
                pass  # 磁盘读取失败按整体重建处理
        payload = {"header": dict(self._header, created=created),
                   "sites": sites,
                   "analysis": self._analysis}
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._cache_path.with_name(self._cache_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(str(tmp), str(self._cache_path))
