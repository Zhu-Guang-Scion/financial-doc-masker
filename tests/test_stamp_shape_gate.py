# -*- coding: utf-8 -*-
"""v2.1.1 S2 红章形状闸（detect_red_regions）先红后绿套件。

== 背景 ==
测试同事实证（_goal_work/tester_feedback_diag_20260913.md §问题 2）：无公章页面
大量「红色印章」提示——红框表格 / 红底单元格 / 橙红圆形 logo 全部落入
detect_red_regions 检测面（HSV 红域 + 面积闸 + R20 细长行闸之后再无形状校验）。

== 形状闸（契约 _goal_work/v211_contract.md §二）==
在既有面积闸 / R20 之后新增：
  a) 填充率带通：分量红像素数 / bbox 面积 ∈ [STAMP_FILL_MIN, STAMP_FILL_MAX]；
  b) 圆度或星形：4πA/P²（A=外轮廓围合面积、P=外轮廓周长）≥ STAMP_CIRC_MIN，
     或五角星顶点特征（_stamp_star_like：approx 顶点 8~14 且凸实度 ≤0.8）；
     实心（fill>STAMP_SOLID_ROUND_FILL）近圆且短边 >STAMP_SOLID_ROUND_MIN_SIDE_PT
     判为圆盘 logo 拒判（圆度分支的标定操作化，见下）；
  c) 尺寸域上限：bbox 长边 ≤ 页面短边 × STAMP_LONG_EDGE_MAX_RATIO。

== 阈值标定（双面：对抗三类 → 零检 vs 真实管线真章面 → 零漏）==
标定工具：_goal_work/g211s2_probe.py（生产同口径逐分量统计——面积闸沿用
生产 contourArea 口径而非红像素数，保证准入集合一致；填充率按契约 a 项用
红像素数）。标定面：对抗三类 + 合成圆环印章（本文件同款画法）+ v210_g2reg
批真实管线 stamp op 全部 80 矩形（9 文件；top3=67），逐连通域统计：

  对抗三类（200dpi 渲染，200/72 zoom）：
    红框表格（整页框 542×442pt + 栏线）: fill=0.0282  circ=0.7774  长边比=0.9111
    红底填充单元格（160×30pt）:          fill=1.0000  circ=0.4140  长边比=0.2686
    橙红圆形 logo（r=30pt 实心圆盘）:     fill=0.7851  circ=0.8967  长边比=0.1010
  合成圆环印章（外圆 r=60 红 + 内圆 r=52 白 + 印文）:
                                         fill=0.1973  circ=0.9004  长边比=0.2015
  真实管线 stamp 面（86 个 R20 后种子分量，top3 口径）：
    fill   ∈ [0.0304, 0.9388]（下端=截图内细线红框，上端=近实心红条）
    circ   ∈ [0.019, 0.8967]
    长边比 ∈ [?, 0.5187]（整页宽红框带）
    稠密近圆域（fill>0.75 ∧ circ≥0.55）真值成员仅 1 个：14.4pt 实心红点。

  契约预设值 [0.05, 0.80] / 0.55 / 0.40 与真值面冲突的实证（零漏硬约束
  优先，契约零节 3「宁留误报不可漏真章」）：
    - fill 下限 0.05 > 真值最小 0.0304（截图细线红框类真值成员漏）→ 放宽 0.020；
    - fill 上限 0.80 < 真值最大 0.9388（近实心红条真值成员漏）→ 放宽 0.95
      （仍排除红底实心单元格 1.0）；
    - 圆度 ≥0.55 作为硬保留条件不可行：真值面 circ 低至 0.019（红框/红字
      批注形态被既有管线判为 stamp 并入真值面），硬卡圆度将漏 20+ 矩形；
      b) 按契约「为辅」定位操作化为「实心圆盘拒判」（fill>0.75 ∧ circ≥0.55
      ∧ 短边>20pt，圆盘 logo 对抗类的唯一可分域；真值成员 14.4pt 红点保留），
      五角星特征保留为豁免分支；
    - 长边比 0.40 < 真值最大 0.5187 → 放宽 0.55（仍排除整页宽红色装饰带
      /本套件 0.9111 的红框表格）。

  闸效果（2026-09-15 实测，_goal_work/g211s2_verify_out.txt 全表）：
    对抗三类 修复前 1/1/1 → 修复后 0/0/0；
    合成圆环印章 保留；
    真实管线 80 矩形中心覆盖：修复前 raw 72/80、fin 80/80 → 闸后
    raw 72/80、fin 80/80（位元级一致——形状闸对真值面零改变；
    raw 差的 8 矩形系既有行合并/文本行扩展效应，与修复无关）。

== 用例 ==
  test_adversarial_red_frames_cells_logos_zero  对抗三类先红（修复前各 ≥1）
  → 修复后零检（转绿断言）。
  test_ring_stamp_still_detected                真值①：合成圆环印章零漏。
  test_real_stamp_zero_miss_top3                真值②：v210 真实管线 stamp op
  数最多的前 3 源（37/18/12 矩形），逐矩形中心覆盖判定（零漏）。
"""
import json
import sys
from pathlib import Path

