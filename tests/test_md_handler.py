# -*- coding: utf-8 -*-
"""Markdown(.md) 处理器测试（照 test_excel_numeric_phone.py 模式）。

覆盖：utf-8 主路径（表格/代码围栏结构保留 + 台账位置串「第N段」）、
GBK 回退（原编码写回 + 回退提示日志）、UTF-8 BOM 保留、零命中逐字节
不变、read_text_detect 三档编码纯函数契约、扫描器 .md 提取分支。

例值均为合成构造（连号假号/虚构公司名），不涉真实语料。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.handlers import md_handler  # noqa: E402
from app.handlers.md_handler import read_text_detect  # noqa: E402
from app.rules_engine import RuleEngine, MaskOptions  # noqa: E402

COMPANY = "江城云舒控股集团有限公司"          # 构造公司名（自定义词通道锚点）
PHONE = "13800001234"                        # 合成连号假手机号

# CRLF 构造样例（仓库全 CRLF；同时验证写回不做换行归一）
SAMPLE = (
    "# 财务报告\r\n"
    "\r\n"
    f"编制单位：{COMPANY}\r\n"
    "\r\n"
    f"联系电话：{PHONE}，详见下表。\r\n"
    "\r\n"
    "| 科目 | 金额 |\r\n"
    "| --- | --- |\r\n"
    "| 营业收入 | 100 万 |\r\n"
    "\r\n"
    "```python\r\n"
    "print('hello')\r\n"
    "```\r\n"
)
# 「编制单位」含「制」（GBK D6C6，0xC6 非 UTF-8 续字节）——保证 GBK 样例
# 在 utf-8-sig 档必然解码失败，三档回退路径可稳定复现
CLEAN = "# 公开说明\r\n\r\n本文件不含敏感信息，仅作结构演示。\r\n"


class _FakeReport:
    def __init__(self):
        self.records = []
        self.logs = []

    def record(self, original, replacement, rule, location):
        self.records.append((original, replacement, rule, location))

    def log(self, msg):
        self.logs.append(msg)

    def progress(self, *a):
        pass


def _run(tmp_path, content: str, encoding: str, name: str = "样例.md"):
    """写源文件 → 直调 handler（dispatcher 注册由主接线批另行打通，此处
    绕过 SUPPORTED_EXTS 依赖）→ 返回 (输出文本字节, report, stats)。"""
    src = tmp_path / name
    dst = tmp_path / f"{Path(name).stem}_脱敏后.md"
    src.write_bytes(content.encode(encoding))
    options = MaskOptions(custom_words=[COMPANY])
    engine = RuleEngine(options)
    report = _FakeReport()
    stats = md_handler.process(src, dst, engine, options, report)
    return dst.read_bytes(), report, stats


def test_utf8_md_masked_with_structure_and_records(tmp_path):
    """主路径：utf-8（无 BOM）CRLF 样例——命中、结构保留、位置串「第N段」。"""
    raw, report, stats = _run(tmp_path, SAMPLE, "utf-8")
    assert stats.get("masked", 0) >= 2
    out = raw.decode("utf-8")
    assert COMPANY not in out, "公司名未被替换"
    assert PHONE not in out, "手机号未被替换"
    # Markdown 结构原样：表格竖线行与代码围栏块逐行保留
    for line in ("| 科目 | 金额 |", "| --- | --- |", "| 营业收入 | 100 万 |",
                 "```python", "print('hello')", "```"):
        assert line in out, f"Markdown 结构被改动：{line!r}"
    assert out.count("\r\n") == SAMPLE.count("\r\n"), "换行符被归一改动"
    # 台账：规则通道 + 1-based 换行段位置串（公司名在第 3 段、手机号在第 5 段）
    assert any(r[0] == COMPANY and r[2] == "custom" and r[3] == "第3段"
               for r in report.records), report.records
    assert any(r[0] == PHONE and r[2] == "phone" and r[3] == "第5段"
               for r in report.records), report.records
    # 两阶段文案（对齐 docx 风格）
    assert any("阶段 1/2" in m for m in report.logs)
    assert any("阶段 2/2" in m for m in report.logs)


def test_gbk_md_roundtrip_with_fallback_log(tmp_path):
    """GBK 样例：回退读取 + 原编码写回（输出仍可按 GBK 读回）+ 回退提示。"""
    raw, report, stats = _run(tmp_path, SAMPLE, "gbk")
    assert stats.get("masked", 0) >= 2
    out = raw.decode("gbk")  # 若误用 UTF-8 写回，此处直接解码失败
    assert COMPANY not in out and PHONE not in out
    assert "| 科目 | 金额 |" in out
    assert any("GBK" in m and "UTF-8" in m for m in report.logs), \
        f"应留 GBK 回退一行人话提示：{report.logs}"


def test_utf8_bom_preserved(tmp_path):
    """BOM 样例：utf-8-sig 读入 → 写回仍带 BOM，且脱敏照常生效。"""
    raw, report, stats = _run(tmp_path, SAMPLE, "utf-8-sig")
    assert raw.startswith(b"\xef\xbb\xbf"), "输出丢失 UTF-8 BOM"
    assert stats.get("masked", 0) >= 2
    out = raw.decode("utf-8-sig")
    assert COMPANY not in out and PHONE not in out


def test_zero_hits_unchanged_bytes(tmp_path):
    """零命中 md：masked=0，输出与源逐字节一致（含 CRLF 不被归一）。"""
    src_bytes = CLEAN.encode("utf-8")
    raw, report, stats = _run(tmp_path, CLEAN, "utf-8")
    assert stats.get("masked", 0) == 0
    assert raw == src_bytes, "零命中文件必须逐字节不变"
    assert not any(r for r in report.records)


def test_read_text_detect_three_tiers(tmp_path):
    """read_text_detect 纯函数契约：utf-8 / utf-8-sig / gbk / gb18030 四态。"""
    p1 = tmp_path / "t1.md"
    p1.write_bytes(CLEAN.encode("utf-8"))
    text, enc = read_text_detect(p1)
    assert enc == "utf-8" and text == CLEAN

    p2 = tmp_path / "t2.md"
    p2.write_bytes(SAMPLE.encode("utf-8-sig"))
    text, enc = read_text_detect(p2)
    assert enc == "utf-8-sig" and text == SAMPLE  # BOM 被剥离、内容原样

    p3 = tmp_path / "t3.md"
    p3.write_bytes(SAMPLE.encode("gbk"))
    text, enc = read_text_detect(p3)
    assert enc == "gbk" and text == SAMPLE

    # 第三档兜底：utf-8 与 gbk 均失败的字节（gb18030 容错替换读入）
    p4 = tmp_path / "t4.md"
    p4.write_bytes(b"ok\xa1\xff")
    text, enc = read_text_detect(p4)
    assert enc == "gb18030" and text.startswith("ok") and "\ufffd" in text


def test_scanner_extract_md(tmp_path):
    """扫描器提取分支：.md 走同一 read_text_detect，返回非空文本 + 零 OCR 页。"""
    from app.candidate_scanner import SUPPORTED_EXTS, _extract_file
    assert ".md" in SUPPORTED_EXTS, "SUPPORTED_EXTS 应含 .md（与探针/构建清单同集契约）"
    p = tmp_path / "scan.md"
    p.write_bytes(SAMPLE.encode("utf-8"))
    text, ocr = _extract_file(p)
    assert text and "编制单位" in text and COMPANY in text
    assert ocr == 0
