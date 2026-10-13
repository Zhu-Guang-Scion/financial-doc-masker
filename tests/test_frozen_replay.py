# -*- coding: utf-8 -*-
"""v1.9.4 首轮检测冻结端到端回归（M1 重放等价 + M2 三编辑算子）。

自造 3 文件小批（native 页 / 扫描页 / 原生页内嵌图直画通道场景，印章关）：
- 甲：native 2 页（公司/人名/自定义词/内嵌截图——截图走 OCR 桩固定行，
  覆盖 孤立人名 + 图内表格列头锚定 + OCR 拼接 + 金额标签兜底 直画通道）；
- 乙：扫描页（<24 字符 → 链路 B 整页 OCR，OCR 桩供行）；
- 丙：native 1 页（与甲跨文件同名原文，验证删除按原文全局生效）。

OCR 以确定性桩注入（monkeypatch pdf_handler._make_ocr_getter，recognize
计数）：M1 判据②=复核轮 recognize 调用数=0（零 OCR 探针）。

M1 判据：
① 首轮→零编辑复核轮 三层等价：映射行（全列有序全等）+ 文本层 + 像素
   （逐页 pixmap samples）；
② 复核轮 OCR 调用数=0（桩计数）+ 命中日志（plan 重放证线）；
③ 首轮纯旁路自证：plan 采集关闭（NullBuilder）重跑首轮，输出与开启逐层
   一致（三层同口径）；
④ 删 plan 文件 → 回落全流程 + log 留痕 + plan 重建。

M2 判据：
- 删除（跨文件同名行只删一行→按原文全局不复发，含印章/凭证/横幅外全部
  通道：native/jspan/内嵌直画），删除清单落日志；
- 改值（按原文全局更新替换文本）；
- 增行（过闸键全局定位被遮 + 防呆键零指令 + 评审决策 #2 文案 + 仅原位记录行）。

口径（自包含、确定性）：OCR 桩固定行；deep_recheck=False（快速模式，
横幅/复检通道不在本套件触发面）；mask_stamp=False（印章关）。
另含 M4 修复回归单测（重放计数口径 + 光栅化阶段指令推迟，不走 e2e 批跑）。

运行：python tests/test_frozen_replay.py
"""
import csv
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("FINMASK_BRANDS", str(
    Path(__file__).resolve().parents[1] / "_eval" / "brands_test.json"))

import pymupdf as fitz
from app.dispatcher import run_batch
from app.handlers import pdf_handler
from app.ocr_engine import OCRLine, OCRPageResult
from app.rules_engine import MaskOptions
from app import masking_plan as mp

# pytest 次要入口：把 test_*(…) 的 root/files/s1/csv1/rows1 形参暴露为
# module 级 fixture（见下方「pytest fixture（仅 pytest 入口）」块），与直跑
# main() 的注入值同构同序。python 直跑不经过 pytest 收集；直跑环境若未装
# pytest，这里的 import 失败也只跳过 fixture 块，main() 口径零变化。
try:
    import pytest
except ImportError:  # pragma: no cover - 直跑环境无 pytest
    pytest = None

FULL_NAME = "云舒雅家居股份有限公司"
CUSTOM = "特批客户订单确认函"
NEW_KEY = "鑫彤建材"
PERSON = "王建国"
LONE_NAME = "李四喜"          # 低置信（孤立闸不收，列锚定收）
CHANGED_REPL = "华为技术有限公司"

PAGE1_TEXT = (
    "变更记录表（批准人） 变更描述说明\n"
    f"{FULL_NAME} suppliers management rules\n"
    f"{CUSTOM}适用于全部大客户经销商网络\n"
    "本办法条款自发布之日起施行请参阅细则\n"
    f"{NEW_KEY}配送管理办法见附件三\n"
    "联系电话0755-1234567传真0755-7654321\n")
PAGE2_TEXT = (
    "附件三：配送与安装服务流程说明文档正文内容足够长以进入原生文本链路"
    "包含诸多与检测无关的制度性描述文字用于撑起每页字符数量门槛之上\n" * 3)

