# -*- coding: utf-8 -*-
"""三格式 handler 缺组件报错文案插值锁（2026-09-17 测试机实锤修复）。

背景：v2.1.2 增补批「报错消歧转正」在 docx/pptx/xlsx 三包装点附内层异常
{e} 时漏了 f 前缀——测试机（缺 VC++ x64）实报文案末尾为字面量
「内部原因： {e}」，内层异常没有透出（消歧目标落空）。本用例以 sys.modules
毒丸模拟 import 失败路径，端到端断言文案已插值且无占位符残留。
"""
import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class _FakeReport:
    def log(self, *a, **k):
        pass


def _run_handler(monkeypatch, modname, pkg):
    """以毒丸模块逼 handler 内层 import 抛 ImportError，返回异常文案。"""
    import builtins
    real_import = builtins.__import__

    def _poison(name, *a, **k):
        if name == pkg:
            raise ImportError(f"DLL load failed while importing etree（{pkg} 剧本）")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _poison)
    mod = importlib.import_module(f"app.handlers.{modname}_handler")
    with pytest.raises(Exception) as ei:
        mod.process(Path("x." + ("docx" if modname == "docx" else
                                 "pptx" if modname == "pptx" else "xlsx")),
                    Path("y"), None, None, _FakeReport())
    return str(ei.value)


@pytest.mark.parametrize("modname,pkg", [
    ("docx", "docx"), ("pptx", "pptx"), ("xlsx", "openpyxl"),
])
def test_dep_error_message_interpolates_inner_exception(monkeypatch, modname, pkg):
    msg = _run_handler(monkeypatch, modname, pkg)
    assert "{e}" not in msg, f"占位符未插值（测试机实锤 bug 回归）: {msg}"
    assert "DLL load failed while importing etree" in msg
    assert "VC++ 2015-2022 x64" in msg
    assert "内部原因：" in msg
