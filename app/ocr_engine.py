# -*- coding: utf-8 -*-
"""OCR 引擎接口（脱敏引擎层）。

默认实现：PP-OCRv5（PaddleOCR）。
替换/扩展：实现 BaseOCREngine 接口后调用 register_ocr_engine() 注册，
app/handlers/pdf_handler.py 无需任何改动。

接口约定：
- recognize(image_bytes, dpi) -> OCRPageResult
- OCRLine.bbox 为 PDF 点坐标（72dpi 基准），像素坐标由引擎内部按 dpi 换算。

离线注意：PaddleOCR 首次初始化可能联网下载模型文件，正式使用前请提前把
模型放到本地缓存目录，确保运行阶段完全离线（TODO: 支持指定离线模型目录）。
"""
from __future__ import annotations

import atexit
import base64
import json
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .utils import MissingDependencyError, OcrPageError


@dataclass
class OCRLine:
    """一条 OCR 识别结果。"""
    text: str
    bbox: tuple            # (x0, y0, x1, y1)，PDF 点坐标
    confidence: float = 1.0


@dataclass
class OCRPageResult:
    """一页 OCR 识别结果。"""
    lines: list = field(default_factory=list)        # 行级结果
    table_cells: list = field(default_factory=list)  # 表格单元格级结果（未接入结构识别时为空）


class BaseOCREngine:
    """OCR 引擎抽象接口（扩展点：替换为 RapidOCR / 商用 SDK 等）。"""

    name = "base"

    def recognize(self, image_bytes: bytes, dpi: int) -> OCRPageResult:
        raise NotImplementedError


class BaseTableRecognizer:
    """表格结构识别抽象接口（扩展点）：为表格区域提供单元格级 bbox。"""

    name = "base-table"

    def recognize(self, image_bytes: bytes, dpi: int) -> list:
        """返回 [OCRLine(...)]，bbox 为单元格坐标（PDF 点）。"""
        raise NotImplementedError


def detect_device() -> str:
    """检测可用推理设备：有可用 NVIDIA GPU（且 paddle 为 GPU 构建）返回 "gpu"，否则 "cpu"。"""
    try:
        import paddle
        if paddle.device.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0:
            return "gpu"
    except Exception:
        pass
    return "cpu"


def detect_onnx_device() -> str:
    """ONNX Runtime 推理设备：可用 providers 含 CUDAExecutionProvider → "gpu"。

    与 detect_device()（paddle 语义，v5 用）分离——exe 的 CPU 闭包不含
    onnxruntime-gpu，此处自然回退 "cpu"。
    """
    try:
        import onnxruntime
        if "CUDAExecutionProvider" in onnxruntime.get_available_providers():
            return "gpu"
    except Exception:
        pass
    return "cpu"


# ---------------------------------------------------------------------------
# 外部 Paddle 环境自动接入（exe 版专用）
# ---------------------------------------------------------------------------
# exe 为控制体积未内置 paddle；启动时调用本函数探测本机已安装的 Anaconda/
# Miniconda 环境（要求 Python 3.11 ABI 且装有 paddlepaddle + paddleocr），
# 命中则把其 site-packages 挂到 sys.path 并注册 DLL 目录，OCR 即刻可用。
# 可用环境变量 FINMASK_EXTERNAL_SITE 手动指定环境根目录（如 D:\Anaconda3）。

_EXTERNAL_SITE_ENV = "FINMASK_EXTERNAL_SITE"
_external_env_result = None


def discover_external_paddle_env() -> list:
    """探测并接入外部 Paddle 环境；返回命中的环境根目录列表（通常 0/1 个）。结果缓存。"""
    global _external_env_result
    if _external_env_result is not None:
        return _external_env_result
    _external_env_result = []
    if sys.version_info[:2] != (3, 11):
        return _external_env_result  # 打包解释器非 3.11 时外部 cp311 扩展不可用

    import os
    import sys as _sys

    roots = []
    env = os.environ.get(_EXTERNAL_SITE_ENV, "").strip()
    if env:
        roots.append(os.path.abspath(env))
    for hard in (r"D:\Anaconda3", r"C:\Anaconda3", r"C:\ProgramData\Anaconda3",
                 r"D:\Miniconda3", r"C:\Miniconda3"):
        roots.append(hard)
    for base in ("anaconda3", "miniconda3"):
        roots.append(os.path.join(os.path.expanduser("~"), base))

    for root in roots:
        site_dir = os.path.join(root, "Lib", "site-packages")
        try:
            has_stack = (os.path.isdir(os.path.join(site_dir, "paddleocr"))
                         and os.path.isdir(os.path.join(site_dir, "paddle"))
                         and os.path.isfile(os.path.join(root, "python311.dll")))
        except OSError:
            continue
        if not has_stack:
            continue
        if site_dir not in _sys.path:
            _sys.path.append(site_dir)  # 追加到尾部：内置模块优先，外部只补缺
        libbin = os.path.join(root, "Library", "bin")
        if os.path.isdir(libbin):
            try:
                os.add_dll_directory(libbin)
            except OSError:
                pass
        # 关键：冻结程序里 site.USER_SITE 为 None，而 paddle 导入时用它定位
        # 自带的 paddle/libs DLL 目录（paddle/base/core.py: set_paddle_lib_path）。
        # 指向外部 site-packages 后，该路径恰好命中外部环境的 paddle/libs。
        try:
            import site as _site_mod
            if not getattr(_site_mod, "USER_SITE", None):
                _site_mod.USER_SITE = site_dir
        except Exception:
            pass
        _external_env_result.append(root)
        break  # 只接入第一个命中的环境
    return _external_env_result


