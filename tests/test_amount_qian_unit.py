# -*- coding: utf-8 -*-
"""v2.0.2 A线「X千元」单位档回归（借支管理办法 site2 类盲区修复）。

根因（v2.0.1 V-A 验收 B 面实锤，_goal_work/v201_va_verdict.md §2①）：
AMOUNT_RE 单位类为 `[万亿]?元`（rules_engine.py:180），「千」不在类内，
依赖单位类的主链路金额通道对「X千元」形态零遮蔽（site2「2千＊」存活）。
暴露面实测：语料 6 处「X千元」token 全部在借支管理办法
（p5 3 处 + p7 3 处，审批权限表「2千元至1万元」类制度参数区间；
PDF 制版空格形态「2 千元」同型，AMOUNT_RE 的 \\s* 已覆盖）。

三用例（先红后绿纪律：①② 改词表前必须跑红）：
- ① 主链路：find_spans（mask_amount=True）对「2千元 / 5千元 / 2 千元」
  命中 amount span——改前零命中；同时锁 金额关（引擎级）零命中 与
  既有「万元」形态不回退；
- ② 真实文件：借支管理办法单文件跑批（金额开 + deep 开，口径参照
  _goal_work/t1_repro_jiezhi.py：进程内钉 resolve_ocr_mode='auto'、
  brands=_eval/brands_test.json）断言全文档零存活「数字+千」形态、
  台账含千档 amount 行。红跑输出落 _goal_work/v202_a_输出/red/
  （可用 FINMASK_QIAN_BATCH_OUT 重定向，绿跑验证用 green/），
  两态各留 存活清单 证据文件；
- ③ 金额关：同配置 mask_amount=False 断言零遮蔽（台账零 amount 行、
  六处「X千元」原样在位——金额闸两态，契约已就位）。

运行：python tests/test_amount_qian_unit.py
（语料缺失（本地语料未配置）时 ②③ 显式 skip）
"""
import os
import re
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 品牌夹具统一选取（本地配置优先，缺失回退开源虚构词夹具 tests/fixtures/）
from _fixtures_common import CORPUS_DIR, resolve_brands_fixture

os.environ.setdefault("FINMASK_BRANDS", resolve_brands_fixture())

# 进程内钉 auto（同 _goal_work/t1_repro_jiezhi.py 口径；须先于 dispatcher/
# pdf_handler 导入）
import app.app_config as _ac
_resolve_ocr_mode_orig = _ac.resolve_ocr_mode  # 原函数留底（pytest 口径恢复用）
_ac.resolve_ocr_mode = lambda: "auto"

from app.rules_engine import MaskOptions, RuleEngine

try:  # v2.0.2 千档解析包装（改前不存在 → 红判据之一）
    from app.rules_engine import _parse_amount_qian
except ImportError:  # 改前兜底：恒返回 None（用例①显式 FAIL=红）
    def _parse_amount_qian(s):
        return None

# pytest 卫生（v2.0.2 C 线搭车：pytest 全量口径防泄漏）。import 期钉 auto 是
# 直跑口径手法——一文件一进程、跑完即退，无泄漏面；而 pytest 全量跑所有套件
# 共用一个解释器、collection 期即导入本模块，该钉会泄漏给同会话后续套件的
# 模式解析用例（实测污染 test_ocr_engine_config / test_ocr_mode /
# test_startup_ocr_log 共 5 条）。此 fixture 在本模块用例跑完后恢复原函数：
# 本模块用例期间钉照常生效（语义零变化），模块外零残留；python 直跑不加载
# fixture，直跑口径零变化。
try:
    import pytest
except ImportError:  # 直跑环境无 pytest
    pytest = None

if pytest is not None:
    @pytest.fixture(autouse=True, scope="module")
    def _unpin_resolve_ocr_mode_after_module():
        yield
        _ac.resolve_ocr_mode = _resolve_ocr_mode_orig

CORPUS = (CORPUS_DIR / "云舒雅家居股份有限公司借支管理办法"
          / "云舒雅家居股份有限公司借支管理办法.pdf")
# 红跑输出（任务书口径）；绿跑验证用 FINMASK_QIAN_BATCH_OUT=...green 覆盖
BATCH_OUT = os.environ.get(
    "FINMASK_QIAN_BATCH_OUT", str(Path(__file__).resolve().parents[1]
                                   / "_goal_work" / "v202_a_输出" / "red"))
