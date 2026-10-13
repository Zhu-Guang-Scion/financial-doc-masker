# -*- coding: utf-8 -*-
"""渲染确定性回归测试（v1.9.1，维护者规格：双种子像素全等 + 映射 md5 一致）。

背景：v1.8.1 四格实验实证同一代码在不同 PYTHONHASHSEED 下扫描件 mask 烧入
形态漂移（叠写星/星间距/＊＊＊：边界）；v1.9.1 将烧入顺序规范化为
(y, x, w, h, 词) 显式排序后，渲染结果必须与种子无关（构造性保证）。

方法：fitz 造 1 页合成 PDF（含 J 类图案：品牌词+冒号、举例枚举跟随、
重叠候选——原生文本行 + 内嵌图横幅行「全名⊃品牌」窄/宽双命中），用
subprocess 两次跑同一个小型掩蔽任务（OCR 全开、deep_recheck 全开），
两次分别在解释器启动前注入 PYTHONHASHSEED=0 / =1（env + sys.executable），
断言两份输出 dpi150 全页像素逐字节全等 + 汇总映射 CSV 内容 md5 一致。

运行：python tests/test_render_determinism.py
（可选）TEST_PKGROOT=<abs 包根> 指定被测代码根，默认项目根。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
PKGROOT = Path(os.environ.get("TEST_PKGROOT", ROOT)).resolve()

# 预设依赖用例：品牌预设外置（v1.8.1 brands.json），测试显式加载语料配置
os.environ["FINMASK_BRANDS"] = str(ROOT / "_eval" / "brands_test.json")

_DRIVER = r'''
import sys, os, json
sys.stdout.reconfigure(encoding="utf-8")
sys.path = [p for p in sys.path if p not in ("", ".")]
sys.path.insert(0, os.environ["PKGROOT"])
import app.handlers.pdf_handler as _ph
import app.app_config as _ac
_ac.resolve_ocr_mode = lambda: "auto"   # 与批跑口径一致
from app.dispatcher import run_batch
from app.rules_engine import MaskOptions
options = MaskOptions(mask_company=True, mask_person=True, mask_amount=True,
                      mask_contact=True, mask_address=True, mask_stamp=True,
                      ocr_image_text=True, person_loose=True, amount_mode="mask",
                      output_dir=sys.argv[2])
summary = run_batch([sys.argv[1]], options, lambda m: None, lambda d, t: None)
print("RESULT_JSON:" + json.dumps(
    {"outputs": summary.get("outputs", []), "mapping": summary.get("mapping", ""),
     "ok": summary.get("ok"), "pkg": _ph.__file__}, ensure_ascii=False))
'''


def _build_synthetic_pdf(path: Path) -> None:
    """1 页合成 PDF：原生文本（全名+简称声明+举例枚举+人名+金额）
    + 内嵌图横幅（第一行「云舒雅家居股份有限公司 特惠」同时命中
    全名别名键与品牌词——窄/宽两块重叠补丁，即 J 台账族烧入序敏感形态）。"""
    import fitz
    help_doc = fitz.open()  # 图内文字：独立页画字 → 高分辨率位图 → 贴回
    hp = help_doc.new_page(width=220, height=64)
    hp.insert_text(fitz.Point(4, 18), "云舒雅家居股份有限公司 特惠",
                   fontsize=13, fontname="china-s")
    hp.insert_text(fitz.Point(4, 42), "云舒雅橱柜 慕蓝朵 专卖",
                   fontsize=11, fontname="china-s")
    png = hp.get_pixmap(matrix=fitz.Matrix(4, 4), alpha=False).tobytes("png")
    help_doc.close()
    doc = fitz.open()
    page = doc.new_page(width=500, height=320)
    page.insert_image(fitz.Rect(20, 20, 240, 84), stream=png)  # 220x64 pt
    page.insert_text(fitz.Point(20, 112), "云舒雅家居股份有限公司费用报销细则",
                     fontsize=11, fontname="china-s")
    page.insert_text(fitz.Point(20, 132), "云舒雅家居股份有限公司以下简称云舒雅。",
                     fontsize=10, fontname="china-s")
    page.insert_text(fitz.Point(20, 152), "（如宏图网络、云杉贸易）为委外加工商。",
                     fontsize=10, fontname="china-s")
    page.insert_text(fitz.Point(20, 172), "审批人：王强敏 电话13812345678",
                     fontsize=10, fontname="china-s")
    page.insert_text(fitz.Point(20, 192), "金额：￥12,345.60元",
                     fontsize=10, fontname="china-s")
    doc.save(str(path), garbage=3, deflate=True)
    doc.close()


def _run_once(seed: int, src: Path, out_dir: Path) -> tuple:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = str(seed)  # 解释器启动前注入（子进程 env 生效）
    env["FINMASK_BRANDS"] = str(ROOT / "_eval" / "brands_test.json")
    env["PKGROOT"] = str(PKGROOT)
    out_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([sys.executable, "-c", _DRIVER, str(src), str(out_dir)],
                          env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=str(ROOT),
                          timeout=300)
    result_line = None
    for ln in (proc.stdout or "").splitlines():
        if ln.startswith("RESULT_JSON:"):
            result_line = json.loads(ln[len("RESULT_JSON:"):])
    return proc.returncode, result_line, (proc.stdout or "")[-800:], (proc.stderr or "")[-800:]


def _render_samples(pdf_path: str):
    import fitz
    doc = fitz.open(pdf_path)
    mat = fitz.Matrix(150 / 72.0, 150 / 72.0)
    pages = [doc[p].get_pixmap(matrix=mat, alpha=False) for p in range(doc.page_count)]
    doc.close()
    return pages


def run() -> bool:
    failed = []
    t0 = time.time()
    work = Path(tempfile.mkdtemp(prefix="finmask_det_"))
    src = work / "合成_渲染确定性.pdf"
    _build_synthetic_pdf(src)
    print(f"合成 PDF：{src}")

    results = {}
    for seed in (0, 1):
        code, payload, out_tail, err_tail = _run_once(seed, src, work / f"out{seed}")
        ok = code == 0 and payload and payload.get("ok") == 1 and payload.get("outputs")
        name = f"seed{seed} 子进程掩蔽任务成功"
        print(f"  {'PASS' if ok else 'FAIL'} {name}"
              + ("" if ok else f"\n      stdout: {out_tail}\n      stderr: {err_tail}"))
        if not ok:
            failed.append(name)
        results[seed] = payload

    if results.get(0) and results.get(1):
        pdf0, pdf1 = results[0]["outputs"][0], results[1]["outputs"][0]
        pages0, pages1 = _render_samples(pdf0), _render_samples(pdf1)
        same_pages = len(pages0) == len(pages1)
        name = "输出页数一致"
        print(f"  {'PASS' if same_pages else 'FAIL'} {name}（{len(pages0)} vs {len(pages1)}）")
        if not same_pages:
            failed.append(name)
        all_eq = same_pages and all(
            p0.width == p1.width and p0.height == p1.height and p0.samples == p1.samples
            for p0, p1 in zip(pages0, pages1))
        name = "dpi150 全页像素逐字节全等（PYTHONHASHSEED=0 vs 1）"
        print(f"  {'PASS' if all_eq else 'FAIL'} {name}")
        if not all_eq:
            failed.append(name)
        csv0 = results[0].get("mapping", "")
        csv1 = results[1].get("mapping", "")
        md5 = lambda p: hashlib.md5(open(p, "rb").read()).hexdigest() if p else ""
        same_csv = bool(csv0) and md5(csv0) == md5(csv1)
        name = "汇总映射 CSV 内容 md5 一致"
        print(f"  {'PASS' if same_csv else 'FAIL'} {name}"
              f"（{os.path.basename(csv0)} vs {os.path.basename(csv1)}）")
        if not same_csv:
            failed.append(name)
    else:
        failed.append("种子运行不完整，后续断言未执行")

    elapsed = time.time() - t0
    if not failed:
        import shutil
        shutil.rmtree(work, ignore_errors=True)  # 全绿即清理；失败保留现场
    print(f"\n共 5 项断言，失败 {len(failed)} 条（用时 {elapsed:.1f}s，"
          f"被测代码根：{PKGROOT}）")
    for f in failed:
        print(f"  FAIL {f}")
    return not failed


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ok = run()
    sys.exit(0 if ok else 1)
