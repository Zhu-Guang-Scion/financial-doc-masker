# -*- coding: utf-8 -*-
"""OCR 模式选择器（v1.8.2）与外部 v6-GPU 环境接口单元测试。

覆盖：
- app_config：模式常量/白名单、resolve_ocr_mode 存量兼容（legacy_v5）、
  get/set_ocr_mode 往返与旧 ocr_engine 键隔离；
- ocr_engine.discover_external_v6_gpu_env：目录/解释器缺失、探针子进程
  注入（缺 CUDA EP / 版本不符仅告警 / 全过 / 探测失败）、结果缓存与重置、
  真实子进程探针（宽容断言）；v1.9.2a 起「先组件后标记」：组件全过还需
  <环境根>/.ready 就绪标记有效，缺失 → 未完成安装级人话（与缺组件级不混）；
- create_engine_for_mode：cpu_only（真实 v6-small@CPU）、auto 分派、
  gpu_v5/gpu_v6 不可用抛 MissingDependencyError、未知模式 ValueError；
- pdf_handler._make_ocr_getter：模式分派、legacy_v5 路由不变、
  模式失败回退 CPU（v6-small）+ 人话日志、兜底缓存与兜底失败抛错、
  gpu_v6 经健康守卫接线（v2.0.1 C3）；
- per-mode 引擎缓存（v2.0.1 C3）：同 mode 实例同一、切 mode 更换、
  invalidate 单键/全清与 gpu_v6 worker 干净关停、构造失败不入缓存、
  GpuV6ResilientEngine 失效联动（worker 断连 → invalidate → 重建成功
  重试 / 重建仍失败 → CPU 兜底不崩，fake worker 子进程级验证）；
- ExternalV6GpuEngine 与 worker 行 JSON 协议（fake worker：握手/识别/坐标
  换算/双格式/fatal/超时/进程死亡/清理），真实权重链路由后续冒烟覆盖。

运行：python tests/test_ocr_mode.py
"""
import base64
import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import app_config
from app.handlers import pdf_handler
from app.ocr_engine import (ExternalEnvProbe, ExternalV6GpuEngine, MissingDependencyError,
                            OCRLine, OCRPageResult, OnnxV6Engine, create_engine_for_mode,
                            create_cpu_fallback_engine, discover_external_v6_gpu_env,
                            get_last_engine_name, reset_v6_gpu_env_cache,
                            _probe_v6_gpu_env_subprocess)
from app.utils import OcrPageError
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

    def expect_mde(self, name, fn, keyword="", exc=MissingDependencyError):
        self.total += 1
        try:
            fn()
        except exc as e:
            if keyword and keyword not in str(e):
                self.failed.append(name)
                print(f"  FAIL {name} 错误信息缺少关键词：{keyword}（got：{e}）")
            else:
                print(f"  PASS {name}")
        except Exception as e:  # noqa: BLE001
            self.failed.append(name)
            print(f"  FAIL {name} 错误类型 {type(e).__name__}：{e}")
        else:
            self.failed.append(name)
            print(f"  FAIL {name} 未抛 MissingDependencyError")


class _Sentinel:
    def __init__(self, name="sentinel"):
        self.name = name


class _FakeReport:
    def __init__(self):
        self.logs = []

    def log(self, msg):
        self.logs.append(str(msg))


class _FakeResilientEngine:
    """ocr_mod.GpuV6ResilientEngine 最小替身（与真类同异常面：构造即取引擎，
    recognize 失效 → invalidate+重建一次重试；重建识别仍失败照常上抛）。"""

    def __init__(self, fake, mode, report_log=None):
        self._fake = fake
        self._mode = mode
        self._log = report_log
        self._engine = fake.get_engine_for_mode(mode, report_log=report_log)

    @property
    def name(self):
        return getattr(self._engine, "name", "")

    def recognize(self, image_bytes, dpi):
        try:
            return self._engine.recognize(image_bytes, dpi)
        except MissingDependencyError:
            self._fake.invalidations.append(self._mode)
            self._engine = self._fake.get_engine_for_mode(self._mode,
                                                          report_log=self._log)
            return self._engine.recognize(image_bytes, dpi)


class _FakeOcrModModes:
    """替身 ocr_engine 模块：同时具备旧 get_ocr_engine 与新模式接口。"""

    def __init__(self, mode_result=None, raise_mode=False,
                 fallback_result=None, raise_fallback=False):
        self.engine_calls = []
        self.mode_calls = []
        self.fallback_calls = 0
        self.invalidations = []
        self._mode_result = mode_result if mode_result is not None else _Sentinel("mode-engine")
        self._raise_mode = raise_mode
        self._fallback_result = fallback_result if fallback_result is not None \
            else _Sentinel("cpu-fallback")
        self._raise_fallback = raise_fallback

    def get_ocr_engine(self, name="ppocrv6_onnx", **kwargs):
        self.engine_calls.append(name)
        return _Sentinel(f"engine-{name}")

    def get_engine_for_mode(self, mode, report_log=None):
        self.mode_calls.append(mode)
        if report_log is not None:
            report_log("（剧本）模式引擎告警一条")
        if self._raise_mode:
            raise MissingDependencyError("模式引擎不可用（剧本）")
        return self._mode_result

    def invalidate_engine_cache(self, mode=None):
        self.invalidations.append(mode)

    def GpuV6ResilientEngine(self, mode, report_log=None):
        return _FakeResilientEngine(self, mode, report_log=report_log)

    def create_engine_for_mode(self, mode, report_log=None):
        self.mode_calls.append(mode)
        if report_log is not None:
            report_log("（剧本）模式引擎告警一条")
        if self._raise_mode:
            raise MissingDependencyError("模式引擎不可用（剧本）")
        return self._mode_result

    def create_cpu_fallback_engine(self):
        self.fallback_calls += 1
        if self._raise_fallback:
            raise MissingDependencyError("兜底也失败（剧本）")
        return self._fallback_result


# ---------------------------------------------------------------------------
# 配置目录替换（沿用 test_ocr_engine_config 的手法）
# ---------------------------------------------------------------------------

def _use_tmp_config_dir():
    tmp = Path(tempfile.mkdtemp(prefix="_mode_test_"))
    app_config.program_data_dir = lambda: tmp
    return tmp


