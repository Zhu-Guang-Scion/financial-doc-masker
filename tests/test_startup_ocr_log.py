# -*- coding: utf-8 -*-
"""T2（P2）客户机误导性日志修复——无外部环境机器的启动日志路径单测。

背景（现场反馈）：无 Python 环境客户机上，v2.0.0 exe 弹出「本 exe 版未内置
OCR 组件…需 paddle…」旧文案。v2.0.0 exe 已内置 PP-OCRv6-ONNX（CPU=small
默认路径，spec excludes paddle/paddleocr、随包 rapidocr+onnx_v6 small 权重），
该文案为假。触发分支：ui_main_window._log_startup 的
`frozen ∧ probe_optional_deps 报 PaddleOCR 缺失`（客户机恒成立，与 OCR 模式
无关）；该分支为纯启动日志，不阻断处理链路（图片型 PDF 经 auto 模式由内置
v6-CPU 正常脱敏，冒烟已实测验证）。

覆盖（客户机口径模拟：meta_path 屏蔽 paddle 系导入 + find_spec 对 paddle 系
返回 None + discover_external_paddle_env 预置 [] + 临时 config.json）：
- paddle_missing_notice：auto/cpu_only 中性新文案（无旧文案关键词）、
  gpu_v5/legacy_v5 按模式给安装指引与出路（防呆语义不削弱）、
  源码版（frozen=False）返回空串走通用「依赖自检」行；
- MainWindow._log_startup 行为级（fake self，无需 Tk）：客户机模拟下
  auto 模式 → 新文案出现、旧文案不再出现；legacy_v5 → 对应提示；
- 引擎回退：同一客户机口径下 PPOCRv5Engine 抛 MissingDependencyError（真缺
  组件仍提示怎么装），而默认 auto 模式 create_engine_for_mode 成功回退内置
  v6（状态栏/诊断同源 note_engine_name 口径不变）；
- pdf_handler 全扫描件 + OCR 未勾选警告：新文案（勾选 OCR 出路）出现、
  旧文案「请改用源码版运行/安装 PaddleOCR」不再出现；
- 源码级防回流锁：app/ 内旧文案短语全量清零。

运行：python tests/test_startup_ocr_log.py
"""
import importlib.util
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import app_config, ocr_engine
from app.handlers import pdf_handler
from app.ocr_engine import MissingDependencyError, create_engine_for_mode
from app.utils import program_data_dir as _real_program_data_dir

APP_DIR = Path(__file__).resolve().parents[1] / "app"

# 旧启动文案的假结论特征（「图片型…文字脱敏」+「暂不可用」组合出现即视为旧文案回流）
_OLD_STARTUP_CLAIM = "图片型（扫描）PDF 的文字脱敏"
_OLD_STARTUP_TAIL = "暂不可用"


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

    def expect_mde(self, name, fn, keyword=""):
        self.total += 1
        try:
            fn()
        except MissingDependencyError as e:
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


class _PaddleImportBlocker:
    """客户机口径：paddle/paddleocr 及其子模块一律 ModuleNotFoundError。"""

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in ("paddle", "paddleocr"):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


class _FakeWindow:
    """MainWindow._log_startup 的最小 fake self（_log_ocr_mode 缺控件走其自身 try）。

    用法：win = _FakeWindow(); win._log_ocr_mode = lambda: MainWindow._log_ocr_mode(win)
    （绑定真实方法：_log_ocr_mode 仅依赖 _append_log 与 var_ocr_mode——后者缺失时
    由其自身 try/except 吞掉，不影响目标分支断言。）
    """

    def __init__(self):
        self.logs = []
        self._dnd_available = True

    def _append_log(self, msg):
        self.logs.append(msg)


class _FakeReport:
    def __init__(self):
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)

    def progress(self, done, total):
        pass


class _StubRuleEngine:
    """全扫描件 + OCR 关闭路径不会触达检测，仅需 mapping_only 属性。"""
    mapping_only = False