class PPOCRv5Engine(BaseOCREngine):
    """PP-OCRv5 引擎封装（PaddleOCR 3.x）。

    要求输出：文本内容、行级 bbox、表格结构、单元格级 bbox。
    TODO: PaddleOCR 2.x / 3.x 的 API 与返回结构差异较大，接入时请按本机
          安装版本核对 _predict_lines() 的解析逻辑。
    """

    name = "PP-OCRv5"

    def __init__(self, lang: str = "ch", device: str = "auto", model_kind: str = "auto"):
        import os
        # 模型已缓存在本地时跳过 paddlex 的模型源连通性检查（离线环境必备）
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        from paddleocr import PaddleOCR  # 延迟导入：缺失时由 create_ocr_engine 转为友好提示

        if device == "auto":
            device = detect_device()  # 检测到可用 NVIDIA GPU 时自动启用 GPU 推理
        if model_kind == "auto":
            # GPU 上用 server 组（精度更高，且无 CPU 端的原生崩溃问题）；
            # CPU 用 mobile 组（实测稳定，见下注）。
            model_kind = "server" if device == "gpu" else "mobile"

        kwargs = dict(
            device=device,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            # Paddle 3.x 在 Windows CPU 上存在 oneDNN 指令转换崩溃的已知问题，
            # 关闭 MKLDNN 保证可运行（GPU 推理不走 MKLDNN，此开关无副作用）。
            enable_mkldnn=False,
        )
        if model_kind == "mobile":
            # mobile 组：CPU 推理快且实测稳定（lang/ocr_version 由模型名决定，
            # 显式传入反而触发 paddlex 的忽略警告）。
            # 注：PP-OCRv5 server 检测模型在本机 Windows CPU（paddle 3.0.0 / 3.3.1 实测）
            # 对大图（整页 200dpi 渲染）存在原生段错误（access violation），
            # 故 CPU 默认使用 mobile 组。
            kwargs["text_detection_model_name"] = "PP-OCRv5_mobile_det"
            kwargs["text_recognition_model_name"] = "PP-OCRv5_mobile_rec"
        else:  # server
            if model_kind != "server":
                raise ValueError(f"未知模型组: {model_kind}（可选 mobile / server）")
            kwargs["ocr_version"] = "PP-OCRv5"
            kwargs["lang"] = lang
        self._ocr = PaddleOCR(**kwargs)
        self.name = f"PP-OCRv5-{model_kind}({device})"
        # TODO: 表格结构识别（PP-StructureV3）接入后，recognize() 中填充
        #       OCRPageResult.table_cells（单元格级 bbox），供 pdf_handler
        #       对表格区域做精准遮罩。
        self._table = None

    # ---- 对外接口 -------------------------------------------------------

    def recognize(self, image_bytes: bytes, dpi: int) -> OCRPageResult:
        img = self._decode(image_bytes)
        raw_lines = self._predict_lines(img)
        scale = 72.0 / float(dpi)  # 像素 -> PDF 点
        result = OCRPageResult()
        for text, score, poly in raw_lines:
            x0, y0, x1, y1 = self._poly_to_bbox(poly)
            result.lines.append(
                OCRLine(text=text,
                        bbox=(x0 * scale, y0 * scale, x1 * scale, y1 * scale),
                        confidence=score)
            )
        # TODO: 若 self._table 可用 -> result.table_cells = 表格单元格级结果
        return result

    # ---- 内部工具 -------------------------------------------------------

    def _call_predict(self, img):
        try:
            return self._ocr.predict(img)  # PaddleOCR >= 3.0
        except AttributeError:
            return self._ocr.ocr(img)      # PaddleOCR 2.x

    @staticmethod
    def _release_gpu_cache() -> bool:
        """尝试清空 GPU 显存缓存（成功返回 True，无 CUDA 环境返回 False）。"""
        try:
            import paddle
            if not paddle.device.is_compiled_with_cuda():
                return False
            paddle.device.cuda.empty_cache()
            return True
        except Exception:
            return False

    @staticmethod
    def _decode(image_bytes: bytes):
        """PNG bytes -> BGR ndarray（优先 OpenCV，其次 Pillow）。"""
        try:
            import cv2
            import numpy as np
            return cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
        except ImportError:
            pass
        try:
            import io
            import numpy as np
            from PIL import Image
            return np.asarray(Image.open(io.BytesIO(image_bytes)).convert("RGB"))[:, :, ::-1]
        except ImportError as e:
            raise MissingDependencyError(
                "OCR 运行需要 numpy（opencv-python 或 pillow 任一）：pip install numpy opencv-python"
            ) from e

    def _predict_lines(self, img):
        """调用 PaddleOCR 并归一化为 [(text, score, poly)] 列表。

        v1.6.9：GPU 长批处理显存累积会抛 OOM——清空 CUDA 缓存后重试一次
        （实测批内大文件整页 OCR 之后，后续文件的图片识别可能因显存
        不足静默失败，导致内嵌截图全部漏遮）。
        """
        try:
            results = self._call_predict(img)
        except Exception:
            if not self._release_gpu_cache():
                raise
            results = self._call_predict(img)  # 清缓存后重试一次
        lines = []
        for res in results or []:
            data = res if isinstance(res, dict) else getattr(res, "res", None)
            if isinstance(data, dict) and "rec_texts" in data:  # 3.x Result
                polys = data.get("rec_polys") or data.get("rec_boxes") or data.get("dt_polys") or []
                for text, score, poly in zip(data.get("rec_texts", []),
                                             data.get("rec_scores", []), polys):
                    lines.append((text, float(score), poly))
            elif isinstance(res, list):  # 2.x: [[box, (text, score)], ...]
                for item in res:
                    try:
                        box, (text, score) = item[0], item[1]
                        lines.append((text, float(score), box))
                    except Exception:
                        continue
        return lines

    @staticmethod
    def _poly_to_bbox(poly):
        """四点多边形 / [x0,y0,x1,y1] -> (x0, y0, x1, y1)。"""
        try:
            import numpy as np
            flat = np.asarray(poly, dtype=float).reshape(-1).tolist()
        except Exception:
            flat = [float(v) for v in poly]
        if len(flat) == 4:
            x0, y0, x1, y1 = flat[0], flat[1], flat[2], flat[3]
        else:
            xs, ys = flat[0::2], flat[1::2]
            x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        return float(x0), float(y0), float(x1), float(y1)


