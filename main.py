# -*- coding: utf-8 -*-
"""财税文档脱敏工具（本地离线版）—— 程序入口。

运行方式：
    python main.py

说明：
- 全部文件处理均在本地完成，不发起任何网络请求。
- 第三方库均为可选依赖（PyMuPDF / python-docx / openpyxl / python-pptx /
  xlrd / paddleocr / pywin32 / tkinterdnd2 / opencv-python），
  缺失时对应功能自动降级并给出友好提示，不影响界面启动。
"""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path


def _enable_windows_dpi_awareness() -> None:
    """Windows 高 DPI 适配：让界面在高分屏上更清晰（失败则忽略）。"""
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def create_root():
    """创建主窗口根对象。

    优先使用 tkinterdnd2 的 Tk（支持文件/文件夹拖拽导入），
    未安装或初始化失败时回退到标准 tkinter.Tk（仅保留按钮导入方式）。
    """
    try:  # 高 DPI 感知：系统缩放下字体清晰、布局按比例放大（须在窗口创建前）
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    try:
        from tkinterdnd2 import TkinterDnD
        return TkinterDnD.Tk(), True
    except Exception:
        import tkinter as tk
        return tk.Tk(), False


def _frozen_ocr_probe() -> None:
    """exe 自检钩子：`财税文档脱敏工具.exe --probe-ocr`

    v1.8.0 起主段自检默认引擎 PP-OCRv6-ONNX（exe 内置 onnxruntime CPU
    闭包 + small 权重，档位自动 CPU→small / GPU→medium，不依赖 Paddle）；
    外接 Paddle（v5）探测降为附加段。结果写入 exe 同目录
    ocr_probe_result.txt 后退出（不弹界面）。
    """
    import traceback

    out = Path(sys.executable).with_name("ocr_probe_result.txt")
    log_lines: list[str] = []

    def _render_and_hit(engine):
        """渲染一张含手机号的图交给引擎识别，返回 (是否命中, 行数)。"""
        import fitz
        doc = fitz.open()
        page = doc.new_page(width=220, height=80)
        page.insert_text((10, 40), "Tel: 13812345678", fontsize=13)
        png = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).tobytes("png")
        doc.close()
        result = engine.recognize(png, 144)
        hit = any("13812345678" in line.text for line in result.lines)
        return hit, len(result.lines)

    # —— 主段：默认引擎 PP-OCRv6-ONNX ——
    try:
        from app.ocr_engine import create_ocr_engine
        engine = create_ocr_engine("ppocrv6_onnx")
        hit, n_lines = _render_and_hit(engine)
        log_lines.append(f"RESULT OK engine={engine.name} "
                         f"lines={n_lines} digit_hit={hit}")
    except Exception as e:
        log_lines.append(f"RESULT FAIL engine=ppocrv6_onnx "
                         f"[{type(e).__name__}] {e}\n{traceback.format_exc()}")

    # —— 附加段：外接 Paddle（v5）探测；缺失不影响 v6 主链路 ——
    try:
        from app.ocr_engine import PPOCRv5Engine, discover_external_paddle_env
        log_lines.append(f"external_paddle={discover_external_paddle_env()}")
        v5 = PPOCRv5Engine()
        hit, n_lines = _render_and_hit(v5)
        log_lines.append(f"V5 OK engine={v5.name} lines={n_lines} digit_hit={hit}")
    except Exception as e:
        log_lines.append(f"V5 SKIP [{type(e).__name__}] {e}")

    out.write_text("\n".join(log_lines), encoding="utf-8")


def main() -> None:
    _enable_windows_dpi_awareness()
    if getattr(sys, "frozen", False) and "--probe-ocr" in sys.argv:
        _frozen_ocr_probe()
        return
    root, dnd_available = create_root()
    from app.ui_main_window import MainWindow  # 界面层
    MainWindow(root, dnd_available=dnd_available)
    root.mainloop()


if __name__ == "__main__":
    main()