import pytest

import fitz

# 本地真实语料根（env FINMASK_CORPUS_DIR 可覆盖；缺失时真值用例 skip）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fixtures_common import CORPUS_DIR  # noqa: E402

import app.handlers.pdf_handler as pdf_handler
from app.handlers.pdf_handler import (
    OCR_DPI,
    _extend_rects_to_text_lines,
    _merge_same_line_rects,
    detect_red_regions,
)

pytest.importorskip("fitz")

# 主树真值面（仓库内 _goal_work 评测产物缓存，不入库；缺失时真值用例 skip）。
PLAN_DIR = (Path(__file__).resolve().parents[1] / "_goal_work"
            / "v210_g2reg_输出" / "cache" / "plans")
CORPUS = CORPUS_DIR

# v210_g2reg 批 stamp op 数最多的前 3 个 plan（37/18/12 个 stamp 矩形）。
TOP3_PLANS = [
    "607f7f07e37ef9043e2289639fdf705416388f1e6dafcd549c4ac63a622ee5b7.json",
    "343eb1340638402d0b1aef8384cb5a46c34a5daded16f2e4f8bf3e79b7181307.json",
    "a72a8e6b70a46f2e9fad37bf8c0011115eccfb3e6d158d6a5f804b25f083b222.json",
]


def _render_png(page, dpi=OCR_DPI):
    zoom = dpi / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return pix.tobytes("png")


def _detect(png, dpi=OCR_DPI):
    rects = detect_red_regions(png, dpi)
    if rects is None:
        pytest.skip("OpenCV 不可用（detect_red_regions 返回 None）")
    return rects


def _adversarial_pages():
    """对抗三类合成页（与标定探针同款画法，fitz 合成）。

    画法依据任务书：draw_rect(color=(1,0,0),width=2) / draw_rect(fill=(1,0.3,0.3))
    / draw_circle(fill=(1,0.35,0.1))；红框表格页按测试同事实证还原为整页表格框。
    """
    doc = fitz.open()
    # 页 0：红框表格——整页表格框（540×440pt）+ 表头行线 + 两条栏线
    pg = doc.new_page(width=595, height=842)
    pg.draw_rect(fitz.Rect(30, 200, 570, 640), color=(1, 0, 0), width=2)
    pg.draw_line(fitz.Point(30, 250), fitz.Point(570, 250), color=(1, 0, 0), width=1.5)
    pg.draw_line(fitz.Point(210, 200), fitz.Point(210, 640), color=(1, 0, 0), width=1.5)
    pg.draw_line(fitz.Point(390, 200), fitz.Point(390, 640), color=(1, 0, 0), width=1.5)
    # 页 1：红底填充单元格（红表头形态）
    pg = doc.new_page(width=595, height=842)
    pg.draw_rect(fitz.Rect(100, 100, 260, 130), color=None, fill=(1, 0.3, 0.3))
    # 页 2：橙红圆形 logo（实心圆盘）
    pg = doc.new_page(width=595, height=842)
    pg.draw_circle(fitz.Point(200, 200), 30, color=None, fill=(1, 0.35, 0.1))
    return doc


def _ring_stamp_page():
    """真值①：合成圆环印章——红环带 + 黑印文（draw_circle+insert_text 组合）。"""
    doc = fitz.open()
    pg = doc.new_page(width=595, height=842)
    c = fitz.Point(300, 300)
    pg.draw_circle(c, 60, color=None, fill=(0.8, 0.1, 0.1))   # 外圆填充红
    pg.draw_circle(c, 52, color=None, fill=(1, 1, 1))         # 内圆填充白 → 环带
    # 环带上散布黑印文（「测试印」样弧排）+ 下方短句
    import cmath
    for k, ch in enumerate("测试印"):
        ang = -0.9 + k * 0.6
        pos = complex(0, -46) * cmath.exp(1j * ang)
        pg.insert_text(fitz.Point(c.x + pos.real - 5, c.y + pos.imag + 5), ch,
                       fontsize=12, color=(0, 0, 0))
    pg.insert_text(fitz.Point(c.x - 30, c.y + 30), "财务专用", fontsize=10,
                   color=(0, 0, 0))
    return doc, (300.0, 300.0)


