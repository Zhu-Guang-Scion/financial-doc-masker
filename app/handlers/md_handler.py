# -*- coding: utf-8 -*-
"""Markdown(.md) 处理器（纯文本统一通道）。

口径：Markdown 结构（表格竖线 / 代码围栏 / 链接）不做特殊解析——
全文按单一文本层送检统一引擎（custom/mapping/公司/人名/金额/联系方式
全通道经 engine.find_spans），即满足「文本与图片层均须处理」红线
（.md 为纯文本格式，无独立图片层；图片仅以链接/替换文本形式存在于
文本层，随全文一并送检）。

编码策略（沿 brand_config.load_brand_config 的 utf-8-sig → GBK 回退
先例，WPS/旧记事本常以 ANSI/GBK 保存中文文档）：
1) utf-8-sig 先试（BOM 被剥离，容忍记事本保存的 BOM）；
2) UnicodeDecodeError → GBK 回退（report.log 一行人话提示建议转存
   UTF-8）；
3) 再失败 → GB18030 + errors="replace" 兜底（无法识别的字节以替换
   符代替并留痕，坏档降级不中断批量任务）。
按实际读入编码「原样写回」：UTF-8 带 BOM 的写回仍带 BOM
（utf-8-sig 写）、不带 BOM 的写 utf-8、GBK 读的写 GBK——保证零命中
文件逐字节不变（读写字节流不做换行归一，\r\n 原样保留）。
"""
from __future__ import annotations

from pathlib import Path

from .text_replacement import apply_replacements

# UTF-8 BOM（判定「读入时是否带 BOM」→ 决定写回是否补 BOM）
_UTF8_BOM = b"\xef\xbb\xbf"


def read_text_detect(path: Path) -> tuple[str, str]:
    """按三档编码策略读纯文本文件 → (text, encoding_used)。

    encoding_used 是「写回时应使用的编码」：utf-8 / utf-8-sig（读入带
    BOM 时补写 BOM）/ gbk / gb18030（兜底档，读入已用替换符容错）。

    实现注记：经 read_bytes + decode 而非 Path.read_text——read_text 走
    通用换行翻译（\r\n → \n），会破坏「零命中逐字节不变」契约；decode
    保留原始换行，写回侧配合 newline="" 关闭再翻译。

    候选词扫描器（candidate_scanner._extract_file 的 .md 分支）复用本
    函数，保证「扫描提取」与「遮蔽读入」两处编码口径单一实现零漂移。
    纯函数：不写日志不落盘，回退提示由调用方按 encoding_used 生成。
    """
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
        return text, ("utf-8-sig" if raw.startswith(_UTF8_BOM) else "utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gbk"), "gbk"
    except UnicodeDecodeError:
        pass
    return raw.decode("gb18030", errors="replace"), "gb18030"


def process(src: Path, dst: Path, engine, options, report) -> dict:
    report.log("阶段 1/2：解析 Markdown 文档 …")
    text, encoding = read_text_detect(src)
    if encoding in ("utf-8", "utf-8-sig"):
        # v2.1.9：UTF-8 严格解码成功即按 UTF-8 处理，但 GBK
        # 文档在窄字符区间可能被误判为 UTF-8——留一行提示供乱码时回溯
        # （每文件一行，纯留痕零控制流变化）。
        report.log("已按 UTF-8 读取（GBK 文档在窄字符区间可能被误判为 UTF-8，"
                   "若发现乱码请转存 UTF-8 重试）")
    elif encoding == "gbk":
        report.log("检测到该文件为非 UTF-8 编码（GBK/ANSI），本次已按 GBK 读取"
                   "并按 GBK 写回；建议用记事本另存为 UTF-8 以获得最佳兼容性。")
    elif encoding == "gb18030":
        # v2.1.9：文案点名 UTF-16——BOM 缺失的 UTF-16 文件
        # 两档严格解码均失败落入此兜底，此前提示未指出该成因。
        report.log("文件编码无法识别（既非 UTF-8 也非 GBK），已按 GB18030 容错"
                   "读取（无法识别的字节以替换符代替）并留痕；若原文件为 "
                   "UTF-16/Unicode 请先转存 UTF-8 后重试，否则内容可能乱码。")

    spans = engine.find_spans(text)
    masked_text = apply_replacements(text, spans)
    for sp in spans:
        # 位置串：命中起点落在第几个换行段（1-based；\r\n 按 \n 计数同口径）
        para = text.count("\n", 0, sp.start) + 1
        report.record(sp.text, sp.replacement, sp.rule, f"第{para}段")

    paras = text.count("\n") + 1 if text else 0
    report.log(f"段落 {paras} 个，命中敏感信息 {len(spans)} 处")
    report.log("阶段 2/2：写出脱敏副本 …")
    _write_text(dst, masked_text, encoding, report)
    return {"masked": len(spans)}


def _write_text(dst: Path, text: str, encoding: str, report) -> None:
    """按读入编码写回（newline="" 关闭换行翻译，保 \r\n 原样）。

    UnicodeEncodeError 兜底：映射对照表替换文本可能含超出原编码的字符
    （如 GBK 外的生僻字），此时降档 GB18030（全 Unicode 可编码）写出
    并留痕，坏值不中断批量任务。
    """
    try:
        with open(dst, "w", encoding=encoding, newline="") as f:
            f.write(text)
    except UnicodeEncodeError as exc:
        report.log(f"替换文本无法按原编码 {encoding} 写出，已降档 GB18030"
                   f"（{exc}）；建议映射替换值使用通用字符。")
        with open(dst, "w", encoding="gb18030", newline="") as f:
            f.write(text)
