# -*- coding: utf-8 -*-
"""v2.1.2 自检升级测试：probe_optional_deps 关键组件真实 import 试加载。

背景（用户实测）：find_spec 不执行模块——「模块在、依赖 DLL 加载失败」
（lxml pyd 需要 VC++ x64 运行库）在自检面不可见，处理期才炸。升级后
docx/lxml.etree 两关键组件真实试加载，异常附内层原因 + VC++ 提示。

运行：pytest tests/test_probe_real_import.py 或 python 直跑。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app.utils as utils_mod  # noqa: E402
from app.utils import probe_optional_deps  # noqa: E402

LXML_LABEL = "lxml（python-docx 依赖库）"
DOCX_LABEL = "python-docx（Word 处理）"


def test_normal_env_all_available():
    """开发/部署正常态：全可用，lxml 条目常在。"""
    res = probe_optional_deps()
    assert res[LXML_LABEL][0] is True
    assert res[DOCX_LABEL][0] is True
    assert all(ok for ok, _ in res.values())


def test_dll_failure_surfaces_inner_cause(monkeypatch):
    """模拟 lxml pyd DLL 加载失败（OSError）：probe 必须翻转为未安装并
    透出内层原因 + VC++ 提示——消掉「自检过了、处理期才炸」盲区。"""
    orig_import = __import__

    def fake_import(name, *a, **k):
        if name == "lxml.etree":
            raise OSError("DLL load failed while importing etree")
        return orig_import(name, *a, **k)

    monkeypatch.setattr(utils_mod, "import_module", fake_import)
    res = probe_optional_deps()
    ok, hint = res[LXML_LABEL]
    assert ok is False
    assert "DLL load failed" in hint
    assert "VC++ 2015-2022" in hint
    # docx 自身 import 不受影响（其试加载独立执行）
    assert res[DOCX_LABEL][0] is True


def test_import_error_surfaces_inner_cause(monkeypatch):
    """模拟 docx import 失败（ImportError 族）：翻转 + 内层原因。"""
    orig_import = __import__

    def fake_import(name, *a, **k):
        if name == "docx":
            raise ImportError("No module named 'docx.models'")
        return orig_import(name, *a, **k)

    monkeypatch.setattr(utils_mod, "import_module", fake_import)
    res = probe_optional_deps()
    ok, hint = res[DOCX_LABEL]
    assert ok is False
    assert "No module named" in hint


if __name__ == "__main__":
    import traceback
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print("FAILURES:", failures)
    sys.exit(1 if failures else 0)
