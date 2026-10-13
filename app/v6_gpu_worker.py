# -*- coding: utf-8 -*-
"""GPU-v6 常驻推理 worker（v1.8.2）—— 在外部 GPU 环境的解释器中运行。

用法：
    <外部环境python.exe> v6_gpu_worker.py --models <权重目录>

权重（档位固定 medium，目录与文件名由 setup_v6_gpu_env.bat 约定）：
    <models>/PP-OCRv6_det_medium.onnx
    <models>/PP-OCRv6_rec_medium.onnx

定位与约束：
- 本脚本必须保持"单文件、零 app 包依赖"：它由 setup_v6_gpu_env.bat 创建的
  独立 Python 环境（onnxruntime-gpu + rapidocr-onnxruntime）执行，宿主可能
  是冻结 exe。只允许标准库 + rapidocr_onnxruntime（间接含 onnxruntime）。
- GPU 推理在子进程内完成，宿主 exe 只内置 onnxruntime CPU 闭包，二者以
  stdin/stdout 的行 JSON 协议通信（每行一个 JSON 对象，UTF-8、ensure_ascii）。

协议：
  启动成功 →  {"event": "ready", "engine": "PP-OCRv6-ONNX-medium(GPU-EXT)"}
  启动失败 →  {"event": "fatal", "error": "..."} 后进程退出（exit 1）
  请求    ←  {"id": n, "op": "recognize", "image_b64": "...", "dpi": 72}
  成功    →  {"id": n, "ok": true,
              "lines": [{"text": "...", "poly": [[x, y], ...], "score": 0.99}]}
  失败    →  {"id": n, "ok": false, "error": "..."}
  stdin EOF → 干净退出（exit 0）

约定：
- lines[].poly 为 rapidocr 原始像素坐标（宿主侧按 dpi 换算为 PDF 点）；
- 所有响应值均为 Python 原生类型（numpy 数组/标量一律转换）；
- GPU（det_use_cuda=rec_use_cuda=True）初始化失败直接 fatal——是否回退
  CPU 由宿主统一决定，worker 不做静默降级。
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

ENGINE_NAME = "PP-OCRv6-ONNX-medium(GPU-EXT)"

_DET_NAME = "PP-OCRv6_det_medium.onnx"
_REC_NAME = "PP-OCRv6_rec_medium.onnx"


def _emit(obj) -> None:
    """输出一行 JSON 并立即刷新（协议要求逐行实时可达）。"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def _to_plain(value):
    """numpy 数组/标量 → Python 原生（list/float/int）；其余原样返回。"""
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return tolist()
        except Exception:
            pass
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            pass
    return value


def _parse_result_lines(result) -> list:
    """rapidocr 输出 → [{"text", "poly", "score"}]。

    双格式兼容（与 app/ocr_engine.py OnnxV6Engine.recognize 同一语义）：
    - rapidocr_onnxruntime 1.4.x：[poly, text, score] 三元组；
    - rapidocr 3.x 风格：(poly, (text, score)) 嵌套。
    坏项跳过不崩。
    """
    lines = []
    for item in result or []:
        try:
            if len(item) == 3 and not isinstance(item[1], (tuple, list)):
                poly, text, score = item[0], item[1], item[2]
            else:
                poly, (text, score) = item[0], item[1]
        except Exception:
            continue
        poly = _to_plain(poly)
        if not isinstance(poly, list):
            try:
                poly = [[float(p[0]), float(p[1])] for p in poly]
            except Exception:
                continue
        try:
            text = str(text)
            score = float(_to_plain(score))
        except Exception:
            continue
        lines.append({"text": text, "poly": poly, "score": score})
    return lines


def _preload_nvidia_dlls() -> None:
    """把本环境内 nvidia pip 包（CUDA/cuDNN 运行库）的 DLL 目录加入搜索路径。

    背景：onnxruntime-gpu 的 Windows wheel 不带 CUDA/cuDNN DLL，缺它们时
    CUDA EP 初始化报 LoadLibrary 错误 126 并静默退回 CPU。setup_v6_gpu_env.bat
    已把 nvidia-*-cu12 运行库一并装入本环境；这里在导入 rapidocr 前
    os.add_dll_directory 登记 <sys.prefix>/Lib/site-packages/nvidia/*/bin，
    令 onnxruntime_providers_cuda.dll 的依赖可解析。纯标准库、幂等；
    目录不存在（CPU-only 环境）时静默跳过，不影响回退路径。
    """
    import glob
    import os

    root = os.path.join(sys.prefix, "Lib", "site-packages", "nvidia")
    dirs = sorted(glob.glob(os.path.join(root, "*", "bin")))
    for d in dirs:
        try:
            os.add_dll_directory(d)
        except OSError:
            pass
    if dirs:
        # 兜底：个别延迟加载的依赖按 PATH 解析，把 bin 目录前置到 PATH。
        os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")


def _build_ocr(models_dir: Path):
    """加载 RapidOCR（GPU）；失败抛出异常由调用方转 fatal。"""
    _preload_nvidia_dlls()
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR(
        det_model_path=str(models_dir / _DET_NAME),
        rec_model_path=str(models_dir / _REC_NAME),
        cls_model_path=None,
        use_cls=False,  # 单据页面恒为正向，方向分类无收益（与宿主 OnnxV6Engine 一致）
        det_use_cuda=True,
        rec_use_cuda=True,
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="GPU-v6 OCR worker（stdin/stdout 行 JSON 协议）")
    parser.add_argument("--models", required=True,
                        help=f"权重目录（含 {_DET_NAME} 与 {_REC_NAME}）")
    args = parser.parse_args(argv)

    models_dir = Path(args.models)
    missing = [str(models_dir / n) for n in (_DET_NAME, _REC_NAME)
               if not (models_dir / n).is_file()]
    if missing:
        _emit({"event": "fatal", "error": "缺少权重文件：" + "、".join(missing)})
        return 1

    try:
        ocr = _build_ocr(models_dir)
    except Exception as e:  # noqa: BLE001  协议层兜底：任何初始化失败都转 fatal
        _emit({"event": "fatal",
               "error": f"GPU 初始化失败：[{type(e).__name__}] {e}"})
        return 1
    _emit({"event": "ready", "engine": ENGINE_NAME})

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            req = json.loads(raw)
            rid = req.get("id")
        except ValueError:
            continue  # 非 JSON 行忽略（协议外噪声不致命）
        if req.get("op") != "recognize":
            _emit({"id": rid, "ok": False, "error": f"未知 op：{req.get('op')!r}"})
            continue
        try:
            image = base64.b64decode(req.get("image_b64") or "")
            result, _elapse = ocr(image)
            _emit({"id": rid, "ok": True, "lines": _parse_result_lines(result)})
        except Exception as e:  # noqa: BLE001  单页失败不退出进程，交宿主决策
            _emit({"id": rid, "ok": False, "error": f"[{type(e).__name__}] {e}"})
    return 0  # stdin EOF：干净退出


if __name__ == "__main__":
    sys.exit(main())
