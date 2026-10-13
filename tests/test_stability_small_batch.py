# -*- coding: utf-8 -*-
"""稳定体验小批 ⑤⑦ 回归锁（2026-10-11）。

⑤ GPU worker 页级错误分级（handlers 评审 P0-1）：协议 ok:false（单页
识别失败、引擎健康）与传输级错误（EOF/超时=进程失效）此前共用
MissingDependencyError——resilient 守卫对前者也杀 worker 重建+同毒图
重试必败+降级 CPU，整批抖动。修复=OcrPageError 分家，页级直抛交
pdf_handler 页级兜底；进程级失效保持自愈路径。
⑦ 替换文本巨字钳制（视觉审查 S3）：巨矩形（截图整图遮蔽）h*0.72 分支
此前把星号放大到半页；钳制绝对上限 24pt，正常文本行（h≈12pt）不触。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

import app.ocr_engine as oe
from app.utils import MissingDependencyError, OcrPageError


def test_fontsize_clamped_on_huge_rect():
    """⑦：巨 rect（400pt 高截图区）替换字号被钳至 24pt 上限。"""
    import fitz
    from app.handlers.pdf_handler import _REPL_FONT_MAX, _fit_fontsize
    big = fitz.Rect(100, 100, 500, 500)
    assert _fit_fontsize(big, "***") == pytest.approx(_REPL_FONT_MAX)
    # 正常文本行（h=12pt）不触上限
    line = fitz.Rect(100, 100, 160, 112)
    assert _fit_fontsize(line, "***") < _REPL_FONT_MAX
    assert _fit_fontsize(line, "***") == pytest.approx(min(12 * 0.72, 60 / 3.276))


def _fake_engine(exc):
    class _E:
        name = "FAKE"

        def recognize(self, b, d):
            raise exc

    return _E()


def test_resilient_page_error_no_rebuild(monkeypatch):
    """⑤：OcrPageError 直抛——不 invalidate、不重建、不降级 CPU。"""
    calls = {"invalidate": 0, "rebuild": 0, "cpu": 0}
    monkeypatch.setattr(oe, "invalidate_engine_cache", lambda m: calls.__setitem__("invalidate", calls["invalidate"] + 1))
    monkeypatch.setattr(oe, "get_engine_for_mode",
                        lambda m, report_log=None: calls.__setitem__("rebuild", calls["rebuild"] + 1) or _fake_engine(OcrPageError("x")))
    monkeypatch.setattr(oe, "create_cpu_fallback_engine",
                        lambda: calls.__setitem__("cpu", calls["cpu"] + 1) or _fake_engine(OcrPageError("y")))

    eng = oe.GpuV6ResilientEngine("gpu_v6", None)
    calls.update(invalidate=0, rebuild=0, cpu=0)  # 构造期调用不计
    monkeypatch.setattr(eng, "_engine", _fake_engine(OcrPageError("单页解码失败")), raising=False)
    eng._engine = _fake_engine(OcrPageError("单页解码失败"))
    with pytest.raises(OcrPageError):
        eng.recognize(b"img", 150)
    assert calls == {"invalidate": 0, "rebuild": 0, "cpu": 0}, \
        f"页级错误不得触发重建链：{calls}"


def test_resilient_transport_error_still_heals(monkeypatch):
    """⑤：进程级失效（MissingDependencyError）保持杀重建自愈路径。"""
    calls = {"invalidate": 0, "rebuild": 0}
    monkeypatch.setattr(oe, "invalidate_engine_cache", lambda m: calls.__setitem__("invalidate", calls["invalidate"] + 1))
    ok_engine = _fake_engine(MissingDependencyError("should not be reached"))
    monkeypatch.setattr(oe, "get_engine_for_mode",
                        lambda m, report_log=None: calls.__setitem__("rebuild", calls["rebuild"] + 1) or ok_engine)

    class _Healed:
        name = "HEALED"

        def recognize(self, b, d):
            return "PAGE_OK"

    eng = oe.GpuV6ResilientEngine("gpu_v6", None)
    eng._engine = _fake_engine(MissingDependencyError("worker EOF"))
    monkeypatch.setattr(oe, "get_engine_for_mode", lambda m, report_log=None: _Healed())
    assert eng.recognize(b"img", 150) == "PAGE_OK"
    assert calls["invalidate"] >= 1, "进程级失效应 invalidate"