def _restore_config_dir():
    app_config.program_data_dir = _real_program_data_dir


def _write_cfg(data: dict):
    app_config.config_path().write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1) 模式常量与白名单
# ---------------------------------------------------------------------------

def test_mode_constants(c: _Case):
    c.check("auto 常量值", app_config.OCR_MODE_AUTO == "auto")
    c.check("cpu_only 常量值", app_config.OCR_MODE_CPU == "cpu_only")
    c.check("gpu_v5 常量值", app_config.OCR_MODE_GPU_V5 == "gpu_v5")
    c.check("gpu_v6 常量值", app_config.OCR_MODE_GPU_V6 == "gpu_v6")
    c.check("白名单含四个合法值",
            app_config._OCR_MODE_VALUES == ("auto", "cpu_only", "gpu_v5", "gpu_v6"))
    c.check("legacy_v5 为内部值、不入白名单",
            app_config.OCR_MODE_LEGACY_V5 == "legacy_v5"
            and app_config.OCR_MODE_LEGACY_V5 not in app_config._OCR_MODE_VALUES)
    c.check("_DEFAULTS 含 ocr_mode=auto 且旧键仍在",
            app_config._DEFAULTS.get("ocr_mode") == "auto"
            and app_config._DEFAULTS.get("ocr_engine") == "ppocrv6_onnx")


# ---------------------------------------------------------------------------
# 2) resolve_ocr_mode（存量兼容）
# ---------------------------------------------------------------------------

def test_resolve_ocr_mode(c: _Case):
    _use_tmp_config_dir()
    real_get_name = app_config.get_ocr_engine_name
    try:
        # 存量语义：无 ocr_mode 键 + ocr_engine=ppocrv5 → legacy_v5
        _write_cfg({"ocr_engine": "ppocrv5"})
        c.check("存量 ppocrv5 → legacy_v5",
                app_config.resolve_ocr_mode() == app_config.OCR_MODE_LEGACY_V5)

        # 无 ocr_mode 键 + ocr_engine=ppocrv6 → auto
        _write_cfg({"ocr_engine": "ppocrv6_onnx"})
        c.check("存量 ppocrv6 → auto",
                app_config.resolve_ocr_mode() == app_config.OCR_MODE_AUTO)

        # ocr_mode 各合法值 → 原值
        for mode in app_config._OCR_MODE_VALUES:
            _write_cfg({"ocr_mode": mode, "ocr_engine": "ppocrv5"})
            c.check(f"ocr_mode={mode} → 原值（旧键不参与）",
                    app_config.resolve_ocr_mode() == mode)

        # 非法 ocr_mode → auto
        _write_cfg({"ocr_mode": "bogus"})
        c.check("非法 ocr_mode → auto",
                app_config.resolve_ocr_mode() == app_config.OCR_MODE_AUTO)

        # 无文件 → 默认值口径（get_ocr_engine_name 可被替换）
        app_config.config_path().unlink(missing_ok=True)
        app_config.get_ocr_engine_name = lambda: app_config.OCR_ENGINE_V5
        c.check("无文件 + 替换 get_ocr_engine_name=v5 → legacy_v5（既有 monkeypatch 手法兼容）",
                app_config.resolve_ocr_mode() == app_config.OCR_MODE_LEGACY_V5)
        app_config.get_ocr_engine_name = lambda: app_config.OCR_ENGINE_V6_ONNX
        c.check("无文件 + 替换 get_ocr_engine_name=v6 → auto",
                app_config.resolve_ocr_mode() == app_config.OCR_MODE_AUTO)

        # 坏文件 → 同样走默认值口径
        app_config.config_path().write_text("{broken", encoding="utf-8")
        c.check("坏文件 → auto（默认值口径）",
                app_config.resolve_ocr_mode() == app_config.OCR_MODE_AUTO)
    finally:
        app_config.get_ocr_engine_name = real_get_name
        _restore_config_dir()


def test_ocr_mode_roundtrip(c: _Case):
    _use_tmp_config_dir()
    try:
        # 存量用户切模式：旧 ocr_engine 键保持不动
        _write_cfg({"ocr_engine": "ppocrv5"})
        app_config.set_ocr_mode(app_config.OCR_MODE_GPU_V6)
        cfg = json.loads(app_config.config_path().read_text(encoding="utf-8"))
        c.check("set_ocr_mode 落盘 ocr_mode",
                cfg.get("ocr_mode") == app_config.OCR_MODE_GPU_V6)
        c.check("旧 ocr_engine 键保持 ppocrv5 不动", cfg.get("ocr_engine") == "ppocrv5")
        c.check("get_ocr_engine_name 仍为 ppocrv5（存量接口不受影响）",
                app_config.get_ocr_engine_name() == "ppocrv5")
        c.check("切换后 resolve 即为新模式（ocr_mode 键优先）",
                app_config.resolve_ocr_mode() == app_config.OCR_MODE_GPU_V6)
        c.check("get_ocr_mode 读回", app_config.get_ocr_mode() == app_config.OCR_MODE_GPU_V6)

        try:
            app_config.set_ocr_mode(app_config.OCR_MODE_LEGACY_V5)
            c.check("legacy_v5 不可写入（拒绝）", False, "未抛 ValueError")
        except ValueError:
            c.check("legacy_v5 不可写入（拒绝）", True)
        try:
            app_config.set_ocr_mode("nope")
            c.check("非法模式被拒绝", False, "未抛 ValueError")
        except ValueError:
            c.check("非法模式被拒绝", True)

        # 未知 ocr_mode 配置值 → get_ocr_mode 回 auto
        _write_cfg({"ocr_mode": "wat"})
        c.check("get_ocr_mode 未知值回 auto", app_config.get_ocr_mode() == app_config.OCR_MODE_AUTO)
    finally:
        _restore_config_dir()


def _write_ready_marker(env_root: Path, **overrides) -> Path:
    """往 tmp 环境根写契约样例口径的有效 .ready（v1.9.2a）；overrides 覆盖字段。"""
    data = {"ort_version": "1.20.2", "cuda_ep": True,
            "rapidocr_version": "1.4.4",
            "created_at": "2026-09-09T12:00:00", "bat_version": "1.9.2a"}
    data.update(overrides)
    marker = env_root / ".ready"
    marker.write_text(json.dumps(data), encoding="utf-8")
    return marker


