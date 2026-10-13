# -*- coding: utf-8 -*-
"""v2.0.1 C1 遮蔽台账回归（红线一可追溯性）：op 级全量 + 与 plan 四关系。

自造 2 文件小批（印章关、快速模式），OCR 桩固定行（确定性）：
- 甲：native 1 页（公司/人名/自定义词/联系电话——主链路 find_spans 通道）；
- 乙：扫描页（<24 字符 → 链路 B 整页 OCR，桩行含「{自定义词}补贴81.00」——
  OCR 拼接 jspan 通道 + R17 行标签金额兜底（rule=amount，主映射表剥除面））。

契约锚（v201_contract.md C1 验收锚逐条）：
① 台账完整性：画块总数=台账行数（逐通道对账，plan 指令为独立对账工件）；
② 重放零新增：零编辑复核轮零新增台账行、不重写台账文件（沿用首轮）；
③ 编辑三态流转：删除→deleted / 改值→edited / 增行→新增 active（op_id
   复用首轮序，增行续号）；
④ 主映射表零变化：amount 行不进主表、只进台账（dispatcher :410 剥除口径
   原样保留的强判据）；
⑤ 回落轮：plan 未命中 → 台账按当轮重检重建 + 留痕日志。

另含纯函数单测：build_ledger_rows（「仅原位」防呆留痕行不入账）与
write_ledger_csv（7 列列头/utf-8-sig BOM）。

运行：python tests/test_masking_ledger.py
"""
import csv
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("FINMASK_BRANDS", str(
    Path(__file__).resolve().parents[1] / "_eval" / "brands_test.json"))

import pymupdf as fitz
from app.dispatcher import (LEDGER_ACTIVE, LEDGER_DELETED, LEDGER_EDITED,
                            LEDGER_HEADER, _FileReport, build_ledger_rows,
                            run_batch, write_ledger_csv)
from app.handlers import pdf_handler
from app.ocr_engine import OCRLine, OCRPageResult
from app.rules_engine import MaskOptions
from app import masking_plan as mp

# pytest 次要入口：把 test_*(…) 的 tmp/root/files/s1/rows1 形参暴露为
# module 级 fixture（见下方「pytest fixture（仅 pytest 入口）」块），与直跑
# main() 的注入值同构同序。python 直跑不经过 pytest 收集；直跑环境若未装
# pytest，这里的 import 失败也只跳过 fixture 块，main() 口径零变化。
try:
    import pytest
except ImportError:  # pragma: no cover - 直跑环境无 pytest
    pytest = None

FULL_NAME = "云舒雅家居股份有限公司"
CUSTOM = "特批客户订单确认函"
NEW_KEY = "鑫彤建材"          # 首轮不遮（M2 增行同款素材），编辑轮增行用
PERSON = "王建国"
CHANGED_REPL = "华为技术有限公司"
PHONE = "0755-1234567"

PAGE1_TEXT = (
    "变更记录表（批准人） 变更描述说明\n"
    f"{FULL_NAME} suppliers management rules\n"
    f"{CUSTOM}适用于全部大客户经销商网络\n"
    "本办法条款自发布之日起施行请参阅细则\n"
    f"{NEW_KEY}配送管理办法见附件三\n"
    f"联系电话{PHONE}传真0755-7654321\n")

# OCR 桩固定行（乙扫描页；「补贴81.00」触发 R17 行标签金额兜底）
_STUB_LINES = [
    OCRLine(text="姓名", bbox=(70.0, 60.0, 110.0, 80.0), confidence=0.95),
    OCRLine(text=PERSON, bbox=(72.0, 125.0, 117.0, 145.0), confidence=0.93),
    OCRLine(text=f"{CUSTOM}补贴81.00", bbox=(70.0, 170.0, 350.0, 190.0),
            confidence=0.92),
    OCRLine(text=f"{NEW_KEY}配送单", bbox=(70.0, 210.0, 300.0, 230.0),
            confidence=0.90),
]


