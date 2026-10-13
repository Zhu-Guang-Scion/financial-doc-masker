# -*- coding: utf-8 -*-
"""v2.0.2 R22 表头谓词修正回归（先红后绿）。

弱点（改前归因分析）：`_mask_text_amount_columns`
旧表头谓词=「任意含 元/￥/¥ 且 ≤24 字」+下方 220pt 几何窗口——
- 伪表头：数据行碎片（「万元以上」「万元至5」「万元及以上的借支款项…」）
  各自命中触发，借支 p5 六处误遮、同矩形被重复绘制（1+3+7+6+5+1=23 次）；
- 真表头漏检：「金额（人民币）」不含「元」字，从不命中。

行为定义（本套件冻结）：
- 正向：真表头「金额（人民币）」（不含元字）+其下纯数字列 → 命中
  （改前跑红：旧谓词 0 命中）；
- 反向：伪表头碎片（数据行文字「万元以上」处于表头候选位置=非列首）
  → 不再触发（改前跑红：旧谓词命中）；正文长句同不再充当伪表头；
- 去重：同矩形多表头命中 → 只画一次（改前跑红：重复绘制、重复计数）；
- 保留：含元列首表头（「（元/天）」类）照常命中（改前改后同绿，防过收）；
- 协同：金额关（options.mask_amount=False）仍零 op（首行闸不变，同 v2.0.1）。
- B2 回炉新增（⑦⑧⑨）：第一版判定树全量轮 23 处新误遮的三类形态反向用例——
  ①扣分值语境（强词碎片「错金额500」+下方「N 分/次」）、②时限语境（弱词列头
  「操作标准」+「48 小时内」）、③页码语境（强词残片+页脚带「23/25」「5/9」）。
  红态口径：v0（现行含元旧谓词）下三例本就绿（元字闸恰好挡住无元碎片），
  三例红态由第一版判定树复现（探针实录归档，开源库不保留），
  本套件将其冻结为防回退回归。

改动面纪律：谓词逻辑全部位于 `_mask_text_amount_columns` 函数内部；
不碰 :180 单位类（A 线辖区）、不碰金额闸三通道、不碰 rules_engine.py。

运行：python tests/test_r22_header_predicate.py（亦兼容 pytest 收集）
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 品牌夹具统一选取（本地配置优先，缺失回退开源虚构词夹具 tests/fixtures/）
from _fixtures_common import resolve_brands_fixture

os.environ.setdefault("FINMASK_BRANDS", resolve_brands_fixture())

import pymupdf as fitz
from app.handlers import pdf_handler
from app.rules_engine import MaskOptions

REAL_HEADER = "金额（人民币）"   # 真表头：不含「元」字（旧谓词从不命中）
YUAN_UNIT_HEADER = "（元/天）"   # 含元列首表头（旧谓词命中，修正后须保留）
PSEUDO_FRAG = "万元以上"        # 伪表头碎片：数据行文字（含「元」）
BODY_LINE = "公司员工因公借款须遵守以下审批权限并按流程逐级报批执行"  # 长正文行
SENTENCE = "万元及以上的借支款项须先由中心总经理批准。"  # 正文长句伪表头（借支 p5 同型）
TABLE_TITLE = "表1：审批权限"    # 表标题（非表头）
NUM_A, NUM_B = "550", "450"


class _Rec:
    """映射记录收集桩（report.record/log 最小接口）。"""

    def __init__(self):
        self.rows = []

    def record(self, original, replacement, rule, location):
        self.rows.append((original, replacement, rule, location))

    def log(self, msg):
        pass


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
    """金额闸两态 options（deep_recheck=False：单测不含复检通道触发面）。"""
    return MaskOptions(mask_company=True, mask_person=True,
                       mask_amount=mask_amount, amount_mode="mask",
                       mask_contact=True, mask_address=True,
                       mask_stamp=False, ocr_image_text=True,
                       deep_recheck=False)


def _dump_words(pg):
    """夹具词框 dump（坐标校准辅助，正常断言不使用）。"""
    return [(round(w[0], 1), round(w[1], 1), round(w[2], 1), round(w[3], 1),
             w[4]) for w in pg.get_text("words")]


# ---------------------------------------------------------------------------
# ① 正向：真表头（不含元字）+ 纯数字列 → 命中（改前跑红）
# ---------------------------------------------------------------------------

def test_real_header_without_yuan_hits():
    print("· ① 正向：真表头「金额（人民币）」（不含元字）放行")
    c = _Case("R22正向-真表头")
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((195, 100), REAL_HEADER, fontname="china-s", fontsize=11)
    pg.insert_text((200, 150), NUM_A, fontname="china-s", fontsize=11)
    pg.insert_text((200, 180), NUM_B, fontname="china-s", fontsize=11)

    rec = _Rec()
    ret = pdf_handler._mask_text_amount_columns(pg, fitz, rec, 0, _opts(True))
    c.check("ret=2（真表头+两数字全命中）", ret == 2, (ret, _dump_words(pg)))
    c.check("映射行（原文/替换/规则/位置）",
            rec.rows == [(NUM_A, "***", "amount", "第 1 页(列对齐)"),
                         (NUM_B, "***", "amount", "第 1 页(列对齐)")], rec.rows)
    c.check("数字被 redaction 销毁", NUM_A not in pg.get_text()
            and NUM_B not in pg.get_text(), pg.get_text()[:160])
    c.check("表头原文保留", REAL_HEADER in pg.get_text(), pg.get_text()[:160])
    doc.close()
    c.done()


# ---------------------------------------------------------------------------
# ② 保留：含元列首表头照常命中（改前改后同绿，防谓词过收）
# ---------------------------------------------------------------------------

def test_yuan_unit_header_topmost_retained():
    print("· ② 保留：含元列首表头「（元/天）」+数字列")
    c = _Case("R22保留-列首含元表头")
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((195, 100), YUAN_UNIT_HEADER, fontname="china-s",
                   fontsize=11)
    pg.insert_text((200, 150), "350", fontname="china-s", fontsize=11)
    words = _dump_words(pg)
    c.check("夹具词框完好（表头单词+数字）",
            any("元" in w[4] for w in words)
            and any(w[4] == "350" for w in words), words)

    rec = _Rec()
    ret = pdf_handler._mask_text_amount_columns(pg, fitz, rec, 0, _opts(True))
    c.check("ret=1（列首含元表头照常命中）", ret == 1, (ret, words))
    c.check("映射行", rec.rows == [("350", "***", "amount", "第 1 页(列对齐)")],
            rec.rows)
    doc.close()
    c.done()


# ---------------------------------------------------------------------------
# ②-b 保留（费用文件 p9 实证）：单位化列头「住宿标准（元/天/人）」位于
#     页中段、上方有正文/上表残留，其下数字列照常命中（位置不限分支）
# ---------------------------------------------------------------------------

def test_unit_semantic_header_midpage_retained():
    print("· ②-b 保留：页中段单位化列头（住宿标准（元/天/人）+数字列）")
    c = _Case("R22保留-中段单位化列头")
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((56, 80), BODY_LINE, fontname="china-s", fontsize=11)
    pg.insert_text((372, 140), "住宿标准（元/天/人）", fontname="china-s",
                   fontsize=11)
    pg.insert_text((368, 180), "800", fontname="china-s", fontsize=11)
    pg.insert_text((368, 210), "700", fontname="china-s", fontsize=11)
    words = _dump_words(pg)

    rec = _Rec()
    ret = pdf_handler._mask_text_amount_columns(pg, fitz, rec, 0, _opts(True))
    c.check("ret=2（单位化列头照常命中其下数字列）", ret == 2, (ret, words))
    c.check("映射行 800/700", [r[0] for r in rec.rows] == ["800", "700"],
            rec.rows)
    doc.close()
    c.done()


# ---------------------------------------------------------------------------
# ②-c 守卫（费用文件 p9 实证）：单元格值「按实际费用报销」处于非列首位置
#     时不得充当表头（改前改后同绿——旧谓词因无元字本就不命中，锁定不回退）
# ---------------------------------------------------------------------------

def test_cell_value_header_guard():
    print("· ②-c 守卫：单元格值「按实际费用报销」非列首不当表头")
    c = _Case("R22守卫-单元格值")
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((439, 100), "其他国家或地区", fontname="china-s",
                   fontsize=11)  # 其上方列文字（阻断列首判定）
    pg.insert_text((439, 140), "按实际费用报销", fontname="china-s",
                   fontsize=11)
    pg.insert_text((521, 200), "9", fontname="china-s", fontsize=11)
    pg.insert_text((528, 200), "/", fontname="china-s", fontsize=11)
    pg.insert_text((534, 200), "21", fontname="china-s", fontsize=11)
    words = _dump_words(pg)

    rec = _Rec()
    ret = pdf_handler._mask_text_amount_columns(pg, fitz, rec, 0, _opts(True))
    c.check("ret=0（单元格值不触发、页码数字不遮）",
            ret == 0 and rec.rows == [], (ret, rec.rows, words))
    doc.close()
    c.done()


# ---------------------------------------------------------------------------
# ③ 反向：非列首伪表头碎片不再触发（改前跑红）
# ---------------------------------------------------------------------------

def test_pseudo_fragment_below_body_rejected():
    print("· ③ 反向：正文行下方的「万元以上」碎片不当表头")
    c = _Case("R22反向-伪表头碎片")
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    # 长正文行在最上（碎片同列带上方有文字 → 碎片非列首）
    pg.insert_text((56, 100), BODY_LINE, fontname="china-s", fontsize=11)
    pg.insert_text((200, 140), PSEUDO_FRAG, fontname="china-s", fontsize=11)
    pg.insert_text((200, 180), "3", fontname="china-s", fontsize=11)
    words = _dump_words(pg)

    rec = _Rec()
    ret = pdf_handler._mask_text_amount_columns(pg, fitz, rec, 0, _opts(True))
    c.check("ret=0（伪表头碎片不触发、数字 3 不遮）", ret == 0 and rec.rows == [],
            (ret, rec.rows, words))
    c.check("数字 3 原文保留", "3" in pg.get_text(), pg.get_text()[:160])
    doc.close()
    c.done()


# ---------------------------------------------------------------------------
# ④ 集成：借支 p5 场景（真表头+数据行碎片+正文长句+页码）——页码不再被遮
#    （改前跑红：长句伪表头遮页码 5 → 输出 ***/8 同型）
# ---------------------------------------------------------------------------

def test_jiezhi_p5_scene_page_number_survives():
    print("· ④ 集成：借支 p5 同型页（碎片/长句不触发，页码 5/8 保留）")
    c = _Case("R22集成-页码保留")
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((229, 80), TABLE_TITLE, fontname="china-s", fontsize=11)
    pg.insert_text((195, 110), REAL_HEADER, fontname="china-s", fontsize=11)
    pg.insert_text((196, 150), "1", fontname="china-s", fontsize=11)
    pg.insert_text((210, 150), PSEUDO_FRAG, fontname="china-s", fontsize=11)
    pg.insert_text((200, 230), SENTENCE, fontname="china-s", fontsize=11)
    # 页码「5」落在长句 ±40pt 列带内（借支 p5 同型：长句在其上方 42pt）；
    # 「/8」在带外，只作陪衬（改前「5」被长句伪表头遮成 ***/8 同型）
    pg.insert_text((430, 260), "5", fontname="china-s", fontsize=11)
    pg.insert_text((521, 260), "5/8", fontname="china-s", fontsize=11)
    words = _dump_words(pg)

    rec = _Rec()
    ret = pdf_handler._mask_text_amount_columns(pg, fitz, rec, 0, _opts(True))
    c.check("ret=1（仅真表头下数字 1 命中）", ret == 1, (ret, words))
    c.check("映射行只有数字 1（碎片/长句零贡献，页码 5 不入映射）",
            rec.rows == [("1", "***", "amount", "第 1 页(列对齐)")], rec.rows)
    c.check("句带内页码 5 原文保留（不再 ***/8 同型）",
            "5" in pg.get_text() and "5/8" in pg.get_text()
            and "批准" in pg.get_text(), pg.get_text()[:200])
    c.check("碎片与长句原文保留",
            PSEUDO_FRAG in pg.get_text() and "批准" in pg.get_text(),
            pg.get_text()[:200])
    doc.close()
    c.done()


# ---------------------------------------------------------------------------
# ⑤ 去重：同矩形多表头命中只画一次（改前跑红：重复绘制+重复计数）
# ---------------------------------------------------------------------------

def test_duplicate_rect_drawn_once():
    print("· ⑤ 去重：双表头命中同列数字只画一次")
    c = _Case("R22去重-同矩形单次")
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    # 两个表头都含「元」（改前旧谓词下双双命中→同矩形画两次；改前去重为
    # 零——真表头「金额（人民币）」不含元字从不命中，无重复可观察）
    pg.insert_text((195, 100), "金额（元）", fontname="china-s", fontsize=11)
    pg.insert_text((195, 140), YUAN_UNIT_HEADER, fontname="china-s",
                   fontsize=11)
    pg.insert_text((200, 190), "500", fontname="china-s", fontsize=11)
    words = _dump_words(pg)

    rec = _Rec()
    ret = pdf_handler._mask_text_amount_columns(pg, fitz, rec, 0, _opts(True))
    c.check("ret=1（同矩形去重后单次）", ret == 1, (ret, words))
    c.check("映射行恰 1 行（改前为 2 行重复）",
            rec.rows == [("500", "***", "amount", "第 1 页(列对齐)")], rec.rows)
    doc.close()
    c.done()


# ---------------------------------------------------------------------------
# ⑥ 协同：金额关仍零 op（v2.0.1 首行闸不变，改前改后同绿）
# ---------------------------------------------------------------------------

def test_amount_gate_off_still_zero_op():
    print("· ⑥ 协同：金额关（mask_amount=False）零 op")
    c = _Case("R22协同-金额闸")
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((195, 100), REAL_HEADER, fontname="china-s", fontsize=11)
    pg.insert_text((200, 150), NUM_A, fontname="china-s", fontsize=11)
    rec = _Rec()
    ret = pdf_handler._mask_text_amount_columns(pg, fitz, rec, 0, _opts(False))
    c.check("ret=0（金额关零命中）", ret == 0, ret)
    c.check("零映射行", rec.rows == [], rec.rows)
    c.check("原文原样在位", REAL_HEADER in pg.get_text()
            and NUM_A in pg.get_text(), pg.get_text()[:160])
    doc.close()
    c.done()


# ---------------------------------------------------------------------------
# ⑦ 反向（B2 回炉·全量轮误遮簇①）：考核扣分值语境——正文碎片「错金额500」
#    ／「下，或差错金额在」当强词伪表头，其下「N 分/次」扣分值列被整列遮
#    （盘点考核 p2/p4 共 16 处实证，几何按对账明细矩形等比复刻）→ 不遮
# ---------------------------------------------------------------------------

def test_deduction_score_fragment_not_header():
    print("· ⑦ 反向：扣分值语境碎片（错金额500／下，或差错金额在）不立表头")
    c = _Case("R22回炉反向-扣分值语境")
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    # 碎片一：字数混排型（盘点 p2 实证触发词，矩形 173/601 上方带内）
    pg.insert_text((160, 440), "错金额500", fontname="china-s", fontsize=11)
    # 其下扣分值「4」+右邻「分/次」（对账明细 rect [173.1,601.8,181.4,616.5]；
    # 词距 ≥7pt 保证两词分立，贴近真实 PDF 词态）
    pg.insert_text((173, 602), "4", fontname="china-s", fontsize=11)
    pg.insert_text((186, 602), "分/次", fontname="china-s", fontsize=11)
    # 碎片二：句读点型（盘点 p4 实证触发词「下，或差错金额在」同型）
    pg.insert_text((140, 470), "下，或差错金额在", fontname="china-s",
                   fontsize=11)
    pg.insert_text((174, 632), "2", fontname="china-s", fontsize=11)
    pg.insert_text((196, 632), "分/次", fontname="china-s", fontsize=11)
    # 碎片三：左邻计量词型（盘点 p2「分/次 ┃ 1」行左邻接同构）
    pg.insert_text((150, 692), "分/次", fontname="china-s", fontsize=11)
    pg.insert_text((196, 692), "4", fontname="china-s", fontsize=11)
    words = _dump_words(pg)

    rec = _Rec()
    ret = pdf_handler._mask_text_amount_columns(pg, fitz, rec, 0, _opts(True))
    c.check("ret=0（扣分值 4/2 不遮）", ret == 0 and rec.rows == [],
            (ret, rec.rows, words))
    c.check("扣分值与碎片原文保留", "4" in pg.get_text()
            and "2" in pg.get_text() and "分/次" in pg.get_text()
            and "错金额500" in pg.get_text(), pg.get_text()[:200])
    doc.close()
    c.done()


# ---------------------------------------------------------------------------
# ⑧ 反向（B2 回炉·全量轮误遮簇②）：时限语境——弱词「操作标准」（无元）
#    真列头不得独立立表头（第一版 A2 漏洞），其下「48 小时内」时限不遮
#    （库存办法 p23 实证，rect [286.9,490.6,298.9,502.6]）→ 不遮
# ---------------------------------------------------------------------------

def test_timelimit_under_weak_header_not_masked():
    print("· ⑧ 反向：弱词列头「操作标准」下「48 小时内」不遮")
    c = _Case("R22回炉反向-时限语境")
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((280, 100), "操作标准", fontname="china-s", fontsize=11)
    pg.insert_text((286, 140), "48", fontname="china-s", fontsize=11)
    pg.insert_text((320, 140), "小时内发起退货流程", fontname="china-s",
                   fontsize=11)
    words = _dump_words(pg)

    rec = _Rec()
    ret = pdf_handler._mask_text_amount_columns(pg, fitz, rec, 0, _opts(True))
    c.check("ret=0（时限 48 不遮）", ret == 0 and rec.rows == [],
            (ret, rec.rows, words))
    c.check("48 与小时内原文保留", "48" in pg.get_text()
            and "小时内" in pg.get_text(), pg.get_text()[:200])
    doc.close()
    c.done()


# ---------------------------------------------------------------------------
# ⑨ 反向（B2 回炉·全量轮误遮簇③）：页码语境——右下角页脚带「23 / 25」
#    与「5/9」分式不被其上方强词残片（注明厂商、品名、单价、数量、——
#    OA p23 公司名 redact 断行残片同型）连坐遮蔽 → 不遮
# ---------------------------------------------------------------------------

def test_page_number_footer_survives():
    print("· ⑨ 反向：页脚带页码 23/25 与 5/9 残片语境不遮")
    c = _Case("R22回炉反向-页码语境")
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    # 残片在页码上方 220pt 带内、x 带覆盖页码中心（OA p23 同构）
    pg.insert_text((390, 580), "注明厂商、品名、单价、数量、",
                   fontname="china-s", fontsize=11)
    # 右下角页码（对账明细 rect [511.3,769.5,523.4,783.4] 同构）
    pg.insert_text((511, 780), "23", fontname="china-s", fontsize=11)
    pg.insert_text((524, 780), "/", fontname="china-s", fontsize=11)
    pg.insert_text((528, 780), "25", fontname="china-s", fontsize=11)
    # 单词斜杠分式页码（非现金 p5「5/9」同型）
    pg.insert_text((460, 780), "5/9", fontname="china-s", fontsize=11)
    words = _dump_words(pg)

    rec = _Rec()
    ret = pdf_handler._mask_text_amount_columns(pg, fitz, rec, 0, _opts(True))
    c.check("ret=0（页码 23/25 与 5/9 不遮）", ret == 0 and rec.rows == [],
            (ret, rec.rows, words))
    c.check("页码与残片原文保留", "23" in pg.get_text()
            and "25" in pg.get_text() and "5/9" in pg.get_text()
            and "单价" in pg.get_text(), pg.get_text()[:240])
    doc.close()
    c.done()


# ---------------------------------------------------------------------------
# 直跑入口（与 pytest 收集共用用例体）
# ---------------------------------------------------------------------------

def main():
    print("== v2.0.2 R22 表头谓词修正回归（B2 回炉·先红后绿）==")
    test_real_header_without_yuan_hits()
    test_yuan_unit_header_topmost_retained()
    test_unit_semantic_header_midpage_retained()
    test_cell_value_header_guard()
    test_pseudo_fragment_below_body_rejected()
    test_jiezhi_p5_scene_page_number_survives()
    test_duplicate_rect_drawn_once()
    test_amount_gate_off_still_zero_op()
    test_deduction_score_fragment_not_header()
    test_timelimit_under_weak_header_not_masked()
    test_page_number_footer_survives()
    print("全部通过")


if __name__ == "__main__":
    main()
