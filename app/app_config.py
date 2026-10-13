# -*- coding: utf-8 -*-
"""应用级配置（config.json，程序目录内持久化）。

与 MaskOptions（每次运行的界面选项）不同，这里存放环境级配置——
典型如 OCR 引擎选型这类"设定一次、长期生效"的开关。
文件缺失/损坏时回退默认值，绝不阻塞主流程。
"""
from __future__ import annotations

import json

from .utils import program_data_dir

CONFIG_FILENAME = "config.json"
BRANDS_FILENAME = "brands.json"  # v1.8.1：品牌预设配置文件（解析/防呆见 app/brand_config.py）

# OCR 引擎可选值（与 app/ocr_engine.py 的 _OCR_FACTORIES 注册名一致）
OCR_ENGINE_V5 = "ppocrv5"            # PP-OCRv5（Paddle 推理；v1.8.0 起为可回退选项，全量保留）
OCR_ENGINE_V6_ONNX = "ppocrv6_onnx"  # PP-OCRv6（ONNX Runtime 推理；档位自动 CPU→small / GPU→medium）

_OCR_ENGINE_VALUES = (OCR_ENGINE_V5, OCR_ENGINE_V6_ONNX)

# v1.8.2：OCR 模式（引擎模式选择器）——独立于旧 ocr_engine 键的新配置键。
# 旧键读写接口（get/set_ocr_engine_name）原样保留，存量用户语义不受影响。
OCR_MODE_AUTO = "auto"      # 自动：PP-OCRv6-ONNX（CPU=small / GPU=medium，与 v1.8.1 默认路径一致）
OCR_MODE_CPU = "cpu_only"   # 仅 CPU：强制 PP-OCRv6-ONNX-small（CPU；GPU 异常时的逃生选项）
OCR_MODE_GPU_V5 = "gpu_v5"  # 高级：本机 Paddle GPU 环境（PP-OCRv5-server）
OCR_MODE_GPU_V6 = "gpu_v6"  # 高级：外部 v6-GPU 环境（setup_v6_gpu_env.bat 创建，PP-OCRv6-medium）

_OCR_MODE_VALUES = (OCR_MODE_AUTO, OCR_MODE_CPU, OCR_MODE_GPU_V5, OCR_MODE_GPU_V6)

# 内部传递值（不写入 config.json）：存量 config.json 只有 ocr_engine=ppocrv5
# 且从未写过 ocr_mode 键时，resolve_ocr_mode() 返回它，表示"继续走 v1.8.1 的
# 旧 v5 链路"；用户在界面切换一次模式（写入 ocr_mode）后即按新模式执行。
OCR_MODE_LEGACY_V5 = "legacy_v5"

# v1.8.0 起默认 PP-OCRv6-ONNX（可行性报告分档结论：CPU=small、GPU=medium）。
# 存量 config.json 已显式写 "ppocrv5" 的用户保持 v5 不变；v5 随时可在界面切回。
# v1.8.2 增加 ocr_mode 键（load_config 按此白名单放行）。
_DEFAULTS = {"ocr_engine": OCR_ENGINE_V6_ONNX, "ocr_mode": OCR_MODE_AUTO}


def config_path():
    return program_data_dir() / CONFIG_FILENAME


def brand_config_path():
    """品牌预设配置路径：exe 同目录 / 源码项目根（与 config_path() 同惯例）。"""
    return program_data_dir() / BRANDS_FILENAME


def load_config() -> dict:
    """读取配置；未知名/坏文件一律回落默认，只放行已知键。"""
    cfg = dict(_DEFAULTS)
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in _DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    """写回配置；写失败（只读目录等）静默忽略——配置属于体验优化而非关键数据。"""
    try:
        config_path().write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def get_ocr_engine_name() -> str:
    name = load_config().get("ocr_engine", OCR_ENGINE_V6_ONNX)
    return name if name in _OCR_ENGINE_VALUES else OCR_ENGINE_V6_ONNX


def set_ocr_engine_name(name: str) -> None:
    if name not in _OCR_ENGINE_VALUES:
        raise ValueError(f"未知 OCR 引擎：{name}（可选：{'/'.join(_OCR_ENGINE_VALUES)}）")
    cfg = load_config()
    cfg["ocr_engine"] = name
    save_config(cfg)


# ---------------------------------------------------------------------------
# v1.8.2：OCR 模式（引擎模式选择器）
# ---------------------------------------------------------------------------

def get_ocr_mode() -> str:
    """读取 ocr_mode 配置值；未知值回 auto。"""
    val = load_config().get("ocr_mode", OCR_MODE_AUTO)
    return val if val in _OCR_MODE_VALUES else OCR_MODE_AUTO


def set_ocr_mode(mode: str) -> None:
    """写入 ocr_mode；旧 ocr_engine 键保持不动（存量 v5 选择不被覆盖）。"""
    if mode not in _OCR_MODE_VALUES:
        raise ValueError(f"未知 OCR 模式：{mode}（可选：{'/'.join(_OCR_MODE_VALUES)}）")
    cfg = load_config()
    cfg["ocr_mode"] = mode
    save_config(cfg)


def resolve_ocr_mode() -> str:
    """解析生效的 OCR 模式（含存量兼容，v1.8.2）。

    规则：
    - config.json 文件中**显式存在** "ocr_mode" 键 → 用它（非法值回 auto）。
      注意必须看原始文件而非 load_config()：_DEFAULTS 已含 ocr_mode，
      load_config 的返回值恒有此键，无法区分"用户写过"与"默认注入"。
    - 文件中没有 ocr_mode 键（存量配置）→ 按旧 ocr_engine 键推断：
      ppocrv5 → OCR_MODE_LEGACY_V5（继续走 v1.8.1 旧 v5 链路），其余 → auto。
    - 文件缺失/损坏 → 走 get_ocr_engine_name() 的默认值口径（该函数可被
      测试替换，与既有手法一致）。

    legacy_v5 仅作内部传递值，不写入 config.json。
    """
    data = None
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        if "ocr_mode" in data:
            val = data.get("ocr_mode")
            return val if val in _OCR_MODE_VALUES else OCR_MODE_AUTO
        if data.get("ocr_engine") == OCR_ENGINE_V5:
            return OCR_MODE_LEGACY_V5
        return OCR_MODE_AUTO
    # 无文件/坏文件：默认值口径（load_config 回退 v6 → auto；坏文件同）
    return OCR_MODE_LEGACY_V5 if get_ocr_engine_name() == OCR_ENGINE_V5 else OCR_MODE_AUTO
