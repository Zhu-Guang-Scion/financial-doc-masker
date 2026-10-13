# -*- coding: utf-8 -*-
"""各文件格式处理器与按扩展名的分流注册表。

处理器统一签名：
    process(src: Path, dst: Path, engine, options, report) -> dict

- engine  ：app.rules_engine.RuleEngine，统一匹配入口；
- options ：app.rules_engine.MaskOptions；
- report  ：调度层注入的回调包装，接口三件——log(msg)、progress(done, total)
  与 record(original, replacement, rule, location)（四参：命中原文、替换文本、
  规则名、位置串）；record 逐笔累积到 report.records，供汇总映射表（CSV
  「映射对照表覆盖」）与遮蔽台账双写消费；
- 返回 dict：至少包含 {"masked": 命中数}，可附加 "output" 覆盖实际输出路径。

所有第三方依赖在函数内延迟导入，缺失时抛 MissingDependencyError（友好提示），
保证“单文件失败不中断批量任务”。
"""
from . import docx_handler, md_handler, pdf_handler, pptx_handler, xls_handler, xlsx_handler

HANDLER_REGISTRY = {
    ".docx": docx_handler.process,
    ".md": md_handler.process,
    ".xlsx": xlsx_handler.process,
    ".xls": xls_handler.process,
    ".pptx": pptx_handler.process,
    ".pdf": pdf_handler.process,
}