class _StubOcr:
    """确定性 OCR 桩：固定行 + 全局调用计数。"""

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
        get_ocr.analysis_cache = None
        return get_ocr

    pdf_handler._make_ocr_getter = fake_make_ocr_getter

    def _restore_stub_ocr():
        pdf_handler._make_ocr_getter = saved

    return _restore_stub_ocr


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
    """甲：1 页原生文本（主链路通道素材）。"""
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    y = 80
    for ln in PAGE1_TEXT.splitlines():
        pg.insert_text((72, y), ln, fontname="china-s", fontsize=11)
        y += 22
    doc.save(str(path))
    doc.close()


def _make_pdf_b(path: Path):
    """乙：扫描件（可提取字符 <24 → 链路 B 整页 OCR）。"""
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    pg.insert_text((72, 80), "单据", fontname="china-s", fontsize=10)
    doc.save(str(path))
    doc.close()


def _run(files, root: Path, mapping_csv: str = ""):
    opts = MaskOptions(output_dir=str(root), deep_recheck=False,
                       mask_stamp=False, ocr_image_text=True, mask_amount=True,
                       custom_words=[CUSTOM], mapping_csv=mapping_csv)
    logs = []
    summary = run_batch(files, opts, logs.append, lambda d, t: None)
    return summary, "\n".join(logs)


def _read_mapping(csv_path):
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        return [[c.strip() for c in row] for row in csv.reader(f)][1:]


def _read_ledger(csv_path):
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    assert rows[0] == LEDGER_HEADER, f"台账列头异常：{rows[0]}"
    return [dict(zip(LEDGER_HEADER, r)) for r in rows[1:]]


def _edit_mapping(src_csv, out_csv, drop_origs=(), repl_overrides=None,
                  add_rows=()):
    rows = _read_mapping(src_csv)
    rows = [r for r in rows if r[0] not in drop_origs]
    if repl_overrides:
        rows = [[r[0], repl_overrides.get(r[0], r[1])] + r[2:] for r in rows]
    rows += [list(r) for r in add_rows]
    with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["原文", "替换为", "规则", "文件", "次数", "位置", "置信度"])
        w.writerows(rows)
    return out_csv


def _plan_rule_counts(src, root: Path) -> Counter:
    """独立对账工件：plan 指令（native_spans+ops）按规则计数（画块口径）。"""
    plan, _ = mp.load_plan(src, MaskOptions(output_dir=str(root)))
    cnt = Counter()
    for pg in plan.pages:
        for s in pg.get("native_spans") or []:
            cnt[s[2]] += 1
        for op in pg.get("ops") or []:
            cnt[op.get("rule", "")] += 1
    return cnt


def _build_world():
    """造数：单测临时目录 + e2e 临时根 + 2 份样例 PDF + OCR 桩。

    直跑 main() 与 pytest 共用。返回 (tmp, root, files, restore)——restore
    为 OCR 桩恢复函数（见 _install_stub_ocr）。"""
    tmp = Path(tempfile.mkdtemp(prefix="v201ledger_unit_"))
    root = Path(tempfile.mkdtemp(prefix="v201ledger_e2e_"))
    src_root = root / "src"
    src_root.mkdir(parents=True)
    files = [str(src_root / n) for n in ("文档甲.pdf", "扫描单据乙.pdf")]
    _make_pdf_a(Path(files[0]))
    _make_pdf_b(Path(files[1]))
    restore = _install_stub_ocr()
    return tmp, root, files, restore


