# -*- coding: utf-8 -*-
"""工具层：可选依赖探测、格式校验算法、金额与文本辅助函数。

本模块只依赖 Python 标准库，保证主程序在“零第三方依赖”时也能正常启动。
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path


# 应用版本号（标题栏/状态栏/日志共用；发布时同步更新此处与使用说明.txt）
# v1.9.4：参与冻结 plan 头部校验键——升版使 v1.9.3 旧 plan 按设计失效（契约 §5）
APP_VERSION = "2.1.9"


class MissingDependencyError(RuntimeError):
    """可选第三方依赖缺失/初始化失败时抛出，message 中给出友好的安装提示。"""


class OcrPageError(RuntimeError):
    """单页 OCR 识别失败（引擎实例本身健康）——稳定体验小批 ⑤（2026-10-11）。

    与 MissingDependencyError 的分界：后者=依赖缺失/worker 进程级失效
    （EOF/意外退出/超时，应失效缓存并重建）；本类=worker 正常回包但该页
    识别失败（协议 ok:false，如单张图解码失败）——进程常驻健康，杀掉
    重建纯属浪费且会整批抖动。抛出口见 ocr_engine GPU 通道；宿主
    （pdf_handler）页级异常兜底接管（单页跳过不中断批次）。
    """


def now_hms() -> str:
    """当前时间戳（[HH:MM:SS] 中的时间部分），供日志使用。"""
    return datetime.datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------------------
# 可选依赖探测（缺失时只降级、不崩溃）
# ---------------------------------------------------------------------------

from importlib import import_module  # noqa: E402 —— 测试接缝（monkeypatch 点）


def _try_import(modname: str) -> tuple:
    """真实 import 试加载（v2.1.2 自检升级）：返回 (ok, 内层原因)。

    find_spec 不执行模块，查不出「模块在、依赖 DLL 加载失败」（用户实测：
    自检过了、处理期才炸）。关键组件用本 helper 试加载，异常如实呈现。
    """
    try:
        import_module(modname)
        return True, ""
    except Exception as e:  # ImportError/OSError(DLL load failed) 均如实呈现
        return False, f"试加载失败：[{type(e).__name__}] {e}"


def probe_optional_deps() -> dict:
    """探测可选依赖是否可用。

    返回 {显示名: (是否可用, 安装提示)}。
    通用组件用 importlib.util.find_spec 探测（不真正 import，避免拖慢启动）；
    关键组件 docx/lxml 另做真实 import 试加载（v2.1.2 自检升级，消掉
    「自检过了、处理期才炸」的机制盲区，异常附内层原因）。
    """
    import importlib.util

    checks = [
        ("PyMuPDF（PDF 处理）", ("fitz", "pymupdf"), "pip install pymupdf"),
        ("python-docx（Word 处理）", ("docx",), "pip install python-docx"),
        ("openpyxl（Excel 处理）", ("openpyxl",), "pip install openpyxl"),
        ("python-pptx（PPT 处理）", ("pptx",), "pip install python-pptx"),
        ("xlrd（.xls 备用读取）", ("xlrd",), "pip install xlrd"),
        ("PaddleOCR（OCR 组件）", ("paddleocr",), "pip install paddlepaddle paddleocr"),
        ("RapidOCR（PP-OCRv6 ONNX 组件）", ("rapidocr_onnxruntime",),
         "pip install rapidocr-onnxruntime onnxruntime"),
        ("pywin32（.xls COM）", ("win32com",), "pip install pywin32（并确保本机安装 Excel 或 WPS）"),
        ("OpenCV（印章检测）", ("cv2",), "pip install opencv-python numpy"),
        # jieba 是公司/
        # 人名通道 100% 依赖的分词器，缺失时页级异常吞噬会静默成「逐页
        # 不脱敏」——列入自检清单
        ("jieba（分词引擎）", ("jieba",), "pip install jieba"),
    ]
    result = {}
    for label, modules, hint in checks:
        ok = any(importlib.util.find_spec(m) is not None for m in modules)
        result[label] = (ok, "" if ok else hint)
    # v2.1.2：关键组件真实试加载（docx 及其底层依赖 lxml；覆盖
    # 「find_spec 通过但 DLL 加载失败」盲区，异常附内层原因）。
    for modname, label in (("docx", "python-docx（Word 处理）"),
                           ("lxml.etree", "lxml（python-docx 依赖库）"),
                           # 分词器缺失=公司/人名通道整链失效，
                           # 真实试加载（import 期建词典，缺失/损坏在此暴露）
                           ("jieba", "jieba（分词引擎）")):
        ok, reason = _try_import(modname)
        if ok:
            result[label] = (True, "")
        else:
            result[label] = (False, reason + "（请安装 VC++ 2015-2022 x64 "
                                           "运行库后重试）")
    return result


# ---------------------------------------------------------------------------
# 身份证 / 手机号 / 邮箱 / 统一社会信用代码 校验（规则引擎调用）
# ---------------------------------------------------------------------------

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_CODES = "10X98765432"


def validate_id_card(number: str) -> bool:
    """18 位居民身份证校验位验证（GB 11643-1999）。"""
    if len(number) != 18 or not number[:17].isdigit():
        return False
    total = sum(int(c) * w for c, w in zip(number[:17], _ID_WEIGHTS))
    return number[17].upper() == _ID_CHECK_CODES[total % 11]


# 手机号号段白名单（3 位前缀）。
# TODO: 号段分配会持续更新，建议按工信部最新公示定期维护本表。
_PHONE_PREFIXES = frozenset({
    "130", "131", "132", "133", "134", "135", "136", "137", "138", "139",
    "145", "146", "147", "148", "149",
    "150", "151", "152", "153", "155", "156", "157", "158", "159",
    "162", "165", "166", "167", "168", "169",
    "170", "171", "172", "173", "174", "175", "176", "177", "178",
    "180", "181", "182", "183", "184", "185", "186", "187", "188", "189",
    "190", "191", "192", "193", "195", "196", "197", "198", "199",
})


def validate_phone(number: str) -> bool:
    """11 位手机号：号段白名单校验。"""
    return len(number) == 11 and number.isdigit() and number[:3] in _PHONE_PREFIXES


_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")


def validate_email(text: str) -> bool:
    """邮箱格式校验。"""
    return len(text) <= 254 and bool(_EMAIL_RE.match(text))


# 统一社会信用代码字符表（不含 I / O / S / V / Z，GB 32100-2015）
_USCC_CHARS = "0123456789ABCDEFGHJKLMNPQRTUWXY"
_USCC_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)


def validate_uscc(code: str) -> bool:
    """统一社会信用代码校验位验证（GB 32100-2015）。"""
    if len(code) != 18:
        return False
    try:
        total = sum(_USCC_CHARS.index(c) * w for c, w in zip(code[:17], _USCC_WEIGHTS))
    except ValueError:
        return False
    return code[17] == _USCC_CHARS[(31 - total % 31) % 31]


# ---------------------------------------------------------------------------
# 金额辅助
# ---------------------------------------------------------------------------

def parse_amount(text: str):
    """从金额文本解析数值，失败返回 None。

    支持：￥3000 / ¥1,234.56 / 3万元 / 3000元 / 12345.67 / 1,234,567.89 /
    4.5亿元（补亿级）
    """
    s = text.replace("，", ",").strip()
    # 外币单位词剥离（50欧元/港元/美元 类，元字前的币种字先去掉）
    for cur in ("欧元", "港元", "美元", "日元", "英镑"):
        s = s.replace(cur, "元") if cur != "元" else s
    s = s.replace("欧", "").replace("港", "").replace("美", "").replace("日", "").replace("英", "")         if any(c in s for c in "欧港美日英") and "元" in s else s
    yi = "亿" in s
    if yi:
        s = s.replace("亿元", "").replace("亿", "")
    s = s.replace("￥", "").replace("¥", "").replace("元", "")
    s = s.replace("人民币", "").replace("RMB", "").replace("CNY", "").replace(",", "").strip()
    wan = False
    if s.endswith("万") and not s.endswith("万一"):
        wan = True  # 2.6万 / ￥2.6万 类万字尾缀（万元/裸万统一处理）
        s = s[:-1]
    try:
        value = float(s)
    except ValueError:
        return None
    if wan:
        value *= 10000
    if yi:
        value *= 100000000
    return value


def partial_mask_amount(text: str) -> str:
    """部分遮罩模式：12345.67 -> 1****.**（保留首位数字与小数点，符号/千分位原样保留）。"""
    out = []
    seen_digit = False
    for ch in text:
        if ch.isdigit():
            out.append(ch if not seen_digit else "*")
            seen_digit = True
        elif ch == "." or ch in ",，":
            out.append(ch)
        else:  # ￥ / ¥ / 元 等符号原样保留
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# 误报样本回收（v1.3）：用户复核映射表时删除的人名行 → 误报样本.csv
# ---------------------------------------------------------------------------

def program_data_dir() -> Path:
    """程序数据目录：exe 为 exe 所在目录，源码运行为项目根目录。"""
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def append_false_positive_samples(rows) -> Path:
    """把被用户删除的人名映射行静默追加到 程序目录/误报样本.csv。

    rows：(原文, 替换为, 文件, 位置, 置信度) 列表。文件不存在则创建（含表头）。
    返回样本文件路径。这是后续人名识别迭代的误报数据源。
    """
    import csv
    path = program_data_dir() / "误报样本.csv"
    new_file = not path.exists()
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["原文", "替换为", "文件", "位置", "置信度"])
        for row in rows:
            writer.writerow(row)
    return path