def test_shape_gate_constants_match_calibration():
    """闸常量与标定结论一致（防静默漂移；标定依据见文件头注释）。"""
    assert pdf_handler.STAMP_FILL_MIN == 0.020
    assert pdf_handler.STAMP_FILL_MAX == 0.95
    assert pdf_handler.STAMP_CIRC_MIN == 0.55
    assert pdf_handler.STAMP_SOLID_ROUND_FILL == 0.75
    assert pdf_handler.STAMP_SOLID_ROUND_MIN_SIDE_PT == 20.0
    assert pdf_handler.STAMP_LONG_EDGE_MAX_RATIO == 0.55


def test_adversarial_red_frames_cells_logos_zero():
    """对抗三类：修复前各检出 ≥1（先红演示，v2.1.1 前 pytest 实录 1/1/1）；
    修复后（形状闸）零检（转绿）。"""
    doc = _adversarial_pages()
    names = ["红框表格", "红底填充单元格", "橙红圆形logo"]
    for pno, name in enumerate(names):
        rects = _detect(_render_png(doc[pno]))
        assert rects == [], (
            f"对抗类「{name}」修复后仍检出 {len(rects)} 处（期望 0）：{rects}")


def test_ring_stamp_still_detected():
    """真值①：合成圆环印章修复后必须仍检出，且印章中心被检出矩形覆盖。"""
    doc, center = _ring_stamp_page()
    rects = _detect(_render_png(doc[0]))
    assert len(rects) >= 1, "合成圆环印章未检出（真章零漏破坏）"
    cx, cy = center
    assert any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in rects), (
        f"圆环印章中心 ({cx},{cy}) 未落入任何检出矩形：{rects}")


def _plan_stamp_rects(plan_path):
    d = json.loads(plan_path.read_text(encoding="utf-8"))
    by_pno = {}
    for pg in d.get("pages", []):
        for o in pg.get("ops", []):
            if o.get("k") == "stamp":
                by_pno.setdefault(pg["pno"], []).append(o["rect"])
    return d["header"]["src_name"], by_pno


def test_real_stamp_zero_miss_top3():
    """真值②：真实管线 stamp op 数最多的前 3 源，逐矩形零漏。

    真值=plan 内全部 stamp op 矩形（真实管线产出，含行合并/文本行扩展后的
    成品矩形）；判定=修复后检出经同一后处理（_merge_same_line_rects +
    _extend_rects_to_text_lines，与生产 _mask_stamps_from_png 同序）后，每个
    真值矩形中心落入任一检出矩形（契约零漏口径）。标定实测：修复前后均
    fin 级 67/67（raw 级差异系既有扩展效应，与形状闸无关）。
    """
    if not PLAN_DIR.is_dir() or not CORPUS.is_dir():
        pytest.skip(f"主树真值面不可得：{PLAN_DIR} / {CORPUS}")
    misses = []
    total = 0
    checked_plans = 0
    for pf in TOP3_PLANS:
        src_name, by_pno = _plan_stamp_rects(PLAN_DIR / pf)
        srcs = [p for p in CORPUS.rglob("*.pdf") if p.name == src_name]
        if not srcs:
            pytest.skip(f"语料源不可得：{src_name}")
        doc = fitz.open(srcs[0])
        try:
            for pno, rects_truth in sorted(by_pno.items()):
                page = doc[pno]
                rects = _detect(_render_png(page))
                rects = _extend_rects_to_text_lines(
                    page, _merge_same_line_rects(rects))
                for tr in rects_truth:
                    total += 1
                    cx, cy = (tr[0] + tr[2]) / 2, (tr[1] + tr[3]) / 2
                    if not any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3]
                               for r in rects):
                        misses.append((src_name[:30], pno,
                                       [round(v, 1) for v in tr],
                                       (round(cx), round(cy))))
            checked_plans += 1
        finally:
            doc.close()
    assert checked_plans == len(TOP3_PLANS), "plan 覆盖不全"
    assert misses == [], (
        f"真章零漏破坏：{len(misses)}/{total} 个真值矩形中心未覆盖：{misses}")


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
