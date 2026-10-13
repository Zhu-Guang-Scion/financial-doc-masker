# -*- coding: utf-8 -*-
"""v2.1.7 批甲（零行为变更）UI 四小项的纯逻辑测试。

只测可脱离 GUI 的半区（不启 tk 窗口、不实例化 MainWindow）：
- 项1 zero_candidate_body：meta.ocr_pages_skipped>0 → 原版文案（扫描件
  原因 + OCR 重跑建议）；==0/缺键/None/非数值 → NO_OCR 版；两模板都含
  {m} 占位（UI 消费点统一 .format(m=文件数)）；
- 项2 collect_supported_files：入口②「选择文件夹」本地收集的全部决策
  逻辑——后缀过滤（大小写折叠）+ 排序 + 仅文件不计目录 + 空结果/目录
  不存在容错（UI 侧已不再走 _add_paths 灌入主处理列表）；
- 项3 to_reuse_texts：{str(源路径): 文本} → {Path(源路径): 文本}
  （scan_sources.reuse_texts 键契约）；空/None → None（=不传，行为与
  现状一致）；并配一条真实扫描器冒烟——Path 键确实命中复用通道
  （files_n/total_chars 可证批内文本被消费而非重新提取）；
- 项4（清除缓存联动 决策留痕_*.csv）：删除路径内嵌在 MainWindow 实例
  方法里，依赖 tk messagebox 与实例控件状态，无法脱离 GUI 纯测——
  本文件跳过（此注即留痕），改动按「镜像遮蔽台账既有删除模式」人工核验。

风格对齐 tests/test_candidate_flow.py（pytest + 中文注释）。运行：
python -m pytest tests/test_ui_batch_a.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import candidate_flow as cf


# ---------------------------------------------------------------------------
# 项1：零候选文案条件化
# ---------------------------------------------------------------------------

def test_zero_candidate_body_skipped_positive_uses_original():
    """有跳页（ocr_pages_skipped>0）：扫描件话术成立 → 原版 BODY。"""
    assert cf.zero_candidate_body({"ocr_pages_skipped": 3}) \
        is cf.ZERO_CANDIDATE_BODY
    # 数值字符串同判为正（meta 来自扫描器计数，防御式 int() 折算）
    assert cf.zero_candidate_body({"ocr_pages_skipped": "7"}) \
        is cf.ZERO_CANDIDATE_BODY


def test_zero_candidate_body_zero_missing_none_tolerant():
    """==0 / 缺键 / None meta / 值为 None/空串/非数值 → NO_OCR 版（容错）。"""
    for meta in ({"ocr_pages_skipped": 0}, {}, None,
                 {"ocr_pages_skipped": None}, {"ocr_pages_skipped": ""},
                 {"ocr_pages_skipped": "若干"}, {"ocr_pages_skipped": []}):
        assert cf.zero_candidate_body(meta) is cf.ZERO_CANDIDATE_BODY_NO_OCR, \
            meta


def test_zero_candidate_body_templates_share_placeholder_and_copy_contract():
    """两模板同含 {m} 占位（可 .format 展开）；文案契约：
    原版=扫描件原因+OCR 重跑建议+手动填入；NO_OCR 版=撤前两者、留指引。"""
    for tpl in (cf.ZERO_CANDIDATE_BODY, cf.ZERO_CANDIDATE_BODY_NO_OCR):
        assert "{m}" in tpl
        assert "自定义敏感词" in tpl
        assert "12" in tpl.format(m=12)
    # 原版专属：扫描件为主原因 + 「重新跑批再试」OCR 建议
    assert "扫描件为主" in cf.ZERO_CANDIDATE_BODY
    assert "重新跑批再试" in cf.ZERO_CANDIDATE_BODY
    # NO_OCR 版：两条都撤（OCR 只以「重跑不会有新发现」的否定式出现）
    no_ocr = cf.ZERO_CANDIDATE_BODY_NO_OCR
    assert "扫描件为主" not in no_ocr
    assert "重新跑批再试" not in no_ocr


# ---------------------------------------------------------------------------
# 项2：入口②选择文件夹本地收集（后缀过滤 + 排序 + 仅文件）
# ---------------------------------------------------------------------------

def test_collect_supported_files_filters_sorts_files_only(tmp_path):
    """支持后缀全收（含大写折叠）、不支持后缀剔除、同名目录不算文件、
    结果按路径排序且为 Path 对象。"""
    (tmp_path / "乙.docx").write_bytes(b"x")
    (tmp_path / "甲.PDF").write_bytes(b"x")        # 大写后缀 → .lower() 折叠
    (tmp_path / "丙.txt").write_bytes(b"x")        # 不支持格式 → 剔除
    sub = tmp_path / "子目录"
    sub.mkdir()
    (sub / "丁.xlsx").write_bytes(b"x")            # 递归子目录收录
    (tmp_path / "戊.pdf").mkdir()                  # 目录占支持名 → 仅文件口径剔除
    out = cf.collect_supported_files(tmp_path)
    assert {p.name for p in out} == {"乙.docx", "甲.PDF", "丁.xlsx"}
    assert out == sorted(out)
    assert all(isinstance(p, Path) for p in out)


def test_collect_supported_files_empty_and_missing(tmp_path):
    """空文件夹 / 不存在的路径 → 空列表（上游「未发现支持格式」提示流兜底）。"""
    empty = tmp_path / "空文件夹"
    empty.mkdir()
    assert cf.collect_supported_files(empty) == []
    assert cf.collect_supported_files(tmp_path / "不存在") == []


# ---------------------------------------------------------------------------
# 项3：批内文本复用键转换（to_reuse_texts）
# ---------------------------------------------------------------------------

def test_to_reuse_texts_empty_returns_none():
    """空 dict / None → None（= 不传 reuse_texts，行为与现状一致——
    生产端 summary 缺 "batch_texts" 键时零影响的契约锚）。"""
    assert cf.to_reuse_texts({}) is None
    assert cf.to_reuse_texts(None) is None


def test_to_reuse_texts_converts_str_keys_to_path():
    """{str(路径): 文本} → {Path(路径): 文本}（scan_sources 键契约）。"""
    k1, k2 = str(Path("批甲") / "a.docx"), str(Path("批甲") / "b.pdf")
    out = cf.to_reuse_texts({k1: "文本一", k2: "文本二"})
    assert out == {Path(k1): "文本一", Path(k2): "文本二"}
    assert all(isinstance(k, Path) for k in out)


def test_to_reuse_texts_keys_hit_scanner_reuse_channel():
    """端到端冒烟：to_reuse_texts 产物喂真实 scan_sources——命中键直接
    复用批内文本（files_n/total_chars 可证文本被消费而非文件提取；文件
    实际不存在也不报错）。顺带锚定项1 依赖的 meta 键 ocr_pages_skipped
    真实存在且复用通道不产生跳页计数。"""
    from app.candidate_scanner import scan_sources
    p = Path("复用冒烟甲.docx")
    text = "江城图书馆内部治理。" * 3
    sess = scan_sources([p], reuse_texts=cf.to_reuse_texts({str(p): text}))
    try:
        assert sess.meta["files_n"] == 1
        assert sess.meta["total_chars"] == len(text)
        assert sess.meta["ocr_pages_skipped"] == 0
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# 项4：清除缓存联动 决策留痕_*.csv —— 无法脱离 GUI 纯测，跳过（见模块
# docstring 留痕注记；改动为镜像遮蔽台账的既有删除模式）。
# ---------------------------------------------------------------------------