class _ClientSim:
    """客户机口径上下文：屏蔽 paddle 系 + 探测不到 + 无外部环境 + 独立 config。

    用法：with _ClientSim(legacy=False) as sim: ...
    """

    def __init__(self, legacy=False):
        self.legacy = legacy
        self._tmp = None
        self._orig_find_spec = None
        self._orig_config_path = None
        self._orig_ext_result = None
        self._blocker = None

    def __enter__(self):
        # 1) import 屏蔽（客户机无 paddle）
        for k in [k for k in list(sys.modules) if k.split(".")[0] in ("paddle", "paddleocr")]:
            del sys.modules[k]
        self._blocker = _PaddleImportBlocker()
        sys.meta_path.insert(0, self._blocker)
        # 2) 探测口径：find_spec 对 paddle 系返回 None（未安装语义，非报错）
        self._orig_find_spec = importlib.util.find_spec

        def _patched(name, path=None):
            if name.split(".")[0] in ("paddle", "paddleocr"):
                return None
            return self._orig_find_spec(name, path)

        importlib.util.find_spec = _patched
        # 3) 无外部 Paddle 环境可接入（客户机无 Anaconda paddle）
        self._orig_ext_result = ocr_engine._external_env_result
        ocr_engine._external_env_result = []
        # 4) 独立 config.json：默认 auto / 存量 v5
        self._tmp = tempfile.TemporaryDirectory(prefix="finmask_t2_")
        cfg_path = Path(self._tmp.name) / "config.json"
        if self.legacy:
            cfg_path.write_text('{"ocr_engine": "ppocrv5"}', encoding="utf-8")
        self._orig_config_path = app_config.config_path
        app_config.config_path = lambda: cfg_path
        return self

    def __exit__(self, *exc):
        importlib.util.find_spec = self._orig_find_spec
        app_config.config_path = self._orig_config_path
        ocr_engine._external_env_result = self._orig_ext_result
        sys.meta_path.remove(self._blocker)
        self._tmp.cleanup()
        return False


# ---------------------------------------------------------------------------

def test_notice_copy_by_mode(c: _Case):
    """paddle_missing_notice 各模式文案口径。"""
    from app.ui_main_window import paddle_missing_notice

    m_auto = paddle_missing_notice(app_config.OCR_MODE_AUTO, True)
    m_cpu = paddle_missing_notice(app_config.OCR_MODE_CPU, True)
    m_g5 = paddle_missing_notice(app_config.OCR_MODE_GPU_V5, True)
    m_l5 = paddle_missing_notice(app_config.OCR_MODE_LEGACY_V5, True)
    m_src = paddle_missing_notice(app_config.OCR_MODE_AUTO, False)

    # ① 默认路径（auto/cpu_only）：新中性文案，旧假结论清零
    for tag, m in (("auto", m_auto), ("cpu_only", m_cpu)):
        c.check(f"{tag}：不再出现旧文案关键词",
                all(p not in m for p in ("未内置 OCR 组件", "暂不可用", "请改用源码版运行")))
        c.check(f"{tag}：含 v2.0.0 现实（内置 PP-OCRv6-ONNX/CPU 默认/不依赖/不受影响）",
                all(k in m for k in ("内置", "PP-OCRv6-ONNX", "CPU 默认路径", "不依赖", "不受影响")),
                m)
    # ② 显式旧 v5 链路：对应提示 + 防呆语义不削弱（仍提示怎么装/怎么切/回退口径）
    c.check("gpu_v5：安装指引（paddlepaddle-gpu + paddleocr / FINMASK_EXTERNAL_SITE）+ 自动回退 v6-CPU",
            all(k in m_g5 for k in ("paddlepaddle-gpu", "paddleocr", "FINMASK_EXTERNAL_SITE",
                                    "回退", "PP-OCRv6-ONNX"))
            and "未内置 OCR 组件" not in m_g5 and "暂不可用" not in m_g5, m_g5)
    c.check("legacy_v5：安装指引 + 建议切换自动模式（真缺组件仍提示怎么装）",
            all(k in m_l5 for k in ("paddlepaddle + paddleocr", "切换", "自动"))
            and "未内置 OCR 组件" not in m_l5 and "暂不可用" not in m_l5, m_l5)
    # ③ 源码版：空串 → 走通用「依赖自检」行，既有语义不变
    c.check("源码版（frozen=False）返回空串（走通用依赖自检行）", m_src == "", repr(m_src))