# OCR 桩固定行（页坐标；李四喜低置信→孤立人名闸不收、列头锚定收）
_STUB_LINES = [
    OCRLine(text="姓名", bbox=(70.0, 60.0, 110.0, 80.0), confidence=0.95),
    OCRLine(text=LONE_NAME, bbox=(72.0, 95.0, 117.0, 115.0), confidence=0.55),
    OCRLine(text=PERSON, bbox=(72.0, 125.0, 117.0, 145.0), confidence=0.93),
    OCRLine(text=f"{CUSTOM}补贴81.00", bbox=(70.0, 170.0, 350.0, 190.0),
            confidence=0.92),
    OCRLine(text=f"{NEW_KEY}配送单", bbox=(70.0, 210.0, 300.0, 230.0),
            confidence=0.90),
]


class _StubOcr:
    """确定性 OCR 桩：固定行 + 全局调用计数（零 OCR 探针）。"""

    name = "stub-ocr"
    calls = 0

    def recognize(self, image_bytes: bytes, dpi: int) -> OCRPageResult:
        type(self).calls += 1
        return OCRPageResult(lines=[OCRLine(ln.text, ln.bbox, ln.confidence)
                                    for ln in _STUB_LINES])


def _install_stub_ocr():
    """注入确定性 OCR 桩，返回恢复函数（还原原 _make_ocr_getter）。

    直跑口径行为不变（main() 对返回值可忽略，进程随即退出）；pytest 口径
    在模块 fixture teardown 时调用恢复，避免桩泄漏污染同进程后续套件
    （pytest 全量跑所有套件共用一个解释器，直跑则一文件一进程）。"""
    saved = pdf_handler._make_ocr_getter

    def fake_make_ocr_getter(report, src=None, rule_engine=None, options=None):
        def get_ocr():
            return _StubOcr()
        get_ocr.analysis_cache = None  # _finish_analysis_cache 直接跳过
        return get_ocr

    pdf_handler._make_ocr_getter = fake_make_ocr_getter

    def _restore_stub_ocr():
        pdf_handler._make_ocr_getter = saved

    return _restore_stub_ocr


class _NullBuilder:
    """plan 采集关闭桩：与 PlanBuilder 同接口全 no-op（纯旁路自证用）。"""

    def __init__(self, src, options):
        pass

    def set_page(self, *a, **k):
        pass

    def mark_raster(self, *a, **k):
        pass

    def set_native_text(self, *a, **k):
        pass

    def add_native_spans(self, *a, **k):
        pass

    def ensure_ocr(self, pno):
        return {}

    def set_ocr_items(self, *a, **k):
        pass

    def add_op_rect(self, *a, **k):
        pass

    def add_op_jspan(self, *a, **k):
        pass

    def snapshot_filename(self, *a, **k):
        pass

    def snapshot_table_rows(self, *a, **k):
        pass

    def export_page(self, pno):
        return {}

    def build(self):
        return {}

    def save(self, output_root):
        return Path(output_root) / "none.json"


class _Case:
    def __init__(self):
        self.failed = []
        self.total = 0

    def check(self, name, cond, detail=""):
        self.total += 1
        if not cond:
            self.failed.append(f"{name}: {detail}")
            print(f"  ✘ {name} {str(detail)[:400]}")
        else:
            print(f"  ✔ {name}")


# ---------------------------------------------------------------------------
# 造数与运行器
# ---------------------------------------------------------------------------

def _make_pdf_a(path: Path):
    """甲：2 页原生文本；第 2 页内嵌一张截图位图（直画 OCR 桩通道）。"""
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    y = 80
    for ln in PAGE1_TEXT.splitlines():
        pg.insert_text((72, y), ln, fontname="china-s", fontsize=11)
        y += 22
    pg.insert_text((360, 100), PERSON + "    新增条款说明",
                   fontname="china-s", fontsize=11)  # 批准人列锚定（native 通道）素材
    pg2 = doc.new_page(width=595, height=842)
    pg2.insert_text((72, 80), PAGE2_TEXT[:1800], fontname="china-s", fontsize=9)
    # 内嵌截图位图（内容无关——OCR 桩不读像素；尺寸须过 60x40pt/0.4% 面积闸）
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 240, 140), False)
    pix.clear_with(90)
    pg2.insert_image(fitz.Rect(300, 120, 540, 380), pixmap=pix)
    doc.save(str(path))
    doc.close()