# ---------------------------------------------------------------------------
# pytest fixture（仅 pytest 入口；python 直跑不加载、不受影响）
# ---------------------------------------------------------------------------
# 直跑口径：main() 先 _build_world() 造一次共享 world，test_round1_integrity
# 跑首轮产出 (s1, rows1) 返回给 main 再注入后续用例。pytest 口径：tmp/root/
# files 用 module 级 fixture 共享（同 main 的单一 world）；s1/rows1 用 module
# 级 fixture 复刻 test_round1_integrity 的首轮产物（清掉 r1 重跑真·首轮——
# 台账只在首轮落盘、冻结重放轮零台账产物，故必须保证这里走首轮检测；全链路
# 确定性 ⇒ 产物与直跑同构），首次被下游用例请求时惰性执行一次后缓存，后续
# 用例共享同一份（同 main 的注入语义）。用例函数体与执行顺序与直跑完全一致，
# 零改动。
if pytest is not None:
    @pytest.fixture(scope="module")
    def _world():
        tmp, root, files, restore = _build_world()
        try:
            yield tmp, root, files
        finally:
            restore()  # 桩不泄漏到同进程后续套件

    @pytest.fixture(scope="module")
    def tmp(_world):
        return _world[0]

    @pytest.fixture(scope="module")
    def root(_world):
        return _world[1]

    @pytest.fixture(scope="module")
    def files(_world):
        return _world[2]

    @pytest.fixture(scope="module")
    def _m1_artifacts(root, files):
        import shutil
        shutil.rmtree(root / "r1", ignore_errors=True)  # 保证真·首轮（台账落盘）
        s1, _log = _run(files, root / "r1")
        rows1 = _read_ledger(Path(s1["ledger"]))
        return s1, rows1

    @pytest.fixture
    def s1(_m1_artifacts):
        return _m1_artifacts[0]

    @pytest.fixture
    def rows1(_m1_artifacts):
        return _m1_artifacts[1]


# ---------------------------------------------------------------------------
# 纯函数单测
# ---------------------------------------------------------------------------

def test_unit_ledger_helpers(c: _Case, tmp: Path):
    print("· 单测：build_ledger_rows / write_ledger_csv（列头/BOM/仅原位剔除）")
    ops = [("甲公司", "×××", "company", "第1页"),
           ("", "***", "amount", "第1页(列对齐)"),  # 空原文 op 也要入账
           ("1200", "***", "amount", "第1页(列对齐)"),
           ("某键", "***", "mapping", "仅原位")]  # 防呆留痕行：非画块不入账
    rows = build_ledger_rows(ops, "文档甲.pdf")
    c.check("op 级全量不去重且空原文入账", len(rows) == 3, str(rows))
    c.check("防呆「仅原位」行不入账（画块对账恒等）",
            all(r[3] != "某键" for r in rows), str(rows))
    c.check("op_id 文件内 1-based 连续", [r[5] for r in rows] == [1, 2, 3],
            str([r[5] for r in rows]))
    c.check("首轮状态全 active", all(r[6] == LEDGER_ACTIVE for r in rows))
    p = write_ledger_csv(tmp, rows)
    c.check("台账文件名口径（遮蔽台账_<时间戳>.csv）",
            p.name.startswith("遮蔽台账_") and p.name.endswith(".csv"), p.name)
    raw = p.read_bytes()
    c.check("utf-8-sig 编码（BOM 在位）", raw.startswith(b"\xef\xbb\xbf"))
    c.check("7 列列头（契约冻结）", _read_ledger(p) and
            raw.decode("utf-8-sig").splitlines()[0].split(",") == LEDGER_HEADER)


def test_file_report_dual_capture(c: _Case):
    print("· 单测：_FileReport.record 双写（主映射表口径零变化 + 台账全量）")
    rep = _FileReport("文档甲.pdf", lambda m: None, lambda d, t: None, 0, 1, 1)
    rep.record("甲公司", "×××", "company", "第1页")
    rep.record("", "***", "amount", "第1页(列对齐)")
    c.check("主表记录维持原 if original 过滤（口径零变化）",
            rep.records == [("甲公司", "×××", "company", "第1页")],
            str(rep.records))
    c.check("台账 op_records 收全量（含空原文）", len(rep.op_records) == 2,
            str(rep.op_records))


# ---------------------------------------------------------------------------
# 端到端：与 plan 四关系
# ---------------------------------------------------------------------------