# ---------------------------------------------------------------------------
# 3) discover_external_v6_gpu_env
# ---------------------------------------------------------------------------

def test_discover_v6_gpu_env(c: _Case):
    # 目录不存在
    p = discover_external_v6_gpu_env(env_dir=Path(tempfile.mkdtemp(prefix="_nope_")) / "missing")
    c.check("目录不存在 → ok=False", p.ok is False)
    c.check("missing 含目录项提示", any("未找到 v6 GPU 环境目录" in m for m in p.missing))

    # 空目录（无 python.exe）
    empty = Path(tempfile.mkdtemp(prefix="_empty_env_"))
    p = discover_external_v6_gpu_env(env_dir=empty)
    c.check("空目录 → ok=False", p.ok is False)
    c.check("missing 含解释器缺失项", any("Python 解释器" in m for m in p.missing))

    # 造一个含 python.exe 的环境骨架（探针注入的 fake 不会真正执行它），
    # 后续用例验证"解释器存在时探针结果 → missing/warnings"的逐项归因。
    (empty / "Scripts").mkdir()
    (empty / "Scripts" / "python.exe").write_bytes(b"")

    # 探针注入：全部通过
    def _probe_ok(python_exe):
        return {"ort_version": "1.20.2", "rapidocr_version": "1.4.4",
                "has_cuda_ep": True, "ort_error": "", "rapidocr_error": ""}

    # v1.9.2a 先组件后标记：组件全过但缺 .ready → 未完成安装级人话
    p = discover_external_v6_gpu_env(env_dir=empty, probe=_probe_ok)
    c.check("组件全过但缺 .ready → ok=False", p.ok is False)
    c.check("组件全过但缺 .ready → reason 含「未完成安装（缺就绪标记）」",
            any("外部环境未完成安装（缺就绪标记），请重跑 setup_v6_gpu_env.bat"
                in m for m in p.missing), f"missing={p.missing}")
    c.check("组件全过但缺 .ready → ready_ok=False 且缺标记原因透传",
            p.ready_ok is False and "缺标记" in p.ready_reason,
            f"ready_ok={p.ready_ok} reason={p.ready_reason!r}")

    _write_ready_marker(empty)   # 补有效标记后，组件全过正例恢复 ok=True
    p = discover_external_v6_gpu_env(env_dir=empty, probe=_probe_ok)
    c.check("探针全过 → ok=True", p.ok is True)
    c.check("探针全过 → warnings 为空", p.warnings == [])
    c.check("探针全过 → ort/rapidocr 版本透传",
            p.ort_version == "1.20.2" and p.rapidocr_version == "1.4.4")
    c.check("探针全过 → has_cuda_ep=True", p.has_cuda_ep is True)
    c.check("探针全过 → ready_ok=True 且 reason 为空",
            p.ready_ok is True and p.ready_reason == "")

    # 缺 CUDA EP → ok=False + 逐项中文 missing
    def _probe_no_cuda(python_exe):
        return {"ort_version": "1.20.2", "rapidocr_version": "1.4.4",
                "has_cuda_ep": False, "ort_error": "", "rapidocr_error": ""}

    p = discover_external_v6_gpu_env(env_dir=empty, probe=_probe_no_cuda)
    c.check("缺 CUDA EP → ok=False", p.ok is False)
    c.check("缺 CUDA EP → missing 含 CUDAExecutionProvider 项",
            any("CUDAExecutionProvider" in m for m in p.missing))

    # 版本不符 → 仅 warnings，ok 不变 False
    def _probe_ver(python_exe):
        return {"ort_version": "1.19.0", "rapidocr_version": "1.5.0",
                "has_cuda_ep": True, "ort_error": "", "rapidocr_error": ""}

    p = discover_external_v6_gpu_env(env_dir=empty, probe=_probe_ver)
    c.check("版本不符 → ok 仍为 True", p.ok is True)
    c.check("版本不符 → warnings 非空（两条）", len(p.warnings) == 2, f"got {p.warnings}")

    # 组件不可导入 → missing
    def _probe_broken(python_exe):
        return {"ort_version": "", "rapidocr_version": "",
                "has_cuda_ep": False, "ort_error": "[ImportError] no ort",
                "rapidocr_error": "[ImportError] no rapidocr"}

    p = discover_external_v6_gpu_env(env_dir=empty, probe=_probe_broken)
    c.check("ort 不可导入 → ok=False", p.ok is False)
    c.check("ort/rapidocr 不可导入逐项留痕",
            any("onnxruntime 不可导入" in m for m in p.missing)
            and any("rapidocr-onnxruntime 不可导入" in m for m in p.missing))

    # 探测子进程失败 → missing
    def _probe_boom(python_exe):
        raise RuntimeError("探测子进程超时（>60s）")

    p = discover_external_v6_gpu_env(env_dir=empty, probe=_probe_boom)
    c.check("探测子进程失败 → ok=False", p.ok is False)
    c.check("探测失败原因入 missing", any("探测子进程超时" in m for m in p.missing))

    # 默认目录路径 + 进程内缓存 + 重置
    reset_v6_gpu_env_cache()
    saved = os.environ.pop("FINMASK_V6_GPU_ENV", None)
    try:
        p1 = discover_external_v6_gpu_env()
        p2 = discover_external_v6_gpu_env()
        c.check("默认目录探测结果进程内缓存（同一对象）", p1 is p2)
        reset_v6_gpu_env_cache()
        p3 = discover_external_v6_gpu_env()
        c.check("重置后重新探测（新对象）", p3 is not p1)
    finally:
        if saved is not None:
            os.environ["FINMASK_V6_GPU_ENV"] = saved
        reset_v6_gpu_env_cache()

    # 真实子进程探针（宽容断言：只验协议形状，不依赖主环境装了什么）
    try:
        info = _probe_v6_gpu_env_subprocess(sys.executable)
        c.check("真实探针返回 dict 且含约定键",
                isinstance(info, dict) and {"ort_version", "has_cuda_ep",
                                            "rapidocr_version"} <= set(info))
    except Exception as e:  # noqa: BLE001
        c.check("真实探针返回 dict 且含约定键", False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# 4) create_engine_for_mode
# ---------------------------------------------------------------------------

def test_create_engine_for_mode(c: _Case):
    from app.ocr_engine import create_ocr_engine as _real_create

    # cpu_only：真实创建 v6-small@CPU（本机 small 权重在 models/onnx_v6）
    eng = create_engine_for_mode(app_config.OCR_MODE_CPU)
    c.check("cpu_only → OnnxV6Engine 实例", isinstance(eng, OnnxV6Engine))
    c.check("cpu_only → 引擎名含 small(cpu)", "small(cpu)" in eng.name, f"got {eng.name}")
    c.check("note_engine_name 已记录 cpu_only 引擎",
            "small(cpu)" in get_last_engine_name(), f"got {get_last_engine_name()!r}")

    # auto：分派到 create_ocr_engine（注入 fake 验证分派，不依赖 GPU 环境）
    created = {}
    import app.ocr_engine as ocr_mod
    ocr_mod.create_ocr_engine = lambda *a, **k: created.setdefault("eng", _Sentinel("auto-engine"))
    try:
        eng = create_engine_for_mode(app_config.OCR_MODE_AUTO)
        c.check("auto → 分派到 create_ocr_engine 现路径",
                isinstance(eng, _Sentinel) and created.get("eng") is eng)
    finally:
        ocr_mod.create_ocr_engine = _real_create

    # gpu_v5：无外接 Paddle → MDE
    real_discover_v5 = ocr_mod.discover_external_paddle_env
    ocr_mod.discover_external_paddle_env = lambda: []
    try:
        c.expect_mde("gpu_v5 无 Paddle 环境 → MissingDependencyError",
                     lambda: create_engine_for_mode(app_config.OCR_MODE_GPU_V5),
                     keyword="Paddle")
    finally:
        ocr_mod.discover_external_paddle_env = real_discover_v5

    # gpu_v6：无外部环境（默认目录不存在）→ MDE
    reset_v6_gpu_env_cache()
    saved_env = os.environ.pop("FINMASK_V6_GPU_ENV", None)
    try:
        base = ocr_mod._v6_gpu_env_base_dir()
        if base.is_dir():  # 现场防御：项目根不应存在 v6_gpu_env
            print(f"  INFO 跳过 gpu_v6 无环境用例（{base} 已存在）")
            c.check("gpu_v6 无外部环境 → MissingDependencyError", True, "（跳过）")
        else:
            c.expect_mde("gpu_v6 无外部环境 → MissingDependencyError",
                         lambda: create_engine_for_mode(app_config.OCR_MODE_GPU_V6),
                         keyword="v6 GPU 环境目录")
    finally:
        if saved_env is not None:
            os.environ["FINMASK_V6_GPU_ENV"] = saved_env
        reset_v6_gpu_env_cache()

    # 未知模式 → ValueError
    try:
        create_engine_for_mode("warp-drive")
        c.check("未知模式 → ValueError", False, "未抛错")
    except ValueError:
        c.check("未知模式 → ValueError", True)

    # CPU 兜底工厂直测
    fb = create_cpu_fallback_engine()
    c.check("create_cpu_fallback_engine → v6-small@CPU",
            isinstance(fb, OnnxV6Engine) and "small(cpu)" in fb.name)


# ---------------------------------------------------------------------------
# 5) _make_ocr_getter 模式感知
# ---------------------------------------------------------------------------

def test_make_ocr_getter_modes(c: _Case):
    real_ocr_mod = pdf_handler.ocr_mod
    try:
        # A：存量 legacy_v5 → 走旧 get_ocr_engine 路径（v5），不触新模式接口
        _use_tmp_config_dir()
        _write_cfg({"ocr_engine": "ppocrv5"})
        fake = _FakeOcrModModes()
        pdf_handler.ocr_mod = fake
        report = _FakeReport()
        eng = pdf_handler._make_ocr_getter(report)()
        c.check("legacy_v5 → get_ocr_engine(ppocrv5)", fake.engine_calls == ["ppocrv5"],
                f"calls={fake.engine_calls}")
        c.check("legacy_v5 → 不触 create_engine_for_mode", fake.mode_calls == [])
        c.check("legacy_v5 → 返回引擎并报就绪",
                isinstance(eng, _Sentinel) and any("已就绪" in m for m in report.logs))

        # B：cpu_only → create_engine_for_mode 分派
        _write_cfg({"ocr_mode": "cpu_only"})
        fake = _FakeOcrModModes()
        pdf_handler.ocr_mod = fake
        report = _FakeReport()
        eng = pdf_handler._make_ocr_getter(report)()
        c.check("cpu_only → create_engine_for_mode(cpu_only)", fake.mode_calls == ["cpu_only"],
                f"mode_calls={fake.mode_calls}")
        c.check("cpu_only → 返回模式引擎", isinstance(eng, _Sentinel))

        # C：模式失败 → 人话提示 + 回退 CPU（v6-small）+ 同批缓存
        _write_cfg({"ocr_mode": "gpu_v6"})
        fake = _FakeOcrModModes(raise_mode=True)
        pdf_handler.ocr_mod = fake
        report = _FakeReport()
        getter = pdf_handler._make_ocr_getter(report)
        eng = getter()
        c.check("模式失败 → 回退 CPU 兜底引擎", isinstance(eng, _Sentinel))
        c.check("模式失败 → 日志含人话回退提示",
                any("已回退 CPU" in m and "v6-small" in m for m in report.logs),
                f"logs={report.logs}")
        c.check("模式失败 → 模式尝试记录在案", fake.mode_calls == ["gpu_v6"])
        getter()
        c.check("同批只初始化一次（兜底不再重试）",
                fake.fallback_calls == 1 and fake.mode_calls == ["gpu_v6"])

        # D：兜底也失败 → 照常抛错（不吞异常、无半成品）
        fake = _FakeOcrModModes(raise_mode=True, raise_fallback=True)
        pdf_handler.ocr_mod = fake
        c.expect_mde("兜底也失败 → MissingDependencyError 上抛",
                     lambda: pdf_handler._make_ocr_getter(_FakeReport())(),
                     keyword="兜底也失败")

        # E：默认（auto）路径与 v1.8.1 等价：无配置文件 → get_ocr_engine(v6)，
        #    且 v6 失败仍回退 v5（保留 v1.8.1 分支）
        tmp = _use_tmp_config_dir()  # 干净目录：无 config.json
        fake = _FakeOcrModModes()
        pdf_handler.ocr_mod = fake
        report = _FakeReport()
        pdf_handler._make_ocr_getter(report)()
        c.check("auto（无配置）→ get_ocr_engine(ppocrv6_onnx) 现路径",
                fake.engine_calls == ["ppocrv6_onnx"] and fake.mode_calls == [])

        # F：gpu_v6 健康接线（v2.0.1 C3）→ 弹性守卫包装，底层引擎经
        #    get_engine_for_mode（per-mode 缓存接线）
        _write_cfg({"ocr_mode": "gpu_v6"})
        fake = _FakeOcrModModes()
        pdf_handler.ocr_mod = fake
        report = _FakeReport()
        eng = pdf_handler._make_ocr_getter(report)()
        c.check("gpu_v6 → getter 返回弹性守卫包装",
                isinstance(eng, _FakeResilientEngine))
        c.check("gpu_v6 → 底层引擎经 get_engine_for_mode 接线",
                fake.mode_calls == ["gpu_v6"] and eng._engine is fake._mode_result)
    finally:
        pdf_handler.ocr_mod = real_ocr_mod
        _restore_config_dir()


# ---------------------------------------------------------------------------
# 6) ExternalV6GpuEngine 与 worker 行 JSON 协议（fake worker）
# ---------------------------------------------------------------------------

_FAKE_WORKER = textwrap.dedent("""
    import base64 as _b64
    import json
    import sys


    def out(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()


    out({"event": "ready", "engine": "fake-ext-engine"})
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        req = json.loads(raw)
        if req.get("image_b64") == _b64.b64encode(b"die").decode():
            sys.exit(7)  # 剧本：进程中途死亡（宿主应转 MissingDependencyError）
        if req.get("op") != "recognize":
            out({"id": req.get("id"), "ok": False, "error": "bad op"})
            continue
        if req.get("image_b64") == _b64.b64encode(b"fail").decode():
            out({"id": req.get("id"), "ok": False, "error": "识别失败（剧本）"})
            continue
        if req.get("image_b64") == _b64.b64encode(b"tri").decode():
            # triple 线格式（向前兼容分支）
            out({"id": req.get("id"), "ok": True, "lines": [
                [[[0, 0], [200, 0], [200, 100], [0, 100]], "TRI", 0.5]]})
            continue
        out({"id": req.get("id"), "ok": True, "lines": [
            {"text": "HELLO-13812345678", "poly": [[0, 0], [200, 0], [200, 100], [0, 100]],
             "score": 0.9},
            {"text": "签名", "poly": [[10, 10], [60, 10], [60, 30], [10, 30]], "score": 0.8}]})
""")

_SILENT_WORKER = "import time\ntime.sleep(120)\n"

_FATAL_WORKER = textwrap.dedent("""
    import json
    import sys
    sys.stdout.write(json.dumps({"event": "fatal",
                                 "error": "GPU 初始化失败（剧本）"}) + "\\n")
    sys.stdout.flush()
    sys.exit(1)
""")


class _FakeDiscover:
    """可复用的 discover 替身：返回合法探测结果（python_exe 可注入假环境树）。"""

    def __init__(self, warnings=None, python_exe=None):
        self.warnings = warnings or []
        self._python_exe = python_exe or sys.executable

    def __call__(self, env_dir=None, probe=None):
        return ExternalEnvProbe(ok=True, python_exe=self._python_exe,
                                missing=[], warnings=list(self.warnings),
                                ort_version="1.20.2", rapidocr_version="1.4.4",
                                has_cuda_ep=True)


def _fake_v6_env_tree(workdir):
    """构造最小假 v6 环境树（供 create_engine_for_mode 默认模型解析落点）：

    <workdir>/fakeenv/Scripts/python.exe ← junction → 真实解释器目录（可 spawn，
    且 worker 只用 stdlib）；<workdir>/fakeenv/models/onnx_v6/ 放假 medium 权重
    （只过 is_file 检查，fake worker 不真加载）。返回 (python_exe, models,
    junction_dir)；junction 创建失败返回 None（调用方按现场跳过）。
    """
    import subprocess as _sp
    env_root = Path(workdir) / "fakeenv"
    scripts = env_root / "Scripts"
    models = env_root / "models" / "onnx_v6"
    try:
        real_dir = Path(sys.executable).parent
        env_root.mkdir(parents=True, exist_ok=True)  # mklink 要求链接父目录存在
        r = _sp.run(["cmd", "/c", "mklink", "/j", str(scripts), str(real_dir)],
                    capture_output=True)  # 字节模式：cmd 输出 GBK，避免解码崩溃
        if r.returncode != 0 or not (scripts / "python.exe").is_file():
            return None
        models.mkdir(parents=True)
        (models / "PP-OCRv6_det_medium.onnx").write_bytes(b"dummy")
        (models / "PP-OCRv6_rec_medium.onnx").write_bytes(b"dummy")
    except OSError:
        return None
    return scripts / "python.exe", models, scripts


def _make_models_dir():
    models = Path(tempfile.mkdtemp(prefix="_v6models_")) / "models" / "onnx_v6"
    models.mkdir(parents=True)
    (models / "PP-OCRv6_det_medium.onnx").write_bytes(b"dummy")
    (models / "PP-OCRv6_rec_medium.onnx").write_bytes(b"dummy")
    return models


def test_external_v6_engine_protocol(c: _Case):
    import app.ocr_engine as ocr_mod

    real_discover = ocr_mod.discover_external_v6_gpu_env
    real_worker_path = ocr_mod.ExternalV6GpuEngine._resolve_worker_path
    workdir = Path(tempfile.mkdtemp(prefix="_v6proto_"))
    worker = workdir / "fake_worker.py"
    worker.write_text(_FAKE_WORKER, encoding="utf-8")
    models = _make_models_dir()
    try:
        ocr_mod.discover_external_v6_gpu_env = _FakeDiscover()
        ocr_mod.ExternalV6GpuEngine._resolve_worker_path = staticmethod(lambda: worker)

        # 握手 + 识别 + 坐标换算（dpi=144 → scale=0.5）
        eng = ExternalV6GpuEngine(model_dir=str(models), timeout_sec=30)
        c.check("握手成功 → 引擎名固定", eng.name == "PP-OCRv6-ONNX-medium(GPU-EXT)")
        res = eng.recognize(b"hello-png", 144)
        c.check("返回 OCRPageResult", isinstance(res, OCRPageResult))
        c.check("行数=2", len(res.lines) == 2)
        c.check("bbox 像素→PDF 点换算（×72/144）",
                res.lines[0].bbox == (0.0, 0.0, 100.0, 50.0), f"got {res.lines[0].bbox}")
        c.check("文本/置信度透传",
                res.lines[0].text == "HELLO-13812345678" and res.lines[0].confidence == 0.9)

        # ok=False 响应 → OcrPageError（稳定体验小批 ⑤：单页失败≠进程死亡，
        # 与 EOF/超时类 MissingDependencyError 分家——resilient 守卫不再误杀
        # 健康 worker；由 pdf_handler 页级兜底接管）
        c.expect_mde("worker ok=false → OcrPageError",
                     lambda: eng.recognize(b"fail", 72), keyword="识别失败（剧本）",
                     exc=OcrPageError)

        # triple 线格式兼容分支
        res3 = eng.recognize(b"tri", 72)
        c.check("triple 线格式兼容", len(res3.lines) == 1 and res3.lines[0].text == "TRI"
                and res3.lines[0].bbox == (0.0, 0.0, 200.0, 100.0))

        # worker 进程死亡 → MissingDependencyError
        req_id_before = eng._req_seq
        c.expect_mde("worker 进程死亡 → MissingDependencyError",
                     lambda: eng.recognize(b"die", 72))
        c.check("死亡用例确实触发了请求", eng._req_seq == req_id_before + 1)
        eng.close()

        # close 幂等
        eng.close()
        c.check("close() 可重入不抛错", True)

        # fatal 握手 → MissingDependencyError（带 worker 报错）
        fatal_worker = workdir / "fatal_worker.py"
        fatal_worker.write_text(_FATAL_WORKER, encoding="utf-8")
        ocr_mod.ExternalV6GpuEngine._resolve_worker_path = staticmethod(lambda: fatal_worker)
        c.expect_mde("fatal 握手 → MissingDependencyError",
                     lambda: ExternalV6GpuEngine(model_dir=str(models)),
                     keyword="GPU 初始化失败（剧本）")

        # 缺权重 → MissingDependencyError
        ocr_mod.ExternalV6GpuEngine._resolve_worker_path = staticmethod(lambda: worker)
        empty_models = Path(tempfile.mkdtemp(prefix="_v6nomodel_"))
        c.expect_mde("外部环境缺 medium 权重 → MissingDependencyError",
                     lambda: ExternalV6GpuEngine(model_dir=str(empty_models)),
                     keyword="medium 权重")

        # 静默 worker → 握手超时（临时调短类属性）
        silent = workdir / "silent_worker.py"
        silent.write_text(_SILENT_WORKER, encoding="utf-8")
        ocr_mod.ExternalV6GpuEngine._resolve_worker_path = staticmethod(lambda: silent)
        old_hs = ocr_mod.ExternalV6GpuEngine.HANDSHAKE_TIMEOUT
        ocr_mod.ExternalV6GpuEngine.HANDSHAKE_TIMEOUT = 1.5
        try:
            c.expect_mde("静默 worker → 握手超时 MissingDependencyError",
                         lambda: ExternalV6GpuEngine(model_dir=str(models)),
                         keyword="握手超时")
        finally:
            ocr_mod.ExternalV6GpuEngine.HANDSHAKE_TIMEOUT = old_hs

        # gpu_v6 单例入口（走注入的 discover + fake worker）
        ocr_mod.ExternalV6GpuEngine._resolve_worker_path = staticmethod(lambda: worker)
        reset_v6_gpu_env_cache()
        singleton = ocr_mod._get_v6_gpu_singleton(model_dir=str(models))
        again = ocr_mod._get_v6_gpu_singleton(model_dir=str(models))
        c.check("gpu_v6 进程级单例（worker 不重复孵化）", singleton is again)
        ocr_mod._close_v6_gpu_singleton()
        c.check("单例关闭后重建为新实例",
                ocr_mod._get_v6_gpu_singleton(model_dir=str(models)) is not again)
        ocr_mod._close_v6_gpu_singleton()
    finally:
        ocr_mod.discover_external_v6_gpu_env = real_discover
        ocr_mod.ExternalV6GpuEngine._resolve_worker_path = real_worker_path
        ocr_mod._close_v6_gpu_singleton()
        reset_v6_gpu_env_cache()


# ---------------------------------------------------------------------------
# 7) per-mode 引擎缓存与 gpu_v6 失效联动（v2.0.1 C3）
# ---------------------------------------------------------------------------

_AGEING_WORKER = textwrap.dedent("""
    import json
    import sys

    sys.stdout.write(json.dumps({"event": "ready"}) + "\\n")
    sys.stdout.flush()
    n = 0
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        n += 1
        if n >= 3:
            sys.exit(7)  # 剧本：本 worker 第 3 个请求即死（新进程计数归零 → 重试成功）
        req = json.loads(raw)
        sys.stdout.write(json.dumps(
            {"id": req.get("id"), "ok": True,
             "lines": [{"text": "HELLO", "poly": [[0, 0], [10, 0], [10, 10], [0, 10]],
                        "score": 0.9}]}) + "\\n")
        sys.stdout.flush()
""")


def test_mode_engine_cache(c: _Case):
    """缓存 API：同 mode 实例同一；切 mode 更换；invalidate 单键/全清；
    invalidate 后重建成功；构造失败不入缓存。"""
    import app.ocr_engine as ocr_mod

    real_create = ocr_mod.create_engine_for_mode
    try:
        calls = []

        def fake_create(mode, report_log=None):
            calls.append(mode)
            return _Sentinel(f"eng-{mode}-{len(calls)}")

        ocr_mod.create_engine_for_mode = fake_create
        ocr_mod.invalidate_engine_cache()
        a1 = ocr_mod.get_engine_for_mode("modeA")
        a2 = ocr_mod.get_engine_for_mode("modeA")
        c.check("同 mode → 两次获取实例同一（仅构造一次）",
                a1 is a2 and calls == ["modeA"])
        b1 = ocr_mod.get_engine_for_mode("modeB")
        c.check("切换 mode → 实例更换（mode 字符串即缓存键）",
                b1 is not a1 and calls == ["modeA", "modeB"])
        ocr_mod.invalidate_engine_cache("modeA")
        c.check("invalidate 单键 → 该键离缓存、他键不受扰",
                "modeA" not in ocr_mod._MODE_ENGINE_CACHE
                and ocr_mod._MODE_ENGINE_CACHE.get("modeB") is b1)
        a3 = ocr_mod.get_engine_for_mode("modeA")
        c.check("invalidate 后重建成功并重新入缓存",
                a3 is not a1 and calls.count("modeA") == 2)

        def boom(mode, report_log=None):
            raise MissingDependencyError("构造失败（剧本）")

        ocr_mod.create_engine_for_mode = boom
        ocr_mod.invalidate_engine_cache("modeA")
        c.expect_mde("构造失败 → MDE 上抛",
                     lambda: ocr_mod.get_engine_for_mode("modeA"),
                     keyword="构造失败")
        c.check("构造失败 → 不入缓存（下次自动重试构造）",
                "modeA" not in ocr_mod._MODE_ENGINE_CACHE)
        ocr_mod.create_engine_for_mode = fake_create
        ocr_mod.invalidate_engine_cache()
        c.check("invalidate(None) → 全清", ocr_mod._MODE_ENGINE_CACHE == {})
    finally:
        ocr_mod.create_engine_for_mode = real_create
        ocr_mod.invalidate_engine_cache()


def test_invalidate_engine_cache_gpu_v6(c: _Case):
    """gpu_v6 失效=worker 干净关停+进程级单例同步置空；cpu 键失效=丢引用。"""
    import app.ocr_engine as ocr_mod

    class _ClosableEng:
        def __init__(self, name):
            self.name = name
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    real_singleton = ocr_mod._V6_GPU_ENGINE_SINGLETON
    v6 = _ClosableEng("fake-v6")
    cpu = _ClosableEng("fake-cpu")
    try:
        ocr_mod._MODE_ENGINE_CACHE[app_config.OCR_MODE_GPU_V6] = v6
        ocr_mod._MODE_ENGINE_CACHE[app_config.OCR_MODE_CPU] = cpu
        ocr_mod._V6_GPU_ENGINE_SINGLETON = v6
        ocr_mod.invalidate_engine_cache(app_config.OCR_MODE_GPU_V6)
        c.check("gpu_v6 失效 → 键清除+worker 干净关停（close×1）",
                app_config.OCR_MODE_GPU_V6 not in ocr_mod._MODE_ENGINE_CACHE
                and v6.close_calls == 1)
        c.check("gpu_v6 失效 → 进程级 v6 单例同步置空（防拿已关停旧实例）",
                ocr_mod._V6_GPU_ENGINE_SINGLETON is None)
        ocr_mod.invalidate_engine_cache(app_config.OCR_MODE_CPU)
        c.check("cpu 键失效=丢引用（不触发 close）",
                app_config.OCR_MODE_CPU not in ocr_mod._MODE_ENGINE_CACHE
                and cpu.close_calls == 0)
        v6b = _ClosableEng("fake-v6-b")
        ocr_mod._MODE_ENGINE_CACHE[app_config.OCR_MODE_GPU_V6] = v6b
        ocr_mod.invalidate_engine_cache(app_config.OCR_MODE_GPU_V6)
        c.check("缓存实例非进程级单例（测试注入态）→ 仍走干净 close",
                v6b.close_calls == 1)
    finally:
        ocr_mod._V6_GPU_ENGINE_SINGLETON = real_singleton
        ocr_mod.invalidate_engine_cache()


def test_gpu_v6_resilient_rebuild(c: _Case):
    """失效联动②（fake worker 子进程级）：识别中途 worker 死亡 → invalidate +
    重建（新 worker 进程）→ 同页重试成功，新引擎重新入缓存。"""
    import app.ocr_engine as ocr_mod

    real_discover = ocr_mod.discover_external_v6_gpu_env
    real_worker_path = ocr_mod.ExternalV6GpuEngine._resolve_worker_path
    workdir = Path(tempfile.mkdtemp(prefix="_v6resil_"))
    worker = workdir / "ageing_worker.py"
    worker.write_text(_AGEING_WORKER, encoding="utf-8")
    tree = _fake_v6_env_tree(workdir)
    if tree is None:  # 现场防御：junction 不可用（非 Windows 等）→ 跳过
        c.check("worker 失效 → invalidate+重建成功，同页重试成功", True, "（跳过）")
        return
    fake_python, models, junction_dir = tree
    try:
        ocr_mod.discover_external_v6_gpu_env = _FakeDiscover(python_exe=str(fake_python))
        ocr_mod.ExternalV6GpuEngine._resolve_worker_path = staticmethod(lambda: worker)
        ocr_mod.invalidate_engine_cache()
        guard = ocr_mod.GpuV6ResilientEngine(app_config.OCR_MODE_GPU_V6)
        c.check("构造 → 底层即 per-mode 缓存引擎",
                ocr_mod._MODE_ENGINE_CACHE.get(app_config.OCR_MODE_GPU_V6)
                is guard._engine)
        r1 = guard.recognize(b"p1", 72)
        r2 = guard.recognize(b"p2", 72)
        c.check("前两次识别正常（同一 worker）",
                len(r1.lines) == 1 and len(r2.lines) == 1)
        old_pid = guard._engine._proc.pid
        r3 = guard.recognize(b"p3", 72)  # 第 3 请求 → 老 worker 死 → invalidate+重建+重试
        c.check("worker 失效 → invalidate+重建成功，同页重试成功",
                len(r3.lines) == 1 and r3.lines[0].text == "HELLO")
        c.check("重建 → 真实换新 worker 进程",
                guard._engine._proc.pid != old_pid)
        c.check("新引擎重新入缓存（后续文件直接复用健康 worker）",
                ocr_mod._MODE_ENGINE_CACHE.get(app_config.OCR_MODE_GPU_V6)
                is guard._engine)
        c.check("name 透传不变", guard.name == "PP-OCRv6-ONNX-medium(GPU-EXT)")
    finally:
        ocr_mod.discover_external_v6_gpu_env = real_discover
        ocr_mod.ExternalV6GpuEngine._resolve_worker_path = real_worker_path
        ocr_mod.invalidate_engine_cache()
        ocr_mod.reset_v6_gpu_env_cache()
        try:
            import os as _os
            _os.rmdir(str(junction_dir))  # 拆 junction（目标目录不受影响）
        except OSError:
            pass


def test_gpu_v6_resilient_fallback(c: _Case):
    """失效联动②回退支（fake worker 子进程级）：worker 断连 → invalidate →
    重建的 worker 仍即死 → 既有回退语义（CPU v6-small）不崩。"""
    import app.ocr_engine as ocr_mod

    real_discover = ocr_mod.discover_external_v6_gpu_env
    real_worker_path = ocr_mod.ExternalV6GpuEngine._resolve_worker_path
    real_cpu_fb = ocr_mod.create_cpu_fallback_engine
    workdir = Path(tempfile.mkdtemp(prefix="_v6fb_"))
    worker = workdir / "die_worker.py"
    worker.write_text(_FAKE_WORKER, encoding="utf-8")
    tree = _fake_v6_env_tree(workdir)
    if tree is None:  # 现场防御：junction 不可用（非 Windows 等）→ 跳过
        c.check("重建仍失败 → 回退 CPU 不崩", True, "（跳过）")
        return
    fake_python, models, junction_dir = tree

    class _FakeCpuEng:
        name = "fake-cpu-v6-small"

        def __init__(self):
            self.calls = []

        def recognize(self, image_bytes, dpi):
            self.calls.append((image_bytes, dpi))
            out = OCRPageResult()
            out.lines.append(OCRLine("CPU-RESCUE", (0.0, 0.0, 10.0, 10.0), 0.9))
            return out

    fake_cpu = _FakeCpuEng()
    try:
        ocr_mod.discover_external_v6_gpu_env = _FakeDiscover(python_exe=str(fake_python))
        ocr_mod.ExternalV6GpuEngine._resolve_worker_path = staticmethod(lambda: worker)
        ocr_mod.create_cpu_fallback_engine = lambda: fake_cpu
        ocr_mod.invalidate_engine_cache()
        logs = []
        guard = ocr_mod.GpuV6ResilientEngine(app_config.OCR_MODE_GPU_V6,
                                             report_log=logs.append)
        res = guard.recognize(b"die", 72)
        c.check("重建仍失败 → 回退 CPU 不崩（同页结果由 CPU 兜底产出）",
                len(res.lines) == 1 and res.lines[0].text == "CPU-RESCUE")
        c.check("兜底引擎收到同页重试", fake_cpu.calls == [(b"die", 72)])
        c.check("守卫当前引擎=CPU 兜底（name 透传切换）",
                guard.name == "fake-cpu-v6-small")
        c.check("日志含 worker 失效与回退人话",
                any("worker 失效" in m for m in logs)
                and any("已回退 CPU" in m and "v6-small" in m for m in logs),
                f"logs={logs}")
        res2 = guard.recognize(b"again", 72)
        c.check("回退后后续识别直走 CPU（不再触 dead worker）",
                res2.lines[0].text == "CPU-RESCUE" and len(fake_cpu.calls) == 2)
        c.check("重建失败 → gpu_v6 锁键不重新入缓存（下文件 getter 重试构造）",
                app_config.OCR_MODE_GPU_V6 not in ocr_mod._MODE_ENGINE_CACHE)
    finally:
        ocr_mod.discover_external_v6_gpu_env = real_discover
        ocr_mod.ExternalV6GpuEngine._resolve_worker_path = real_worker_path
        ocr_mod.create_cpu_fallback_engine = real_cpu_fb
        ocr_mod.invalidate_engine_cache()
        ocr_mod.reset_v6_gpu_env_cache()
        try:
            import os as _os
            _os.rmdir(str(junction_dir))  # 拆 junction（目标目录不受影响）
        except OSError:
            pass


def test_worker_parse_lines(c: _Case):
    """worker 侧解析函数（numpy 转换/双格式/坏项跳过）——直接函数级验证。"""
    import app.v6_gpu_worker as worker

    class _Arr:
        """最小 ndarray 替身：支持 tolist。"""

        def __init__(self, data):
            self._data = data

        def tolist(self):
            return self._data

    class _NpScalar:
        def __init__(self, v):
            self._v = v

        def item(self):
            return self._v

    triple = worker._parse_result_lines([[_Arr([[0, 0], [10, 0], [10, 10], [0, 10]]),
                                          "HELLO", _NpScalar(0.9)]])
    c.check("worker 解析 triple 格式",
            triple == [{"text": "HELLO", "poly": [[0, 0], [10, 0], [10, 10], [0, 10]],
                        "score": 0.9}])
    nested = worker._parse_result_lines([(_Arr([[0, 0], [1, 1]]), ("签名", 0.5))])
    c.check("worker 解析嵌套格式", nested[0]["text"] == "签名" and nested[0]["score"] == 0.5)
    bad = worker._parse_result_lines(["garbage", None, [_Arr([[1, 1], [2, 2]]), 123, "oops"]])
    c.check("worker 坏项跳过不崩", bad == [])


# ---------------------------------------------------------------------------

def run():
    print("== OCR 模式选择器与外部 v6-GPU 环境接口测试 ==")
    c = _Case()
    test_mode_constants(c)
    test_resolve_ocr_mode(c)
    test_ocr_mode_roundtrip(c)
    test_discover_v6_gpu_env(c)
    test_create_engine_for_mode(c)
    test_make_ocr_getter_modes(c)
    test_external_v6_engine_protocol(c)
    test_mode_engine_cache(c)
    test_invalidate_engine_cache_gpu_v6(c)
    test_gpu_v6_resilient_rebuild(c)
    test_gpu_v6_resilient_fallback(c)
    test_worker_parse_lines(c)
    print(f"\n共 {c.total} 条用例，失败 {len(c.failed)} 条")
    return not c.failed


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