def _make_pdf_b(path: Path):
    """乙：扫描件（可提取字符 <24 → 链路 B 整页 OCR）。"""
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 80), "单据", fontname="china-s", fontsize=10)
    doc.save(str(path))
    doc.close()


def _make_pdf_c(path: Path):
    """丙：与甲跨文件同名原文（删除全局语义载体）。"""
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    text = (f"供应商档案登记表\n{FULL_NAME}入册编号A1001\n"
            f"{CUSTOM}存档一份\n{NEW_KEY}为定点配送单位\n"
            "本表由档案室统一保管备查\n")
    pg.insert_text((72, 90), text, fontname="china-s", fontsize=11)
    doc.save(str(path))
    doc.close()


def _run(files, root: Path, mapping_csv: str = ""):
    # v2.0.1 金额闸（契约 C2）：本套件验证 plan 冻结/重放结构，其「金额兜底
    # a 档」断言依赖 R17 行标签金额 op 存在——金额闸落地后该通道仅在
    # mask_amount=True 时产生 op，故场景显式金额开（期望断言零改动）。
    opts = MaskOptions(output_dir=str(root), deep_recheck=False,
                       mask_stamp=False, ocr_image_text=True,
                       mask_amount=True, amount_mode="mask",
                       custom_words=[CUSTOM], mapping_csv=mapping_csv)
    logs = []
    summary = run_batch(files, opts, logs.append, lambda d, t: None)
    return summary, "\n".join(logs)


def _read_rows(csv_path):
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return [[cell.strip() for cell in row] for row in csv.reader(f)][1:]


def _text_layers(pdf_path):
    doc = fitz.open(str(pdf_path))
    try:
        return [pg.get_text("text") for pg in doc]
    finally:
        doc.close()


def _pixel_layers(pdf_path):
    doc = fitz.open(str(pdf_path))
    try:
        return [pg.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False).samples
                for pg in doc]
    finally:
        doc.close()


def _edit_csv(src_csv, out_csv, drop_origs=(), repl_overrides=None,
              add_rows=()):
    rows = _read_rows(src_csv)
    rows = [r for r in rows if r[0] not in drop_origs]
    if repl_overrides:
        rows = [[r[0], repl_overrides.get(r[0], r[1])] + r[2:] for r in rows]
    rows += [list(r) for r in add_rows]
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["原文", "替换为", "规则", "文件", "次数", "位置", "置信度"])
        w.writerows(rows)
    return out_csv


def _build_world():
    """造数：临时根 + 3 份样例 PDF + OCR 桩。直跑 main() 与 pytest 共用。

    返回 (root, files, restore)——restore 为 OCR 桩恢复函数（见
    _install_stub_ocr）。"""
    root = Path(tempfile.mkdtemp(prefix="v194frozen_"))
    src_root = root / "src"
    src_root.mkdir(parents=True)
    files = [str(src_root / n) for n in
             ("云舒雅家居管理制度甲.pdf", "扫描单据乙.pdf", "供应商名录丙.pdf")]
    _make_pdf_a(Path(files[0]))
    _make_pdf_b(Path(files[1]))
    _make_pdf_c(Path(files[2]))
    restore = _install_stub_ocr()
    return root, files, restore


