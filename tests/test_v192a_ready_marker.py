# -*- coding: utf-8 -*-
"""v1.9.2a discover_external_v6_gpu_env 就绪标记（.ready）加验场景矩阵。

契约（先组件后标记，两级人话不得混用）：
- 缺组件级（既有逻辑不变）：python.exe / onnxruntime / CUDA EP / rapidocr
  缺什么报什么——此路径标记状态无关；
- 未完成安装级（新增）：组件全过但 .ready 缺失 / JSON 不可解析（损坏）/
  必要字段缺失或不合法 → 统一人话「外部环境未完成安装（缺就绪标记），
  请重跑 setup_v6_gpu_env.bat」+ 括号附具体原因（缺标记/标记损坏/标记字段不全）；
- ExternalV6GpuEngine 构造时人话 reason 随 MissingDependencyError 透传
  （消费层回退 CPU 行为不变，本文件只断言消息面）。

探针手法：tmp 伪造环境骨架 + probe 注入（同 tests/test_ocr_mode.py 先例）；
引擎消息面用例 monkeypatch ocr_mod._probe_v6_gpu_env_subprocess 走真实
discover 全流程（标记校验读真实 tmp 文件）。

运行：python tests/test_v192a_ready_marker.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ocr_engine import (ExternalV6GpuEngine, MissingDependencyError,
                            discover_external_v6_gpu_env, reset_v6_gpu_env_cache)
import app.ocr_engine as ocr_mod


_READY_HEAD = "外部环境未完成安装（缺就绪标记），请重跑 setup_v6_gpu_env.bat"

_VALID_READY = {"ort_version": "1.20.2", "cuda_ep": True,
                "rapidocr_version": "1.4.4",
                "created_at": "2026-09-09T12:00:00", "bat_version": "1.9.2a"}


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

    def expect_mde(self, name, fn, keyword="", absent=""):
        self.total += 1
        try:
            fn()
        except MissingDependencyError as e:
            msg = str(e)
            if keyword and keyword not in msg:
                self.failed.append(name)
                print(f"  FAIL {name} 错误信息缺少关键词：{keyword}（got：{msg}）")
            elif absent and absent in msg:
                self.failed.append(name)
                print(f"  FAIL {name} 错误信息不应出现：{absent}（got：{msg}）")
            else:
                print(f"  PASS {name}")
        except Exception as e:  # noqa: BLE001
            self.failed.append(name)
            print(f"  FAIL {name} 错误类型 {type(e).__name__}：{e}")
        else:
            self.failed.append(name)
            print(f"  FAIL {name} 未抛 MissingDependencyError")


def _probe_all_pass(python_exe):
    """组件级全过的 fake 探针结果（版本与钉版一致）。"""
    return {"ort_version": "1.20.2", "rapidocr_version": "1.4.4",
            "has_cuda_ep": True, "ort_error": "", "rapidocr_error": ""}


def _probe_no_cuda(python_exe):
    """组件级缺 CUDA EP 的 fake 探针结果（其余全过）。"""
    return {"ort_version": "1.20.2", "rapidocr_version": "1.4.4",
            "has_cuda_ep": False, "ort_error": "", "rapidocr_error": ""}


def _make_env_skeleton(prefix="_v192a_"):
    """tmp 伪造 venv 骨架：Scripts/python.exe 占位（fake 探针不会真执行它）。"""
    root = Path(tempfile.mkdtemp(prefix=prefix))
    (root / "Scripts").mkdir()
    (root / "Scripts" / "python.exe").write_bytes(b"")
    return root


def _write_ready(root: Path, text=None, data=None, raw=None) -> Path:
    """写 .ready：data=dict → JSON；text/raw 非 None → 原样字节（损坏用）。"""
    marker = root / ".ready"
    if data is not None:
        marker.write_text(json.dumps(data), encoding="utf-8")
    elif text is not None:
        marker.write_text(text, encoding="utf-8")
    elif raw is not None:
        marker.write_bytes(raw)
    return marker


def _joined_missing(p):
    return "\n".join(p.missing)


# ---------------------------------------------------------------------------
# 场景 1：组件全过 + .ready 有效 JSON 全字段 → ok=True
# ---------------------------------------------------------------------------

def test_s1_all_pass_with_valid_marker(c: _Case):
    root = _make_env_skeleton()
    _write_ready(root, data=dict(_VALID_READY))
    p = discover_external_v6_gpu_env(env_dir=root, probe=_probe_all_pass)
    c.check("S1 组件全过+有效标记 → ok=True", p.ok is True, f"missing={p.missing}")
    c.check("S1 ready_ok=True / ready_reason 为空",
            p.ready_ok is True and p.ready_reason == "",
            f"ready_ok={p.ready_ok} reason={p.ready_reason!r}")
    c.check("S1 warnings 为空（版本与钉版一致）", p.warnings == [], f"got {p.warnings}")
    c.check("S1 missing 为空", p.missing == [])


# ---------------------------------------------------------------------------
# 场景 2：组件全过 + 无 .ready → 未完成安装级（缺标记）
# ---------------------------------------------------------------------------

def test_s2_all_pass_without_marker(c: _Case):
    root = _make_env_skeleton()   # 不写 .ready
    p = discover_external_v6_gpu_env(env_dir=root, probe=_probe_all_pass)
    c.check("S2 组件全过+无标记 → ok=False", p.ok is False)
    c.check("S2 reason 含契约原文「未完成安装（缺就绪标记）…重跑 setup_v6_gpu_env.bat」",
            _READY_HEAD in _joined_missing(p), f"missing={p.missing}")
    c.check("S2 具体原因含「缺标记」", "（缺标记：" in _joined_missing(p),
            f"missing={p.missing}")
    c.check("S2 ready_ok=False 且 ready_reason 同源透传",
            p.ready_ok is False and p.ready_reason == p.missing[-1],
            f"ready_ok={p.ready_ok} reason={p.ready_reason!r}")
    c.check("S2 不得出现缺组件级字样（两级不混）",
            "不可导入" not in _joined_missing(p)
            and "CUDAExecutionProvider" not in _joined_missing(p))


# ---------------------------------------------------------------------------
# 场景 3：半成品（组件缺 CUDA EP）+ 无标记 → 缺组件级，不得出现「未就绪」字样
# ---------------------------------------------------------------------------

def test_s3_component_missing_priority(c: _Case):
    root = _make_env_skeleton()   # 组件缺 CUDA EP 且无标记：先组件后标记
    p = discover_external_v6_gpu_env(env_dir=root, probe=_probe_no_cuda)
    c.check("S3 组件缺 CUDA EP → ok=False", p.ok is False)
    c.check("S3 reason 为缺组件级（CUDAExecutionProvider 逐项）",
            "CUDAExecutionProvider" in _joined_missing(p), f"missing={p.missing}")
    c.check("S3 不得出现「未就绪」字样（两级不混断言）",
            "未就绪" not in _joined_missing(p), f"missing={p.missing}")
    c.check("S3 不得出现未完成安装级人话",
            _READY_HEAD not in _joined_missing(p)
            and "标记" not in _joined_missing(p), f"missing={p.missing}")
    c.check("S3 ready_ok=None（标记未验：组件级未过，标记状态无关）",
            p.ready_ok is None and p.ready_reason == "",
            f"ready_ok={p.ready_ok} reason={p.ready_reason!r}")


# ---------------------------------------------------------------------------
# 场景 4：标记 JSON 损坏 → 未完成安装级（标记损坏）
# ---------------------------------------------------------------------------

def test_s4_broken_marker(c: _Case):
    cases = [
        ("非 JSON 文本", dict(text="{这不是 JSON")),
        ("非法 UTF-8 字节", dict(raw=b"\xff\xfe{\x00broken")),
        ("JSON 顶层非对象（数组）", dict(text="[1, 2, 3]")),
    ]
    for label, kw in cases:
        root = _make_env_skeleton()
        _write_ready(root, **kw)
        p = discover_external_v6_gpu_env(env_dir=root, probe=_probe_all_pass)
        joined = _joined_missing(p)
        c.check(f"S4 标记损坏（{label}）→ ok=False", p.ok is False)
        c.check(f"S4 标记损坏（{label}）→ reason 含「未完成安装（缺就绪标记）」",
                _READY_HEAD in joined, f"missing={p.missing}")
        c.check(f"S4 标记损坏（{label}）→ 具体原因含「标记损坏」",
                "（标记损坏：" in joined, f"missing={p.missing}")
        c.check(f"S4 标记损坏（{label}）→ ready_ok=False",
                p.ready_ok is False)


# ---------------------------------------------------------------------------
# 场景 5：标记字段不全 → 未完成安装级（标记字段不全）
# ---------------------------------------------------------------------------

def test_s5_incomplete_marker(c: _Case):
    cases = [
        ("cuda_ep=false", dict(_VALID_READY, cuda_ep=False)),
        ("cuda_ep 缺失", {k: v for k, v in _VALID_READY.items() if k != "cuda_ep"}),
        ("缺 created_at", {k: v for k, v in _VALID_READY.items() if k != "created_at"}),
        ("缺 bat_version", {k: v for k, v in _VALID_READY.items() if k != "bat_version"}),
        ("ort_version 非字符串", dict(_VALID_READY, ort_version=1.20)),
        ("rapidocr_version 空串", dict(_VALID_READY, rapidocr_version="")),
    ]
    for label, data in cases:
        root = _make_env_skeleton()
        _write_ready(root, data=data)
        p = discover_external_v6_gpu_env(env_dir=root, probe=_probe_all_pass)
        joined = _joined_missing(p)
        c.check(f"S5 字段不全（{label}）→ ok=False", p.ok is False)
        c.check(f"S5 字段不全（{label}）→ reason 含「未完成安装（缺就绪标记）」",
                _READY_HEAD in joined, f"missing={p.missing}")
        c.check(f"S5 字段不全（{label}）→ 具体原因含「标记字段不全」",
                "（标记字段不全：" in joined, f"missing={p.missing}")
        c.check(f"S5 字段不全（{label}）→ 不得误报「标记损坏」",
                "标记损坏" not in joined)
        c.check(f"S5 字段不全（{label}）→ ready_ok=False 且 reason 同源",
                p.ready_ok is False and p.ready_reason == p.missing[-1])


# ---------------------------------------------------------------------------
# 场景 6：各 ok=False 场景 → ExternalV6GpuEngine 构造抛 MDE 且消息携带 reason
#（monkeypatch 模块级探针走真实 discover 全流程，env_dir 显式指向 tmp）
# ---------------------------------------------------------------------------

def test_s6_engine_message_face(c: _Case):
    real_probe = ocr_mod._probe_v6_gpu_env_subprocess
    try:
        ocr_mod._probe_v6_gpu_env_subprocess = _probe_all_pass

        root = _make_env_skeleton()   # 组件全过 + 无标记
        c.expect_mde("S6 无标记 → 引擎 MDE 消息携带未完成安装级 reason",
                     lambda: ExternalV6GpuEngine(env_dir=str(root)),
                     keyword=_READY_HEAD)
        p = discover_external_v6_gpu_env(env_dir=root, probe=_probe_all_pass)
        c.expect_mde("S6 无标记 → 引擎消息与 probe.ready_reason 同源",
                     lambda: ExternalV6GpuEngine(env_dir=str(root)),
                     keyword=p.ready_reason)

        root = _make_env_skeleton()   # 组件全过 + 标记损坏
        _write_ready(root, text="not-json{{{")
        c.expect_mde("S6 标记损坏 → 引擎 MDE 消息携带「标记损坏」",
                     lambda: ExternalV6GpuEngine(env_dir=str(root)),
                     keyword="标记损坏")

        root = _make_env_skeleton()   # 组件全过 + 字段不全
        _write_ready(root, data=dict(_VALID_READY, cuda_ep=False))
        c.expect_mde("S6 字段不全 → 引擎 MDE 消息携带「标记字段不全」",
                     lambda: ExternalV6GpuEngine(env_dir=str(root)),
                     keyword="标记字段不全")

        # 两级不混（消息面）：组件级失败时引擎消息只报缺组件，不报未完成安装
        def _probe_broken(python_exe):
            return {"ort_version": "", "rapidocr_version": "",
                    "has_cuda_ep": False, "ort_error": "[ImportError] no ort",
                    "rapidocr_error": ""}

        root = _make_env_skeleton()   # 组件缺 ort + 无标记
        ocr_mod._probe_v6_gpu_env_subprocess = _probe_broken
        c.expect_mde("S6 组件级失败 → 引擎消息只报缺组件（两级不混）",
                     lambda: ExternalV6GpuEngine(env_dir=str(root)),
                     keyword="onnxruntime 不可导入", absent=_READY_HEAD)
    finally:
        ocr_mod._probe_v6_gpu_env_subprocess = real_probe
        reset_v6_gpu_env_cache()


# ---------------------------------------------------------------------------
# 场景 7：版本钉版不符 + 标记有效 → ok=True + warnings 非空（既有语义回归）
# ---------------------------------------------------------------------------

def test_s7_version_mismatch_still_warning_only(c: _Case):
    root = _make_env_skeleton()
    _write_ready(root, data=dict(_VALID_READY))

    def _probe_ver(python_exe):
        return {"ort_version": "1.19.0", "rapidocr_version": "1.5.0",
                "has_cuda_ep": True, "ort_error": "", "rapidocr_error": ""}

    p = discover_external_v6_gpu_env(env_dir=root, probe=_probe_ver)
    c.check("S7 版本不符+标记有效 → ok=True（仅警告不阻断）", p.ok is True,
            f"missing={p.missing}")
    c.check("S7 warnings 两条（ort/rapidocr 各一）", len(p.warnings) == 2,
            f"got {p.warnings}")
    c.check("S7 ready_ok=True", p.ready_ok is True)
    c.check("S7 版本照常透传", p.ort_version == "1.19.0"
            and p.rapidocr_version == "1.5.0")


# ---------------------------------------------------------------------------

def run():
    print("== v1.9.2a discover 就绪标记（.ready）场景矩阵 ==")
    c = _Case()
    reset_v6_gpu_env_cache()
    test_s1_all_pass_with_valid_marker(c)
    test_s2_all_pass_without_marker(c)
    test_s3_component_missing_priority(c)
    test_s4_broken_marker(c)
    test_s5_incomplete_marker(c)
    test_s6_engine_message_face(c)
    test_s7_version_mismatch_still_warning_only(c)
    reset_v6_gpu_env_cache()
    print(f"\n共 {c.total} 条用例，失败 {len(c.failed)} 条")
    return not c.failed


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