# 存活「数字+千」形态（全半角数字 + 制版空格 + 千）
QIAN_SURVIVE_RE = re.compile(r"[0-9０-９][ \u3000]*千")
# 主链路样例（覆盖无空格/制版空格两态 + 既有万元形态回归锚）
SAMPLE_TEXT = ("单笔审批权限：2千元至1万元（含1万元）；"
               "副总裁/财务总监 2 千元以下（含2 千元）；"
               "备用金上限 5千元。")


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
            print(f"  FAIL {name} {str(detail)[:400]}")

    def done(self):
        print(f"共 {self.total} 条，失败 {len(self.failed)} 条（{self.name}）")
        if self.failed:
            raise AssertionError(f"{self.name}: {self.failed}")


def _batch_options(mask_amount, out_dir):
    """单文件跑批 options（口径=t1_repro_jiezhi/VA B 面：金额开+deep 开）。"""
    return MaskOptions(mask_company=True, mask_person=True,
                       mask_amount=mask_amount, mask_contact=True,
                       mask_address=True, mask_stamp=True,
                       ocr_image_text=True, person_loose=True,
                       amount_mode="mask", output_dir=str(out_dir))


def _run_batch(mask_amount, out_dir):
    """单文件跑批（借支管理办法），返回 (summary, ledger行列表, 输出PDF路径)。"""
    import io
    import json
    import glob
    import pymupdf as fitz
    from app.dispatcher import run_batch
    out_dir = Path(out_dir)
    if out_dir.exists():
        for stale in out_dir.rglob("*"):
            if stale.is_file():
                stale.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    logs = []
    summary = run_batch([str(CORPUS)], _batch_options(mask_amount, out_dir),
                        lambda m: logs.append(m), lambda d, t: None)
    (out_dir / "_batch_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    # run_batch 在 output_dir 下再建 脱敏输出_<ts> 子目录（三件套在其中）
    ledgers = sorted(glob.glob(str(out_dir / "**" / "遮蔽台账_*.csv"),
                               recursive=True))
    rows = []
    if ledgers:
        with open(ledgers[-1], encoding="utf-8-sig", newline="") as f:
            rows = [r for r in csv.reader(f) if r and r[0] != "文件"]
    pdfs = sorted(out_dir.rglob("*_脱敏后.pdf"))
    return summary, rows, (pdfs[0] if pdfs else None)


def _doc_text(pdf_path):
    import pymupdf as fitz
    with fitz.open(pdf_path) as doc:
        return {pg.number + 1: pg.get_text() for pg in doc}


def _scan_qian(pages_text):
    """全文档存活「数字+千」形态清单（页码+上下文示意）。"""
    hits = []
    for pno in sorted(pages_text):
        flat = pages_text[pno].replace("\n", "|")
        for m in QIAN_SURVIVE_RE.finditer(flat):
            a, b = max(0, m.start() - 10), min(len(flat), m.end() + 10)
            hits.append(f"p{pno}: …{flat[a:b]}…")
    return hits


# ---------------------------------------------------------------------------
# ① 主链路 find_spans（红先：改词表前「千」档零命中）
# ---------------------------------------------------------------------------

def test_main_link_qian_amount():
    print("· ① 主链路 find_spans「X千元」命中 + 金额关引擎级零命中")
    c = _Case("主链路")

    eng_on = RuleEngine(MaskOptions(mask_amount=True, amount_mode="mask"))
    spans = [sp for sp in eng_on.find_spans(SAMPLE_TEXT) if sp.rule == "amount"]
    texts = {sp.text for sp in spans}
    qian_hits = {t.replace(" ", "") for t in texts if "千" in t}

    c.check("金额开：「2千元」命中", "2千元" in qian_hits, sorted(texts))
    c.check("金额开：「5千元」命中", "5千元" in qian_hits, sorted(texts))
    c.check("金额开：制版空格「2 千元」命中（归一后）", "2千元" in qian_hits,
            sorted(texts))
    c.check("金额开：千档 span 数=4（2千元×1+2 千元×2+5千元×1）",
            len([sp for sp in spans if "千" in sp.text]) == 4,
            [sp.text for sp in spans])
    c.check("既有形态不回退：「1万元」仍命中",
            any(t.replace(" ", "") == "1万元" for t in texts), sorted(texts))
    c.check("span 文本即原文（无越界吞并）",
            all(t in SAMPLE_TEXT for t in texts), sorted(texts))
    c.check("千档有效性闸放行：_amount_ok('2千元')",
            eng_on._amount_ok("2千元"), None)
    c.check("千档解析：_parse_amount_qian('2千元')==2000",
            _parse_amount_qian("2千元") == 2000, _parse_amount_qian("2千元"))
    c.check("千档解析：_parse_amount_qian('2.5 千元')==2500",
            _parse_amount_qian("2.5 千元") == 2500, _parse_amount_qian("2.5 千元"))
    c.check("千档解析：透传零行为差（'3万元'仍走 parse_amount）",
            _parse_amount_qian("3万元") == 30000, _parse_amount_qian("3万元"))

    eng_off = RuleEngine(MaskOptions(mask_amount=False, amount_mode="mask"))
    off = [sp for sp in eng_off.find_spans(SAMPLE_TEXT) if sp.rule == "amount"]
    c.check("金额关（引擎级）：零 amount span", off == [], [s.text for s in off])

    c.done()


# ---------------------------------------------------------------------------
# ② 真实文件跑批（金额开+deep 开）：六处「X千元」形态照遮
# ---------------------------------------------------------------------------

def test_jiezhi_batch_qian_masked():
    if not CORPUS.exists():
        print(f"· ② 真实文件跑批：语料缺失，skip（{CORPUS}）")
        return
    print(f"· ② 借支管理办法单文件跑批（金额开+deep 开）→ {BATCH_OUT}")
    c = _Case("跑批金额开")

    summary, rows, out_pdf = _run_batch(True, BATCH_OUT)
    c.check("跑批成功 ok=1 fail=0",
            summary.get("ok") == 1 and summary.get("fail") == 0,
            {k: summary.get(k) for k in ("ok", "fail", "masked")})
    c.check("输出 PDF 存在", bool(out_pdf), BATCH_OUT)
    if not out_pdf:
        c.done()
        return

    pages = _doc_text(out_pdf)
    survivors = _scan_qian(pages)
    # 存活清单证据落盘（红绿两态各留一份，供报告引用）
    ev = Path(BATCH_OUT) / "_qian存活清单.txt"
    ev.write_text("\n".join(survivors) if survivors else "（零存活）",
                  encoding="utf-8")
    c.check("全文档零存活「数字+千」形态（六处全遮）", survivors == [],
            f"存活 {len(survivors)} 处: {survivors[:8]}")
    c.check("全文档文本层零残留「千」字（语料 6 处千均在金额形态内）",
            sum(t.count("千") for t in pages.values()) == 0,
            {p: t.count("千") for p, t in pages.items() if "千" in t})

    amt_rows = [r for r in rows if len(r) > 4 and r[2] == "amount"]
    qian_rows = [r for r in amt_rows if "千" in r[3]]
    c.check("台账含千档 amount 行（≥4：2千元×3+5千元形态按实际记录）",
            len(qian_rows) >= 4, [r[:5] for r in qian_rows])
    c.check("既有万元形态不回退：台账仍含「万元」amount 行",
            any("万" in r[3] for r in amt_rows), f"amount 行数={len(amt_rows)}")

    c.done()


# ---------------------------------------------------------------------------
# ③ 金额关跑批（同配置 mask_amount=False）：零遮蔽
# ---------------------------------------------------------------------------

def test_jiezhi_batch_amount_off():
    if not CORPUS.exists():
        print(f"· ③ 金额关跑批：语料缺失，skip（{CORPUS}）")
        return
    out_off = str(BATCH_OUT).rstrip("/\\") + "_金额关"
    print(f"· ③ 借支管理办法单文件跑批（金额关，同配置）→ {out_off}")
    c = _Case("跑批金额关")

    summary, rows, out_pdf = _run_batch(False, out_off)
    c.check("跑批成功 ok=1 fail=0",
            summary.get("ok") == 1 and summary.get("fail") == 0,
            {k: summary.get(k) for k in ("ok", "fail", "masked")})
    c.check("输出 PDF 存在", bool(out_pdf), out_off)
    if not out_pdf:
        c.done()
        return

    pages = _doc_text(out_pdf)
    hits = _scan_qian(pages)
    c.check("金额关：六处「X千元」形态原样在位（6 处全存活）",
            len(hits) == 6, f"存活 {len(hits)} 处: {hits}")

    amt_rows = [r for r in rows if len(r) > 2 and r[2] == "amount"]
    c.check("金额关：台账零 amount 行（零 op 契约）", amt_rows == [],
            amt_rows[:5])
    c.check("金额关：masked 恰=实体基线 8（company 6+person 2）",
            summary.get("masked") == 8, summary.get("masked"))

    c.done()


# ---------------------------------------------------------------------------
# 直跑入口（与 pytest 收集共用用例体）
# ---------------------------------------------------------------------------

def main():
    print("== v2.0.2 A线「X千元」单位档回归（先红后绿）==")
    test_main_link_qian_amount()
    test_jiezhi_batch_qian_masked()
    test_jiezhi_batch_amount_off()
    print("全部通过")


if __name__ == "__main__":
    main()