# ---------------------------------------------------------------------------
# pytest fixture（仅 pytest 入口；python 直跑不加载、不受影响）
# ---------------------------------------------------------------------------
# 直跑口径：main() 先 _build_world() 造一次共享 world，test_m1_frozen_replay
# 跑首轮产出 (s1, csv1, rows1) 返回给 main 再注入后续用例。pytest 口径：
# root/files 用 module 级 fixture 共享（同 main 的单一 world）；s1/csv1/rows1
# 用 module 级 fixture 复刻 test_m1_frozen_replay 的首轮产物（清掉 r1 重跑真
# 首轮，产物与直跑逐字节同构——全链路确定性），首次被下游用例请求时惰性执行
# 一次后缓存，后续用例共享同一份（同 main 的注入语义）。用例函数体与执行顺序
# 与直跑完全一致，零改动。
if pytest is not None:
    @pytest.fixture(scope="module")
    def _world():
        root, files, restore = _build_world()
        try:
            yield root, files
        finally:
            restore()  # 桩不泄漏到同进程后续套件

    @pytest.fixture(scope="module")
    def root(_world):
        return _world[0]

    @pytest.fixture(scope="module")
    def files(_world):
        return _world[1]

    @pytest.fixture(scope="module")
    def _m1_artifacts(root, files):
        import shutil
        shutil.rmtree(root / "r1", ignore_errors=True)  # 保证真·首轮（有 plan 落盘）
        s1, _log = _run(files, root / "r1")
        rows1 = _read_rows(s1["mapping"])
        csv1 = str(root / "csv1.csv")
        _edit_csv(s1["mapping"], csv1)
        return s1, csv1, rows1

    @pytest.fixture
    def s1(_m1_artifacts):
        return _m1_artifacts[0]

    @pytest.fixture
    def csv1(_m1_artifacts):
        return _m1_artifacts[1]

    @pytest.fixture
    def rows1(_m1_artifacts):
        return _m1_artifacts[2]


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def test_replay_count_and_order(c: _Case):
    """M4 修复回归（V-C 三缺陷之 2/3）：重放计数口径 + 光栅化阶段指令推迟。

    - banner/visual_recheck 通道首轮不计入 masked（返回值弃用/仅 leaked），
      重放同规则（PLAN_UNCOUNTED_RULES）；
    - band_recheck 指令首轮在光栅化阶段（全部页之后）才应用，重放页内重放
      时必须归集推迟（PLAN_RASTER_STAGE_RULES），其映射行才不插队。
    """
    print("· M4 修复回归：重放计数口径（banner/visual 不计）+ band 指令推迟")
    from app.handlers import pdf_handler as ph

    class _Rep:
        def __init__(self):
            self.rules = []

        def record(self, *a, **k):
            self.rules.append(k.get("rule", a[2] if len(a) > 2 else ""))

        def log(self, _msg):
            pass

    ops = [
        {"t": "rect", "k": "draw", "rect": [50, 50, 150, 70], "repl": "***",
         "rec": "***", "orig": "某词", "rule": "visual_recheck",
         "loc": "第1页(视觉复检)", "conf": ""},
        {"t": "rect", "k": "draw", "rect": [50, 100, 150, 120], "repl": "***",
         "rec": "整图遮盖", "orig": "凭证截图", "rule": "invoice",
         "loc": "第1页(图内文字)", "conf": ""},
        {"t": "rect", "k": "draw", "rect": [50, 200, 150, 220], "repl": "***",
         "rec": "***", "orig": "计中心", "rule": "band_recheck",
         "loc": "第1页(条带复检)", "conf": ""},
    ]
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    rep = _Rep()
    deferred: dict = {}
    n = ph._apply_plan_ops(pg, ops, rep, 0, fitz, None, deferred)
    c.check("band 指令归集推迟（不随页内应用）",
            deferred.get(0) == [ops[2]], str(deferred))
    c.check("页内记录序=存储序（band 行不插队）",
            rep.rules == ["visual_recheck", "invoice"], str(rep.rules))
    c.check("banner/visual 通道不计 masked（首轮同口径）", n == 1, str(n))
    n2 = ph._apply_plan_ops(pg, deferred.get(0) or [], rep, 0, fitz, None)
    c.check("光栅化阶段应用 band 指令并计入 masked（band 首轮计入）",
            n2 == 1 and rep.rules[-1] == "band_recheck", str((n2, rep.rules)))
    # 常量与首轮计数点同源自证：两通道规则串与 pdf_handler 埋点逐字一致
    c.check("口径常量内容",
            mp.PLAN_UNCOUNTED_RULES == {"banner", "visual_recheck"}
            and mp.PLAN_RASTER_STAGE_RULES == {"band_recheck"},
            str((mp.PLAN_UNCOUNTED_RULES, mp.PLAN_RASTER_STAGE_RULES)))
    doc.close()


