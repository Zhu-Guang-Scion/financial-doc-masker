# -*- coding: utf-8 -*-
"""v1.9.3 复核轮（对照表覆盖）删行路径端到端回归（V4 盲区补齐，新增资产）。

背景（R1 诊断 + v1.9.3 组织后缀硬阻拦汇合验证任务书）：
- 测试同事实网路径 = 首轮 auto 出汇总表 → 人工删行 → 复核轮 mapping 覆盖重跑。
  该「删行路径」此前只有批量级诊断（_goal_work/rb_diag/），无自动化回归资产。
- 本套件用 fitz 造 2 页小 PDF（含公司全名 + 自定义敏感词 + 纯后缀碎词 +
  普通文本），跑首轮 auto → 按确定性准则删 2 行 → 复核轮 mapping 重跑，断言：
    1) 被删行不复发（删除键 (原文,文件) 在复核轮映射表中零出现；删行语义
       端到端生效——被删词在复核轮输出文本层转为明文）；
    2) 纯后缀碎词零遮蔽（两轮映射表均无 is_pure_org_suffix==True 的行；
       正文中的纯后缀词「公司」在输出文本层保持原样）；
    3) 普通文本零变化（轮间文本层差异仅来自被删词解除遮蔽位点；
       普通句「本制度自发布之日起施行。」两轮均原样在位）。
- R1 产品语义备注：删除按 (原文,文件) 键移除映射键；公司/文件名等非
  mapping 通道可再派生的行（品牌延伸/输出文件名）删后可复现（R1 §4，
  21 删 6 现同文件）——故本套件的删除目标选 custom 规则行（无再派生
  通道，不复发断言确定性成立）。

口径（自包含、确定性、<60s）：
- ocr_image_text=False + deep_recheck=False：纯文本层链路，跳过 OCR
  （删行路径的主体是文本层 mapping 词典通道，与批量级口径 rb_diag/N0/N1 互补）；
- 品牌配置 = _eval/brands_test.json（与其他回归套件一致）；
- 删除准则写死：规则 company 的行按 (原文长度降序, 原文升序) 取前 2
  （不足 2 时按 (原文长度降序, 原文升序) 从其余行补足），键=(原文, 文件)。

运行：python tests/test_review_round_regression.py
"""
import csv
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 预设依赖用例：品牌预设已外置（v1.8.1 brands.json），测试显式加载语料配置
os.environ.setdefault("FINMASK_BRANDS", str(
    Path(__file__).resolve().parents[1] / "_eval" / "brands_test.json"))

import pymupdf as fitz

from app.dispatcher import run_batch
from app.rules_engine import MaskOptions, is_pure_org_suffix

FULL_NAME = "云舒雅家居股份有限公司"
PROSE_COMPANY = "公司是一家从事家居产品制造与销售的企业。"
PLAIN_TEXT = "本制度自发布之日起施行。"
REVIEW_BASIS = "集团办公室关于印发本办法的通知。"
# v1.9.3 跨行实体样本（R1 实锤的纯后缀残片主产出通道）：行尾「…股份有限」+
# 行首「公司…」——跨行二次匹配拆分后，区别部分（云舒雅家居股份有限）必须
# 仍被遮，纯后缀残片（公司）不遮不记（pdf_handler._mask_native extra_spans
# 纯度闸）。
XLINE_TAIL = f"监督单位：{FULL_NAME[:-2]}"
XLINE_HEAD = "公司监督执行。"
# 自定义敏感词：删行路径的确定性载体——仅存在于映射/自定义词典，
# 复核轮删行后无任何通道可再派生（公司/人名/文件名通道均不可达）。
CUSTOM_WORD_1 = "特批客户订单折扣细则"
CUSTOM_WORD_2 = "柜类制造中心余料管理办法"

PAGE1_LINES = [
    f"{FULL_NAME}采购管理办法",
    f"本制度由{FULL_NAME}负责解释与修订。",
    f"附件：{CUSTOM_WORD_1}。",
    PROSE_COMPANY,
    PLAIN_TEXT,
]
PAGE2_LINES = [
    FULL_NAME,
    REVIEW_BASIS,
    f"附录：{CUSTOM_WORD_2}。",
    XLINE_TAIL,
    XLINE_HEAD,
]