def test_round1_integrity(c: _Case, root: Path, files):
    """① 台账完整性（多通道小样逐通道对账）+ ④ 主映射表零变化。"""
    print("· 首轮：台账落盘 + 逐通道对账 + amount 行只进台账不进主表")
    s1, log1 = _run(files, root / "r1")
    c.check("首轮 2 文件全成功", s1["ok"] == 2, str(s1))
    ledger_path = Path(s1["ledger"])
    c.check("首轮台账落盘（与映射表同目录）",
            ledger_path.parent == Path(s1["mapping"]).parent, str(s1))
    rows1 = _read_ledger(ledger_path)
    c.check("台账非空", len(rows1) > 0, str(len(rows1)))
    c.check("首轮状态全 active", all(r["状态"] == LEDGER_ACTIVE for r in rows1))
    c.check("op_id 每文件内 1-based 连续（烧入规范序）",
            all(sorted(int(r["op_id"]) for r in rows1 if r["文件"] == f)
                == list(range(1, sum(1 for r in rows1 if r["文件"] == f) + 1))
                for f in {r["文件"] for r in rows1}))
    # —— 逐通道对账：台账行数 = plan 画块指令数（独立工件对账）——
    # filename 通道（finalize_output_name 的文件名重命名记录，v1.4 既有口径）
    # 非画块、不在 plan 指令域，单独断言；其余通道逐一与 plan 对账。
    # v2.1.9 B12：记录条件修复后，filename 行仅在词干真实替换/碰撞加序时
    # 入账（原 src_name 含扩展名恒真 → 每文件 1 行噪音）。本夹具文件名不含
    # 任何实体词 → 词干零改动 → filename 行=0；有重命名时恰 1 行且 loc=文件名。
    for src in files:
        fname = Path(src).name
        led_all = Counter(r["通道"] for r in rows1 if r["文件"] == fname)
        led = Counter({k: v for k, v in led_all.items() if k != "filename"})
        n_fn = led_all.get("filename", 0)
        c.check(f"{fname} 文件名行按 B12 语义入账（0=词干零改动；1=真实重命名）",
                n_fn in (0, 1)
                and (n_fn == 0 or any(r["通道"] == "filename" and r["页"] == "文件名"
                                      for r in rows1 if r["文件"] == fname)),
                str(led_all))
        plan_cnt = _plan_rule_counts(src, root / "r1")
        c.check(f"{fname} 画块总数=台账行数（总对账，filename 通道除外）",
                sum(led.values()) == sum(plan_cnt.values()),
                f"台账={dict(led)} plan={dict(plan_cnt)}")
        c.check(f"{fname} 逐通道对账全等",
                led == plan_cnt, f"台账={dict(led)} plan={dict(plan_cnt)}")
    # —— 通道覆盖自证：amount 家族必须入账 ——
    amt = [r for r in rows1 if r["通道"] == "amount"]
    c.check("R17 行标签金额兜底入台账（rule=amount）",
            any(r["原文"] == "81.00" for r in amt), str(amt))
    # —— 主映射表零变化：amount 行不进主表，其余台账键与主表键同域 ——
    mapping_rows = _read_mapping(s1["mapping"])
    c.check("主映射表零变化：amount 行零出现（:410 剥除口径原样保留）",
            all(r[2] != "amount" for r in mapping_rows),
            str([r for r in mapping_rows if r[2] == "amount"]))
    led_keys = {(r["原文"], r["替换"], r["通道"])
                for r in rows1 if r["通道"] != "amount"}
    map_keys = {(r[0], r[1], r[2]) for r in mapping_rows if r[2] != "amount_formula"}
    c.check("主表键 ⊆ 台账键（台账是全量超集）", map_keys <= led_keys,
            f"仅主表有：{map_keys - led_keys}")
    c.check("首轮台账生成日志留痕", "已生成遮蔽台账" in log1, log1[-400:])
    return s1, rows1


def test_round2_replay_zero_new(c: _Case, root: Path, files, s1, rows1):
    """② 复核轮重放：零新增台账行、不重写台账文件。"""
    print("· 零编辑复核轮：零新增、不重写（台账沿用首轮产物）")
    csv2 = str(root / "csv2.csv")
    _edit_mapping(s1["mapping"], csv2)
    _StubOcr.calls = 0
    s2, log2 = _run(files, root / "r1", mapping_csv=csv2)
    c.check("复核轮 2 文件全成功", s2["ok"] == 2, str(s2))
    c.check("命中冻结 plan（重放证线）",
            log2.count("冻结复核：命中首轮冻结 plan") == 2, log2[:400])
    batch2 = Path(s2["output_dir"])
    ledgers2 = list(batch2.glob("遮蔽台账_*.csv"))
    c.check("重放轮不重写台账文件（批次目录零台账产物）",
            not ledgers2, str([p.name for p in ledgers2]))
    c.check("沿用留痕日志", "遮蔽台账：本轮为冻结重放" in log2
            and "零新增" in log2, log2[-500:])
    c.check("summary 无 ledger 键（零新增不重写）", "ledger" not in s2,
            str(s2.get("ledger")))
    # 重放映射行与首轮全等（既有 M1 语义，作为「重放行=首轮行」旁证）
    c.check("重放映射行=首轮映射行（零新增旁证）",
            _read_mapping(s2["mapping"]) == _read_mapping(s1["mapping"]))