def test_m1_frozen_replay(c: _Case, root: Path, files):
    print("· M1：首轮 → plan 落盘 → 零编辑重放三层等价 + 零 OCR")
    s1, log1 = _run(files, root / "r1")
    c.check("首轮 3 文件全成功", s1["ok"] == 3, str(s1))
    plan_dir = root / "r1" / "cache" / "plans"
    plans = sorted(plan_dir.glob("*.json"))
    c.check("3 份 plan 落盘", len(plans) == 3, str([p.name for p in plans]))
    # 通道覆盖自证：甲页 2（内嵌图直画）与乙页 1（扫描 OCR）指令形态
    pa = mp.load_plan(files[0], MaskOptions(output_dir=str(root / "r1")))[0]
    pb = mp.load_plan(files[1], MaskOptions(output_dir=str(root / "r1")))[0]
    pgb2 = pa.page_by_pno(1)
    locs = [op.get("loc", "") for op in pgb2.get("ops") or []]
    c.check("甲页2 孤立人名直画指令入 plan",
            any("图内文字" in lc for lc in locs), str(locs))
    c.check("甲页2 图内表格列头锚定指令入 plan",
            any("图内表格人名" in lc for lc in locs), str(locs))
    c.check("甲页2 内嵌图 OCR items 自包含", bool((pgb2.get("ocr") or {}).get("items")))
    pgb1 = pb.page_by_pno(0)
    c.check("乙页1 mode=ocr", pgb1["mode"] == "ocr")
    jspans = [op for op in pgb1.get("ops") or [] if op.get("t") == "jspan"]
    c.check("乙页1 OCR 拼接区间指令（含金额兜底 a 档）",
            any(sp.get("p") == "a" for sp in jspans)
            and any(sp.get("orig") == CUSTOM for sp in jspans),
            str([(sp.get("orig"), sp.get("p")) for sp in jspans]))
    c.check("命中页 raster 标记入 plan",
            pgb1.get("raster") is True and pgb2.get("raster") is True)
    c.check("乙页1 low_conf 计数入 plan", pgb1.get("low_conf") == 1,
            str(pgb1.get("low_conf")))

    out1 = [Path(p) for p in s1["outputs"]]
    rows1 = _read_rows(s1["mapping"])
    csv1 = str(root / "csv1.csv")
    _edit_csv(s1["mapping"], csv1)

    # —— 零编辑复核轮 ——
    _StubOcr.calls = 0
    s2, log2 = _run(files, root / "r1", mapping_csv=csv1)
    c.check("复核轮 3 文件全成功", s2["ok"] == 3, str(s2))
    c.check("命中冻结 plan 日志（plan 重放证线）",
            log2.count("冻结复核：命中首轮冻结 plan") == 3, log2[:500])
    c.check("复核轮 OCR 调用数=0（零 OCR 探针）", _StubOcr.calls == 0,
            str(_StubOcr.calls))
    c.check("零 OCR 零检测日志", log2.count("零 OCR 零检测") >= 3)
    out2 = [Path(p) for p in s2["outputs"]]
    rows2 = _read_rows(s2["mapping"])
    c.check("映射行逐行有序全等（映射层）", rows1 == rows2,
            f"\n only1={sorted(map(str, rows1))[:3]}\n only2={sorted(map(str, rows2))[:3]}")
    # outputs 顺序=jobs 顺序（文件清单序），按下标配对（文件名可能因编辑而异）
    eq_text = all(_text_layers(a) == _text_layers(b)
                  for a, b in zip(out1, out2))
    c.check("文本层等价（呈现层）", eq_text)
    eq_pix = all(_pixel_layers(a) == _pixel_layers(b)
                 for a, b in zip(out1, out2))
    c.check("像素等价（遮蔽并集坐标的视觉面）", eq_pix)
    c.check("输出文件名等价（文件名快照回放）",
            [p.name for p in sorted(out1)] == [p.name for p in sorted(out2)],
            f"{[p.name for p in out1]} vs {[p.name for p in out2]}")
    return s1, csv1, rows1


def test_first_round_purity(c: _Case, root: Path, files, s1, rows1):
    print("· M1③：首轮纯旁路自证（关闭 plan 采集重跑，三层不变）")
    saved = mp.PlanBuilder
    mp.PlanBuilder = _NullBuilder
    try:
        s0, _log0 = _run(files, root / "r0")
    finally:
        mp.PlanBuilder = saved
    c.check("关闭采集：3 文件成功", s0["ok"] == 3, str(s0))
    rows0 = _read_rows(s0["mapping"])
    c.check("映射行与开启采集全等", rows0 == rows1,
            f"n0={len(rows0)} n1={len(rows1)}")
    out0 = [Path(p) for p in s0["outputs"]]
    out1 = [Path(p) for p in s1["outputs"]]
    c.check("文本层不变", all(_text_layers(a) == _text_layers(b)
                              for a, b in zip(out0, out1)))
    c.check("像素不变", all(_pixel_layers(a) == _pixel_layers(b)
                            for a, b in zip(out0, out1)))
    c.check("无 plan 落盘（关闭采集时）",
            not any((root / "r0" / "cache" / "plans").glob("*.json")))