def test_log_startup_auto_mode(c: _Case):
    """行为级：客户机口径 + 默认 auto 模式 → _log_startup 输出新文案、旧文案绝迹。"""
    from app.ui_main_window import MainWindow

    win = _FakeWindow()
    win._log_ocr_mode = lambda: MainWindow._log_ocr_mode(win)
    old_frozen = getattr(sys, "frozen", None)
    sys.frozen = True  # exe 口径
    try:
        with _ClientSim(legacy=False):
            MainWindow._log_startup(win)
    finally:
        if old_frozen is None:
            del sys.frozen
        else:
            sys.frozen = old_frozen

    joined = "\n".join(win.logs)
    c.check("auto：旧文案（未内置 OCR 组件/图片型…暂不可用）不再出现",
            "未内置 OCR 组件" not in joined
            and not (_OLD_STARTUP_CLAIM in joined and _OLD_STARTUP_TAIL in joined))
    hits = [ln for ln in win.logs if "PaddleOCR（v5）组件" in ln]
    c.check("auto：新中性说明出现（内置 v6、不依赖 Paddle、不受影响）",
            bool(hits) and all(k in hits[0] for k in ("内置", "PP-OCRv6-ONNX", "不依赖", "不受影响")),
            joined)
    c.check("auto：不误导安装（新文案不含 pip/paddle 安装指引——默认路径无需装）",
            hits and "pip install" not in hits[0] and "Anaconda" not in hits[0])


def test_log_startup_legacy_v5(c: _Case):
    """行为级：存量 v5 配置 + 客户机口径 → 给出 v5 安装/切换提示（防呆保留）。"""
    from app.ui_main_window import MainWindow

    win = _FakeWindow()
    win._log_ocr_mode = lambda: MainWindow._log_ocr_mode(win)
    old_frozen = getattr(sys, "frozen", None)
    sys.frozen = True
    try:
        with _ClientSim(legacy=True):
            MainWindow._log_startup(win)
    finally:
        if old_frozen is None:
            del sys.frozen
        else:
            sys.frozen = old_frozen

    joined = "\n".join(win.logs)
    c.check("legacy_v5：存量 v5 模式提示仍在（启动横幅）",
            any("存量 v5 配置" in ln for ln in win.logs))
    hits = [ln for ln in win.logs if "存量 v5 配置需要本机 Paddle 环境" in ln]
    c.check("legacy_v5：v5 缺组件安装指引 + 切换自动出路（假结论不再出现）",
            bool(hits) and all(k in hits[0] for k in ("paddlepaddle + paddleocr", "切换", "自动"))
            and "暂不可用" not in hits[0] and "未内置 OCR 组件" not in hits[0], joined)


def test_engine_fallback_v6_cpu(c: _Case):
    """客户机口径下：v5 真缺组件照常抛 MDE（防呆），默认 auto 成功回退内置 v6。"""
    from app.ocr_engine import create_ocr_engine

    with _ClientSim(legacy=False):
        # 真实处理路径口径：_make_ocr_getter → get_ocr_engine/create_ocr_engine
        # （工厂层负责把缺组件转为人话 MDE；直接构造 PPOCRv5Engine 的裸
        # ModuleNotFoundError 不属稳定契约，不作断言对象）
        c.expect_mde("客户机口径：ppocrv5 真缺组件 → MissingDependencyError（含安装提示）",
                     lambda: create_ocr_engine("ppocrv5"), keyword="paddleocr")
        eng = create_engine_for_mode(app_config.OCR_MODE_AUTO)
        c.check("客户机口径：默认 auto 模式引擎创建成功（内置 v6 链路，不依赖 Paddle）",
                eng.name.startswith("PP-OCRv6-ONNX-") and "paddle" not in eng.name.lower(),
                eng.name)
        c.check("状态栏/诊断同源引擎名（note_engine_name→get_last_engine_name）与就绪引擎一致",
                ocr_engine.get_last_engine_name() == eng.name,
                f"{ocr_engine.get_last_engine_name()} != {eng.name}")
        fb = create_engine_for_mode(app_config.OCR_MODE_CPU)
        c.check("cpu_only 模式 → v6-small@CPU（GPU 异常逃生路径不受影响）",
                fb.name == "PP-OCRv6-ONNX-small(cpu)", fb.name)