class OnnxV6Engine(BaseOCREngine):
    """PP-OCRv6 引擎封装（RapidOCR + ONNX Runtime，全程不依赖 Paddle）。

    用途：绕开 paddlepaddle 3.0.0 无法推理 v6（PIR）的版本阻断——模型由
    RapidAI 官方预转换为 ONNX，识别字典内嵌于 rec 模型元数据（character），
    放入 models/onnx_v6/ 即用。与 PPOCRv5Engine 输出同构（bbox 为 PDF 点）。
    """

    name = "PP-OCRv6-ONNX"

    _TIERS = ("tiny", "small", "medium")  # 与 RapidAI 发布档位一致

    def __init__(self, tier: str = "small", model_dir: str = None,
                 intra_op_num_threads: int = None, use_gpu: bool = None):
        if tier not in self._TIERS:
            raise ValueError(f"未知模型档位：{tier}（可选：{'/'.join(self._TIERS)}）")
        base = self._resolve_model_dir(model_dir)
        det = base / f"PP-OCRv6_det_{tier}.onnx"
        rec = base / f"PP-OCRv6_rec_{tier}.onnx"
        missing = [str(p) for p in (det, rec) if not p.is_file()]
        if missing:
            raise FileNotFoundError("缺少 PP-OCRv6 ONNX 模型：" + "、".join(missing))
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as e:
            raise MissingDependencyError(
                "PP-OCRv6（ONNX）推理需要 RapidOCR 组件："
                "pip install rapidocr-onnxruntime onnxruntime"
            ) from e
        # v1.8.0：use_gpu=None 按可用 providers 自动；medium 仅限 GPU
        # （8GB 工程机实测峰值 2.5GB），CPU 闭包无 CUDA EP 自然为 False。
        if use_gpu is None:
            use_gpu = detect_onnx_device() == "gpu"
        kwargs = dict(det_model_path=str(det), rec_model_path=str(rec),
                      cls_model_path=None, use_cls=False,  # 单据页面恒为正向，方向分类无收益
                      det_use_cuda=use_gpu, rec_use_cuda=use_gpu)
        if intra_op_num_threads:
            kwargs["intra_op_num_threads"] = intra_op_num_threads
        try:
            self._ocr = RapidOCR(**kwargs)
        except Exception:
            if not use_gpu:
                raise
            # GPU EP 初始化失败（驱动/显存异常）→ 回退 CPU 重试一次（留痕），
            # 引擎可用性优先于速度（P0：引擎崩溃=整批失败）
            import sys
            sys.stderr.write("[OnnxV6Engine] GPU 初始化失败，回退 CPU 重试\n")
            kwargs["det_use_cuda"] = kwargs["rec_use_cuda"] = False
            use_gpu = False
            self._ocr = RapidOCR(**kwargs)
        self.name = f"PP-OCRv6-ONNX-{tier}({'gpu' if use_gpu else 'cpu'})"

    def recognize(self, image_bytes: bytes, dpi: int) -> OCRPageResult:
        result, _elapse = self._ocr(image_bytes)  # 支持 bytes 直入，无外部解码依赖
        scale = 72.0 / float(dpi)
        out = OCRPageResult()
        for item in result or []:
            # rapidocr_onnxruntime 1.4.x 返回 [poly, text, score] 三元组；
            # 兼容 (poly, (text, score)) 嵌套形式（rapidocr 3.x 风格）。
            try:
                if len(item) == 3 and not isinstance(item[1], (tuple, list)):
                    poly, text, score = item[0], item[1], item[2]
                else:
                    poly, (text, score) = item[0], item[1]
            except Exception:
                continue
            x0, y0, x1, y1 = PPOCRv5Engine._poly_to_bbox(poly)
            out.lines.append(
                OCRLine(text=text,
                        bbox=(x0 * scale, y0 * scale, x1 * scale, y1 * scale),
                        confidence=float(score))
            )
        return out

    @staticmethod
    def _resolve_model_dir(model_dir: str = None) -> Path:
        """模型目录解析：显式参数 > 环境变量 FINMASK_ONNX_MODELS > 程序目录/models/onnx_v6。"""
        import os
        if model_dir:
            return Path(model_dir)
        env = os.environ.get("FINMASK_ONNX_MODELS", "").strip()
        if env:
            return Path(env)
        if getattr(sys, "frozen", False):
            # PyInstaller 6 onedir：datas 落在 _internal（sys._MEIPASS 指向）；
            # 兼容旧版式（数据在 exe 同级）做双路径回退
            meipass = Path(getattr(sys, "_MEIPASS", ""))
            if meipass and (meipass / "models" / "onnx_v6").is_dir():
                return meipass / "models" / "onnx_v6"
            base = Path(sys.executable).parent
        else:
            base = Path(__file__).resolve().parent.parent
        return base / "models" / "onnx_v6"


class PPStructureTableEngine(BaseTableRecognizer):
    """PP-StructureV3 表格结构识别（TODO: 按 PaddleOCR 文档接入实现）。"""

    name = "PP-StructureV3"

    def __init__(self):
        raise MissingDependencyError(
            "表格结构识别尚未接入：请在 app/ocr_engine.py 中实现 PP-StructureV3 "
            "的加载与 recognize()，使 OCRPageResult.table_cells 返回单元格级 bbox。"
        )

    def recognize(self, image_bytes: bytes, dpi: int) -> list:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 外部 v6-GPU 环境接入（v1.8.2：GPU-v6 模式）
# ---------------------------------------------------------------------------
# exe 主程序只内置 onnxruntime CPU 闭包（体积与稳定性考虑，严禁打入
# onnxruntime-gpu）；GPU-v6 模式把 GPU 推理整体交给 setup_v6_gpu_env.bat
# 创建的独立 Python 环境（venv：onnxruntime-gpu==1.20.2 +
# rapidocr-onnxruntime==1.4.4 + medium 权重），由常驻子进程 worker
# （app/v6_gpu_worker.py）承载，宿主与 worker 以行 JSON 协议通信。
# 环境目录定位：显式参数 > 环境变量 FINMASK_V6_GPU_ENV > 程序目录/v6_gpu_env
# （frozen=exe 同目录，源码=项目根，与 program_data_dir 惯例一致）。