def test_fallback(c: _Case, root: Path, files, csv1, s1):
    print("· M1④：删 plan → 回落全流程 + log 留痕 + plan 重建")
    plan_b_path = mp.plan_path_for(
        files[1], mp.output_root(files[1], MaskOptions(output_dir=str(root / "r1"))))
    c.check("乙 plan 在盘", plan_b_path.exists())
    plan_b_path.unlink()
    _StubOcr.calls = 0
    s3, log3 = _run(files, root / "r1", mapping_csv=csv1)
    c.check("回落轮 3 文件全成功", s3["ok"] == 3, str(s3))
    c.check("乙回落留痕（人话：原因+已回落）",
            "未命中首轮冻结 plan" in log3 and "回落" in log3, log3[:600])
    c.check("甲丙仍走冻结重放",
            log3.count("冻结复核：命中首轮冻结 plan") == 2, log3[:600])
    c.check("回落轮重建乙 plan", plan_b_path.exists())
    c.check("回落轮发生真实 OCR（全流程语义）", _StubOcr.calls > 0,
            str(_StubOcr.calls))
    c.check("回落轮乙映射行与首轮一致（确定性全流程）",
            {r[0] for r in _read_rows(s3["mapping"]) if "乙" in r[3]}
            == {r[0] for r in _read_rows(s1["mapping"]) if "乙" in r[3]})


def test_m2_delete(c: _Case, root: Path, files, csv1):
    print("· M2：删除按原文全局生效（跨文件同名行只删一行→全局不复发）")
    csv4 = str(root / "csv4.csv")
    _edit_csv(csv1, csv4, drop_origs=(CUSTOM,))
    s4, log4 = _run(files, root / "r1", mapping_csv=csv4)
    c.check("删除轮 3 文件全成功", s4["ok"] == 3, str(s4))
    rows4 = _read_rows(s4["mapping"])
    c.check("被删原文在汇总表零出现（跨文件全局）",
            all(r[0] != CUSTOM for r in rows4),
            str([r for r in rows4 if r[0] == CUSTOM]))
    c.check("删除清单落日志（含通道说明）",
            f"按原文全局生效" in log4 and CUSTOM in log4, log4[:800])
    # 呈现层：甲/丙原生页原文恢复明文（未被遮蔽）
    outs = {Path(p).name: Path(p) for p in s4["outputs"]}
    name_a = [n for n in outs if "甲" in n]
    name_c = [n for n in outs if "丙" in n]
    t_a = "\n".join(_text_layers(outs[name_a[0]]))
    t_c = "\n".join(_text_layers(outs[name_c[0]]))
    c.check("甲输出文本层恢复原文（native 通道解除遮蔽）", CUSTOM in t_a)
    c.check("丙输出文本层恢复原文（跨文件同名行不复发）", CUSTOM in t_c)
    c.check("其余行不受删除牵连（云舒雅公司行仍在）",
            any(r[0] == FULL_NAME and r[2] == "company" for r in rows4),
            str([r[0] for r in rows4 if r[2] == "company"]))
    c.check("删除轮仍零 OCR（冻结重放）", log4.count("零 OCR 零检测") >= 2)


def test_m2_change(c: _Case, root: Path, files, csv1):
    print("· M2：改值按原文全局更新替换文本")
    csv5 = str(root / "csv5.csv")
    _edit_csv(csv1, csv5, repl_overrides={FULL_NAME: CHANGED_REPL})
    s5, log5 = _run(files, root / "r1", mapping_csv=csv5)
    c.check("改值轮 3 文件全成功", s5["ok"] == 3, str(s5))
    rows5 = _read_rows(s5["mapping"])
    c.check("替换文本全局更新（映射层）",
            all(r[1] == CHANGED_REPL for r in rows5 if r[0] == FULL_NAME),
            str([r for r in rows5 if r[0] == FULL_NAME]))
    outs = {Path(p).name: Path(p) for p in s5["outputs"]}
    t_all = "\n".join(
        "\n".join(_text_layers(p)) for n, p in outs.items() if "乙" not in n)
    c.check("呈现层写入新替换文本", CHANGED_REPL in t_all)
    c.check("改值日志留痕", "改值" in log5, log5[:800])