def test_round3_edit_three_states(c: _Case, root: Path, files, s1, rows1):
    """③ 编辑三态流转：删除→deleted / 改值→edited / 增行→新增 active。"""
    print("· 编辑轮：三态流转 + op_id 复用首轮序")
    csv3 = str(root / "csv3.csv")
    _edit_mapping(s1["mapping"], csv3, drop_origs=(CUSTOM,),
                  repl_overrides={FULL_NAME: CHANGED_REPL},
                  add_rows=[(NEW_KEY, "XX建材", "custom", "手工增行", "1", "-", "")])
    _StubOcr.calls = 0
    s3, log3 = _run(files, root / "r1", mapping_csv=csv3)
    c.check("编辑轮 2 文件全成功", s3["ok"] == 2, str(s3))
    c.check("编辑轮台账落盘", bool(s3.get("ledger")), str(s3.get("ledger")))
    rows3 = _read_ledger(Path(s3["ledger"]))
    states = {r["状态"] for r in rows3}
    c.check("三态齐现（deleted/edited/active）",
            {LEDGER_DELETED, LEDGER_EDITED, LEDGER_ACTIVE} <= states, str(states))
    # 删除：CUSTOM 全通道行保留并标 deleted，op_id 复用首轮序
    r1_by = {(r["文件"], r["原文"], r["op_id"]) for r in rows1}
    del_rows = [r for r in rows3 if r["原文"] == CUSTOM]
    c.check("删除行保留于台账且状态=deleted",
            del_rows and all(r["状态"] == LEDGER_DELETED for r in del_rows),
            str([(r["文件"], r["通道"], r["状态"]) for r in del_rows]))
    c.check("删除行 op_id 复用首轮序",
            all((r["文件"], r["原文"], r["op_id"]) in r1_by for r in del_rows),
            str([(r["文件"], r["op_id"]) for r in del_rows]))
    # 改值：FULL_NAME 行状态=edited，替换文本全局更新，op_id 不变
    chg_rows = [r for r in rows3 if r["原文"] == FULL_NAME]
    c.check("改值行状态=edited 且替换文本全局更新",
            chg_rows and all(r["状态"] == LEDGER_EDITED
                             and r["替换"] == CHANGED_REPL for r in chg_rows),
            str([(r["替换"], r["状态"]) for r in chg_rows]))
    c.check("改值行 op_id 复用首轮序",
            all((r["文件"], r["原文"], r["op_id"]) in r1_by for r in chg_rows),
            str([(r["文件"], r["op_id"]) for r in chg_rows]))
    # 增行：NEW_KEY 新画块 → 新增 active 行，op_id 续号（大于该文件首轮最大号）
    add_rows = [r for r in rows3 if r["原文"] == NEW_KEY]
    c.check("增行产生新台账行且状态=active",
            add_rows and all(r["状态"] == LEDGER_ACTIVE for r in add_rows),
            str(add_rows))
    c.check("增行行列序正确（通道=mapping/custom、页=当轮定位页）",
            add_rows and all(r["通道"] in ("mapping", "custom")
                             and r["页"].startswith("第1页") for r in add_rows),
            str([(r["页"], r["通道"], r["替换"]) for r in add_rows]))
    ok_cont = True
    for f in {r["文件"] for r in add_rows}:
        # 续号基准=首轮画块 op_id 上限（filename 行是 finalize 收尾的簿记行，
        # v1.4 既有 always-record 行为，不占画块序——其在编辑轮顺延是预期）
        max1 = max((int(r["op_id"]) for r in rows1
                    if r["文件"] == f and r["通道"] != "filename"), default=0)
        if any(int(r["op_id"]) <= max1 for r in add_rows if r["文件"] == f):
            ok_cont = False
    c.check("增行行 op_id 续号（大于该文件首轮画块最大 op_id）", ok_cont,
            str([(r["文件"], r["op_id"]) for r in add_rows]))
    # 幸存未触及行：op_id 与状态保持（王建国=person 通道，两处 op 未触及）
    keep = [r for r in rows3 if r["原文"] == PERSON]
    c.check("未触及行 op_id 与状态保持（active+首轮序）",
            keep and all((r["文件"], r["原文"], r["op_id"]) in r1_by
                         and r["状态"] == LEDGER_ACTIVE for r in keep),
            str([(r["文件"], r["op_id"], r["状态"]) for r in keep]))
    c.check("编辑保存点重写日志", "遮蔽台账已按编辑后指令集更新" in log3,
            log3[-500:])
    c.check("台账 op_id 全表唯一连续（重编后）",
            all(sorted(int(r["op_id"]) for r in rows3 if r["文件"] == f)
                == list(range(1, sum(1 for r in rows3 if r["文件"] == f) + 1))
                for f in {r["文件"] for r in rows3}))


