# -*- coding: utf-8 -*-
"""界面层：主窗口（tkinter）。

界面与业务完全解耦：
- 所有文件处理在子线程中执行（app.dispatcher.run_batch）；
- 子线程一律通过 root.after(...) 调度刷新界面，禁止跨线程直接操作控件；
- 拖拽导入依赖 tkinterdnd2（可选），缺失时保留按钮导入方式。
"""
from __future__ import annotations

import os
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .dispatcher import SUPPORTED_EXTS, run_batch
from .rules_engine import MaskOptions
from .utils import APP_VERSION, now_hms, probe_optional_deps
from . import candidate_flow  # 候选词推荐流程层（纯逻辑，见 candidate_flow.py）

_PLACEHOLDER_FG = "#9a9a9a"


def paddle_missing_notice(mode: str, frozen: bool) -> str:
    """启动自检 PaddleOCR（v5）组件缺失时的说明文案（v2.0.0 口径）。

    v2.0.0 起 exe 内置 PP-OCRv6-ONNX 引擎（CPU=small 默认路径，不依赖 Paddle），
    旧版「exe 无内置组件、图片脱敏不可用」的假结论已不实（已修复）。
    按生效 OCR 模式分流（防呆语义不变：真缺组件仍提示怎么装/怎么切）：
    - auto / cpu_only（默认路径）：Paddle 缺失与图片脱敏无关，仅中性说明；
    - gpu_v5 / legacy_v5（显式选用旧 v5 链路）：环境缺失给出对应安装提示与
      出路——gpu_v5 处理时自动回退内置 v6-CPU；legacy_v5 图片脱敏不可用，
      建议切换“自动”模式。
    frozen=False（源码版）返回空串，沿用通用「依赖自检」行。
    """
    if not frozen:
        return ""
    from . import app_config
    if mode == app_config.OCR_MODE_GPU_V5:
        return ("说明：GPU-v5 模式需要本机 Paddle GPU 环境：未发现可接入的 Paddle 环境"
                "（需 Python 3.11 的 Anaconda/Miniconda 并安装 paddlepaddle-gpu + "
                "paddleocr，或用环境变量 FINMASK_EXTERNAL_SITE 指向环境根目录后重启）。"
                "处理时将自动回退内置 PP-OCRv6-ONNX（CPU）。")
    if mode == app_config.OCR_MODE_LEGACY_V5:
        return ("说明：存量 v5 配置需要本机 Paddle 环境：未发现可接入的 Paddle 环境"
                "（需 Python 3.11 的 Anaconda/Miniconda 并安装 paddlepaddle + paddleocr）。"
                "图片型（扫描）PDF 的文字脱敏将不可用（其余功能不受影响）；建议在上方"
                "“OCR 模式”中切换为“自动”以使用内置 PP-OCRv6-ONNX 引擎。")
    return ("说明：本机未安装 PaddleOCR（v5）组件——本程序已内置 PP-OCRv6-ONNX 引擎"
            "（CPU 默认路径，不依赖 Paddle），图片型（扫描）PDF 的文字脱敏不受影响。")


class _ToolTip:
    """轻量悬浮提示（原生 tkinter 实现；delay=0 时移入立即显示）。"""

    def __init__(self, widget, text: str, delay: int = 500):
        self._widget = widget
        self._text = text
        self._delay = delay
        self._tip = None
        self._after_id = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        if self._delay <= 0:
            self._show()
            return
        self._after_id = self._widget.after(self._delay, self._show)

    def _show(self):
        if self._tip is not None:
            return
        x = self._widget.winfo_rootx() + 12
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 6
        self._tip = tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self._text, justify="left", background="#FFFFE1",
                 relief="solid", borderwidth=1, font=("Microsoft YaHei UI", 8),
                 wraplength=380, padx=6, pady=3).pack()

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    def _cancel(self):
        if self._after_id is not None:
            try:
                self._widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None


def _info_dot(parent, text: str) -> tk.Label:
    """选项旁的“ⓘ”信息符号：鼠标移入立即浮出说明。"""
    dot = tk.Label(parent, text="ⓘ", fg="#1E6FB8", cursor="hand2",
                   font=("Segoe UI Symbol", 9))
    _ToolTip(dot, text, delay=0)
    return dot
_NORMAL_FG = "#000000"