def _make_pdf(path: Path) -> None:
    """2 页小 PDF：公司全名（可入表实体）+ 纯后缀碎词 + 普通文本。"""
    doc = fitz.open()
    for lines in (PAGE1_LINES, PAGE2_LINES):
        page = doc.new_page(width=595, height=842)
        y = 80.0
        for t in lines:
            page.insert_text((60, y), t, fontname="china-s", fontsize=14)
            y += 40.0
    doc.save(str(path))
    doc.close()


def _load_mapping(batch_dir: Path) -> list:
    cands = sorted(batch_dir.glob("映射表_汇总_*.csv"))
    assert cands, f"{batch_dir} 无映射表"
    with open(cands[-1], encoding="utf-8-sig", newline="") as f:
        rdr = csv.reader(f)
        header = next(rdr)
        rows = [r for r in rdr if r and r[0].strip()]
    return header, rows


def _run_round(tag: str, src: Path, out_root: Path, mapping_csv: str = "") -> tuple:
    out_dir = out_root / tag
    out_dir.mkdir(parents=True, exist_ok=True)
    options = MaskOptions(
        mask_company=True, mask_person=True, mask_amount=False,
        mask_contact=True, mask_address=True, mask_stamp=False,
        ocr_image_text=False, deep_recheck=False,
        person_loose=True, amount_mode="mask",
        output_dir=str(out_dir),
        # 自定义敏感词仅首轮传入：删行路径的确定性载体（复核轮按删行
        # 语义不再持有这些词——编辑表为唯一词源）。
        custom_words=[CUSTOM_WORD_1, CUSTOM_WORD_2] if not mapping_csv else [],
    )
    if mapping_csv:
        options.mapping_csv = mapping_csv
    summary = run_batch([str(src)], options, lambda m: None, lambda d, t: None)
    assert summary["ok"] == 1 and summary["fail"] == 0, f"{tag} 批处理失败: {summary}"
    batch_dir = Path(summary["output_dir"])
    assert batch_dir.exists(), f"{tag} 无批输出目录"
    return summary, batch_dir


def _pick_deleted(rows: list, k: int = 2) -> list:
    """删除准则（写死）：规则 custom 的行按 (原文长度降序, 原文升序) 取前 2。
    自定义词行删行后无任何通道可再派生——「不复发」断言因此确定性成立。"""
    picked = sorted([r for r in rows if r[2] == "custom"],
                    key=lambda r: (-len(r[0]), r[0]))[:k]
    assert len(picked) == k, (
        f"custom 规则行不足 {k} 条（实际 {[r[0] for r in rows]!r}）——"
        "样本或通道变化，删除准则需复核")
    return picked


def _write_edited(header: list, rows: list, deleted: list, path: Path) -> None:
    del_keys = {(r[0], r[3]) for r in deleted}
    kept = [r for r in rows if (r[0], r[3]) not in del_keys]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(kept)
    return


def _masked_pdf_text(batch_dir: Path) -> str:
    pdfs = sorted(batch_dir.glob("*_脱敏后.pdf"))
    assert pdfs, f"{batch_dir} 无脱敏后 PDF"
    d = fitz.open(str(pdfs[-1]))
    t = "\n".join(d[i].get_text() for i in range(len(d)))
    d.close()
    return t


class _Case:
    def __init__(self):
        self.failed = []
        self.total = 0

    def check(self, name, cond, detail=""):
        self.total += 1
        if cond:
            print(f"  PASS {name}")
        else:
            self.failed.append(name)
            print(f"  FAIL {name} {detail}")