def test_round4_fallback_rebuild(c: _Case, root: Path, files, s1):
    """⑤ 回落轮：plan 未命中 → 台账重建 + 留痕。

    回落轮在对照表覆盖模式下全量重检（映射表驱动检测，自动规则停用），
    画块集合与首轮（auto 轮）不同源——不强求逐行等价，对账锚=当轮重建的
    plan 指令（独立工件）：画块总数=台账行数、逐通道全等。
    """
    print("· 回落轮：删 plan → 全量重检 → 台账重建留痕")
    for f in files:
        mp.plan_path_for(
            f, mp.output_root(f, MaskOptions(output_dir=str(root / "r1")))
        ).unlink()
    csv4 = str(root / "csv4.csv")
    _edit_mapping(s1["mapping"], csv4)
    _StubOcr.calls = 0
    s4, log4 = _run(files, root / "r1", mapping_csv=csv4)
    c.check("回落轮 2 文件全成功", s4["ok"] == 2, str(s4))
    c.check("回落留痕（未命中+回落）",
            "未命中首轮冻结 plan" in log4 and "回落" in log4, log4[:400])
    c.check("回落轮台账重建落盘", bool(s4.get("ledger")), str(s4.get("ledger")))
    c.check("重建留痕日志", "按本轮重检重建" in log4, log4[-400:])
    rows4 = _read_ledger(Path(s4["ledger"]))
    c.check("回落轮台账非空（当轮实际画块全留痕）", len(rows4) > 0, str(len(rows4)))
    for src in files:
        fname = Path(src).name
        led_all = Counter(r["通道"] for r in rows4 if r["文件"] == fname)
        led = Counter({k: v for k, v in led_all.items() if k != "filename"})
        plan_cnt = _plan_rule_counts(src, root / "r1")  # 回落轮重建的新 plan
        c.check(f"回落轮 {fname} 画块总数=台账行数（总对账，filename 除外）",
                sum(led.values()) == sum(plan_cnt.values()),
                f"台账={dict(led)} plan={dict(plan_cnt)}")
        c.check(f"回落轮 {fname} 逐通道对账全等",
                led == plan_cnt, f"台账={dict(led)} plan={dict(plan_cnt)}")


def main():
    print("== v2.0.1 C1 遮蔽台账回归 ==")
    c = _Case()
    tmp, root, files, _restore = _build_world()
    try:
        test_unit_ledger_helpers(c, tmp)
        test_file_report_dual_capture(c)
        s1, rows1 = test_round1_integrity(c, root, files)
        test_round2_replay_zero_new(c, root, files, s1, rows1)
        test_round3_edit_three_states(c, root, files, s1, rows1)
        test_round4_fallback_rebuild(c, root, files, s1)
    finally:
        _restore()  # 直跑出口恢复桩（进程即退，行为不变；防御性卫生）

    print(f"\n共 {c.total} 条用例，失败 {len(c.failed)} 条")
    return not c.failed


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
