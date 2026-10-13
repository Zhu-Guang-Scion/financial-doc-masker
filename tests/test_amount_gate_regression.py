# -*- coding: utf-8 -*-
"""v2.0.1 金额闸三通道回归（契约 C2：调用点传参，注入 options.mask_amount）。

行为定义（契约冻结）：
- 金额关（mask_amount=False）＝三通道零 op（不画块、不记映射行、plan 零指令）；
- 金额开（mask_amount=True）＝与闸前行为逐位一致（闸开=原路径，零变化强判据）。

三通道（与 pdf_handler 落点一一对应）：
- R22 文本层列对齐金额 `_mask_text_amount_columns`：签名 options 形参 + 首行闸，
  调用点（_process_page）传入——本套件直调函数，喂「伪表头碎片+纯数字列」
  正反样例（借支 p5 机理：任意含「元」词皆当表头）；
- R17 行标签金额：调用点在 `_mask_joined_ocr_lines` 内以 options.mask_amount
  包裹——经该函数入口驱动（options 沿调用链传入的落点），引擎固定金额开，
  单变量=闸；实体（company）span 绘制两态同权（company 触发不动）；
- invoice/凭证整图遮：两处 _raw_amt 金额词触发分支追加 and options.mask_amount
  ——经 `_mask_embedded_images` 端到端驱动（OCR 桩 + 引擎桩），另验证
  company 实体触发路径两态不受金额开关影响。

运行：python tests/test_amount_gate_regression.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("FINMASK_BRANDS", str(
    Path(__file__).resolve().parents[1] / "_eval" / "brands_test.json"))

import pymupdf as fitz
from app.handlers import pdf_handler
from app.ocr_engine import OCRLine, OCRPageResult
from app.rules_engine import MaskOptions, RuleEngine, SensitiveSpan

COMPANY = "云舒雅家居股份有限公司"
COMPANY_REPL = "ABC股份有限公司"
PSEUDO_HEADER = "万元以上"        # 伪表头碎片（含「元」且 ≤24 字——R22 表头谓词命中）
PLAIN_HEADER = "备注说明"         # 无元词表头（R22 反例：金额开也不命中）
NUM = "500"                       # 表头下方同列裸数字（无单位——主链路不命中，R22 兜底命中）
AMT_LABEL_TEXT = f"{COMPANY}补贴81.00"   # R17 行标签样例（引擎不消费 81.00，兜底命中）
INVOICE_AMT_TEXT = "￥621.00"     # invoice 金额词样例（示例乙 p3 刷卡纸 RMB621.00 同型）


class _Rec:
    """映射记录收集桩（report.record/log 最小接口）。"""

    def __init__(self):
        self.rows = []

    def record(self, original, replacement, rule, location):
        self.rows.append((original, replacement, rule, location))

    def log(self, msg):
        pass

    def by_rule(self, rule):
        return [r for r in self.rows if r[2] == rule]


class _Case:
    def __init__(self, name):
        self.name = name
        self.failed = []
        self.total = 0

    def check(self, name, cond, detail=""):
        self.total += 1
        if cond:
            print(f"  PASS {name}")
        else:
            self.failed.append(f"{name}: {detail}")
            print(f"  FAIL {name} {str(detail)[:300]}")

    def done(self):
        print(f"共 {self.total} 条，失败 {len(self.failed)} 条（{self.name}）")
        if self.failed:
            raise AssertionError(f"{self.name}: {self.failed}")


def _opts(mask_amount):
    """闸两态 options（deep_recheck=False：单测不含复检通道触发面）。"""
    return MaskOptions(mask_company=True, mask_person=True,
                       mask_amount=mask_amount, amount_mode="mask",
                       mask_contact=True, mask_address=True,
                       mask_stamp=False, ocr_image_text=True,
                       deep_recheck=False)


# ---------------------------------------------------------------------------
# ① R22 文本层列对齐（_mask_text_amount_columns：options 形参 + 首行闸）
# ---------------------------------------------------------------------------

def _r22_page(header):
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((200, 100), header, fontname="china-s", fontsize=11)
    pg.insert_text((205, 150), NUM, fontname="china-s", fontsize=11)
    return doc, pg


def test_r22_amount_gate():
    """伪表头+纯数字列：金额开照命中（与闸前一致）；金额关零 op 且原文不动。"""
    print("· ① R22 文本层列对齐金额（首行闸）")
    c = _Case("R22")

    # 金额开：命中（闸开=原路径）
    doc, pg = _r22_page(PSEUDO_HEADER)
    rec = _Rec()
    ret = pdf_handler._mask_text_amount_columns(
        pg, fitz, rec, 4, _opts(True))
    c.check("金额开 ret=1（伪表头+纯数字列照命中）", ret == 1, ret)
    c.check("金额开映射行（原文/替换/规则/位置）",
            rec.rows == [(NUM, "***", "amount", "第 5 页(列对齐)")], rec.rows)
    c.check("金额开原文数字被 redaction 销毁", NUM not in pg.get_text(),
            pg.get_text()[:120])
    doc.close()

    # 金额关：零 op（不画块、不记映射行，原文数字原样在位）
    doc, pg = _r22_page(PSEUDO_HEADER)
    rec = _Rec()
    ret = pdf_handler._mask_text_amount_columns(
        pg, fitz, rec, 4, _opts(False))
    c.check("金额关 ret=0（零命中）", ret == 0, ret)
    c.check("金额关零映射行（零 op）", rec.rows == [], rec.rows)
    c.check("金额关伪表头与数字原样在位",
            PSEUDO_HEADER in pg.get_text() and NUM in pg.get_text(),
            pg.get_text()[:120])
    doc.close()

    # 反例（金额开）：表头无元词 → 通道自身谓词不命中（闸未钝化通道判定）
    doc, pg = _r22_page(PLAIN_HEADER)
    rec = _Rec()
    ret = pdf_handler._mask_text_amount_columns(
        pg, fitz, rec, 4, _opts(True))
    c.check("金额开反例（无元词表头）ret=0", ret == 0 and rec.rows == [],
            (ret, rec.rows))
    doc.close()

    c.done()


# ---------------------------------------------------------------------------
# ② R17 行标签金额（调用点在 _mask_joined_ocr_lines 内以 options.mask_amount 包裹）
# ---------------------------------------------------------------------------

def test_r17_amount_gate():
    """行标签金额经 _mask_joined_ocr_lines 驱动：引擎固定金额开（单变量=闸），
    金额开出兜底 op、实体 span 照常；金额关零金额 op、实体 span 不受影响。"""
    print("· ② R17 行标签金额（调用点闸）")
    c = _Case("R17")
    items = [OCRLine(text=AMT_LABEL_TEXT, bbox=(70.0, 170.0, 350.0, 190.0),
                     confidence=0.92)]
    # 引擎两态共用且金额开：证明闸在调用点读 options，而非引擎差异
    engine = RuleEngine(MaskOptions(mask_amount=True, amount_mode="mask"))

    # 金额开：R17 出 op（与闸前一致）+ company span 照常
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    rec = _Rec()
    ret = pdf_handler._mask_joined_ocr_lines(
        pg, items, engine, _opts(True), rec, 5, "(OCR)")
    c.check("金额开 ret=2（company 1 + R17 兜底 1）", ret == 2, ret)
    c.check("金额开 R17 映射行", rec.by_rule("amount")
            == [("81.00", "***", "amount", "第6页(OCR)")], rec.by_rule("amount"))
    c.check("金额开 company 映射行不受影响",
            rec.by_rule("company")
            == [(COMPANY, COMPANY_REPL, "company", "第6页(OCR)")],
            rec.by_rule("company"))
    doc.close()

    # 金额关：零金额 op，company span 照常（实体触发不动）
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    rec = _Rec()
    ret = pdf_handler._mask_joined_ocr_lines(
        pg, items, engine, _opts(False), rec, 5, "(OCR)")
    c.check("金额关 ret=1（仅 company span）", ret == 1, ret)
    c.check("金额关零金额映射行", rec.by_rule("amount") == [],
            rec.by_rule("amount"))
    c.check("金额关 company 映射行不受影响",
            rec.by_rule("company")
            == [(COMPANY, COMPANY_REPL, "company", "第6页(OCR)")],
            rec.by_rule("company"))
    doc.close()

    c.done()


# ---------------------------------------------------------------------------
# ③ invoice/凭证整图遮（两处 _raw_amt 金额词触发分支 + company 触发不动）
# ---------------------------------------------------------------------------

class _NoHitEngine:
    """引擎桩：任何文本零实体命中（让金额形态证据成为唯一触发源）。"""

    name = "stub-nohit"

    def find_spans(self, text, force_person=False):
        return []

    def get_confidence(self, rule, text):
        return "高"

    def is_lone_ocr_name(self, text):
        return False


class _CompanyEngine:
    """引擎桩：仅 company 词命中（金额关/开下实体路径同权）。"""

    name = "stub-company"

    def find_spans(self, text, force_person=False):
        i = text.find(COMPANY)
        if i < 0:
            return []
        return [SensitiveSpan(i, i + len(COMPANY), "company", COMPANY,
                              COMPANY_REPL, "高")]

    def get_confidence(self, rule, text):
        return "高"

    def is_lone_ocr_name(self, text):
        return False


class _FixedOcr:
    """OCR 桩：固定行（内容无关像素）。"""

    name = "stub-ocr"

    def __init__(self, lines):
        self._lines = lines

    def recognize(self, image_bytes, dpi):
        return OCRPageResult(lines=[OCRLine(t, b, cf)
                                    for (t, b, cf) in self._lines])


def _embed_page():
    """原生页 + 内嵌小图（宽 240<300pt 小图分支；过 60x40pt/0.4% 面积闸；
    页面文本无凭证语境词——凭证语境分支不触发，单变量=金额形态证据）。"""
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 80),
                   "附件三：配送与安装服务流程说明正文内容足够长以进入原生文本链路",
                   fontname="china-s", fontsize=9)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 240, 140), False)
    pix.clear_with(90)
    pg.insert_image(fitz.Rect(300, 120, 540, 380), pixmap=pix)
    return doc, pg


def test_invoice_amount_word_gate():
    """金额词触发正反：图内 OCR 仅金额形态、实体零命中——
    金额开=整图按凭证遮（与闸前一致）；金额关=该分支零 op。"""
    print("· ③ invoice 金额词触发（_raw_amt 分支闸）")
    c = _Case("invoice")
    lines = [(INVOICE_AMT_TEXT, (310.0, 130.0, 420.0, 160.0), 0.95)]

    # 金额开：整图遮 1 op（闸开=原路径）
    doc, pg = _embed_page()
    rec = _Rec()
    ret = pdf_handler._mask_embedded_images(
        pg, _NoHitEngine(), _opts(True), lambda: _FixedOcr(lines), rec, 4, fitz)
    c.check("金额开 ret=1（金额形态小图整图遮）", ret == 1, ret)
    c.check("金额开 invoice 映射行",
            rec.rows == [("凭证截图", "整图遮盖", "invoice", "第5页(图内文字)")],
            rec.rows)
    doc.close()

    # 金额关：该分支零 op（页面无凭证语境词/发票特征词——其余分支亦不触发）
    doc, pg = _embed_page()
    rec = _Rec()
    ret = pdf_handler._mask_embedded_images(
        pg, _NoHitEngine(), _opts(False), lambda: _FixedOcr(lines), rec, 4, fitz)
    c.check("金额关 ret=0（金额词触发分支零 op）", ret == 0, ret)
    c.check("金额关零映射行", rec.rows == [], rec.rows)
    doc.close()

    c.done()


def test_invoice_company_path_unaffected():
    """company 触发不受金额开关影响：图内仅实体文本——两态同出一笔 company op，
    且不触发 invoice 整图遮（无金额形态证据；实体路径不读金额闸）。"""
    print("· ③+ company 触发路径两态不受金额开关影响")
    c = _Case("invoice-company")
    lines = [(COMPANY, (310.0, 130.0, 470.0, 160.0), 0.95)]
    expect = [(COMPANY, COMPANY_REPL, "company", "第5页(图内文字)")]

    for on, tag in ((True, "金额开"), (False, "金额关")):
        doc, pg = _embed_page()
        rec = _Rec()
        ret = pdf_handler._mask_embedded_images(
            pg, _CompanyEngine(), _opts(on), lambda: _FixedOcr(lines),
            rec, 4, fitz)
        c.check(f"{tag} ret=1（仅 company span）", ret == 1, ret)
        c.check(f"{tag} company 映射行照常", rec.rows == expect, rec.rows)
        c.check(f"{tag} 零 invoice 整图遮",
                rec.by_rule("invoice") == [], rec.by_rule("invoice"))
        doc.close()

    c.done()


# ---------------------------------------------------------------------------
# 直跑入口（与 pytest 收集共用用例体）
# ---------------------------------------------------------------------------

def main():
    print("== v2.0.1 金额闸三通道回归（契约 C2）==")
    test_r22_amount_gate()
    test_r17_amount_gate()
    test_invoice_amount_word_gate()
    test_invoice_company_path_unaffected()
    print("全部通过")


if __name__ == "__main__":
    main()