_V6_GPU_ENV_DIRNAME = "v6_gpu_env"
_V6_GPU_ENV_ENVVAR = "FINMASK_V6_GPU_ENV"
_V6_ORT_PIN = "1.20.2"        # onnxruntime-gpu 钉版（不符仅告警不阻断）
_V6_RAPIDOCR_PIN = "1.4.4"    # rapidocr-onnxruntime 钉版（同上）
_V6_PROBE_TIMEOUT = 60.0      # 探测子进程超时
_V6_READY_MARKER = ".ready"   # v1.9.2a 就绪标记：环境根目录下 JSON（setup 成功最后一步写出；
                              # 任一步中断必然无标记 → discover 防呆分级报错）

_v6_gpu_env_result = None     # discover 结果进程内缓存（仿 _external_env_result 惯例）
_V6_GPU_ENGINE_SINGLETON = None  # GPU-v6 引擎进程级单例（worker 常驻，防进程泄漏）


@dataclass
class ExternalEnvProbe:
    """外部环境探测结果（GPU-v6 用）。ok = missing 为空。

    ready_ok：.ready 就绪标记校验结果（v1.9.2a，先组件后标记）——
    None=未验（组件级未过，标记状态无关）；True=标记有效；
    False=标记缺失/损坏/字段不全（人话原因在 ready_reason，并同时
    并入 missing 通道，随 MissingDependencyError 透传到回退日志）。
    """
    ok: bool
    python_exe: str = ""
    missing: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    ort_version: str = ""
    rapidocr_version: str = ""
    has_cuda_ep: bool = False
    ready_ok: bool | None = None
    ready_reason: str = ""


def _v6_gpu_env_base_dir(env_dir=None) -> Path:
    """环境目录解析：显式参数 > FINMASK_V6_GPU_ENV > 程序目录/v6_gpu_env。"""
    import os
    if env_dir:
        return Path(env_dir)
    env = os.environ.get(_V6_GPU_ENV_ENVVAR, "").strip()
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    else:
        base = Path(__file__).resolve().parent.parent
    return base / _V6_GPU_ENV_DIRNAME


# 在外部环境解释器内执行的探测脚本（纯标准库；stdout 末行输出 JSON）。
_V6_PROBE_SCRIPT = r'''
import json
info = {"ort_version": "", "rapidocr_version": "", "has_cuda_ep": False,
        "ort_error": "", "rapidocr_error": ""}
try:
    import onnxruntime
    info["ort_version"] = str(getattr(onnxruntime, "__version__", ""))
    info["has_cuda_ep"] = "CUDAExecutionProvider" in onnxruntime.get_available_providers()
except Exception as e:
    info["ort_error"] = "[%s] %s" % (type(e).__name__, e)
try:
    import rapidocr_onnxruntime
    try:
        from importlib import metadata
        info["rapidocr_version"] = metadata.version("rapidocr-onnxruntime")
    except Exception:
        info["rapidocr_version"] = str(getattr(rapidocr_onnxruntime, "__version__", ""))
except Exception as e:
    info["rapidocr_error"] = "[%s] %s" % (type(e).__name__, e)
print(json.dumps(info))
'''


def _subprocess_no_window_flags() -> int:
    """窗口化 exe 里派生子进程不闪控制台窗口（非 Windows 恒 0）。"""
    import os
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _probe_v6_gpu_env_subprocess(python_exe: str,
                                 timeout: float = _V6_PROBE_TIMEOUT) -> dict:
    """在目标环境解释器内运行探测脚本，返回信息 dict；失败抛 RuntimeError。

    抽成独立函数以便单测注入 fake（零导入污染：宿主进程不 import 外部包）。
    """
    try:
        proc = subprocess.run(
            [str(python_exe), "-c", _V6_PROBE_SCRIPT],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, creationflags=_subprocess_no_window_flags())
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"探测子进程超时（>{int(timeout)}s）") from e
    except OSError as e:
        raise RuntimeError(f"探测子进程无法启动：{e}") from e
    if proc.returncode != 0:
        tail = [ln for ln in (proc.stderr or proc.stdout or "").splitlines() if ln.strip()]
        detail = tail[-1] if tail else f"exit={proc.returncode}"
        raise RuntimeError(f"探测子进程退出码 {proc.returncode}：{detail}")
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise RuntimeError("探测子进程输出无法解析（未找到 JSON 行）")


def reset_v6_gpu_env_cache() -> None:
    """清空 discover_external_v6_gpu_env 的进程内缓存（测试/重探测用）。"""
    global _v6_gpu_env_result
    _v6_gpu_env_result = None


def _validate_v6_ready_marker(env_root: Path) -> tuple:
    """校验就绪标记 `<env_root>/.ready`（v1.9.2a）。返回 (ready_ok, 失败原因)。

    仅在组件探针全过后调用（先组件后标记，两级人话不得混用）。
    三种失败均为同一段人话开头 + 括号附一行具体原因：
    - 缺标记：文件不存在（setup 任一步中断的必然现场）
    - 标记损坏：文件存在但非 UTF-8 JSON 对象
    - 标记字段不全：JSON 可解析但必要字段缺失/类型不符
      （ort_version/rapidocr_version/created_at/bat_version 须非空字符串，
       cuda_ep 须为布尔 true）
    版本是否与钉版一致不在此校验（版本不符走 warnings 通道，不阻断）。
    """
    head = "外部环境未完成安装（缺就绪标记），请重跑 setup_v6_gpu_env.bat"
    marker = env_root / _V6_READY_MARKER
    if not marker.is_file():
        return False, f"{head}（缺标记：{marker}）"
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return False, f"{head}（标记损坏：{marker} 读取/解析失败：{e}）"
    if not isinstance(data, dict):
        return False, f"{head}（标记损坏：{marker} JSON 顶层应为对象）"
    problems = []
    for key in ("ort_version", "rapidocr_version", "created_at", "bat_version"):
        val = data.get(key)
        if not isinstance(val, str) or not val:
            problems.append(f"{key} 缺失或非字符串")
    if data.get("cuda_ep") is not True:
        problems.append("cuda_ep 缺失或非 true")
    if problems:
        return False, f"{head}（标记字段不全：{'；'.join(problems)}）"
    return True, ""