def test_m2_add_and_gates(c: _Case, root: Path, files, csv1):
    print("· M2/M3：增行（过闸全局被遮）+ 防呆四闸（零指令+文案+仅原位）")
    add_rows = [
        (NEW_KEY, "XX建材", "custom", "手工增行", "1", "-", ""),
        ("关", "***", "mapping", "手工增行", "1", "-", ""),
        ("公司", "***", "mapping", "手工增行", "1", "-", ""),
        ("（普通合伙）", "***", "mapping", "手工增行", "1", "-", ""),
        ("条款", "***", "mapping", "手工增行", "1", "-", ""),
    ]
    csv6 = str(root / "csv6.csv")
    _edit_csv(csv1, csv6, add_rows=add_rows)
    s6, log6 = _run(files, root / "r1", mapping_csv=csv6)
    c.check("增行轮 3 文件全成功", s6["ok"] == 3, str(s6))
    rows6 = _read_rows(s6["mapping"])
    # —— 过闸增行：全局定位被遮（native 页 + 扫描页 OCR joined 两形态）——
    add_rows_out = [r for r in rows6 if r[0] == NEW_KEY]
    c.check("增行词被遮（映射行出现且替换文本正确）",
            add_rows_out and all(r[1] == "XX建材" for r in add_rows_out),
            str(add_rows_out))
    files_with_add = {r[3] for r in add_rows_out}
    c.check("增行跨文件全局定位（含扫描件 OCR 页）",
            len(files_with_add) == 3, str(files_with_add))
    c.check("增行定位日志", "增行键已按冻结文本全局定位遮蔽" in log6
            and NEW_KEY in log6, log6[:900])
    outs6 = {Path(p).name: Path(p) for p in s6["outputs"]}
    t_a6 = "\n".join(_text_layers([p for n, p in outs6.items() if "甲" in n][0]))
    c.check("甲 native 页增行词呈现层被替换", NEW_KEY not in t_a6
            and "XX建材" in t_a6)
    # —— 防呆四闸：零自动指令 + 文案 + 仅原位记录行 ——
    for key in ("关", "公司", "（普通合伙）", "条款"):
        c.check(f"防呆键「{key}」零自动遮蔽指令（映射表无输出行）",
                all(r[0] != key or "仅原位" in r[5] for r in rows6),
                str([r for r in rows6 if r[0] == key]))
    c.check("评审决策 #2 文案落日志（调度层逐键 4 次 + 重放轮逐文件提示）",
            log6.count("该键过于通用，仅应用于当前位置（该键仅记录于映射表，"
                       "不产生自动遮蔽指令）") >= 4, str(log6.count(
                           "该键过于通用，仅应用于当前位置")))
    inplace = [r for r in rows6 if "仅原位" in r[5]]
    c.check("防呆键仅原位记录行（保留于汇总表）",
            {r[0] for r in inplace} == {"关", "公司", "（普通合伙）", "条款"},
            str(inplace))
    # 「条款」在甲正文出现且保持明文（未被通用键误伤）
    c.check("正文「条款」保持明文", "条款" in t_a6)


def main():
    print("== v1.9.4 首轮检测冻结端到端回归（M1+M2） ==")
    c = _Case()
    root, files, _restore = _build_world()
    try:
        test_replay_count_and_order(c)
        s1, csv1, rows1 = test_m1_frozen_replay(c, root, files)
        test_first_round_purity(c, root, files, s1, rows1)
        test_fallback(c, root, files, csv1, s1)
        test_m2_delete(c, root, files, csv1)
        test_m2_change(c, root, files, csv1)
        test_m2_add_and_gates(c, root, files, csv1)
    finally:
        _restore()  # 直跑出口恢复桩（进程即退，行为不变；防御性卫生）

    print(f"\n共 {c.total} 条用例，失败 {len(c.failed)} 条")
    return not c.failed


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