class MainWindow:
    def __init__(self, root, dnd_available: bool = False):
        self.root = root
        self._dnd_available = dnd_available
        self._worker = None
        self._last_progress_ts = 0.0
        self._last_progress_value = -1.0
        self._last_mapping_path = None  # 最近一次汇总映射表路径（复核/误报回收用）

        # 占位/提示文案
        self._words_hint = ("每行一个敏感词，例如：\n张三\n示例置业（词组型公司字号、生僻人名、"
                            "品牌/项目代号尤其建议填入）")
        self._outdir_hint = "默认：与源文件相同目录"
        self._mapping_hint = "选择编辑过的对照表CSV后重新生成"

        self._setup_window()
        self._build_widgets()
        self._bind_dnd()
        self._log_startup()

    # ------------------------------------------------------------------
    # 窗口基础
    # ------------------------------------------------------------------
    def _setup_window(self):
        from .utils import APP_VERSION
        self.root.title(f"财税文档脱敏工具 v{APP_VERSION}（本地离线版）")
        # 高 DPI：按系统缩放放大窗口尺寸（字体为磅值，随 tk scaling 自动缩放）
        try:
            dpi = float(self.root.winfo_fpixels("1i"))
        except Exception:
            dpi = 96.0
        scale = max(1.0, dpi / 96.0)
        try:
            self.root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass
        self.root.geometry(f"{int(1120 * scale)}x{int(744 * scale)}")
        self.root.minsize(int(960 * scale), int(600 * scale))
        self.root.option_add("*Font", ("Microsoft YaHei UI", 9))
        style = ttk.Style(self.root)
        for theme in ("vista", "xpnative", "clam"):  # 原生 Windows 朴素风格
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        # 视觉标识：分区标题加粗、主按钮强调
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"),
                        padding=(16, 3))
        self._make_icon()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _make_icon(self):
        """程序图标：无图标文件，运行时用 PIL 画一枚“脱”字圆角标。"""
        try:
            from PIL import Image, ImageDraw, ImageFont, ImageTk
            img = Image.new("RGBA", (32, 32), (21, 101, 92, 255))
            d = ImageDraw.Draw(img)
            try:
                font = ImageFont.truetype("msyh.ttc", 17)
            except Exception:
                font = ImageFont.load_default()
            d.text((16, 16), "脱", font=font, fill="white", anchor="mm")
            self._icon_img = ImageTk.PhotoImage(img)
            self.root.iconphoto(False, self._icon_img)
        except Exception:
            pass  # 图标失败不影响功能

    # ------------------------------------------------------------------
    # 控件构建（自上而下：文件列表 / 脱敏选项 / 自定义敏感词 / 输出配置 / 进度 / 日志）
    # ------------------------------------------------------------------
    def _build_widgets(self):
        outer = ttk.Frame(self.root, padding=(12, 8))
        outer.pack(fill=tk.BOTH, expand=True)

        # —— 状态栏（最先 pack 到底侧，保证布局稳定）——
        from .utils import APP_VERSION
        status = ttk.Frame(outer)
        status.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Label(status, text=f"v{APP_VERSION} · 完全本地离线 · 数据不出本机",
                  foreground="#8A8A8A").pack(side=tk.LEFT)
        ttk.Label(status, text="by 逐光", foreground="#8A8A8A").pack(side=tk.RIGHT)
        # v1.8.2：引擎指示（初始待批处理；批处理结束回调刷新为最近就绪的引擎名）
        self._lbl_engine = ttk.Label(status, text="引擎：待批处理", foreground="#8A8A8A")
        self._lbl_engine.pack(side=tk.RIGHT, padx=(0, 16))

        # —— 1. 文件列表区 ——
        ttk.Label(outer, text="待处理文件（可拖拽导入 .docx / .xlsx / .xls / .pptx / .pdf / .md；"
                              "处理 .xls 需要本机安装 Excel 或 WPS）：").pack(anchor="w")
        list_wrap = ttk.Frame(outer)
        list_wrap.pack(fill=tk.BOTH, expand=True, pady=(2, 4))
        self.listbox = tk.Listbox(list_wrap, selectmode=tk.EXTENDED,
                                  exportselection=False, activestyle="dotbox", height=4)
        vsb = ttk.Scrollbar(list_wrap, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=vsb.set)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._list_hint = tk.Label(list_wrap, text="拖入文件 / 文件夹，或点击下方“添加文件...”",
                                   fg="#9A9A9A", bg="SystemButtonFace",
                                   font=("Microsoft YaHei UI", 9))

        btns = ttk.Frame(outer)
        btns.pack(fill=tk.X, pady=(0, 6))
        ttk.Button(btns, text="添加文件...", command=self._on_add_files).pack(side=tk.LEFT)
        ttk.Button(btns, text="移除选中", command=self._on_remove_selected).pack(side=tk.LEFT, padx=8)
        ttk.Button(btns, text="清空列表", command=self._on_clear_list).pack(side=tk.LEFT)

        # —— 2. 脱敏选项区（按使用逻辑分组：先选“脱敏什么”，再选“怎么处理”） ——
        self.var_company = tk.BooleanVar(value=True)
        self.var_person = tk.BooleanVar(value=True)
        self.var_amount = tk.BooleanVar(value=False)
        self.var_contact = tk.BooleanVar(value=True)
        self.var_address = tk.BooleanVar(value=True)
        self.var_ocr = tk.BooleanVar(value=False)
        self.var_xls2xlsx = tk.BooleanVar(value=False)
        self.var_stamp = tk.BooleanVar(value=False)
        self.var_person_loose = tk.BooleanVar(value=True)
        self.var_deep_recheck = tk.BooleanVar(value=True)  # 深度复检（默认开）
        self.var_amount_mode = tk.StringVar(value="mask")  # mask=全部隐去 / scale=缩放脱敏
        # v2.1.8 仅脱敏指定词（预研方案 A）：默认关；词框空时置灰（见③区绑定）
        self.var_words_only = tk.BooleanVar(value=False)

        grp_content = ttk.LabelFrame(outer, text="① 脱敏内容（勾选需要脱敏的信息）", padding=(8, 2))
        grp_content.pack(fill=tk.X, pady=(0, 4))
        for col, (var, text) in enumerate((
                (self.var_company, "企业基本信息（公司名等）"),
                (self.var_person, "人名"),
                (self.var_address, "地址"),
                (self.var_contact, "证件/联系方式（身份证/手机号/邮箱）"),
                (self.var_amount, "金额"),
                (self.var_stamp, "红色印章"))):
            ttk.Checkbutton(grp_content, variable=var, text=text).grid(
                row=0, column=col, sticky="w", padx=(0, 10), pady=1)
        grp_content.columnconfigure(0, weight=1)

        grp_mode = ttk.LabelFrame(outer, text="② 处理方式（可选调整）", padding=(8, 2))
        grp_mode.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(grp_mode, text="金额处理：").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(grp_mode, variable=self.var_amount_mode, value="mask",
                        text="全部隐去（显示***）").grid(row=0, column=1, sticky="w", padx=(0, 12))
        ttk.Radiobutton(grp_mode, variable=self.var_amount_mode, value="scale",
                        text="缩放脱敏（×随机系数K，公式写入映射表）").grid(row=0, column=2,
                                                                     columnspan=2, sticky="w")
        self.var_include_inst = tk.BooleanVar(value=False)
        _mode_tips = {
            "人名宽松识别": "开启后对无职务上下文的姓名更宽容：漏检更少，但普通词被误判为"
                       "人名的概率略增。处理合同/制度类文档建议开启；发现误报可在"
                       "“复核映射表”中删除该行。",
            "扩展机构（大学/医院/协会等）": "额外识别大学、医院、协会、研究院等机构名称"
                                    "（默认仅识别企业类主体）。",
            "图片内文字（OCR）": "对扫描件与内嵌截图做 OCR 并遮盖其中的文字。处理速度明显"
                          "变慢；exe 版内置 PP-OCRv6-ONNX 引擎（CPU 默认路径），"
                          "无需额外安装组件。",
            ".xls 转存为 xlsx": "旧版 .xls 处理后另存为 .xlsx（公式与格式可能被规整化）。",
            "深度复检（防残留，较慢）": "光栅化前对页面做条带放大与整页两轮 OCR 复检，兜住 logo 横幅装饰小字、矢量转曲文字等常规遮盖扫不到的残留。关闭（快速模式）可明显提速，适合内容规整、以文字层为主的文档；含品牌横幅图片的文档建议保持开启。",
        }
        for i, (var, text) in enumerate((
                (self.var_person_loose, "人名宽松识别"),
                (self.var_include_inst, "扩展机构（大学/医院/协会等）"),
                (self.var_ocr, "图片内文字（OCR）"),
                (self.var_xls2xlsx, ".xls 转存为 xlsx"),
                (self.var_deep_recheck, "深度复检（防残留，较慢）"))):
            col = i * 2
            ttk.Checkbutton(grp_mode, variable=var, text=text).grid(
                row=1, column=col, sticky="w", padx=(0, 2), pady=1)
            _info_dot(grp_mode, _mode_tips[text]).grid(
                row=1, column=col + 1, sticky="w", padx=(0, 8))
        grp_mode.columnconfigure(len(_mode_tips) * 2, weight=1)

        # —— OCR 模式选择（v1.8.2：环境级配置 ocr_mode 键，写入程序目录
        #        config.json 跨会话生效；旧 ocr_engine 键保留不动，存量
        #        ppocrv5 用户未切换模式前继续走 v5 旧链路） ——
        from . import app_config
        from .ocr_engine import ppocrv6_available
        self._ocr_mode_labels = {
            app_config.OCR_MODE_AUTO: "自动（默认；CPU=v6-small / GPU=v6-medium）",
            app_config.OCR_MODE_CPU: "仅 CPU（强制 v6-small；GPU 异常逃生）",
            app_config.OCR_MODE_GPU_V5: "GPU-v5（高级：需本机 Paddle GPU 环境）",
            app_config.OCR_MODE_GPU_V6: "GPU-v6（高级：需先运行 setup_v6_gpu_env.bat）",
        }
        ttk.Label(grp_mode, text="OCR 模式：").grid(row=2, column=0, sticky="w", pady=(3, 0))
        # legacy_v5 不设下拉项：存量 v5 用户初始显示「自动」，启动日志另行提示
        # （本次批处理仍走 v5，切换模式写入 ocr_mode 后新路径才生效）。
        _cur_mode = app_config.resolve_ocr_mode()
        self.var_ocr_mode = tk.StringVar(
            value=self._ocr_mode_labels.get(_cur_mode, self._ocr_mode_labels[app_config.OCR_MODE_AUTO]))
        self.cmb_ocr_mode = ttk.Combobox(
            grp_mode, state="readonly", width=44, textvariable=self.var_ocr_mode,
            values=list(self._ocr_mode_labels.values()))
        self.cmb_ocr_mode.grid(row=2, column=1, columnspan=2, sticky="w", pady=(3, 0))
        self.cmb_ocr_mode.bind("<<ComboboxSelected>>", self._on_ocr_mode_selected)
        _v6_ok, _v6_reason = ppocrv6_available()
        _mode_tip = ("选择图片/扫描件 OCR 的推理模式（写入 config.json，重启后仍生效）。"
                     "自动：CPU 用 v6-small、NVIDIA GPU 用 v6-medium（显存峰值实测约 2.5GB）；"
                     "仅 CPU：强制 v6-small@CPU，GPU 异常时的逃生选项；"
                     "GPU-v5：使用本机 Paddle GPU 环境（PP-OCRv5-server，高级）；"
                     "GPU-v6：使用 setup_v6_gpu_env.bat 创建的独立 GPU 环境"
                     "（PP-OCRv6-medium，建议显存 ≥4GB，高级）。"
                     "任何模式不可用时处理中自动回退 CPU（v6-small）。")
        if not _v6_ok:
            _mode_tip += f" 当前本机 v6 组件不可用：{_v6_reason}（自动模式处理时会回退 v5。）"
        _info_dot(grp_mode, _mode_tip).grid(row=2, column=3, sticky="w", pady=(3, 0))

        # —— 3. 自定义敏感词区（含必要性说明：引擎识别有覆盖上限，自定义词是
        #        保证最终呈现效果最可靠的补充手段） ——
        grp_words = ttk.LabelFrame(
            outer, text="③ 自定义敏感词（强烈建议填写 —— 直接决定脱敏是否完整）",
            padding=(8, 2))
        grp_words.pack(fill=tk.X, pady=(0, 4))
        words_note = ttk.Label(grp_words, text=(
            "为什么需要填写：引擎按内置规则自动识别公司名、人名、证件等常见实体，但无法保证覆盖全部真实内容——"
            "词组型企业字号（如“某某在线”这类四字组合）、生僻人名、品牌/项目代号、以及文档特有的第三方名称都可能漏检；"
            "图片小字与手写签名也存在识别上限。把文档中确定需要遮盖的词逐行填入（每行一个），"
            "引擎将按最高优先级精确遮盖，这是保证最终呈现效果最直接、最可靠的一道保险；"
            "处理后请结合“复核映射表”逐条确认。"),
            foreground="#595959", wraplength=880, justify="left")
        words_note.grid(row=0, column=0, sticky="we", pady=(0, 2))

        def _on_grp_words_configure(event, _lbl=words_note):
            _lbl.configure(wraplength=max(320, event.width - 24))
        grp_words.bind("<Configure>", _on_grp_words_configure)

        # 候选词发现入口②：③区标题行常驻「从文档发现候选词...」（ⓘ 悬浮
        # 说明为原文）。行为纯增量：不触碰本区既有控件的布局与语义。
        self.btn_discover = ttk.Button(grp_words, text=candidate_flow.DISCOVER_BUTTON,
                                       command=self._on_discover_candidates)
        self.btn_discover.grid(row=0, column=1, sticky="e", padx=(8, 0))
        _info_dot(grp_words, candidate_flow.DISCOVER_INFO).grid(
            row=0, column=2, sticky="w", padx=(2, 0))

        words_wrap = ttk.Frame(grp_words)
        words_wrap.grid(row=1, column=0, sticky="we")
        self.txt_words = tk.Text(words_wrap, height=2, wrap="none", undo=True)
        wvsb = ttk.Scrollbar(words_wrap, orient="vertical", command=self.txt_words.yview)
        self.txt_words.configure(yscrollcommand=wvsb.set)
        self.txt_words.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        wvsb.pack(side=tk.RIGHT, fill=tk.Y)
        grp_words.columnconfigure(0, weight=1)
        self._apply_words_placeholder()
        self.txt_words.bind("<FocusIn>", self._on_words_focus_in)
        self.txt_words.bind("<FocusOut>", self._on_words_focus_out)

        # v2.1.8 「仅脱敏这些词」开关（预研方案 A）：与词框语义绑定，
        # 词框空（含占位态）时置灰禁用；动态跟随 <<Modified>>。
        self.chk_words_only = ttk.Checkbutton(
            grp_words, text="仅脱敏这些词（跳过其他自动识别）",
            variable=self.var_words_only, state="disabled")
        self.chk_words_only.grid(row=2, column=0, sticky="w", pady=(3, 0))

        def _sync_words_only_state(*_):
            has_words = bool(self._read_custom_words())
            self.chk_words_only.configure(
                state=("normal" if has_words else "disabled"))
            if not has_words:
                self.var_words_only.set(False)

        self.txt_words.bind("<<Modified>>",
            lambda e: (self.txt_words.edit_modified(False),
                       _sync_words_only_state()))
        self.txt_words.bind("<FocusOut>",
            lambda e, f=self._on_words_focus_out: (f(e), _sync_words_only_state()),
            add="+")

        # —— 4. 输出设置区 ——
        grp_out = ttk.LabelFrame(outer, text="④ 输出设置（两项均可留空）", padding=(8, 2))
        grp_out.pack(fill=tk.X, pady=(0, 4))
        out_row1 = ttk.Frame(grp_out)
        out_row1.pack(fill=tk.X, pady=2)
        ttk.Label(out_row1, text="输出目录：", width=22, anchor="w").pack(side=tk.LEFT)
        self.var_outdir = tk.StringVar(value=self._outdir_hint)
        self.ent_outdir = ttk.Entry(out_row1, textvariable=self.var_outdir)
        self.ent_outdir.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self.ent_outdir.configure(foreground=_PLACEHOLDER_FG)
        self.ent_outdir.bind("<FocusIn>",
                             lambda e: self._entry_focus_in(self.ent_outdir, self.var_outdir, self._outdir_hint))
        self.ent_outdir.bind("<FocusOut>",
                             lambda e: self._entry_focus_out(self.ent_outdir, self.var_outdir, self._outdir_hint))
        ttk.Button(out_row1, text="浏览...", width=9, command=self._on_browse_outdir).pack(side=tk.LEFT)

        out_row2 = ttk.Frame(grp_out)
        out_row2.pack(fill=tk.X, pady=2)
        ttk.Label(out_row2, text="映射对照表覆盖（可选）：", width=22, anchor="w").pack(side=tk.LEFT)
        self.var_mapping = tk.StringVar(value=self._mapping_hint)
        self.ent_mapping = ttk.Entry(out_row2, textvariable=self.var_mapping)
        self.ent_mapping.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self.ent_mapping.configure(foreground=_PLACEHOLDER_FG)
        self.ent_mapping.bind("<FocusIn>",
                              lambda e: self._entry_focus_in(self.ent_mapping, self.var_mapping, self._mapping_hint))
        self.ent_mapping.bind("<FocusOut>",
                              lambda e: self._entry_focus_out(self.ent_mapping, self.var_mapping, self._mapping_hint))
        ttk.Button(out_row2, text="浏览...", width=9, command=self._on_browse_mapping).pack(side=tk.LEFT)
        _info_dot(out_row2,
                  "选择此前导出并编辑过的映射表 CSV 后，重跑将严格按表执行：\n"
                  "表内保留的行照常遮盖，删除的行不再遮盖（删除即视为白名单）。").pack(
            side=tk.LEFT, padx=(6, 0))
        # v1.9.0 分析缓存清除：复核轮 OCR 结果缓存在 <输出目录>/cache/，
        # 一般无需手动清除（内容哈希+版本头部自动失效）；按钮供排障/释放磁盘。
        ttk.Button(out_row2, text="清除分析缓存", width=12,
                   command=self._on_clear_analysis_cache).pack(side=tk.LEFT, padx=(6, 0))

        # —— 5. 处理与进度区 ——
        proc = ttk.Frame(outer)
        proc.pack(fill=tk.X, pady=(6, 4))
        self.btn_start = ttk.Button(proc, text="开始处理", command=self._on_start,
                                    style="Primary.TButton")
        self.btn_start.pack(side=tk.LEFT)
        self.btn_review = ttk.Button(proc, text="复核映射表...", command=self._open_mapping_review,
                                     state="disabled")
        self.btn_review.pack(side=tk.LEFT, padx=(8, 0))
        _info_dot(proc,
                  "查看/编辑本批次“原文 → 替换”清单；删除误报行保存后，\n"
                  "把该 CSV 选入“映射对照表覆盖”即可严格重跑。").pack(side=tk.LEFT, padx=(2, 0))
        self.progress = ttk.Progressbar(proc, orient="horizontal", mode="determinate", maximum=100)
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(12, 0))
        self.var_progress = tk.StringVar(value="")
        ttk.Label(proc, textvariable=self.var_progress, width=8, anchor="w").pack(side=tk.LEFT)

        # —— 6. 日志区 ——
        ttk.Label(outer, text="处理日志：").pack(anchor="w")
        log_wrap = ttk.Frame(outer)
        log_wrap.pack(fill=tk.BOTH, expand=True, pady=(2, 0))
        self.txt_log = tk.Text(log_wrap, height=5, wrap="word", state="disabled",
                               background="#FAFAFA", relief="solid", bd=1)
        self.txt_log.tag_configure("warn", foreground="#B26A00")
        self.txt_log.tag_configure("err", foreground="#C62828")
        lvsb = ttk.Scrollbar(log_wrap, orient="vertical", command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=lvsb.set)
        self.txt_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        lvsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._update_list_hint()

    def _update_list_hint(self):
        """文件列表为空时显示中央引导文案。"""
        try:
            if self.listbox.size() == 0:
                self._list_hint.place(relx=0.5, rely=0.5, anchor="center")
            else:
                self._list_hint.place_forget()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 占位文本（自定义敏感词）
    # ------------------------------------------------------------------
    def _apply_words_placeholder(self):
        self.txt_words.delete("1.0", "end")
        self.txt_words.insert("1.0", self._words_hint)
        self.txt_words.mark_set("insert", "1.0")
        self.txt_words.configure(fg=_PLACEHOLDER_FG)
        self._words_is_placeholder = True

    def _on_words_focus_in(self, _event=None):
        if getattr(self, "_words_is_placeholder", False):
            self.txt_words.delete("1.0", "end")
            self.txt_words.configure(fg=_NORMAL_FG)
            self._words_is_placeholder = False

    def _on_words_focus_out(self, _event=None):
        if not self.txt_words.get("1.0", "end").strip():
            self._apply_words_placeholder()

    def _read_custom_words(self) -> list:
        """读取自定义敏感词：按行分割、去重、忽略空行；占位内容视为未启用。"""
        content = self.txt_words.get("1.0", "end").strip("\n")
        if not content.strip() or content.strip() == self._words_hint.strip():
            return []
        words, seen = [], set()
        for line in content.splitlines():
            w = line.strip()
            if w and w not in seen:
                seen.add(w)
                words.append(w)
        return words

    # ------------------------------------------------------------------
    # 占位文本（单行输入框）
    # ------------------------------------------------------------------
    def _entry_focus_in(self, entry, var, hint):
        if var.get() == hint:
            entry.delete(0, tk.END)
            entry.configure(foreground=_NORMAL_FG)

    def _entry_focus_out(self, entry, var, hint):
        if not var.get().strip():
            var.set(hint)
            entry.configure(foreground=_PLACEHOLDER_FG)

    # ------------------------------------------------------------------
    # 文件列表操作 / 浏览按钮
    # ------------------------------------------------------------------
    def _on_add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择待脱敏文件",
            filetypes=[("支持的文档", "*.docx *.xlsx *.xls *.pptx *.pdf *.md"), ("所有文件", "*.*")],
        )
        self._add_paths(paths)

    def _on_ocr_mode_selected(self, _event=None):
        """OCR 模式切换：立即持久化 ocr_mode 键（旧 ocr_engine 键保持不动）。"""
        from . import app_config
        label = self.var_ocr_mode.get()
        mode = next((k for k, v in self._ocr_mode_labels.items() if v == label), None)
        if mode:
            app_config.set_ocr_mode(mode)
            self._append_log(f"OCR 模式已切换为：{label}（已写入 config.json，下次批处理生效）")

    def _on_remove_selected(self):
        for i in reversed(self.listbox.curselection()):
            self.listbox.delete(i)
        self._update_list_hint()

    def _on_clear_list(self):
        self.listbox.delete(0, tk.END)
        self._update_list_hint()
        self._append_log("已清空待处理列表。")

    def _on_browse_outdir(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.var_outdir.set(str(Path(d)))
            self.ent_outdir.configure(foreground=_NORMAL_FG)

    def _on_browse_mapping(self):
        f = filedialog.askopenfilename(title="选择映射对照表 CSV",
                                       filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
        if f:
            self.var_mapping.set(str(Path(f)))
            self.ent_mapping.configure(foreground=_NORMAL_FG)

    def _on_clear_analysis_cache(self):
        """v1.9.0：清除输出目录下的分析缓存（<输出目录>/cache/*.json）。

        v1.9.4：连带清除 <输出目录>/cache/plans/ 下的首轮冻结 plan（两者
        同为含敏感原文的分析工件，用户「清除」意图覆盖两者）。
        v2.0.1：连带清除各批次输出文件夹中的遮蔽台账 CSV（遮蔽台账_*.csv，
        与映射表同级敏感面——含全部遮蔽 op 的明文原文，与映射表联动清除）。
        并行联动清除 决策留痕_*.csv（与映射表同目录同
        敏感级的新增产物，镜像台账的扫收/删除/文案模式）。
        缓存只影响复核轮提速，删除后下次处理自动重建，脱敏结果不变。
        """
        outdir = self.var_outdir.get().strip()
        if not outdir or outdir == self._outdir_hint:
            # v2.1.9：补留空口径指引——输出目录留空时缓存/台账
            # 写在源文件所在目录（analysis_cache 同源口径），只改文案。
            messagebox.showinfo(
                "提示", "请先在“输出目录”中填写输出目录，再清除分析缓存。\n"
                "若处理时输出目录留空，请填入当时源文件所在目录。")
            return
        root = Path(outdir)
        # v2.0.1：遮蔽台账与映射表同批次目录存放（脱敏输出_*/），按文件名
        # 模式扫收（含直接落在输出根的边缘布局）；映射表 CSV 不在清除范围
        # （维持既有口径，台账是本次联动新增的覆盖对象）。
        # 决策留痕 CSV 同模式并行扫收（同目录同敏感级）。
        ledger_files = [p for p in sorted(root.glob("脱敏输出_*/遮蔽台账_*.csv"))
                        if p.is_file()]
        ledger_files += [p for p in sorted(root.glob("遮蔽台账_*.csv"))
                         if p.is_file()]
        audit_files = [p for p in sorted(root.glob("脱敏输出_*/决策留痕_*.csv"))
                       if p.is_file()]
        audit_files += [p for p in sorted(root.glob("决策留痕_*.csv"))
                        if p.is_file()]
        cache_dir = root / "cache"
        if not cache_dir.is_dir():
            if not ledger_files and not audit_files:
                self._append_log(f"清除分析缓存：未发现缓存目录 {cache_dir}，无需清除。")
                messagebox.showinfo("提示", f"未发现分析缓存目录：\n{cache_dir}")
                return
            n = self._delete_ledger_files(ledger_files)
            n_audit = self._delete_audit_files(audit_files)
            audit_seg = (f"、决策留痕 {n_audit} 个" if audit_files else "")
            self._append_log(f"已清除遮蔽台账 {n} 个{audit_seg}（含敏感原文，与映射表同规格）。"
                             "脱敏结果不受影响。")
            messagebox.showinfo("提示", f"已清除遮蔽台账 {n} 个{audit_seg} 文件。")
            return
        files = [p for p in cache_dir.glob("*.json") if p.is_file()]
        plans_dir = cache_dir / "plans"  # v1.9.4：首轮冻结 plan 目录
        plan_files = ([p for p in plans_dir.glob("*.json") if p.is_file()]
                      if plans_dir.is_dir() else [])
        if not files and not plan_files and not any(cache_dir.iterdir()) \
                and not ledger_files and not audit_files:
            self._append_log(f"清除分析缓存：{cache_dir} 已是空目录，无需清除。")
            messagebox.showinfo("提示", f"缓存目录为空：\n{cache_dir}")
            return
        ledger_note = (f"与 {len(ledger_files)} 个遮蔽台账文件（含敏感原文）"
                       if ledger_files else "")
        audit_note = (f"与 {len(audit_files)} 个决策留痕文件（含敏感原文）"
                      if audit_files else "")
        if not messagebox.askokcancel(
                "清除分析缓存",
                f"将删除缓存目录中的 {len(files)} 个 OCR 缓存文件与 "
                f"{len(plan_files)} 个首轮冻结 plan 文件{ledger_note}{audit_note}：\n"
                f"{cache_dir}\n\n"
                "删除后下次处理需重新 OCR 与首轮检测（较慢），脱敏结果不受"
                "影响。汇总映射表未删除（复核重跑需要），如需清除请手动"
                "删除。确定清除吗？"):
            self._append_log("清除分析缓存：已取消。")
            return
        n = 0
        for p in files + plan_files:
            try:
                p.unlink()
                n += 1
            except OSError as e:
                self._append_log(f"清除分析缓存：删除 {p.name} 失败：{e}")
        n += self._delete_ledger_files(ledger_files)
        n += self._delete_audit_files(audit_files)
        try:  # 目录清空后一并移除（best-effort，失败不影响）
            if plans_dir.is_dir() and not any(plans_dir.iterdir()):
                plans_dir.rmdir()
            if cache_dir.is_dir() and not any(cache_dir.iterdir()):
                cache_dir.rmdir()
        except OSError:
            pass
        audit_seg = (f"、决策留痕 {len(audit_files)} 个" if audit_files else "")
        self._append_log(f"已清除分析缓存：{n} 个文件（含首轮冻结 plan "
                         f"{len(plan_files)} 个、遮蔽台账 {len(ledger_files)} 个"
                         f"{audit_seg}，"
                         f"原 {cache_dir}）。下次处理将重新检测并自动重建。")

    def _delete_ledger_files(self, ledger_files) -> int:
        """v2.0.1：删除遮蔽台账 CSV（best-effort），返回成功数。"""
        n = 0
        for p in ledger_files:
            try:
                p.unlink()
                n += 1
            except OSError as e:
                self._append_log(f"清除遮蔽台账：删除 {p.name} 失败：{e}")
        return n

    def _delete_audit_files(self, audit_files) -> int:
        """删除决策留痕 CSV（best-effort，镜像
        _delete_ledger_files 模式），返回成功数。"""
        n = 0
        for p in audit_files:
            try:
                p.unlink()
                n += 1
            except OSError as e:
                self._append_log(f"清除决策留痕：删除 {p.name} 失败：{e}")
        return n

    def _add_paths(self, paths):
        """添加文件/文件夹：文件夹递归收集支持的格式，自动去重。"""
        added, ignored = 0, 0
        existing = set(self.listbox.get(0, tk.END))
        for p in paths:
            pp = Path(p)
            if pp.is_dir():
                files = sorted(f for f in pp.rglob("*")
                               if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS)
                targets = files
            elif pp.is_file():
                targets = [pp] if pp.suffix.lower() in SUPPORTED_EXTS else []
                if not targets:
                    ignored += 1
            else:
                targets = []
            for f in targets:
                sp = str(f)
                if sp in existing:
                    continue
                existing.add(sp)
                self.listbox.insert(tk.END, sp)
                added += 1
        if added:
            self._append_log(f"已添加 {added} 个待处理文件。")
        if ignored:
            self._append_log(f"有 {ignored} 个路径不是支持的文档格式，已忽略。")
        self._update_list_hint()

    # ------------------------------------------------------------------
    # 拖拽导入（tkinterdnd2，可选）
    # ------------------------------------------------------------------
    def _bind_dnd(self):
        if not self._dnd_available:
            return
        try:
            import tkinterdnd2
            # tkdnd 的 drop target 按“悬停控件”生效：只注册到 root 时，
            # 鼠标悬停在 listbox/text 等子控件上无法触发拖放，必须一并注册。
            for widget in (self.root, self.listbox, self.txt_words, self.txt_log):
                try:
                    widget.drop_target_register(tkinterdnd2.DND_FILES)
                    widget.dnd_bind("<<Drop>>", self._on_drop)
                except Exception:
                    continue
        except Exception:
            self._dnd_available = False

    def _on_drop(self, event):
        try:
            # 注意：Anaconda 的 tkinter 没有 Misc.tk_splitlist，直接用底层 tkapp.splitlist
            paths = self.root.tk.splitlist(event.data)
        except Exception as e:
            self._append_log(f"拖拽解析失败：[{type(e).__name__}] {e}")
            return
        if paths:
            self._append_log(f"检测到拖入 {len(paths)} 项。")
        self._add_paths(list(paths))

    # ------------------------------------------------------------------
    # 处理流程（子线程 + after 调度）
    # ------------------------------------------------------------------
    def _on_start(self):
        if self._worker is not None and self._worker.is_alive():
            return
        files = list(self.listbox.get(0, tk.END))
        if not files:
            messagebox.showwarning("提示", "请先添加待处理文件。")
            self._append_log("未选择任何文件，请先添加。")
            return

        outdir = self.var_outdir.get().strip()
        if outdir == self._outdir_hint:
            outdir = ""
        mapping = self.var_mapping.get().strip()
        if mapping == self._mapping_hint:
            mapping = ""

        options = MaskOptions(
            mask_company=self.var_company.get(),
            mask_person=self.var_person.get(),
            mask_amount=self.var_amount.get(),
            mask_contact=self.var_contact.get(),
            mask_address=self.var_address.get(),
            ocr_image_text=self.var_ocr.get(),
            xls_to_xlsx=self.var_xls2xlsx.get(),
            mask_stamp=self.var_stamp.get(),
            deep_recheck=self.var_deep_recheck.get(),
            words_only=self.var_words_only.get(),  # v2.1.8 仅指定词模式
            custom_words=self._read_custom_words(),
            mapping_csv=mapping,
            output_dir=outdir,
            person_loose=self.var_person_loose.get(),
            include_institutions=self.var_include_inst.get(),
            amount_mode=("scale" if self.var_amount.get() and self.var_amount_mode.get() == "scale"
                         else "mask"),
        )
        if options.words_only and not options.custom_words:
            messagebox.showwarning(
                "无法开始",
                "已勾选「仅脱敏这些词」，但③区自定义敏感词为空——请先填写指定词或取消该勾选。",
                parent=self)
            return
        if options.mask_amount and options.amount_mode == "scale":
            self._append_log("金额缩放脱敏：将乘以本批次随机系数 K（公式写入汇总映射表）。")
        if not (options.mask_company or options.mask_person or options.mask_amount
                or options.mask_contact or options.mask_address
                or options.custom_words or options.mask_stamp):
            self._append_log("提示：当前未启用任何脱敏规则，输出将与原文件内容相同。")
        elif not options.custom_words and not options.mapping_csv:
            # 自定义敏感词必要性提醒（引擎识别有覆盖上限，见③区说明）
            self._append_log("提示：本次未填写自定义敏感词。引擎按内置规则识别，词组型公司名、"
                             "生僻人名、品牌/项目代号等可能漏检；处理后请务必通过“复核映射表”"
                             "核对结果，发现漏检可将该词填入③区后重跑。")

        self.btn_start.config(state="disabled")
        self.progress["value"] = 0
        self._append_log(f"开始处理：共 {len(files)} 个文件。")
        # v1.3 误报样本回收：对照表覆盖重跑前，把用户删除的人名行记入 误报样本.csv
        if mapping:
            from .utils import append_false_positive_samples
            try:
                deleted = self._collect_deleted_person_rows(mapping)
                if deleted:
                    sample_path = append_false_positive_samples(deleted)
                    self._append_log(f"已回收 {len(deleted)} 条误报样本 → "
                                     f"{sample_path}（用于后续识别调优）")
            except Exception as e:
                self._append_log(f"误报样本回收失败（不影响处理）：{e}")
        self._worker = threading.Thread(target=self._worker_main,
                                        args=(files, options), daemon=True, name="MaskWorker")
        self._worker.start()

    def _worker_main(self, files, options):
        """子线程入口：只通过 after() 与界面通信。"""

        def log_bridge(msg):
            self.root.after(0, self._append_log, msg)

        def progress_bridge(done, total):
            self.root.after(0, self._set_progress, done, total)

        summary = None
        try:
            summary = run_batch(files, options, log_bridge, progress_bridge)
        except Exception as e:  # 兜底：调度层自身异常也不能吞掉
            log_bridge(f"[程序异常] {type(e).__name__}: {e}")
        finally:
            self.root.after(0, lambda: self._on_worker_done(summary))

    def _on_worker_done(self, summary=None):
        self.btn_start.config(state="normal")
        self._refresh_engine_indicator()  # v1.8.2：状态栏显示最近就绪的 OCR 引擎
        if summary is None:
            return
        mapping = summary.get("mapping", "")
        if mapping:
            self._last_mapping_path = mapping
            self.btn_review.config(state="normal")
        msg = (f"成功 {summary['ok']} 个，失败 {summary['fail']} 个，"
               f"共脱敏 {summary.get('masked', 0)} 处，"
               f"用时 {int(summary.get('seconds', 0))} 秒。")
        out_dir = summary.get("output_dir", "")
        if out_dir:
            msg += f"\n\n输出文件夹：\n{out_dir}"
        if mapping:
            msg += f"\n\n汇总映射表：\n{mapping}\n\n可点「复核映射表...」人工复核（删除误报行后保存即可严格重跑）。"
        # 批内已抽取文本随 summary 下传给入口①后台重扫
        # （reuse_texts 复用，命中键免二次提取/OCR）。键缺席/为空 → 传空
        # dict，行为与现状一致（生产端 dispatcher 另行实现，消费端全 .get）。
        batch_texts = summary.get("batch_texts") or {}
        self._show_done_dialog(
            "处理完成（存在失败项）" if summary["fail"] else "处理完成",
            msg, out_dir, batch_texts=batch_texts)

    # ------------------------------------------------------------------
    # 状态栏引擎指示（v1.8.2）
    # ------------------------------------------------------------------
    def _set_active_engine_name(self, name: str):
        """状态栏引擎指示刷新（批处理结束时调用，显示最近就绪的引擎名）。"""
        self._lbl_engine.configure(text=f"引擎：{name}" if name else "引擎：待批处理")

    def _refresh_engine_indicator(self):
        """从 ocr_engine 模块读取最近就绪的引擎名刷新状态栏（读取失败不致命）。"""
        try:
            from .ocr_engine import get_last_engine_name
            self._set_active_engine_name(get_last_engine_name())
        except Exception:
            pass

    def _show_done_dialog(self, title: str, msg: str, out_dir: str = "",
                          batch_texts: dict | None = None):
        """完成对话框：带“打开输出文件夹”按钮；关闭后未手动打开则自动打开
        （保留 v1.6.3 的自动打开行为）。模态，居中于主窗口。
        batch_texts（{str(源路径): 文本}）透传入口①后台
        重扫作 reuse_texts 复用语料；None/空 = 不复用（行为与现状一致）。"""
        opened = {"flag": False}

        def open_folder():
            if out_dir and Path(out_dir).is_dir():
                try:
                    os.startfile(out_dir)  # Windows 资源管理器
                    opened["flag"] = True
                except OSError as e:
                    self._append_log(f"打开输出文件夹失败：{e}")

        win = tk.Toplevel(self.root)
        win.title(title)
        win.resizable(False, False)
        win.transient(self.root)
        frm = ttk.Frame(win, padding=(16, 12))
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, text=msg, justify="left").pack(anchor="w")
        # 候选词推荐入口①：完成弹窗尾部候选词推荐区。
        # 纯增量行：文件列表为空/扫描组件未就绪/候选 <3 或全部已登记/同批
        # 已提示 → 不产出任何控件，完成弹窗零视觉差；关闭弹窗即弃扫描会话。
        _g2_holder = self._attach_candidate_recommendation(
            frm, batch_texts=batch_texts)
        if _g2_holder is not None:
            _g2_holder["row"].pack(fill=tk.X, pady=(10, 0))
            win.bind("<Destroy>", lambda _e, _h=_g2_holder, _w=win:
                     self._detach_candidate_recommendation(_h, _e, _w))
        btns = ttk.Frame(frm)
        btns.pack(fill=tk.X, pady=(12, 0))
        if out_dir:
            ttk.Button(btns, text="打开输出文件夹", command=open_folder).pack(side=tk.LEFT)
        ok = ttk.Button(btns, text="确定", command=win.destroy, style="Primary.TButton")
        ok.pack(side=tk.RIGHT)
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        win.grab_set()
        win.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - win.winfo_width()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - win.winfo_height()) // 3)
        win.geometry(f"+{x}+{y}")
        ok.focus_set()
        self.root.wait_window(win)
        if not opened["flag"] and out_dir and Path(out_dir).is_dir():
            try:
                os.startfile(out_dir)  # v1.6.3：点确定后自动打开（未手动打开时）
            except OSError as e:
                self._append_log(f"打开输出文件夹失败：{e}")

    # ------------------------------------------------------------------
    # 映射表复核对话框（v1.3）：默认按置信度升序（低在前），列可点击排序，
    # 支持删除误报行后保存；删除的人名行在下次对照表覆盖重跑时回收到
    # 误报样本.csv。
    # ------------------------------------------------------------------
    def _open_mapping_review(self):
        path = getattr(self, "_last_mapping_path", "")
        if not path or not Path(path).exists():
            messagebox.showinfo("提示", "尚无汇总映射表。请先处理文件。")
            return
        try:
            import csv as _csv
            try:
                with open(path, encoding="utf-8-sig") as f:
                    rows = list(_csv.reader(f))
            except UnicodeDecodeError:
                # 用户在 Excel 中另存为 ANSI/GBK 后复核（v1.6.5：此前按钮无响应）
                with open(path, encoding="gbk", errors="replace") as f:
                    rows = list(_csv.reader(f))
        except OSError as e:
            messagebox.showerror("读取失败", f"无法读取映射表：\n{e}")
            return
        if not rows:
            messagebox.showinfo("提示", "映射表为空。")
            return
        header, data = rows[0], rows[1:]

        win = tk.Toplevel(self.root)
        win.title(f"复核映射表 - {Path(path).name}")
        win.geometry("1150x620")
        cols = list(header)

        tree = ttk.Treeview(win, columns=cols, show="headings")
        vsb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        conf_rank = {"低": 0, "中": 1, "高": 2}
        sort_state = {}  # col -> 是否降序

        def sort_by(col, descending):
            idx = cols.index(col)
            def key(row):
                v = row[idx]
                if col == "次数":
                    try:
                        return (0, int(v))
                    except ValueError:
                        return (1, 0)
                if col == "置信度":
                    return conf_rank.get(v, 9)  # 低=0 在前；空（非人名）最后
                return v
            data.sort(key=key, reverse=descending)
            tree.delete(*tree.get_children())
            for r in data:
                tree.insert("", tk.END, values=r)
            sort_state[col] = descending

        for c in cols:
            tree.heading(c, text=c,
                         command=lambda cc=c: sort_by(cc, not sort_state.get(cc, False)))
        for c in cols:
            tree.column(c, width=170 if c in ("原文", "位置") else 110, anchor="w")
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        def default_sort():
            sort_by("置信度", descending=False)  # 默认：置信度升序（低在前）

        btns = ttk.Frame(win)
        btns.pack(fill=tk.X, side=tk.BOTTOM)

        def delete_selected():
            sel = tree.selection()
            if not sel:
                return
            for item in sel:
                values = tree.item(item, "values")
                tree.delete(item)
                for r in list(data):
                    if list(r) == list(values):
                        data.remove(r)
                        break

        def save():
            import csv as _c
            kept = [list(tree.item(i, "values")) for i in tree.get_children()]
            try:
                _f = open(path, "w", newline="", encoding="utf-8-sig")
            except OSError as _e:
                # 稳定体验补项：CSV 常被 Excel/WPS 打开
                # 占用——此前 PermissionError 在 Tk 回调内被吞，用户点保存
                # 无任何反馈、编辑成果静默丢失
                from tkinter import messagebox as _mb
                _mb.showerror(
                    "保存失败",
                    "映射表正被其他程序占用（如 Excel/WPS），\n"
                    "请关闭该程序后重试。\n\n技术细节：" + str(_e))
                return
            with _f as f:
                w = _c.writer(f)
                w.writerow(header)
                w.writerows(kept)
            # v1.6.2 修复：保存时立即回收删除的人名行 → 误报样本.csv。
            # 原设计在"重跑前"对比旧表与新表，但 v1.5 起保存即把本表设为
            # 对照表覆盖，重跑前对比退化为"自己比自己"，回收永远为空。
            new_keys = {(r[0], r[1], r[3] if len(r) > 3 else "") for r in kept if r}
            deleted = [(r[0], r[1], r[3] if len(r) > 3 else "", "",
                        r[6] if len(r) > 6 else "")
                       for r in data
                       if r and len(r) > 3 and r[2] == "person"
                       and (r[0], r[1], r[3] if len(r) > 3 else "") not in new_keys]
            if deleted:
                try:
                    from .utils import append_false_positive_samples
                    sample_path = append_false_positive_samples(deleted)
                    self._append_log(f"已回收 {len(deleted)} 条误报样本 → {sample_path}")
                except Exception as e:
                    self._append_log(f"误报样本回收失败（不影响保存）：{e}")
            # v1.5 联动：保存后自动将该表设为对照表覆盖，下次处理即严格按其执行
            self._last_mapping_path = path
            self.var_mapping.set(path)
            # v1.6.5：同步弹窗内数据快照——重复点“保存修改”不会向
            # 误报样本.csv 重复追加同一批删除行
            data.clear()
            data.extend(kept)
            self._append_log(f"映射表已保存并自动设为对照表覆盖：{path}")
            self._append_log("下次「开始处理」将严格按此映射表执行（删除的行不再遮盖）。")
            messagebox.showinfo("已保存", "映射表已更新，并自动设为对照表覆盖。\n"
                                          "下次「开始处理」将严格按新映射执行。",
                                parent=win)

        ttk.Button(btns, text="删除选中行", command=delete_selected).pack(side=tk.LEFT, padx=6, pady=6)
        ttk.Button(btns, text="保存修改", command=save).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text="关闭", command=win.destroy).pack(side=tk.RIGHT, padx=6)
        default_sort()

    def _collect_deleted_person_rows(self, new_mapping_csv: str) -> list:
        """对照表覆盖重跑前：对比上次汇总映射表，找出被用户删除的人名行（误报样本）。"""
        import csv as _csv
        old_path = getattr(self, "_last_mapping_path", "")
        if not old_path or not Path(old_path).exists() \
                or Path(old_path).resolve() == Path(new_mapping_csv).resolve():
            return []
        try:
            with open(old_path, encoding="utf-8-sig") as f:
                old_rows = list(_csv.reader(f))
            with open(new_mapping_csv, encoding="utf-8-sig") as f:
                new_rows = list(_csv.reader(f))
        except OSError:
            return []
        if not old_rows or not new_rows:
            return []
        new_keys = {(r[0], r[1], r[3] if len(r) > 3 else "")
                    for r in new_rows[1:] if r}
        deleted = []
        for r in old_rows[1:]:
            if not r or len(r) < 4:
                continue
            if r[2] != "person":
                continue
            key = (r[0], r[1], r[3] if len(r) > 3 else "")
            if key not in new_keys:
                deleted.append((r[0], r[1], key[2], "", r[6] if len(r) > 6 else ""))
        return deleted

    def _set_progress(self, done, total):
        """进度条更新（节流）：高频回调时限制 UI 刷新频率，避免卡顿。"""
        if total <= 0:
            self.progress["value"] = 0
            return
        value = max(0.0, min(100.0, float(done) / float(total) * 100.0))
        now = time.monotonic()
        finished = float(done) >= float(total)
        if not finished and (now - self._last_progress_ts) < 0.1 \
                and abs(value - self._last_progress_value) < 1.0:
            return
        self._last_progress_ts = now
        self._last_progress_value = value
        self.progress["value"] = value

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def _append_log(self, msg: str):
        self.txt_log.configure(state="normal")
        self.txt_log.insert(tk.END, f"[{now_hms()}] {msg}\n")
        self.txt_log.see(tk.END)
        self.txt_log.configure(state="disabled")

    def _log_ocr_mode(self):
        """v1.8.2：启动时输出当前 OCR 模式与预期引擎；存量 v5 配置另行提示。"""
        try:
            from . import app_config
            mode = app_config.resolve_ocr_mode()
            if mode == app_config.OCR_MODE_LEGACY_V5:
                self._append_log("检测到存量 v5 配置：本次批处理仍将使用 PP-OCRv5"
                                 "（在上方“OCR 模式”中切换后新模式才生效）。")
                return
            expected = {
                app_config.OCR_MODE_AUTO: "PP-OCRv6-ONNX（CPU=small / GPU=medium 自动）",
                app_config.OCR_MODE_CPU: "PP-OCRv6-ONNX-small(cpu)",
                app_config.OCR_MODE_GPU_V5: "PP-OCRv5-server(gpu)（需本机 Paddle GPU 环境）",
                app_config.OCR_MODE_GPU_V6: "PP-OCRv6-ONNX-medium(GPU-EXT)（需 v6_gpu_env 环境）",
            }.get(mode, "")
            self._append_log(f"OCR 模式：{self.var_ocr_mode.get()}（预期引擎：{expected}）。")
        except Exception:
            pass  # 模式提示属于体验增强，绝不阻塞启动

    def _log_startup(self):
        self._append_log(f"就绪（v{APP_VERSION}）。本工具完全本地运行，不访问网络。")
        self._log_ocr_mode()  # v1.8.2：当前 OCR 模式与预期引擎（存量 v5 用户提示）
        if not self._dnd_available:
            self._append_log("提示：未安装 tkinterdnd2，拖拽导入不可用（可使用“添加文件...”按钮）。")
        frozen = getattr(sys, "frozen", False)  # PyInstaller 打包环境
        if frozen:
            from .ocr_engine import discover_external_paddle_env
            found = discover_external_paddle_env()
            if found:
                self._append_log(f"已自动接入本机 Paddle 环境：{found[0]}"
                                 f"（图片内文字脱敏可勾选使用）")
        _deps = probe_optional_deps()  # ⑧：双跑修复（docx/lxml/jieba 真实试加载×2 拖慢启动）
        for label, (ok, hint) in _deps.items():
            if ok:
                continue
            if frozen and label.startswith("PaddleOCR"):
                # exe 内置 PP-OCRv6-ONNX（CPU 默认路径，不依赖 Paddle），
                # 旧版误导文案不实已废（口径见 paddle_missing_notice）。仅显式
                # 选用旧 v5 链路（gpu_v5/存量 legacy_v5）时才提示 Paddle 安装/切换。
                from . import app_config
                notice = paddle_missing_notice(app_config.resolve_ocr_mode(), frozen)
                if notice:
                    self._append_log(notice)
            else:
                self._append_log(f"依赖自检：{label} 未安装（{hint}）")
        if all(ok for _, (ok, _) in _deps.items()):
            self._append_log("依赖自检：全部可选组件可用。")

    # ------------------------------------------------------------------
    # v2.1.0 候选词推荐：双入口 + 扫描会话零持久化闭环。
    # 本节全部为新增方法；批文件集指纹只存内存 set（程序退出即弃），
    # 扫描数据全程内存，关闭即 session.close()；日志与弹窗只出现计数。
    # ------------------------------------------------------------------
    def _candidate_seen_fingerprints(self) -> set:
        """同批不重复提示的记忆集（惰性初始化，内存态，退出即弃）。"""
        seen = getattr(self, "_g2_seen_fingerprints", None)
        if seen is None:
            seen = set()
            self._g2_seen_fingerprints = seen
        return seen

    def _current_file_paths(self) -> list:
        return list(self.listbox.get(0, tk.END))

    def _candidate_faces_snapshot(self) -> candidate_flow.RegistrationFaces:
        """已登记词五登记面快照（与装载端同源读取）：brands/aliases/org_suffixes
        取自 brands.json 现文件（loader 同一函数），③区取当前 Text 内容，
        对照表覆盖键取映射 CSV（load_mapping_csv 与引擎同一解析）。"""
        brands = aliases = org_adds = ()
        try:
            from .brand_config import load_brand_config, resolve_config_path
            cfg = load_brand_config(path=resolve_config_path())
            brands = tuple(cfg.brands)
            aliases = tuple(cfg.aliases)
            org_adds = tuple(cfg.org_suffixes)
        except Exception:
            pass
        custom = ()
        try:
            custom = tuple(self._read_custom_words())
        except Exception:
            pass
        mapping = ()
        try:
            mp = self.var_mapping.get().strip()
            if mp and mp != self._mapping_hint and Path(mp).is_file():
                from .rules_engine import load_mapping_csv
                mapping = tuple(load_mapping_csv(mp))
        except Exception:
            pass
        return candidate_flow.RegistrationFaces(
            brands=brands, aliases=aliases, custom_words=custom,
            mapping_keys=mapping, org_suffix_adds=org_adds)

    def _on_discover_candidates(self):
        """入口②：③区常驻按钮。语料=当前文件列表；为空先弹文件夹选择
        （所选文件夹只作本次扫描语料本地收集，不灌入
        主处理列表——所见即所扫，但误点「开始处理」不会全 folder 跑脱敏）。
        扫描组件未就绪 → 「扫描组件未就绪」信息框（组件缺失仅提示，不崩溃）。"""
        try:
            mod, err = candidate_flow.load_scanner_module()
            if mod is None:
                messagebox.showinfo(candidate_flow.SCANNER_NOT_READY_TITLE, err)
                return
            paths = self._current_file_paths()
            if not paths:
                d = filedialog.askdirectory(title="选择要扫描候选词的文件夹")
                if not d:
                    return
                # 本地收集不入列表：后缀过滤+排序+仅文件，
                # 与 _add_paths 同一过滤口径（SUPPORTED_EXTS），纯扫描语料。
                paths = [str(f) for f in
                         candidate_flow.collect_supported_files(d)]
                if not paths:
                    messagebox.showinfo("提示", "所选文件夹中未发现支持的文档格式。")
                    return
            self._append_log(f"候选词扫描：开始扫描 {len(paths)} 个文件"
                             "（结果仅供勾选确认，全程内存、关闭即弃）。")
            CandidateReviewDialog(self, paths)
        except Exception as e:  # 兜底：入口失败绝不拖垮主窗口
            self._append_log(f"[程序异常] 候选词入口失败：{type(e).__name__}: {e}")

    def _attach_candidate_recommendation(self, parent, batch_texts: dict | None = None):
        """入口①：完成弹窗推荐区。后台重扫当前文件列表，命中键复用批内
        已抽取文本（summary["batch_texts"] → reuse_texts，
        免二次提取/OCR；键缺席时退化为直接重扫文件=现状），候选 ≥3 且存在
        未登记候选且同批未提示过 → 展示推荐行（文案只含计数）；否则安静
        移除。任何异常都不影响完成弹窗本体（返回 None=零视觉差）。"""
        try:
            paths = self._current_file_paths()
            if not paths:
                return None
            fingerprint = candidate_flow.batch_fingerprint(paths)
            holder = {"row": None, "lbl": None, "btn": None, "session": None,
                      "paths": paths, "fingerprint": fingerprint, "done": False,
                      # 批内文本随 holder 传后台线程（防御式取值；
                      # 生产端缺键 → 空 dict → to_reuse_texts → None=不复用）
                      "batch_texts": dict(batch_texts or {})}
            # 已登记词快照（主线程采集）——后台扫描
            # 线程不触碰 tkinter 控件；快照随 holder 传 worker。
            try:
                _faces = self._candidate_faces_snapshot()
                _cfg: list = []
                for _name, _members in _faces.face_members():
                    _cfg.extend(m for m in _members if isinstance(m, str))
                holder["configured_words"] = tuple(dict.fromkeys(_cfg))
            except Exception:
                holder["configured_words"] = ()
            row = ttk.Frame(parent)
            lbl = ttk.Label(row, text=candidate_flow.RECOMMEND_SCANNING_TEXT)
            lbl.pack(side=tk.LEFT)
            btn = ttk.Button(row, text=candidate_flow.RECOMMEND_BUTTON,
                             state="disabled",
                             command=lambda: self._open_review_from_holder(holder))
            btn.pack(side=tk.LEFT, padx=(8, 0))
            holder.update({"row": row, "lbl": lbl, "btn": btn})

            def scan_main():
                mod, err = candidate_flow.load_scanner_module()
                session = None
                if mod is not None:
                    try:
                        session = mod.scan_sources(
                            paths,
                            configured_words=holder.get("configured_words") or None,
                            # {str(路径): 文本} → {Path(路径): 文本}
                            # （scan_sources 键契约）；空 → None（=不传，现状）
                            reuse_texts=candidate_flow.to_reuse_texts(
                                holder.get("batch_texts")))
                    except Exception:
                        session = None

                def deliver():
                    self._attach_recommendation_finish(holder, session)

                try:
                    self.root.after(0, deliver)
                except Exception:
                    try:  # 界面已销毁：直接丢弃（零持久化闭环）
                        if session is not None:
                            session.close()
                    except Exception:
                        pass

            threading.Thread(target=scan_main, daemon=True,
                             name="CandidateRecommendScan").start()
            return holder
        except Exception:
            return None

    def _attach_recommendation_finish(self, holder, session):
        """入口①后台扫描完成（主线程）：过推荐闸 → 填充推荐行或安静移除。"""
        try:
            if holder.get("done"):  # 完成弹窗已关闭：会话即弃
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass
                return
            holder["done"] = True
            row = holder["row"]
            if session is None:  # 组件未就绪/扫描失败：安静收起（不吵）
                row.destroy()
                return
            stats = {}
            try:
                _, stats = candidate_flow.build_review_items(
                    list(getattr(session, "candidates", []) or []),
                    self._candidate_faces_snapshot())
            except Exception:
                pass
            n_visible = int(stats.get("visible_n", 0))
            unreg = int(stats.get("unregistered_n", 0))
            seen = self._candidate_seen_fingerprints()
            if not candidate_flow.should_recommend(n_visible, unreg,
                                                   holder["fingerprint"], seen):
                try:
                    session.close()
                except Exception:
                    pass
                row.destroy()
                return
            candidate_flow.mark_batch_seen(holder["fingerprint"], seen)
            holder["session"] = session
            holder["lbl"].configure(
                text=candidate_flow.RECOMMEND_TEXT.format(n=n_visible))
            holder["btn"].configure(state="normal")
        except Exception:
            try:
                holder["row"].destroy()
            except Exception:
                pass

    def _open_review_from_holder(self, holder):
        """入口①按钮：把已完成扫描的会话移交候选清单对话框（免二次扫描）。"""
        try:
            holder["btn"].configure(state="disabled")
        except Exception:
            pass
        session = holder.get("session")
        holder["session"] = None
        CandidateReviewDialog(self, holder.get("paths") or
                              self._current_file_paths(), session=session)

    def _detach_candidate_recommendation(self, holder, event=None, top=None):
        """完成弹窗关闭：未转移的扫描会话立即丢弃（零持久化闭环）。"""
        try:
            if top is not None and event is not None \
                    and getattr(event, "widget", None) is not top:
                return  # 子控件销毁事件不触发（只认弹窗本体）
        except Exception:
            pass
        holder["done"] = True
        session = holder.get("session")
        holder["session"] = None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 关闭窗口
    # ------------------------------------------------------------------
    def _on_close(self):
        if self._worker is not None and self._worker.is_alive():
            if not messagebox.askokcancel("退出", "仍有任务在后台处理，确定退出吗？（退出将中断未完成的任务）"):
                return
        self.root.destroy()