def discover_external_v6_gpu_env(env_dir=None, probe=None) -> ExternalEnvProbe:
    """探测外部 v6-GPU 环境（探测在子进程完成，宿主零导入污染）。结果缓存。

    缺什么报什么（missing 为中文逐项）；版本与钉版不符仅记 warnings，
    不影响 ok。env_dir 显式传入时绕过缓存（测试/多环境场景）。

    v1.9.2a 防呆分级（先组件后标记，两级人话不得混用）：组件探针
    （python.exe / onnxruntime / CUDA EP / rapidocr）任一不过 → 走既有
    缺组件级逐项报错（标记状态无关）；组件全过才验 `<env_root>/.ready`
    就绪标记，缺失/损坏/字段不全 → ok=False + 「外部环境未完成安装
    （缺就绪标记）」人话 reason（结果落在 ready_ok/ready_reason，并入
    missing 通道透传）。
    """
    global _v6_gpu_env_result
    if env_dir is None and _v6_gpu_env_result is not None:
        return _v6_gpu_env_result
    probe_fn = probe or _probe_v6_gpu_env_subprocess

    missing, warnings = [], []
    python_exe, info = "", {}
    base = _v6_gpu_env_base_dir(env_dir)
    if not base.is_dir():
        missing.append(
            f"未找到 v6 GPU 环境目录：{base}"
            f"（请先在程序目录运行 setup_v6_gpu_env.bat 创建，"
            f"或设置环境变量 {_V6_GPU_ENV_ENVVAR} 指向环境目录）")
    else:
        cand = base / "Scripts" / "python.exe"   # venv 布局优先
        if not cand.is_file():
            cand = base / "python.exe"
        if not cand.is_file():
            missing.append(f"环境目录中未找到 Python 解释器"
                           f"（应有 {base / 'Scripts' / 'python.exe'}）")
        else:
            python_exe = str(cand)
            try:
                info = probe_fn(python_exe) or {}
            except Exception as e:  # noqa: BLE001  探测失败按缺失项留痕
                missing.append(f"环境探测失败：{e}")

    ort_version = str(info.get("ort_version", ""))
    rapidocr_version = str(info.get("rapidocr_version", ""))
    has_cuda_ep = bool(info.get("has_cuda_ep"))
    if info:
        if info.get("ort_error"):
            missing.append(f"外部环境 onnxruntime 不可导入：{info['ort_error']}")
        if info.get("rapidocr_error"):
            missing.append(f"外部环境 rapidocr-onnxruntime 不可导入：{info['rapidocr_error']}")
        if not info.get("ort_error") and not has_cuda_ep:
            missing.append("外部环境 onnxruntime 缺少 CUDAExecutionProvider"
                           "（需安装 onnxruntime-gpu 而非 CPU 版 onnxruntime）")
        # 版本钉版校验：不符仅留痕告警，不阻断（ok 不因此变 False）
        if ort_version and ort_version != _V6_ORT_PIN:
            warnings.append(f"onnxruntime 版本 {ort_version} 与推荐 {_V6_ORT_PIN} "
                            "不一致（未验证组合，建议按 setup_v6_gpu_env.bat 安装）")
        if rapidocr_version and rapidocr_version != _V6_RAPIDOCR_PIN:
            warnings.append(f"rapidocr-onnxruntime 版本 {rapidocr_version} 与推荐 "
                            f"{_V6_RAPIDOCR_PIN} 不一致（未验证组合）")

    # —— v1.9.2a：先组件后标记——组件探针全过才验 .ready（两级人话不混）。
    # 任一组件级 missing 存在时标记状态无关（ready_ok=None=未验）。
    ready_ok, ready_reason = None, ""
    if not missing:
        ready_ok, ready_reason = _validate_v6_ready_marker(base)
        if not ready_ok:
            missing.append(ready_reason)

    result = ExternalEnvProbe(ok=not missing, python_exe=python_exe,
                              missing=missing, warnings=warnings,
                              ort_version=ort_version,
                              rapidocr_version=rapidocr_version,
                              has_cuda_ep=has_cuda_ep,
                              ready_ok=ready_ok, ready_reason=ready_reason)
    if env_dir is None:
        _v6_gpu_env_result = result
    return result


