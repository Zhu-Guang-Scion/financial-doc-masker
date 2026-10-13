# -*- coding: utf-8 -*-
"""老版 Excel(.xls) 处理器。

.xls 为二进制老格式，写出方案按优先级：
1) COM 链路（本机安装 Excel 或 WPS，需 pywin32）：
   先复制/另存副本，再对副本单元格级编辑，可原格式保存 .xls；
   勾选“.xls 处理后输出为 xlsx”时另存为 .xlsx（少一次格式转换）；
2) 备用链路（xlrd 读取 + openpyxl 写出）：输出为 .xlsx，并明确记录格式变化。

所有操作均针对副本/另存文件，绝不修改源文件。
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ..utils import MissingDependencyError
from .text_replacement import apply_replacements
from .xlsx_handler import contact_numeric_replacement

_COM_PROGIDS = ("Excel.Application", "Ket.Application", "ET.Application")  # Excel / WPS 表格
_XLSX_FILE_FORMAT = 51  # xlOpenXMLWorkbook（.xlsx）


def process(src: Path, dst: Path, engine, options, report) -> dict:
    report.log("优先尝试 COM 链路（需要本机安装 Excel 或 WPS）…")
    if _try_import_com():
        return _process_with_com(src, dst, engine, options, report)
    report.log("未检测到可用的 Excel/WPS COM 组件，改用 xlrd 备用链路")
    return _process_with_xlrd(src, dst, engine, options, report)


def _try_import_com() -> bool:
    try:
        import pythoncom  # noqa: F401
        import win32com.client  # noqa: F401
        return True
    except ImportError:
        return False


def _create_com_app(client):
    """依次尝试 Excel / WPS 的 COM ProgID。"""
    for progid in _COM_PROGIDS:
        try:
            return client.DispatchEx(progid)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# 方案 1：COM 链路（Excel / WPS）
# ---------------------------------------------------------------------------

def _process_with_com(src: Path, dst: Path, engine, options, report) -> dict:
    import pythoncom
    import win32com.client

    convert_to_xlsx = dst.suffix.lower() == ".xlsx"
    if convert_to_xlsx:
        open_path = src.resolve()          # 打开源文件 -> 另存为 xlsx
    else:
        shutil.copy2(str(src), str(dst))   # 先复制副本，绝不修改源文件
        open_path = dst.resolve()

    report.log("阶段 1/2：通过 COM 打开工作簿 …")
    pythoncom.CoInitialize()  # COM 组件必须在当前工作线程初始化
    app = None
    wb = None
    try:
        app = _create_com_app(win32com.client)
        if app is None:
            raise MissingDependencyError(
                "无法启动 Excel/WPS COM 组件（请确认本机已安装 Excel 或 WPS）"
            )
        app.Visible = False
        app.DisplayAlerts = False
        wb = app.Workbooks.Open(str(open_path))
        if convert_to_xlsx:
            wb.SaveAs(str(dst.resolve()), FileFormat=_XLSX_FILE_FORMAT)

        hits = 0
        sheet_count = wb.Worksheets.Count
        for i in range(1, sheet_count + 1):
            ws = wb.Worksheets.Item(i)
            used = ws.UsedRange
            if used is None:
                continue
            rows = int(used.Rows.Count)
            cols = int(used.Columns.Count)
            sheet_hits = 0
            # TODO(性能): 大表建议整块读取 UsedRange.Value 二维数组，仅回写变更单元格
            for r in range(1, rows + 1):
                for c in range(1, cols + 1):
                    cell = used.Cells(r, c)
                    v = cell.Value
                    if v is None or isinstance(v, bool):
                        continue
                    if getattr(cell, "HasFormula", False) and options.keep_excel_formula:
                        continue  # TODO: “保留公式仅脱敏显示值”模式
                    if isinstance(v, (int, float)):
                        ct = contact_numeric_replacement(v, engine, options)
                        if ct is not None:  # 数值格手机号/证号独立通道
                            rule, repl = ct
                            cell.Value = repl
                            report.record(str(int(v)), repl, rule,
                                          f"[{ws.Name}] R{r}C{c}")
                            sheet_hits += 1
                            continue
                        new_v = engine.mask_numeric(v)  # 数值单元格金额脱敏
                        if new_v is not None:
                            cell.Value = new_v
                            sheet_hits += 1
                    elif isinstance(v, str):
                        spans = engine.find_spans(v)
                        if spans:
                            new_text = apply_replacements(v, spans)
                            # TODO: 以 "=" 开头的替换文本需转义（前缀 '），避免被当作公式
                            cell.Value = new_text
                            for sp in spans:
                                report.record(sp.text, sp.replacement, sp.rule,
                                              f"[{ws.Name}] R{r}C{c}")
                            sheet_hits += len(spans)
            if sheet_hits:
                report.log(f"工作表[{ws.Name}]：脱敏 {sheet_hits} 处")
            hits += sheet_hits
            report.progress(i, sheet_count)

        if not convert_to_xlsx:
            wb.Save()
        wb.Close(SaveChanges=False)
        wb = None
        report.log(f"阶段 2/2：写出脱敏副本（命中 {hits} 处）…")
        return {"masked": hits, "engine": "COM", "output": str(dst)}
    finally:
        if wb is not None:
            try:
                wb.Close(SaveChanges=False)
            except Exception as e:
                # v2.1.9：工作簿关闭失败此前静默——COM 对象可能
                # 悬挂并占用输出文件（用户侧表现为「文件被 Excel/WPS 占用」），
                # 失败必须留痕（控制流不变，仍继续退出清理）。
                try:
                    report.log(f"⚠ 工作簿关闭失败（[{type(e).__name__}] {e}），"
                               f"输出文件可能仍被 Excel/WPS 占用，请人工检查。")
                except Exception:
                    pass
        if app is not None:
            try:
                app.Quit()
            except Exception as e:
                # v2.1.9：COM 进程退出失败此前静默——后台可能
                # 残留 Excel/WPS 进程持续占用资源，失败必须留痕。
                try:
                    report.log(f"⚠ Excel/WPS 进程退出失败"
                               f"（[{type(e).__name__}] {e}），后台可能残留"
                               f"Excel/WPS 进程，请人工检查。")
                except Exception:
                    pass
        pythoncom.CoUninitialize()


# ---------------------------------------------------------------------------
# 方案 2：备用链路（xlrd -> openpyxl，输出 .xlsx）
# ---------------------------------------------------------------------------

def _process_with_xlrd(src: Path, dst: Path, engine, options, report) -> dict:
    try:
        import xlrd
    except ImportError as e:
        raise MissingDependencyError(
            "处理 .xls 需要本机安装 Excel 或 WPS（COM 链路），"
            "或安装备用读取库：pip install xlrd"
        ) from e
    try:
        from openpyxl import Workbook
    except ImportError as e:
        raise MissingDependencyError("备用链路还需 openpyxl：pip install openpyxl") from e

    if dst.suffix.lower() == ".xls":
        dst = dst.with_suffix(".xlsx")
        report.log("提示：无 COM 组件，无法保留 .xls 原格式，已自动改为输出 .xlsx")

    report.log("阶段 1/2：xlrd 读取 .xls …")
    book = xlrd.open_workbook(str(src))
    wb = Workbook()
    wb.remove(wb.active)
    hits = 0
    for si in range(book.nsheets):
        sh = book.sheet_by_index(si)
        ws = wb.create_sheet(sh.name[:31])
        for r in range(sh.nrows):
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_TEXT:
                    spans = engine.find_spans(cell.value)
                    if spans:
                        new_text = apply_replacements(cell.value, spans)
                        ws.cell(row=r + 1, column=c + 1, value=new_text)
                        for sp in spans:
                            report.record(sp.text, sp.replacement, sp.rule,
                                          f"[{sh.name}] R{r + 1}C{c + 1}")
                        hits += len(spans)
                    else:
                        ws.cell(row=r + 1, column=c + 1, value=cell.value)
                elif cell.ctype == xlrd.XL_CELL_NUMBER:
                    ct = contact_numeric_replacement(cell.value, engine, options)
                    if ct is not None:  # 数值格手机号/证号独立通道
                        rule, repl = ct
                        ws.cell(row=r + 1, column=c + 1, value=repl)
                        report.record(str(int(cell.value)), repl, rule,
                                      f"[{sh.name}] R{r + 1}C{c + 1}")
                        hits += 1
                    else:
                        new_v = engine.mask_numeric(cell.value)
                        if new_v is not None:
                            ws.cell(row=r + 1, column=c + 1, value=new_v)
                            hits += 1
                        else:
                            ws.cell(row=r + 1, column=c + 1, value=cell.value)
                elif cell.ctype == xlrd.XL_CELL_DATE:
                    # TODO: 按 xlrd.xldate 还原日期时间后写入
                    ws.cell(row=r + 1, column=c + 1, value=cell.value)
                elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                    ws.cell(row=r + 1, column=c + 1, value=bool(cell.value))
                # 空单元格 / 错误值不写入
        report.progress(si + 1, book.nsheets)

    report.log("阶段 2/2：openpyxl 写出 .xlsx（格式由 .xls 转为 .xlsx）…")
    wb.save(str(dst))
    return {"masked": hits, "engine": "xlrd→xlsx", "output": str(dst)}