class CandidateReviewDialog:
    """候选词确认对话框。

    模态 Toplevel：Treeview 列=选择（☐/☑ 文本切换）/候选词/置信分↓默认
    （同分频次↓）/频次/来源文件数/样例上下文（首现 ±15 字，≥7 位数字
    打码）/徽标；Top50/页分页器 + 搜索框（实时过滤）+ 全选本页/反选/清空
    （作用域=当前页可见行，禁选行不参与）+「显示已隐藏的防呆拦截项({H})」
    开关（灰显+原因）；行单击→详情弹窗（±60 字+前 5 来源文件「等共 N 份」
    收尾）；与现有机构后缀增词撞车的候选（S5）勾选时必弹二次确认；底部
    「写入 brands.json({K})」
    主按钮（K=0 禁用）+取消。

    零持久化闭环：扫描会话（含全部候选/上下文/来源）全程内存；取消/关闭/
    写入成功 → session.close() 即弃。日志与弹窗只出现计数，不出现候选词面。
    """

    def __init__(self, window: "MainWindow", paths: list, session=None):
        self._mw = window
        self._root = window.root
        self._paths = [str(p) for p in (paths or [])]
        self._session = session            # 入口①移交的现成会话；None=对话框内扫描
        self._cancel = threading.Event()
        self._thread = None
        self._closed = False
        self._written_done = False
        self._interrupted = False
        self._failed_n = 0
        self._items = []
        self._stats = {}
        self._checked = set()              # 已勾选词面（只含可勾行）
        self._row_items = {}               # iid -> ReviewItem（当前页）
        self._page = 1
        self._sort_key = "score"           # 默认：置信分降序
        self._sort_desc = True
        self._last_progress_ts = 0.0
        self._var_search = tk.StringVar()
        self._var_show_hidden = tk.BooleanVar(value=False)
        self._var_scan = tk.StringVar(value="正在扫描（0/0）……")
        # 已登记词快照：主线程采集一次（线程安全），
        # 传扫描器作外部可信容器（configured_words）——用户已配置词的内部
        # 残片在扫描期即隐藏；流程层另做精确/覆盖筛除。
        self._configured_words = ()
        if session is None and self._paths:
            try:
                faces = window._candidate_faces_snapshot()
                merged: list = []
                for _name, members in faces.face_members():
                    merged.extend(m for m in members if isinstance(m, str))
                self._configured_words = tuple(dict.fromkeys(merged))
            except Exception:
                self._configured_words = ()
        self._build()
        self._center()
        self._win.grab_set()
        self._win.protocol("WM_DELETE_WINDOW", self._on_close)
        if session is not None:
            self._win.after(0, lambda: self._on_scan_done(session))
        else:
            self._start_scan()
        self._win.wait_window(self._win)

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _build(self):
        win = self._win = tk.Toplevel(self._root)
        title = candidate_flow.REVIEW_DIALOG_TITLE.format(m=len(self._paths))
        win.title(title if self._paths else "候选词确认")
        win.geometry("1000x620")
        win.minsize(900, 560)
        win.transient(self._root)

        # 顶部黄条（部分失败/部分结果；默认隐藏）
        self._banner = tk.Label(win, text="", fg="#7A5900", bg="#FFF3CD",
                                anchor="w", padx=10, pady=3)

        # 扫描进度区（扫描完成后整块隐藏）
        self._frm_scan = ttk.Frame(win, padding=(12, 10))
        ttk.Label(self._frm_scan, textvariable=self._var_scan).pack(anchor="w")
        self._pb = ttk.Progressbar(self._frm_scan, orient="horizontal",
                                   mode="determinate", maximum=100)
        self._pb.pack(fill=tk.X, pady=(6, 4))
        ttk.Button(self._frm_scan, text="取消扫描", command=self._on_cancel_scan)\
            .pack(anchor="e")

        # 清单区（工具条 + 表格 + 分页）
        self._frm_list = ttk.Frame(win, padding=(12, 4))
        self._build_toolbar(self._frm_list)
        self._build_tree(self._frm_list)
        self._build_pager(self._frm_list)
        # 底部按钮条
        btns = ttk.Frame(win, padding=(12, 8))
        btns.pack(fill=tk.X, side=tk.BOTTOM)
        ttk.Label(btns, text=f"ⓘ {candidate_flow.REVIEW_HINT}",
                  foreground="#595959").pack(side=tk.LEFT)
        self._btn_write = ttk.Button(btns,
                                     text=candidate_flow.WRITE_BUTTON_DISABLED,
                                     command=self._on_write, state="disabled",
                                     style="Primary.TButton")
        self._btn_write.pack(side=tk.RIGHT)
        ttk.Button(btns, text="取消", command=self._on_close).pack(
            side=tk.RIGHT, padx=(0, 8))

    def _build_toolbar(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(bar, text="搜索：").pack(side=tk.LEFT)
        ent = ttk.Entry(bar, textvariable=self._var_search, width=24)
        ent.pack(side=tk.LEFT)
        ttk.Button(bar, text="全选本页", command=lambda: self._select_page(True))\
            .pack(side=tk.LEFT, padx=(10, 4))
        ttk.Button(bar, text="反选", command=lambda: self._select_page(None))\
            .pack(side=tk.LEFT, padx=4)
        ttk.Button(bar, text="清空选择", command=lambda: self._select_page(False))\
            .pack(side=tk.LEFT, padx=4)
        self._lbl_sel = ttk.Label(bar, text="已选 0 / 0 个")
        self._lbl_sel.pack(side=tk.LEFT, padx=(10, 0))
        self._cb_hidden = ttk.Checkbutton(
            bar, text="显示已隐藏的防呆拦截项(0)",
            variable=self._var_show_hidden, command=self._on_toggle_hidden)
        self._cb_hidden.pack(side=tk.RIGHT)
        self._var_search.trace_add("write", lambda *_: self._on_search_changed())

    def _build_tree(self, parent):
        cols = ("sel", "word", "score", "freq", "files", "ctx", "badge")
        heads = {"sel": "选择", "word": "候选词", "score": "置信分",
                 "freq": "频次", "files": "来源文件数", "ctx": "样例上下文",
                 "badge": "已配置标记"}
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True)
        self._tree = tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                         selectmode="none")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        for c in cols:
            tree.heading(c, text=heads[c], command=(lambda cc=c: self._on_sort(cc))
                         if c in ("word", "score", "freq", "files") else
                         (lambda: None))
        widths = {"sel": 48, "word": 170, "score": 64, "freq": 56,
                  "files": 76, "ctx": 340, "badge": 170}
        for c in cols:
            anchor = "center" if c in ("sel", "score", "freq", "files") else "w"
            stretch = c in ("ctx", "word")
            tree.column(c, width=widths[c], anchor=anchor, stretch=stretch)
        tree.tag_configure("dim", foreground="#9a9a9a")
        tree.tag_configure("warn", foreground="#B26A00")
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        tree.bind("<Button-1>", self._on_tree_click)

    def _build_pager(self, parent):
        bar = ttk.Frame(parent)
        bar.pack(fill=tk.X, pady=(4, 0))
        self._btn_prev = ttk.Button(bar, text="◀ 上一页",
                                    command=lambda: self._go_page(-1), width=9)
        self._btn_prev.pack(side=tk.LEFT)
        self._lbl_page = ttk.Label(
            bar, text=f"第 1/{cf_page_count(0)} 页（每页 {candidate_flow.TOP_PAGE} 条）")
        self._lbl_page.pack(side=tk.LEFT, padx=8)
        self._btn_next = ttk.Button(bar, text="下一页 ▶",
                                    command=lambda: self._go_page(1), width=9)
        self._btn_next.pack(side=tk.LEFT)

    # ------------------------------------------------------------------
    # 扫描（worker 线程；线程纪律：子线程 + after + 进度节流）
    # ------------------------------------------------------------------
    def _start_scan(self):
        mod, err = candidate_flow.load_scanner_module()
        if mod is None:
            messagebox.showinfo(candidate_flow.SCANNER_NOT_READY_TITLE, err,
                                parent=self._win)
            self._closed = True
            try:
                self._win.destroy()
            except Exception:
                pass
            return
        self._thread = threading.Thread(target=self._scan_main, args=(mod,),
                                        daemon=True, name="CandidateScanWorker")
        self._thread.start()

    def _scan_main(self, mod):
        def on_progress(done, total):
            if self._cancel.is_set():
                return False  # 礼貌请求停止（扫描器若不支持该协议则忽略）
            try:
                now = time.monotonic()
                if total and done < total and (now - self._last_progress_ts) < 0.1:
                    return None
                self._last_progress_ts = now
                self._win.after(0, self._set_scan_progress, done, total)
            except Exception:
                pass
            return None

        try:
            session = mod.scan_sources(self._paths, on_progress=on_progress,
                                       configured_words=self._configured_words
                                       or None)
        except Exception as exc:  # 全局失败降级
            self._after_main(self._on_scan_error, type(exc).__name__)
            return
        if self._closed:  # 对话框已关：会话即弃（零持久化闭环）
            try:
                session.close()
            except Exception:
                pass
            return
        if self._cancel.is_set():
            self._interrupted = True
        self._after_main(self._on_scan_done, session)

    def _after_main(self, fn, *args):
        """worker → 主线程调度；窗口已销毁时丢弃事件（含会话收尾）。"""
        try:
            self._win.after(0, lambda: fn(*args))
        except Exception:
            pass

    def _set_scan_progress(self, done, total):
        try:
            total = int(total or 0)
            done = int(done or 0)
            self._var_scan.set(f"正在扫描（{done}/{total}）……")
            self._pb["value"] = (0.0 if total <= 0
                                 else max(0.0, min(100.0, done / total * 100.0)))
        except Exception:
            pass

    def _on_cancel_scan(self):
        """取消扫描：礼貌请求停止（部分结果照常展示）；扫描器
        不支持停止协议时，关闭对话框同样保证零持久化。"""
        self._cancel.set()
        if self._thread is not None and self._thread.is_alive():
            self._var_scan.set("正在停止……（已扫描部分将照常展示）")
        else:
            self._on_close()

    def _on_scan_error(self, exc_name):
        if self._closed:
            return
        self._var_scan.set("扫描失败。")
        messagebox.showerror(
            candidate_flow.ALL_FAILED_TITLE,
            candidate_flow.ALL_FAILED_BODY.format(m=len(self._paths))
            + f"\n\n[{exc_name}]", parent=self._win)

    def _on_scan_done(self, session):
        if self._closed:
            try:
                session.close()
            except Exception:
                pass
            return
        self._session = session
        meta = dict(getattr(session, "meta", {}) or {})
        self._failed_n = int(meta.get("failed_files", 0) or 0)
        cands = list(getattr(session, "candidates", []) or [])
        try:
            self._items, self._stats = candidate_flow.build_review_items(
                cands, self._mw._candidate_faces_snapshot())
        except Exception as exc:
            messagebox.showerror("提示", f"候选清单构建失败：{type(exc).__name__}",
                                 parent=self._win)
            self._on_close()
            return
        # 进度区 → 清单区切换
        self._frm_scan.pack_forget()
        self._frm_list.pack(fill=tk.BOTH, expand=True)
        notes = []
        if not cands:
            if self._failed_n >= len(self._paths) and self._paths:
                messagebox.showerror(
                    candidate_flow.ALL_FAILED_TITLE,
                    candidate_flow.ALL_FAILED_BODY.format(m=len(self._paths)),
                    parent=self._win)
            else:
                messagebox.showinfo(
                    candidate_flow.ZERO_CANDIDATE_TITLE,
                    # 正文按 meta 条件化——本批有跳页
                    # （ocr_pages_skipped>0）才保留「扫描件为主/勾选 OCR
                    # 重跑」话术；无跳页换 NO_OCR 版（重跑无增量发现）。
                    candidate_flow.zero_candidate_body(meta).format(
                        m=len(self._paths)),
                    parent=self._win)
            self._on_close()
            return
        if self._failed_n:
            notes.append(candidate_flow.PARTIAL_BANNER.format(k=self._failed_n))
        if self._interrupted or meta.get("interrupted"):
            # v2.1.4：扫描器停止协议落地后，部分结果口径=实际已提取数
            # （meta files_n），与「正在停止……部分结果将照常展示」文案对齐
            scanned = int(meta.get("files_n") or max(0, len(self._paths) - self._failed_n))
            notes.append(candidate_flow.PARTIAL_RESULT_NOTE.format(
                m=scanned, total=len(self._paths)))
        if self._stats.get("visible_n", 0) == 0 and self._stats.get("hidden_n", 0):
            notes.append(f"全部候选被防呆拦截（共 {self._stats['hidden_n']} 项，"
                         "可勾选右下开关查看拦截原因）")
        if notes:
            self._banner.configure(text="　".join(notes))
            self._banner.pack(fill=tk.X, side=tk.TOP, before=self._frm_list)
        self._cb_hidden.configure(
            text=f"显示已隐藏的防呆拦截项({self._stats.get('hidden_n', 0)})")
        self._render()

    # ------------------------------------------------------------------
    # 渲染 / 交互
    # ------------------------------------------------------------------
    def _display_items(self):
        base = list(self._items)
        if not self._var_show_hidden.get():
            base = [it for it in base if not it.is_hidden]
        base = candidate_flow.filter_items(base, self._var_search.get())
        key = self._sort_key
        if key == "word":
            base.sort(key=lambda it: it.word, reverse=self._sort_desc)
        elif key == "score":
            base.sort(key=lambda it: (it.score, it.freq), reverse=self._sort_desc)
        elif key == "freq":
            base.sort(key=lambda it: (it.freq, it.score), reverse=self._sort_desc)
        elif key == "files":
            base.sort(key=lambda it: (it.files_n, it.freq), reverse=self._sort_desc)
        return base

    def _render(self):
        display = self._display_items()
        pages = cf_page_count(len(display))
        self._page = max(1, min(self._page, pages))
        page_items = cf_page_slice(display, self._page)
        tree = self._tree
        tree.delete(*tree.get_children())
        self._row_items.clear()
        session = self._session
        for it in page_items:
            ctx = ""
            if session is not None and it.source_ref is not None:
                try:
                    ctx = candidate_flow.mask_long_digits(
                        session.context_text(it.source_ref, 15))
                except Exception:
                    ctx = ""
            if it.is_hidden:
                sel_mark, tag = "🚫", "dim"
            elif not it.selectable:
                sel_mark, tag = "—", "dim"
            elif it.collision_org_suffix:
                sel_mark, tag = "☑" if it.word in self._checked else "☐", "warn"
            else:
                sel_mark, tag = "☑" if it.word in self._checked else "☐", ""
            iid = tree.insert("", tk.END, values=(
                sel_mark, it.word, f"{it.score:.0f}", it.freq, it.files_n,
                ctx, it.badge), tags=(tag,) if tag else ())
            self._row_items[iid] = it
        self._lbl_page.configure(
            text=f"第 {self._page}/{pages} 页（每页 {candidate_flow.TOP_PAGE} 条）")
        self._btn_prev.configure(state="normal" if self._page > 1 else "disabled")
        self._btn_next.configure(state="normal" if self._page < pages else "disabled")
        total_sel = int(self._stats.get("total", 0)) - self._stats.get("person_hidden", 0) \
            - self._stats.get("hidden_n", 0) \
            - self._stats.get("configured_filtered", 0) \
            - self._stats.get("covered_filtered", 0)
        self._lbl_sel.configure(
            text=f"已选 {len(self._checked)} / {max(0, total_sel)} 个")
        cfg_n = int(self._stats.get("configured_filtered", 0)) \
            + int(self._stats.get("covered_filtered", 0))
        if cfg_n:
            self._lbl_sel.configure(
                text=self._lbl_sel.cget("text") + f"（已按现有配置筛除 {cfg_n} 项）")
        self._btn_write.configure(
            state="normal" if self._checked else "disabled",
            text=(candidate_flow.WRITE_BUTTON.format(k=len(self._checked))
                  if self._checked else candidate_flow.WRITE_BUTTON_DISABLED))

    def _on_search_changed(self):
        self._page = 1
        if hasattr(self, "_lbl_page"):
            self._render()

    def _on_toggle_hidden(self):
        self._page = 1
        self._render()

    def _on_sort(self, col):
        if self._sort_key == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_key, self._sort_desc = col, True
        self._render()

    def _go_page(self, delta):
        self._page += delta
        self._render()

    def _select_page(self, mode):
        """全选(True)/反选(None)/清空(False)：作用域=当前页可见行，禁选行
        不参与（跨页全选会让用户勾选从未看过的词）。"""
        display = self._display_items()
        page_items = cf_page_slice(display, self._page)
        for it in page_items:
            if not it.selectable or it.is_hidden:
                continue
            if mode is True:
                self._checked.add(it.word)
            elif mode is False:
                self._checked.discard(it.word)
            else:
                if it.word in self._checked:
                    self._checked.discard(it.word)
                else:
                    self._checked.add(it.word)
        self._render()

    def _on_tree_click(self, event):
        tree = self._tree
        if tree.identify("region", event.x, event.y) != "cell":
            return None
        iid = tree.identify_row(event.y)
        item = self._row_items.get(iid)
        if item is None:
            return None
        col = tree.identify_column(event.x)
        if col == "#1":  # 选择列：☐/☑ 文本切换
            self._toggle(item)
        else:            # 行单击 → 只读详情弹窗（只读详情：看够判断，不落盘明文）
            self._show_detail(item)
        return None

    def _toggle(self, item):
        if not item.selectable or item.is_hidden:
            return
        if item.word in self._checked:
            self._checked.discard(item.word)
            self._render()
            return
        if item.requires_confirm:  # 与机构后缀增词撞车（S5）：唯一必弹二次确认项
            if not messagebox.askyesno(
                    candidate_flow.S5_CONFIRM_TITLE,
                    candidate_flow.S5_CONFIRM_BODY.format(word=item.word),
                    parent=self._win):
                return
        self._checked.add(item.word)
        self._render()

    def _show_detail(self, item):
        session = self._session
        if session is None or item.source_ref is None:
            return
        try:
            ctx60 = candidate_flow.mask_long_digits(
                session.context_text(item.source_ref, 60))
            files = list(session.source_files(item.source_ref, 5))
        except Exception:
            ctx60, files = "", []
        files_n = item.files_n
        more = f"　等共 {files_n} 份" if files_n > len(files) else ""
        detail = tk.Toplevel(self._win)
        detail.title(f"候选词详情 - {item.word}")
        detail.transient(self._win)
        detail.resizable(False, False)
        frm = ttk.Frame(detail, padding=(14, 10))
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, text=item.word,
                  font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        facts = (f"频次：{item.freq}　来源文件数：{files_n}　置信分：{item.score:.0f}\n"
                 f"通道：{'、'.join(item.channels) if item.channels else '—'}"
                 f"　机构形态：{'是' if item.org_form else '否'}"
                 f"　声明配对：{'是' if item.declared else '否'}")
        if item.already_configured:
            facts += f"\n登记明细：{candidate_flow.FACE_LABELS.get(item.already_configured, item.already_configured)}"
        if item.collision_org_suffix:
            facts += "\n⚠写入 brands.json 后，现有同名组织后缀增词条目将失效"
        if item.is_hidden:
            facts += f"\n拦截原因：{item.badge}"
        ttk.Label(frm, text=facts, justify="left").pack(anchor="w", pady=(4, 6))
        ttk.Label(frm, text="首现上下文（±60 字，连续 ≥7 位数字已打码）：",
                  foreground="#595959").pack(anchor="w")
        txt = tk.Text(frm, height=5, width=76, wrap="word", state="disabled",
                      background="#FAFAFA", relief="solid", bd=1)
        txt.configure(state="normal")
        txt.insert("1.0", ctx60 or "（无上下文）")
        txt.configure(state="disabled")
        txt.pack(fill=tk.X, pady=(2, 6))
        if files:
            names = "、".join(Path(f).name for f in files)
            ttk.Label(frm, text=f"来源文件：{names}{more}",
                      wraplength=640, justify="left").pack(anchor="w")
        ttk.Button(frm, text="关闭", command=detail.destroy).pack(anchor="e")
        detail.grab_set()
        detail.update_idletasks()
        x = self._win.winfo_rootx() + 60
        y = self._win.winfo_rooty() + 60
        detail.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------------
    # 写入 / 关闭（零持久化闭环）
    # ------------------------------------------------------------------
    def _on_write(self):
        words = [it.word for it in self._items if it.word in self._checked]
        if not words:
            return
        try:
            outcome = candidate_flow.commit_selected(words)
        except Exception as e:
            messagebox.showerror(candidate_flow.WRITE_FAIL_TITLE,
                                 f"写入失败：{type(e).__name__}: {e}",
                                 parent=self._win)
            return
        result = outcome["result"]
        if not result.ok:  # 原配置未改动，保持对话框打开供重试
            messagebox.showerror(outcome["dialog"]["title"],
                                 outcome["dialog"]["body"], parent=self._win)
            return
        # 零持久化红线：日志只落计数与文件名（拒绝明细含词面 → 只报条数）
        rejected = len([w for w in (result.warnings or []) if "已忽略" in w])
        if rejected:
            self._log(f"写前校验拒绝 {rejected} 条（过短/通用词），已略。")
        self._log(outcome["log_line"])
        self._written_done = True
        self._on_close()
        messagebox.showinfo(outcome["dialog"]["title"], outcome["dialog"]["body"],
                            parent=self._root)

    def _log(self, msg):
        try:
            self._mw._append_log(msg)
        except Exception:
            pass

    def _on_close(self):
        """关闭：取消扫描 + session.close() 丢弃全部扫描数据（零持久化闭环）。"""
        self._closed = True
        self._cancel.set()
        session = self._session
        self._session = None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        try:
            self._win.destroy()
        except Exception:
            pass

    def _center(self):
        try:
            self._win.update_idletasks()
            x = self._root.winfo_rootx() + max(
                0, (self._root.winfo_width() - self._win.winfo_width()) // 2)
            y = self._root.winfo_rooty() + max(
                0, (self._root.winfo_height() - self._win.winfo_height()) // 3)
            self._win.geometry(f"+{x}+{y}")
        except Exception:
            pass


def cf_page_count(n_items, per_page=candidate_flow.TOP_PAGE):
    """模块级小适配：candidate_flow.page_count（便于测试替换/阅读）。"""
    return candidate_flow.page_count(n_items, per_page)


def cf_page_slice(items, page, per_page=candidate_flow.TOP_PAGE):
    return candidate_flow.page_slice(items, page, per_page)