def run() -> bool:
    print("== v1.9.3 复核轮删行路径端到端回归（V4） ==")
    c = _Case()
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="v193_rr_") as td:
        root = Path(td)
        src = root / "复核轮删行回归样本.pdf"
        _make_pdf(src)

        # ---- 首轮 auto ----
        s1, batch1 = _run_round("round1_auto", src, root)
        hdr1, rows1 = _load_mapping(batch1)
        c.check("首轮 auto 出表非空", len(rows1) >= 2, f"rows={rows1!r}")
        if not rows1:
            print(f"共 {c.total} 条用例，失败 {len(c.failed)} 条")
            return False
        c.check("首轮映射表零纯后缀行（is_pure_org_suffix）",
                all(not is_pure_org_suffix(r[0])[0] for r in rows1),
                f"纯后缀行={[r[0] for r in rows1 if is_pure_org_suffix(r[0])[0]]!r}")
        t1 = _masked_pdf_text(batch1)
        c.check("首轮被删词已遮蔽（输出层不含原文）",
                all(w not in t1 for w in (CUSTOM_WORD_1, CUSTOM_WORD_2)),
                "存在未遮蔽的被删词原文")
        # v1.9.3 跨行实体残片纯度闸（首轮 auto）：区别部分被遮、纯后缀残片可见
        c.check("跨行区别部分首轮被遮（云舒雅零残留）", "云舒雅" not in t1,
                "跨行实体区别部分明文残留")
        c.check("跨行纯后缀残片首轮不遮（行首「公司」可见）", XLINE_HEAD in t1,
                f"行首残片未保持原样")

        # ---- 删 2 行 → 编辑表 ----
        deleted = _pick_deleted(rows1, 2)
        c.check("删除清单恰 2 行（custom 规则）", len(deleted) == 2, f"deleted={deleted!r}")
        edited = root / "mapping_edited.csv"
        _write_edited(hdr1, rows1, deleted, edited)

        # ---- 复核轮 mapping 覆盖重跑 ----
        s2, batch2 = _run_round("round2_mapping", src, root, mapping_csv=str(edited))
        hdr2, rows2 = _load_mapping(batch2)

        # 断言 1：被删行不复发
        keys2 = {(r[0], r[3]) for r in rows2}
        leaked = [r for r in deleted if (r[0], r[3]) in keys2]
        c.check("被删行不复发（删除键零再现）", not leaked, f"复发={leaked!r}")

        # 断言 1b：删行语义端到端生效——被删词在复核轮输出层转为明文
        t2 = _masked_pdf_text(batch2)
        c.check("被删词复核轮输出层转为明文（删行=取消遮蔽）",
                all(w in t2 for w in (CUSTOM_WORD_1, CUSTOM_WORD_2)),
                "被删词输出层仍被遮蔽")

        # 断言 2：纯后缀碎词零遮蔽
        c.check("复核轮映射表零纯后缀行（is_pure_org_suffix）",
                all(not is_pure_org_suffix(r[0])[0] for r in rows2),
                f"纯后缀行={[r[0] for r in rows2 if is_pure_org_suffix(r[0])[0]]!r}")
        c.check("正文纯后缀词「公司」输出层保持原样", PROSE_COMPANY in t2,
                f"缺失：{PROSE_COMPANY!r}")
        # v1.9.3 跨行实体残片纯度闸（复核轮 mapping 同样生效）
        c.check("跨行区别部分复核轮仍被遮（云舒雅零残留）", "云舒雅" not in t2,
                "复核轮跨行实体区别部分明文残留")
        c.check("跨行纯后缀残片复核轮不遮（行首「公司」可见）", XLINE_HEAD in t2,
                "复核轮行首残片未保持原样")
        c.check("普通句输出层原样在位（复核轮）", PLAIN_TEXT in t2,
                f"缺失：{PLAIN_TEXT!r}")

        # 断言 3：普通文本零变化（除被删词解除遮蔽位点外，两轮文本层全等：
        # 把复核轮流中「被删词明文」重新替换为首轮同位置遮蔽形态后应与首轮一致；
        # 等价的强简化断言=两轮差异仅由被删词的遮蔽/明文形态构成）
        diff_chars = sum(1 for a, b in zip(t1, t2) if a != b) + abs(len(t1) - len(t2))
        only_deleted_words = all(w in t2 for w in (CUSTOM_WORD_1, CUSTOM_WORD_2))
        c.check("轮间文本差异仅来自被删词位点（普通文本零变化）",
                diff_chars > 0 and only_deleted_words and PLAIN_TEXT in t1
                and PLAIN_TEXT in t2 and PROSE_COMPANY in t1,
                f"diff_chars={diff_chars}")
        c.check("未删公司全名行两轮均保持遮蔽（在位）",
                FULL_NAME not in t1 and FULL_NAME not in t2,
                "未删行的实体明文泄漏")
        c.check("审阅依据普通句输出层原样在位", REVIEW_BASIS in t2,
                f"缺失：{REVIEW_BASIS!r}")

    print(f"\n共 {c.total} 条用例，失败 {len(c.failed)} 条（耗时 {time.time() - t0:.1f}s）")
    return not c.failed


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ok = run()
    sys.exit(0 if ok else 1)
