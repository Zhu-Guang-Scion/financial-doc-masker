# -*- coding: utf-8 -*-
"""财税文档脱敏工具（本地离线版）应用包。

分层结构：
- app.ui_main_window   界面层（tkinter 控件与事件）
- app.dispatcher       业务调度层（批量任务编排、进度、异常隔离）
- app.rules_engine     脱敏引擎层（统一规则引擎：正则 + 词典 + 校验算法）
- app.ocr_engine       脱敏引擎层（OCR 引擎接口与 PP-OCRv5 实现）
- app.handlers         各文件格式处理器（docx / xlsx / xls / pptx / pdf）
- app.utils            工具层（依赖探测、格式校验算法、通用辅助）
"""

__version__ = "2.1.9"  # v2.1.5 起与 app.utils.APP_VERSION 对齐（原 0.1.0 为从未跟随的死字段）
