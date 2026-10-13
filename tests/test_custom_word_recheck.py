# -*- coding: utf-8 -*-
"""v2.0.3 F2「custom 用户词最强兜底」回归用例（江图 P0 修复批次）。

维护者批示：用户显式配置的词享受最强兜底——配置了的词必须风雨无阻。

E1 归因（guangtu_d2_l1.md + v203_wf2_probe.py 在盘实证）：江图通知 PDF p1
落款行压红章，首轮整页 OCR det 丢行 / 条带 3x 净版口径 det 抓到整行但 rec
丢「广」字（「中共城图书馆委员会」）→ custom 词连续子串不成立 → 全词通道
零候选 →「中共█…█员会」残片并列残留。次级归因（v203_wf2_accept.py 首跑
实证）：现行 _band_recheck 在页处理后的实况上渲染条带——印章黑框先画吞掉
落款行中部，实况 band OCR 亦无法还原整行 ⇒ 兜底必须扫「净版」（源文件重
开、未画遮盖的页）。

修法（F2 双通道）：
- _custom_word_pristine_band：光栅化阶段从净版页按同款条带几何重扫，仅对
  user 配置词做「单字缺失假设」——词去掉任一字后仍为行文本连续子串即补遮
  （词占行比例 ≥50% 整行遮蔽，否则残串矩形向缺口侧外扩一字符宽）；
- _visual_recheck：原生页视觉复检行同语义缺字假设（链路 A 平价）。

词源口径（v2.0.3 二轮 WF2b 收窄，维护者评审决策）：仅 engine._word_keys（UI
自定义词，MaskOptions.custom_words 逐字显式配置）参与缺字兜底；brands.json
品牌词（_static_brand_words）不参与——WF2 首版并集词源在零 UI 词语料
（brands_test 11 词）上对全部光栅化页无条件净版条带重扫，39 份口径
elapsed 1174→2192s（+87%）结构性回归，故收窄回炉；品牌词维持主链路精确
匹配覆盖（v2.0.x 现状）。映射/学习/公司/人名通道不扩面；零 UI 词时通道
完全关闭零 OCR 开销、零行为变化（负向对照②③b⑤）。

运行：python tests/test_custom_word_recheck.py
（真 fitz 空白页 + fake OCR/engine/report，不依赖真实权重。）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pymupdf as fitz

import app.handlers.pdf_handler as ph
from app.handlers.pdf_handler import (_custom_word_pristine_band,
                                      _visual_recheck)
from app.ocr_engine import OCRLine, OCRPageResult

# E1 实测几何（v203_wf2_probe.py band y0=480 条带，页坐标）
E1_REC_DROP_BBOX = (318.0, 642.0, 481.0, 667.0)   # band det 整行框，rec 丢「广」
E1_DET_TRUNC_BBOX = (362.42, 641.57, 479.99, 664.49)  # 首轮 det 左端截断框（D2 现场）


class _FakeEngine:
    """仅提供复检通道词集来源（与 test_band_neighbor_regression 同构）。"""

    def __init__(self, short_names=None, word_keys=(), static_brands=()):
        self._short_names = dict(short_names or {})
        self._mapping = {}
        self._word_keys = set(word_keys)
        self._static_brand_words = set(static_brands)
        self._company_alias = {}
        self._person_conf = {}


class _FakeOCR:
    """首个条带返回目标行，其余条带为空（真 OCR 各条带内容不同的最小替身）。"""

    def __init__(self, lines):
        self._lines = lines
        self.calls = 0

    def recognize(self, image_bytes, dpi):
        self.calls += 1
        out = OCRPageResult()
        if self.calls == 1:
            for text, bbox in self._lines:
                out.lines.append(OCRLine(text=text, bbox=bbox, confidence=0.95))
        return out


class _FakeReport:
    def __init__(self):
        self.records = []
        self.logs = []

    def record(self, **kw):
        self.records.append(kw)

    def log(self, msg):
        self.logs.append(msg)


def _two_pages(with_image=False):
    """(实况页, 净版页)——同一几何的空白页分属两个文档（模拟源文件重开）。"""
    doc_live = fitz.open()
    live = doc_live.new_page(width=595, height=842)
    doc_pristine = fitz.open()
    pristine = doc_pristine.new_page(width=595, height=842)
    if with_image:
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 8, 8))
        live.insert_image(fitz.Rect(100, 100, 200, 200), pixmap=pix)
    return doc_live, live, doc_pristine, pristine


class _DrawCapture:
    """捕获 _draw_replacement 补丁（模块属性打点，测后还原）。记录
    (页对象, 矩形)，用于断言补丁画在实况页且几何正确。"""

    def __init__(self):
        self.drawn = []  # (page, rect)
        self._orig = None

    def __enter__(self):
        self._orig = ph._draw_replacement

        def _cap(page, rect, rep):
            self.drawn.append((page, fitz.Rect(rect)))
            return self._orig(page, rect, rep)

        ph._draw_replacement = _cap
        return self

    def __exit__(self, *exc):
        ph._draw_replacement = self._orig
        return False

    def rects(self):
        return [r for _p, r in self.drawn]


GUANGTU_WORDS = ("江城市图书馆", "江城图书馆", "江图")  # D1 实证 _word_keys 三词


def test_pristine_band_rec_drop_line_masked():
    """①a E1 主形态（rec 丢字）：净版 band 行「中共城图书馆委员会」缺「广」，
    改前全词通道零候选（红）；改后缺字假设命中、整行遮蔽画在实况页（绿）
    ——残片「中共」「员会」同行一并盖住。"""
    doc_live, live, doc_pristine, pristine = _two_pages()
    ocr = _FakeOCR([("中共城图书馆委员会", E1_REC_DROP_BBOX)])
    engine = _FakeEngine(word_keys=GUANGTU_WORDS)
    report = _FakeReport()
    with _DrawCapture() as cap:
        hits = _custom_word_pristine_band(live, pristine, engine,
                                          (lambda: ocr), report, 0, fitz)
    doc_live.close()
    doc_pristine.close()

    assert hits >= 1, f"缺字假设应命中 ≥1 处，实际 hits={hits}（改前红态）"
    gap_recs = [r for r in report.records if "缺字兜底" in str(r.get("location", ""))]
    assert gap_recs, f"应存在缺字兜底台账记录，实际 {report.records}"
    assert gap_recs[0]["original"] == "江城图书馆", \
        f"缺字兜底应归属用户配置词，实际 {gap_recs[0]['original']!r}"
    assert gap_recs[0]["rule"] == "band_recheck"
    # 补丁必须画在实况页（净版页仅作 OCR 输入，不画不改）
    assert all(p is live for p, _r in cap.drawn), "补丁应画在实况页而非净版页"
    # 整行遮蔽：补丁须覆盖 det 整行框（含「中共」「员会」残片区）
    r = cap.rects()[0]
    assert r.x0 <= E1_REC_DROP_BBOX[0] + 1 and r.x1 >= E1_REC_DROP_BBOX[2] - 1, \
        f"补丁应整行覆盖 {E1_REC_DROP_BBOX}，实际 ({r.x0:.1f},{r.y0:.1f},{r.x1:.1f},{r.y1:.1f})"
    assert r.y0 <= E1_REC_DROP_BBOX[1] + 1 and r.y1 >= E1_REC_DROP_BBOX[3] - 1


def test_pristine_band_det_truncated_line_masked():
    """①b E1 变体（det 左端截断）：首轮形态行「城图书馆委员会」（缺首字
    「广」，D2 现场实测框）——缺字假设去词首字后命中，整行遮蔽。"""
    doc_live, live, doc_pristine, pristine = _two_pages()
    ocr = _FakeOCR([("城图书馆委员会", E1_DET_TRUNC_BBOX)])
    engine = _FakeEngine(word_keys=GUANGTU_WORDS)
    report = _FakeReport()
    with _DrawCapture() as cap:
        hits = _custom_word_pristine_band(live, pristine, engine,
                                          (lambda: ocr), report, 0, fitz)
    doc_live.close()
    doc_pristine.close()

    assert hits >= 1, f"det 截断行缺字假设应命中，实际 hits={hits}"
    r = cap.rects()[0]
    # 词占行 5/7 ≥50% → 整行遮蔽；且向左外扩覆盖缺口侧（「中共广」区）
    assert r.x1 >= E1_DET_TRUNC_BBOX[2] - 1
    assert r.x0 <= E1_DET_TRUNC_BBOX[0], \
        f"整行补丁应覆盖截断框左缘，实际 x0={r.x0:.1f}"


def test_pristine_band_gap_span_extends_toward_gap_side():
    """①c 低覆盖行（词占行 <50%）按残串矩形向缺口侧外扩一字符宽。"""
    doc_live, live, doc_pristine, pristine = _two_pages()
    bbox = (100.0, 600.0, 400.0, 625.0)  # 13 字行，宽 300，字宽≈23
    ocr = _FakeOCR([("中共城图书馆委员会关于印发", bbox)])
    engine = _FakeEngine(word_keys=GUANGTU_WORDS)
    report = _FakeReport()
    with _DrawCapture() as cap:
        hits = _custom_word_pristine_band(live, pristine, engine,
                                          (lambda: ocr), report, 0, fitz)
    doc_live.close()
    doc_pristine.close()

    assert hits >= 1, f"低覆盖行缺字假设应命中，实际 hits={hits}"
    r = cap.rects()[0]
    cw = (bbox[2] - bbox[0]) / 13.0
    # 缺口在词首（去「广」）：补丁左缘须越过「共州」交界进入缺口字符带
    assert r.x0 <= bbox[0] + 2 * cw + 0.5, \
        f"补丁应向缺口侧外扩一字符宽（左缘 ≤ {bbox[0] + 2 * cw:.1f}），实际 x0={r.x0:.1f}"
    # 残串「城图书馆」占 text[2:6]：补丁右缘须盖住残串尾字「馆」
    assert r.x1 >= bbox[0] + 6 * cw - 0.5, \
        f"补丁应盖住残串尾字，实际 x1={r.x1:.1f}"
    # 不整行吞：右缘不得越过行尾「印发」区（低覆盖不做整行遮蔽）
    assert r.x1 <= bbox[2] + cw + 0.5, \
        f"低覆盖行不应整行遮蔽，实际 x1={r.x1:.1f} vs 行右缘 {bbox[2]}"


def test_pristine_band_no_custom_words_zero_overhead():
    """② 负向对照：无 custom 词时不初始化 OCR、不产任何补丁/记录——缺字
    兜底通道零开销、零行为变化。"""
    doc_live, live, doc_pristine, pristine = _two_pages()
    ocr = _FakeOCR([("中共城图书馆委员会", E1_REC_DROP_BBOX)])
    engine = _FakeEngine(short_names={"云舒雅家居": "***"}, word_keys=(),
                         static_brands=())
    report = _FakeReport()
    with _DrawCapture() as cap:
        hits = _custom_word_pristine_band(live, pristine, engine,
                                          (lambda: ocr), report, 0, fitz)
    doc_live.close()
    doc_pristine.close()

    assert hits == 0
    assert ocr.calls == 0, f"无 custom 词不应触发任何 OCR 调用，实际 {ocr.calls} 次"
    assert not report.records and not cap.rects(), "无 custom 词不应有补丁/记录"


def test_pristine_band_full_word_line_gate():
    """③ 行级全词闸：行内已含任一配置词全词 ⇒ 完整可读行，缺字假设整体
    不参与（首轮精确匹配通道已按全词处理；更长短词的去字残串不得在已
    遮蔽区间上重复出补丁）。残行（行内无任何配置词全词）才走缺字假设。"""
    doc_live, live, doc_pristine, pristine = _two_pages()
    ocr = _FakeOCR([("中共江城图书馆委员会", (312.0, 641.0, 481.0, 665.0))])
    engine = _FakeEngine(word_keys=GUANGTU_WORDS)
    report = _FakeReport()
    with _DrawCapture() as cap:
        hits = _custom_word_pristine_band(live, pristine, engine,
                                          (lambda: ocr), report, 0, fitz)
    doc_live.close()
    doc_pristine.close()

    assert hits == 0, f"全词完整可读行不应由缺字兜底出补丁，实际 hits={hits}"
    assert not report.records and not cap.rects()


def test_pristine_band_brands_words_excluded_boundary():
    """③b 口径边界（v2.0.3 二轮 WF2b 收窄，原 brands.json 路线用例反转）：
    brands.json 品牌词（_static_brand_words）不参与缺字兜底——仅 brands 词、
    无 UI 词的引擎（=39 份语料 brands_test.json 口径）上通道完全关闭：零
    OCR 调用、零补丁、零台账记录。理由：WF2 首版并集词源在零 UI 词语料上
    对全部光栅化页无条件净版条带重扫（39 份 elapsed 1174→2192s，+87% 结构
    性回归）；品牌词由主链路精确匹配覆盖，用户如需品牌词兜底走 UI 词通道
    （custom_words 逐字配置）。"""
    doc_live, live, doc_pristine, pristine = _two_pages()
    ocr = _FakeOCR([("中共城图书馆委员会", E1_REC_DROP_BBOX)])
    engine = _FakeEngine(static_brands=GUANGTU_WORDS)  # 仅 brands 词源
    report = _FakeReport()
    with _DrawCapture() as cap:
        hits = _custom_word_pristine_band(live, pristine, engine,
                                          (lambda: ocr), report, 0, fitz)
    doc_live.close()
    doc_pristine.close()

    assert hits == 0, \
        f"brands 词不应参与缺字兜底（WF2b 收窄口径），实际 hits={hits}"
    assert ocr.calls == 0, \
        f"仅 brands 词时通道应关闭零 OCR，实际 {ocr.calls} 次调用"
    assert not report.records and not cap.rects(), \
        "仅 brands 词时不应有补丁/记录"


def test_visual_recheck_gap_parity():
    """④ 原生页视觉复检同权兜底：转曲/图内落款行缺字形态由 visual_recheck
    补遮（链路 A 深度复检通道与净版条带通道同语义）。"""
    doc_live, live, doc_pristine, pristine = _two_pages(with_image=True)
    ocr = _FakeOCR([("中共城图书馆委员会", E1_REC_DROP_BBOX)])
    engine = _FakeEngine(word_keys=GUANGTU_WORDS)
    report = _FakeReport()
    with _DrawCapture() as cap:
        leaked = _visual_recheck(live, engine, (lambda: ocr), report, 0, fitz)
    doc_live.close()
    doc_pristine.close()

    assert leaked, "视觉复检缺字假设应产出泄漏补丁清单"
    words = [w for _r, w in leaked]
    assert "江城图书馆" in words, f"缺字兜底应归属用户配置词，实际 {words}"
    gap_recs = [r for r in report.records if "缺字兜底" in str(r.get("location", ""))]
    assert gap_recs and gap_recs[0]["rule"] == "visual_recheck"
    r = cap.rects()[0]
    assert r.x0 <= E1_REC_DROP_BBOX[0] + 1 and r.x1 >= E1_REC_DROP_BBOX[2] - 1


def test_visual_recheck_no_custom_words_noop():
    """⑤ 负向对照（视觉复检）：无 UI 词时缺字假设零产出——含仅 brands 词
    形态（WF2b 收窄口径对 visual 通道同域生效：brands 词不参与缺字兜底）。"""
    doc_live, live, doc_pristine, pristine = _two_pages(with_image=True)
    ocr = _FakeOCR([("中共城图书馆委员会", E1_REC_DROP_BBOX)])
    engine = _FakeEngine(short_names={"云舒雅家居": "***"}, word_keys=(),
                         static_brands=GUANGTU_WORDS)
    report = _FakeReport()
    with _DrawCapture() as cap:
        leaked = _visual_recheck(live, engine, (lambda: ocr), report, 0, fitz)
    doc_live.close()
    doc_pristine.close()

    assert leaked == [], f"无 UI 词视觉复检不应产出缺字补丁，实际 {leaked}"
    assert not report.records


def test_visual_recheck_brands_words_excluded_boundary():
    """⑤b 口径边界（视觉复检通道，WF2b 收窄）：仅 brands 词源时 visual
    缺字假设零产出。注（如实记录）：_visual_recheck 的 gap_words ⊆ 复检
    词集 words（后者本就不含 _static_brand_words），WF2 首版在 _custom_keys
    上的并集于此通道不可达（惰性并集）——本用例在收窄前后均绿，收窄属
    口径一致性清理（与净版条带通道同源 _custom_gap_word_keys），行为零
    变化；真正的行为翻转在净版条带通道③b（并集词源下红）。"""
    doc_live, live, doc_pristine, pristine = _two_pages(with_image=True)
    ocr = _FakeOCR([("中共城图书馆委员会", E1_REC_DROP_BBOX)])
    engine = _FakeEngine(short_names={"云舒雅家居": "***"},
                         static_brands=GUANGTU_WORDS)
    report = _FakeReport()
    with _DrawCapture() as cap:
        leaked = _visual_recheck(live, engine, (lambda: ocr), report, 0, fitz)
    doc_live.close()
    doc_pristine.close()

    gap_words_hit = [w for _r, w in leaked if w in GUANGTU_WORDS]
    assert not gap_words_hit, \
        f"brands 词不应参与 visual 缺字兜底（WF2b 收窄口径），实际 {gap_words_hit}"
    assert not [r for r in report.records
                if "缺字兜底" in str(r.get("location", ""))]


CASES = [test_pristine_band_rec_drop_line_masked,
         test_pristine_band_det_truncated_line_masked,
         test_pristine_band_gap_span_extends_toward_gap_side,
         test_pristine_band_no_custom_words_zero_overhead,
         test_pristine_band_full_word_line_gate,
         test_pristine_band_brands_words_excluded_boundary,
         test_visual_recheck_gap_parity,
         test_visual_recheck_no_custom_words_noop,
         test_visual_recheck_brands_words_excluded_boundary]


def run():
    failed = 0
    for fn in CASES:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {fn.__name__}: [{type(e).__name__}] {e}")
    print(f"共 {len(CASES)} 条，失败 {failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    run()
