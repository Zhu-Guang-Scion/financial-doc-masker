# -*- coding: utf-8 -*-
"""OCR 引擎选型（config.json 开关）与 ONNX v6 引擎单元测试（v1.7.5）。

覆盖：
- app_config：读写往返、默认值、坏文件/未知引擎容错；
- 引擎工厂注册表（ppocrv5 / ppocrv6_onnx）；
- OnnxV6Engine：模型缺失报错、recognize 像素→PDF 点坐标换算；
- pdf_handler._make_ocr_getter：按配置选引擎、v6 失败回退 v5、v5 失败照常抛错。

运行：python tests/test_ocr_engine_config.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import app_config
from app.ocr_engine import (MissingDependencyError, OCRPageResult, OnnxV6Engine,
                            _OCR_FACTORIES, create_ocr_engine, ppocrv6_available)
from app.utils import program_data_dir as _real_program_data_dir


class _Case:
    """极简用例收集器（与既有回归套件的 run() 风格一致）。"""

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


class _FakeReport:
    def __init__(self):
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)


class _FakeEngine:
    name = "fake-v5"


class _FakeOcrMod:
    """替身 ocr_engine 模块：记录 get_ocr_engine 调用并按剧本返回/抛错。"""

    def __init__(self, v6_ok=True, v5_ok=True):
        self.calls = []
        self.v6_ok = v6_ok
        self.v5_ok = v5_ok

    def get_ocr_engine(self, name="ppocrv5", **kwargs):
        self.calls.append(name)
        if name == app_config.OCR_ENGINE_V6_ONNX and not self.v6_ok:
            raise MissingDependencyError("v6 初始化失败（剧本）")
        if name == app_config.OCR_ENGINE_V5 and not self.v5_ok:
            raise MissingDependencyError("v5 初始化失败（剧本）")
        return _FakeEngine()


def _use_tmp_config_dir():
    tmp = Path(tempfile.mkdtemp(prefix="_cfg_test_"))
    app_config.program_data_dir = lambda: tmp
    return tmp


def _restore_config_dir():
    app_config.program_data_dir = _real_program_data_dir


def test_app_config(c: _Case):
    _use_tmp_config_dir()
    try:
        c.check("默认引擎为 ppocrv6_onnx（v1.8.0 起默认切 v6）",
                app_config.get_ocr_engine_name() == "ppocrv6_onnx")

        app_config.set_ocr_engine_name("ppocrv5")
        c.check("set 后读回为 ppocrv5（v5 全量保留可回退）",
                app_config.get_ocr_engine_name() == "ppocrv5")
        c.check("config.json 已落盘", app_config.config_path().is_file())

        app_config.config_path().write_text("{broken json", encoding="utf-8")
        c.check("坏文件回退默认 v6", app_config.get_ocr_engine_name() == "ppocrv6_onnx")

        app_config.config_path().write_text('{"ocr_engine": "unknown-x"}', encoding="utf-8")
        c.check("未知引擎名回退默认 v6", app_config.get_ocr_engine_name() == "ppocrv6_onnx")

        try:
            app_config.set_ocr_engine_name("nope")
            c.check("非法引擎名被拒绝", False, "未抛 ValueError")
        except ValueError:
            c.check("非法引擎名被拒绝", True)
    finally:
        _restore_config_dir()


def test_factory_registry(c: _Case):
    c.check("注册表含 ppocrv5", "ppocrv5" in _OCR_FACTORIES)
    c.check("注册表含 ppocrv6_onnx", "ppocrv6_onnx" in _OCR_FACTORIES)


def test_v6_engine_errors_and_conversion(c: _Case):
    empty = Path(tempfile.mkdtemp(prefix="_nomodel_"))
    try:
        create_ocr_engine("ppocrv6_onnx", model_dir=str(empty))
        c.check("模型缺失抛 MissingDependencyError", False, "未抛错")
    except MissingDependencyError as e:
        c.check("模型缺失抛 MissingDependencyError", "初始化失败" in str(e))
    except Exception as e:  # noqa: BLE001
        c.check("模型缺失抛 MissingDependencyError", False, f"错误类型 {type(e).__name__}")

    # recognize 坐标换算： RapidOCR 像素多边形 -> PDF 点（72dpi 基准）
    eng = OnnxV6Engine.__new__(OnnxV6Engine)

    def _fake_ocr(image_bytes):
        return ([[[[0, 0], [200, 0], [200, 100], [0, 100]], ("HELLO", 0.9)],
                 [[[10, 10], [60, 10], [60, 30], [10, 30]], ("签名", 0.8)]], 0.5)

    eng._ocr = _fake_ocr
    res = eng.recognize(b"png-bytes", dpi=144)  # scale = 72/144 = 0.5
    c.check("返回 OCRPageResult", isinstance(res, OCRPageResult))
    c.check("行数=2", len(res.lines) == 2)
    c.check("bbox 像素→点换算", res.lines[0].bbox == (0.0, 0.0, 100.0, 50.0),
            f"got {res.lines[0].bbox}")
    c.check("文本/置信度透传", res.lines[0].text == "HELLO" and res.lines[0].confidence == 0.9)
    c.check("空结果不崩溃",
            len(OnnxV6Engine.__new__(OnnxV6Engine).recognize(b"x", 72) if False else
                _run_empty(eng)) == 0)


def _run_empty(eng):
    eng._ocr = lambda data: (None, 0.1)
    return eng.recognize(b"x", 72).lines


def test_make_ocr_getter(c: _Case):
    from app.handlers import pdf_handler

    real_ocr_mod = pdf_handler.ocr_mod
    real_get_name = app_config.get_ocr_engine_name
    # v2.1.5 环境隔离修复：_make_ocr_getter 按现行实现先解析 ocr_mode
    # （显式键优先）——真实 config.json 曾被 GUI 写入 gpu_v6，测试进程
    # 读到后误入 GpuV6ResilientEngine 分支（_FakeOcrMod 无该属性即当年
    # 存量红的直接诱因）。本用例检验的是 auto 语义的引擎选择/回退，
    # 进程内钉 auto，与真实用户配置解耦。
    real_resolve_mode = app_config.resolve_ocr_mode
    app_config.resolve_ocr_mode = lambda: app_config.OCR_MODE_AUTO
    try:
        # A：配置 v6 且可用 → 用 v6
        app_config.get_ocr_engine_name = lambda: app_config.OCR_ENGINE_V6_ONNX
        fake = _FakeOcrMod(v6_ok=True)
        pdf_handler.ocr_mod = fake
        report = _FakeReport()
        get_ocr = pdf_handler._make_ocr_getter(report)
        eng = get_ocr()
        c.check("配置 v6 时创建 v6", fake.calls == ["ppocrv6_onnx"])
        c.check("v6 就绪", isinstance(eng, _FakeEngine))
        c.check("日志含就绪信息", any("已就绪" in m for m in report.logs))

        # B：配置 v6 但失败 → 回退 v5
        fake2 = _FakeOcrMod(v6_ok=False)
        pdf_handler.ocr_mod = fake2
        report2 = _FakeReport()
        eng2 = pdf_handler._make_ocr_getter(report2)()
        c.check("v6 失败回退 v5", fake2.calls == ["ppocrv6_onnx", "ppocrv5"],
                f"calls={fake2.calls}")
        c.check("回退日志已记录", any("回退" in m for m in report2.logs))
        c.check("回退后引擎可用", isinstance(eng2, _FakeEngine))

        # C：配置 v5 且 v5 失败 → 照常抛错（不吞异常）。
        # v2.1.5：现行 _make_ocr_getter 按 ocr_mode 分派——存量 v5 用户
        # 语义 = 显式 ocr_engine=ppocrv5 且无 ocr_mode 键 → LEGACY_V5，
        # 本场景局部钉该模式（其余场景 auto）。
        app_config.get_ocr_engine_name = lambda: app_config.OCR_ENGINE_V5
        app_config.resolve_ocr_mode = lambda: app_config.OCR_MODE_LEGACY_V5
        fake3 = _FakeOcrMod(v5_ok=False)
        pdf_handler.ocr_mod = fake3
        try:
            pdf_handler._make_ocr_getter(_FakeReport())()
            c.check("v5 失败照常抛错", False, "未抛 MissingDependencyError")
        except MissingDependencyError:
            c.check("v5 失败照常抛错", True)
        app_config.resolve_ocr_mode = lambda: app_config.OCR_MODE_AUTO

        # D：同批任务内缓存（不重复初始化）
        fake4 = _FakeOcrMod(v6_ok=True)
        pdf_handler.ocr_mod = fake4
        app_config.get_ocr_engine_name = lambda: app_config.OCR_ENGINE_V6_ONNX
        getter = pdf_handler._make_ocr_getter(_FakeReport())
        getter()
        getter()
        c.check("同批只初始化一次", fake4.calls == ["ppocrv6_onnx"])
    finally:
        pdf_handler.ocr_mod = real_ocr_mod
        app_config.get_ocr_engine_name = real_get_name
        app_config.resolve_ocr_mode = real_resolve_mode


def test_v6_available_probe(c: _Case):
    ok, reason = ppocrv6_available()
    c.check("探测返回 (bool, str) 二元组", isinstance(ok, bool) and isinstance(reason, str))
    if ok:
        print(f"  INFO 本机 v6 就绪：{reason or 'OK'}")


def run():
    print("== OCR 引擎配置与 ONNX v6 引擎测试 ==")
    c = _Case()
    test_app_config(c)
    test_factory_registry(c)
    test_v6_engine_errors_and_conversion(c)
    test_make_ocr_getter(c)
    test_v6_available_probe(c)
    print(f"\n共 {c.total} 条用例，失败 {len(c.failed)} 条")
    return not c.failed


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