class ExternalV6GpuEngine(BaseOCREngine):
    """外部 v6-GPU 引擎（v1.8.2）：常驻子进程 worker 承载 onnxruntime-gpu 推理。

    生命周期：discover（含 CUDA EP 检查）→ spawn worker → 握手（120s 超时）；
    任何一环失败抛 MissingDependencyError（由消费层统一回退 CPU v6-small）。
    recognize() 的坐标换算与双格式兼容和 OnnxV6Engine 语义逐行一致。
    """

    name = "PP-OCRv6-ONNX-medium(GPU-EXT)"
    HANDSHAKE_TIMEOUT = 120.0

    def __init__(self, env_dir=None, model_dir=None, timeout_sec: float = 300.0):
        probe = discover_external_v6_gpu_env(env_dir)
        if not probe.ok:
            raise MissingDependencyError(
                "GPU-v6 外部环境不可用：" + "；".join(probe.missing))
        self.warnings = list(probe.warnings)
        for w in self.warnings:
            sys.stderr.write(f"[ExternalV6GpuEngine] 警告：{w}\n")

        base = Path(probe.python_exe).parent.parent  # .../Scripts/python.exe → venv 根
        models = Path(model_dir) if model_dir else base / "models" / "onnx_v6"
        det = models / "PP-OCRv6_det_medium.onnx"
        rec = models / "PP-OCRv6_rec_medium.onnx"
        gone = [str(p) for p in (det, rec) if not p.is_file()]
        if gone:
            raise MissingDependencyError(
                "GPU-v6 外部环境缺少 medium 权重：" + "、".join(gone)
                + "（请重新运行 setup_v6_gpu_env.bat）")

        worker = self._resolve_worker_path()
        if not worker.is_file():
            raise MissingDependencyError(
                f"缺少 GPU-v6 worker 组件：{worker}（安装不完整，请重新安装/打包）")

        self._timeout_sec = float(timeout_sec)
        self._req_seq = 0
        self._resp_q = queue.Queue()
        try:
            self._proc = subprocess.Popen(
                [str(probe.python_exe), str(worker), "--models", str(models)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,  # 防管道写满阻塞 worker
                text=True, bufsize=1, encoding="utf-8", errors="replace",
                creationflags=_subprocess_no_window_flags())
        except OSError as e:
            raise MissingDependencyError(f"GPU-v6 worker 进程启动失败：{e}") from e
        threading.Thread(target=self._read_loop, daemon=True,
                         name="v6-gpu-worker-reader").start()

        # —— 握手：等 ready / fatal / EOF ——
        try:
            line = self._resp_q.get(timeout=self.HANDSHAKE_TIMEOUT)
        except queue.Empty:
            self.close()
            raise MissingDependencyError(
                f"GPU-v6 worker 握手超时（>{int(self.HANDSHAKE_TIMEOUT)}s，"
                "首次初始化含 GPU 上下文创建；请确认外部环境与显卡驱动正常）")
        msg = self._parse_json(line)
        if line == "" or msg is None:
            self.close()
            raise MissingDependencyError(
                "GPU-v6 worker 提前退出（握手阶段无有效输出，"
                "常见原因：显存不足或 onnxruntime-gpu 与驱动不匹配）")
        if msg.get("event") == "fatal":
            self.close()
            raise MissingDependencyError(f"GPU-v6 worker 初始化失败：{msg.get('error')}")
        if msg.get("event") != "ready":
            self.close()
            raise MissingDependencyError(
                f"GPU-v6 worker 握手协议异常：{json.dumps(msg, ensure_ascii=True)[:200]}")

    # ---- 对外接口 -------------------------------------------------------

    def recognize(self, image_bytes: bytes, dpi: int) -> OCRPageResult:
        if self._proc is None or self._proc.poll() is not None:
            raise MissingDependencyError(
                "GPU-v6 worker 进程已退出（请重试本批处理；反复出现时检查显卡驱动"
                "与 v6_gpu_env 环境）")
        self._req_seq += 1
        rid = self._req_seq
        payload = {"id": rid, "op": "recognize", "dpi": int(dpi),
                   "image_b64": base64.b64encode(image_bytes).decode("ascii")}
        try:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
        except (OSError, ValueError) as e:
            raise MissingDependencyError(f"GPU-v6 worker 通信失败：{e}") from e
        while True:
            try:
                line = self._resp_q.get(timeout=self._timeout_sec)
            except queue.Empty:
                raise MissingDependencyError(
                    f"GPU-v6 worker 响应超时（>{int(self._timeout_sec)}s）")
            if line == "":  # 读线程 EOF 哨兵：worker 退出
                raise MissingDependencyError("GPU-v6 worker 进程意外退出")
            msg = self._parse_json(line)
            if msg is None or msg.get("id") != rid:
                continue  # 非 JSON 行/串音行丢弃
            break
        if not msg.get("ok"):
            # 稳定体验补项：单页识别
            # 失败≠进程死亡——worker 协议明确「单页失败不退出进程，交宿主
            # 决策」，此前与 EOF/超时共用 MissingDependencyError 会被
            # GpuV6ResilientEngine 误判为进程级失效→杀 worker 重建→同毒
            # 图重试必败→降级 CPU，整批进入抖动循环。改抛 OcrPageError
            # （页级，引擎健康），由 pdf_handler 页级兜底接管。
            raise OcrPageError(f"GPU-v6 单页识别失败：{msg.get('error')}")

        # 坐标换算与双格式兼容：与 OnnxV6Engine.recognize 语义逐行一致
        #（worker 正常回 dict 行；triple/嵌套分支保持线协议向前兼容）。
        scale = 72.0 / float(dpi)
        out = OCRPageResult()
        for item in msg.get("lines") or []:
            try:
                if isinstance(item, dict):
                    poly, text, score = item.get("poly"), item.get("text"), item.get("score", 1.0)
                elif len(item) == 3 and not isinstance(item[1], (tuple, list)):
                    poly, text, score = item[0], item[1], item[2]
                else:
                    poly, (text, score) = item[0], item[1]
            except Exception:
                continue
            if poly is None or text is None:
                continue
            x0, y0, x1, y1 = PPOCRv5Engine._poly_to_bbox(poly)
            out.lines.append(
                OCRLine(text=text,
                        bbox=(x0 * scale, y0 * scale, x1 * scale, y1 * scale),
                        confidence=float(score))
            )
        return out

    def close(self) -> None:
        """安全终止 worker（terminate → kill 兜底）；可重入。"""
        proc = getattr(self, "_proc", None)
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()  # 先关 stdin：worker 循环 EOF 自然退出
                except Exception:
                    pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        except Exception:
            pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ---- 内部工具 -------------------------------------------------------

    def _read_loop(self):
        """后台读线程：worker stdout 逐行入队；EOF 投递空串哨兵。"""
        try:
            for line in self._proc.stdout:
                self._resp_q.put(line)
        except Exception:
            pass
        finally:
            self._resp_q.put("")

    @staticmethod
    def _parse_json(line):
        try:
            msg = json.loads(line)
            return msg if isinstance(msg, dict) else None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _resolve_worker_path() -> Path:
        """worker 脚本定位：源码=本模块同目录；frozen=_MEIPASS/app（datas 落点）。"""
        here = Path(__file__).resolve().parent / "v6_gpu_worker.py"
        if here.is_file():
            return here
        if getattr(sys, "frozen", False):
            meipass = Path(getattr(sys, "_MEIPASS", ""))
            if meipass:
                cand = meipass / "app" / "v6_gpu_worker.py"
                if cand.is_file():
                    return cand
            cand = Path(sys.executable).parent / "app" / "v6_gpu_worker.py"
            if cand.is_file():
                return cand
        return here


def _close_v6_gpu_singleton() -> None:
    global _V6_GPU_ENGINE_SINGLETON
    eng, _V6_GPU_ENGINE_SINGLETON = _V6_GPU_ENGINE_SINGLETON, None
    if eng is not None:
        eng.close()


def _get_v6_gpu_singleton(report_log=None, **engine_kwargs) -> ExternalV6GpuEngine:
    """GPU-v6 引擎进程级单例：worker 常驻复用、随进程退出清理（防泄漏）。
    不入 _ENGINE_CACHE（缓存键与生命周期单独管理）。engine_kwargs 仅在首次
    创建时生效（测试注入 model_dir / 多参数场景）。"""
    global _V6_GPU_ENGINE_SINGLETON
    if _V6_GPU_ENGINE_SINGLETON is None:
        eng = ExternalV6GpuEngine(**engine_kwargs)
        _V6_GPU_ENGINE_SINGLETON = eng
        atexit.register(_close_v6_gpu_singleton)
        if report_log:
            for w in eng.warnings:
                report_log(f"GPU-v6 环境告警：{w}")
    return _V6_GPU_ENGINE_SINGLETON


def _create_gpu_v5_engine() -> PPOCRv5Engine:
    """GPU-v5 模式引擎：复用外接 Paddle 探测 + GPU 判定；不可用抛 MDE。"""
    found = discover_external_paddle_env()
    if not found:
        raise MissingDependencyError(
            "GPU-v5 模式需要本机 Paddle GPU 环境：未发现可接入的 Paddle 环境"
            "（需 Python 3.11 的 Anaconda/Miniconda 并安装 paddlepaddle-gpu + "
            "paddleocr，或用环境变量 FINMASK_EXTERNAL_SITE 指向环境根目录）")
    if detect_device() != "gpu":
        raise MissingDependencyError(
            "GPU-v5 模式需要可用的 NVIDIA GPU：当前 Paddle 环境不支持 CUDA 推理")
    try:
        return PPOCRv5Engine(device="gpu")  # model_kind 自动 → server 档
    except MissingDependencyError:
        raise
    except Exception as e:
        raise MissingDependencyError(
            f"GPU-v5 引擎初始化失败：[{type(e).__name__}] {e}") from e


def create_cpu_fallback_engine() -> OnnxV6Engine:
    """CPU 兜底引擎（v6-small@CPU）：任何 GPU/外部模式不可用时的最后防线。"""
    return OnnxV6Engine(tier="small", use_gpu=False)


def create_engine_for_mode(mode: str, report_log=None) -> BaseOCREngine:
    """按 OCR 模式创建引擎（v1.8.2 模式选择器统一入口）。

    auto → create_ocr_engine("ppocrv6_onnx")（v1.8.1 现路径原样，tier 自动）；
    cpu_only → v6-small@CPU；gpu_v5 → 本机 Paddle GPU（v5-server）；
    gpu_v6 → 外部环境常驻 worker（进程级单例）。
    失败不在此处吞：依赖/环境缺失抛 MissingDependencyError，由消费层
    （pdf_handler._make_ocr_getter）统一回退 CPU（v6-small）。
    """
    from . import app_config
    if mode == app_config.OCR_MODE_AUTO:
        eng = create_ocr_engine("ppocrv6_onnx")
    elif mode == app_config.OCR_MODE_CPU:
        eng = create_cpu_fallback_engine()
    elif mode == app_config.OCR_MODE_GPU_V5:
        eng = _create_gpu_v5_engine()
    elif mode == app_config.OCR_MODE_GPU_V6:
        eng = _get_v6_gpu_singleton(report_log=report_log)
    else:
        raise ValueError(
            f"未知 OCR 模式：{mode}（可选：{'/'.join(app_config._OCR_MODE_VALUES)}）")
    note_engine_name(eng.name)
    return eng


# ---------------------------------------------------------------------------
# per-mode 引擎缓存（v2.0.1 C3）：批量内每模式只构造一次，消除逐文件重建
# ---------------------------------------------------------------------------

_MODE_ENGINE_CACHE: dict = {}  # mode 原值字符串 -> BaseOCREngine（per-mode 单例）


def get_engine_for_mode(mode: str, report_log=None) -> BaseOCREngine:
    """per-mode 引擎单例（v2.0.1 C3）：同 mode 同批内只构造一次。

    首次调用走 create_engine_for_mode 既有构造路径（含 gpu_v6 的 discover
    检查与 .ready 契约），成功后入缓存；同 mode 再次调用直接返回缓存实例。
    构造失败（如 MissingDependencyError）不入缓存，下次调用重新尝试构造。
    mode 即缓存键（按调用方传入原值，auto 不预解析）；不同模式键天然隔离。
    gpu_v6 worker 健康失效（recognize 异常/EOF/fatal）由调用方触发
    invalidate_engine_cache(mode) + 重建一次，见 GpuV6ResilientEngine。
    """
    eng = _MODE_ENGINE_CACHE.get(mode)
    if eng is None:
        eng = create_engine_for_mode(mode, report_log=report_log)
        _MODE_ENGINE_CACHE[mode] = eng
    return eng


def invalidate_engine_cache(mode: str = None) -> None:
    """清 per-mode 引擎缓存（v2.0.1 C3）：mode=None 全清；指定 mode 只清该键。

    gpu_v6 键随缓存清空走既有干净退出路径（close()：关 stdin → terminate →
    kill）关停 worker 子进程，并同步清进程级 v6 单例（防下次重建拿到已
    关停旧实例）；cpu/gpu_v5 等进程内引擎无进程外资源，失效=丢弃引用。
    """
    from . import app_config
    for k in (list(_MODE_ENGINE_CACHE) if mode is None else [mode]):
        eng = _MODE_ENGINE_CACHE.pop(k, None)
        if eng is None:
            continue
        if k == app_config.OCR_MODE_GPU_V6:
            if eng is _V6_GPU_ENGINE_SINGLETON:
                _close_v6_gpu_singleton()  # 既有干净退出路径（含全局置 None）
            else:
                eng.close()


class GpuV6ResilientEngine(BaseOCREngine):
    """gpu_v6 引擎健康守卫（v2.0.1 C3 失效联动②）：识别中途 worker 失效不崩。

    pdf_handler getter（gpu_v6 分支）的接线包装：内部经 get_engine_for_mode
    取 per-mode 单例；recognize 遇 MissingDependencyError（worker 异常/EOF/
    fatal/超时的统一抛出口）→ invalidate_engine_cache(mode) + 重建一次并
    重试同一页；重建/重试仍失败 → 既有回退语义（CPU v6-small，日志人话与
    init 期兜底同款）重试一次；CPU 也失败才上抛（由逐页异常兜底接管，
    单页失败不中断批次）。name 恒透传当前真引擎名（就绪日志/诊断不变）。
    """

    def __init__(self, mode: str, report_log=None):
        self._mode = mode
        self._report_log = report_log
        self._engine = get_engine_for_mode(mode, report_log=report_log)

    @property
    def name(self) -> str:
        return getattr(self._engine, "name", "")

    def recognize(self, image_bytes: bytes, dpi: int) -> OCRPageResult:
        try:
            return self._engine.recognize(image_bytes, dpi)
        except OcrPageError:
            # ⑤：单页失败（引擎健康）——不 invalidate 不重建，直抛交
            # pdf_handler 页级兜底（杀进程重建对健康 worker 是纯浪费）
            raise
        except MissingDependencyError as e:
            invalidate_engine_cache(self._mode)
            if self._report_log:
                self._report_log(f"GPU-v6 worker 失效（{e}），正在重建引擎并重试…")
            try:
                self._engine = get_engine_for_mode(self._mode,
                                                   report_log=self._report_log)
                return self._engine.recognize(image_bytes, dpi)
            except MissingDependencyError as e2:
                invalidate_engine_cache(self._mode)  # 重建重试仍失败：失效实例不留缓存
                if self._report_log:
                    self._report_log(f"所选 OCR 模式不可用（{e2}），已回退 CPU（v6-small）。")
                self._engine = create_cpu_fallback_engine()
                return self._engine.recognize(image_bytes, dpi)


# ---------------------------------------------------------------------------
# 最近就绪引擎名（进程内，状态栏/诊断用）
# ---------------------------------------------------------------------------

_last_engine_name = ""


def note_engine_name(name: str) -> None:
    """记录最近一次成功初始化的引擎名（get/创建引擎成功时调用）。"""
    global _last_engine_name
    _last_engine_name = str(name or "")


def get_last_engine_name() -> str:
    return _last_engine_name


# ---------------------------------------------------------------------------
# 引擎注册表（扩展点）与缓存
# ---------------------------------------------------------------------------

_OCR_FACTORIES = {"ppocrv5": PPOCRv5Engine, "ppocrv6_onnx": OnnxV6Engine}
_ENGINE_CACHE = {}


def register_ocr_engine(name: str, factory) -> None:
    """扩展点：注册自定义 OCR 引擎工厂（factory(**kwargs) -> BaseOCREngine）。"""
    _OCR_FACTORIES[name] = factory


def resolve_v6_tier(device: str = None) -> str:
    """v6 默认档位（v1.8.0）：CPU → small；GPU → medium（显存实测通过）。

    tiny 不参与自动选档（rec 字典仅 6904 字，识别禁用，仅检测/预筛用途）。
    """
    if device is None:
        device = detect_onnx_device()
    return "medium" if device == "gpu" else "small"


def create_ocr_engine(name: str = "ppocrv6_onnx", **kwargs) -> BaseOCREngine:
    """创建 OCR 引擎；依赖缺失/初始化失败时抛出带安装提示的 MissingDependencyError。

    v1.8.0 起默认引擎为 ppocrv6_onnx（档位自动：CPU→small / GPU→medium）；
    ppocrv5 全量保留，显式传入 name 即可回退。
    """
    factory = _OCR_FACTORIES.get(name)
    if factory is None:
        raise ValueError(f"未知的 OCR 引擎：{name}（可选：{list(_OCR_FACTORIES)}）")
    if name == "ppocrv6_onnx" and "tier" not in kwargs:
        kwargs["tier"] = resolve_v6_tier()  # 未显式指定档位时按设备自动
    try:
        eng = factory(**kwargs)
    except MissingDependencyError:
        raise
    except Exception as e:
        if name == "ppocrv6_onnx":
            hint = ("PP-OCRv6（ONNX）初始化失败：请确认 models/onnx_v6/ 下有 det/rec "
                    "模型文件，且已安装 rapidocr-onnxruntime 与 onnxruntime。")
        else:
            hint = "图片内文字脱敏需要 PP-OCRv5 组件（PaddleOCR）：pip install paddlepaddle paddleocr。"
        raise MissingDependencyError(f"{hint}初始化失败：[{type(e).__name__}] {e}") from e
    note_engine_name(eng.name)  # v1.8.2：记录最近就绪的引擎名（状态栏/诊断用）
    return eng


def get_ocr_engine(name: str = "ppocrv6_onnx", **kwargs) -> BaseOCREngine:
    """带缓存的引擎获取：同一批任务内只初始化一次（模型加载耗时较长）。"""
    key = (name, tuple(sorted(kwargs.items())))
    if key not in _ENGINE_CACHE:
        _ENGINE_CACHE[key] = create_ocr_engine(name, **kwargs)
    return _ENGINE_CACHE[key]


def reset_ocr_engine() -> None:
    """清空引擎缓存（更换引擎/参数后调用）。"""
    _ENGINE_CACHE.clear()


def ppocrv6_available() -> tuple:
    """轻量探测 PP-OCRv6（ONNX）是否可用：(是否可用, 不可用原因)。

    只查包存在性与模型文件，不真正导入 onnxruntime（避免拖慢 UI 启动）。
    """
    import importlib.util

    if importlib.util.find_spec("rapidocr_onnxruntime") is None:
        return False, "缺少 rapidocr-onnxruntime 组件（pip install rapidocr-onnxruntime onnxruntime）"
    base = OnnxV6Engine._resolve_model_dir()
    missing = [p.name for p in (base / "PP-OCRv6_det_small.onnx", base / "PP-OCRv6_rec_small.onnx")
               if not p.is_file()]
    if missing:
        return False, "缺少模型文件：" + "、".join(missing) + f"（目录：{base}）"
    return True, ""