def test_pdf_handler_scan_warning_copy(c: _Case):
    """全扫描件 + OCR 未勾选：新警告（勾选 OCR 出路）出现，旧文案绝迹。

    走 pdf_handler.process 真链路（skipped 路径不触达 OCR/检测，无需真实引擎）。
    """
    import fitz
    from app.rules_engine import MaskOptions

    tmp = tempfile.TemporaryDirectory(prefix="finmask_t2_pdf_")
    try:
        base = Path(tmp.name)
        src = fitz.open()
        page = src.new_page(width=595, height=842)
        page.insert_text((72, 100), "供应商: 北京雄商置业有限公司 电话 13812345678",
                         fontname="F0", fontfile=r"C:\Windows\Fonts\simhei.ttf", fontsize=13)
        pix = page.get_pixmap(matrix=fitz.Matrix(200 / 72, 200 / 72), alpha=False)
        scan = fitz.open()
        sp = scan.new_page(width=595, height=842)
        sp.insert_image(fitz.Rect(0, 0, 595, 842), pixmap=pix)
        src_pdf = base / "扫描件.pdf"
        scan.save(src_pdf)
        scan.close()
        src.close()

        report = _FakeReport()
        options = MaskOptions(ocr_image_text=False, deep_recheck=False,
                              output_dir=str(base / "out"))
        (base / "out").mkdir(parents=True, exist_ok=True)
        summary = pdf_handler.process(src_pdf, base / "out" / "out.pdf",
                                      _StubRuleEngine(), options, report)
        joined = "\n".join(report.logs)
        c.check("全扫描件+OCR 关闭：处理成功不崩", summary.get("ok", 0) >= 0 and (base / "out" / "out.pdf").exists(),
                str(summary))
        warns = [ln for ln in report.logs if "均为图片型" in ln]
        c.check("警告行仍在（防呆语义不削弱）", bool(warns), joined)
        c.check("新警告给出正确出路（勾选 OCR + 内置引擎），旧文案（源码版/安装 PaddleOCR）绝迹",
                warns and all(k in warns[0] for k in ("勾选", "内置", "PP-OCRv6-ONNX"))
                and all(p not in warns[0] for p in ("请改用源码版运行", "安装 PaddleOCR。")),
                joined)
    finally:
        tmp.cleanup()


def test_no_old_copy_in_source(c: _Case):
    """源码级防回流锁：app/ 内旧文案特征短语全量清零、新分支已接线。"""
    ui_src = (APP_DIR / "ui_main_window.py").read_text(encoding="utf-8")
    pdf_src = (APP_DIR / "handlers" / "pdf_handler.py").read_text(encoding="utf-8")
    for phrase in ("未内置 OCR 组件", "请改用源码版运行", "安装 PaddleOCR。"):
        c.check(f"源码锁：旧短语「{phrase}」已从 app/ 清零",
                phrase not in ui_src and phrase not in pdf_src)
    c.check("源码锁：旧启动假结论（图片型…文字脱敏+暂不可用）已清零",
            not (_OLD_STARTUP_CLAIM in ui_src and _OLD_STARTUP_TAIL in ui_src))
    c.check("源码锁：_log_startup 已接线 paddle_missing_notice（按模式分流）",
            "paddle_missing_notice(" in ui_src and "resolve_ocr_mode()" in ui_src)
    c.check("源码锁：源码版（非冻结）通用「依赖自检」行保留（防呆语义不变）",
            "依赖自检：{label} 未安装（{hint}）" in ui_src)


# ---------------------------------------------------------------------------

def run():
    print("== T2 启动日志误导文案修复（无外部环境客户机口径）单测 ==")
    c = _Case()
    test_notice_copy_by_mode(c)
    test_log_startup_auto_mode(c)
    test_log_startup_legacy_v5(c)
    test_engine_fallback_v6_cpu(c)
    test_pdf_handler_scan_warning_copy(c)
    test_no_old_copy_in_source(c)
    print(f"\n共 {c.total} 条用例，失败 {len(c.failed)} 条")
    return not c.failed


if __name__ == "__main__":
    try:
        sys.exit(0 if run() else 1)
    except Exception:
        traceback.print_exc()
        sys.exit(2)
