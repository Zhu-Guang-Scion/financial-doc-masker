# 财税文档脱敏工具 | Financial Document Masker

**一款完全本地离线运行的 Windows 桌面工具，对合同、报表、凭证、制度文档等财税文档一键脱敏——文本层与图片层（扫描件、截图、印章）统一处理，数据不出本机。**

**A fully offline Windows desktop tool for one-click sanitization of financial documents (contracts, reports, invoices, policy files) — handling both text layers and image layers (scanned pages, screenshots, red-seal stamps) with unified treatment. Data never leaves your machine.**

> 完全本地离线 · 数据不出本机 · 开源软件（AGPL-3.0）
> Fully Local & Offline · Data Never Leaves Your Machine · Open Source (AGPL-3.0)

---

## 简介 | Overview

### 中文

财税文档脱敏工具面向财税从业者在对外提供文档前的隐私清理场景：把文档中可识别具体主体或财产信息的内容——公司名称、人名、金额、机构名称、地址、联系方式、身份证号、红色公章等——替换为不可识别的形态，同时尽量保留文档的版式与可读性。

两个基本立场：

- **通用类别化，而非针对某个品牌**：引擎按「敏感类别」通用判定（公司名/人名/金额/机构/地址/联系方式/证件/印章），不围绕任何单一公司定制；
- **完全本地离线**：全部处理在本机完成，不发起任何网络请求（唯一例外是可选 OCR 组件的安装与模型首次下载，之后完全离线），不收集任何数据。

支持格式：`.docx` / `.xlsx` / `.xls` / `.pptx` / `.pdf` / `.md`（PDF 含文字型与扫描型）。

其他特性：

- 自定义敏感词替换为可区分短代号（敏1、敏2…），映射表自带「原文→代号」对照；
- 可选「仅脱敏这些词」模式：跳过全部自动识别，只处理指定词（速度快、零误伤）；
- 金额两种模式：全部隐去 / 缩放脱敏（全批次 ×随机系数 K，勾稽关系按比例保持）；
- 映射表与遮蔽台账：逐批次生成 CSV 记录，支持编辑后二次处理（命中时零 OCR 秒级重放）；
- 候选词推荐：自动扫描文档中反复出现的机构/品牌类高频专名，按置信分列出供勾选。

### English

Financial Document Masker is designed for finance and tax professionals who need to sanitize documents before sharing them externally. It replaces content that can identify specific entities or assets — company names, personal names, monetary amounts, institutional names, addresses, contact information, ID card numbers, red-seal stamps — with non-identifiable forms, while preserving the document's layout and readability.

Two core principles:

- **Generic category-based detection, not brand-specific**: The engine detects sensitive *categories* (company/person/amount/institution/address/contact/ID/seal) universally, not tailored to any single company;
- **Fully local & offline**: All processing happens on your machine. No network requests (the only exception is the optional OCR component installation and first-time model download, after which everything runs offline). No data collection.

Supported formats: `.docx` / `.xlsx` / `.xls` / `.pptx` / `.pdf` / `.md` (PDF includes both text-native and scanned).

Additional features:
- Custom sensitive words replaced with distinguishable short codes (敏1, 敏2, …); mapping table carries a "original → code" legend;
- Optional "mask these words only" mode: skip all automatic detection, process only specified words;
- Two amount modes: full mask / scale mode (batch × random factor K, ratio relationships preserved);
- Mapping table & audit ledger: per-batch CSV with edit-and-rerun support (zero-OCR replay on match);
- Candidate discovery: auto-scan for high-frequency proper nouns with ranked suggestions.

---

## ⚠ 诚实说明 | Honest Disclosure

### 中文

本项目目前处于**早期阶段**，以下事实如实告知：

- **测试样本量有限**：目前的回归测试语料仅约 50 份文档（含合同、制度、报表、扫描件等），覆盖面有限，不能保证在你的文档上表现一致；
- **基于少量用户反馈迭代**：功能演进主要依赖少数用户的实际使用反馈，误报/漏报治理仍在持续进行中；
- **公章遮蔽效果**：红色公章的遮蔽目前以**整块黑色矩形覆盖**实现——能保证公章不可识别，但视觉上与「去除印章留白」的效果有区别，可能影响周边文字的可读性；
- **识别能力上限**：OCR 对图片小字、手写签名的识别存在物理上限；词组型公司字号、生僻人名可能漏检（③区自定义敏感词是兜底手段）；
- **建议**：脱敏后的文档请**务必人工复核**后再对外提供。

### English

This project is in its **early stage**. The following facts are disclosed honestly:

- **Limited test corpus**: Current regression testing uses only ~50 documents (contracts, policies, reports, scanned files). Coverage is limited and results may vary on your documents;
- **Iterated on limited feedback**: Feature evolution is driven by a small number of real user reports; false-positive and false-negative reduction is ongoing;
- **Red-seal masking appearance**: Red-seal stamps are masked with a **solid black rectangle overlay** — the seal is guaranteed unidentifiable, but the visual effect differs from "remove seal, leave blank space" and may affect readability of adjacent text;
- **Detection limits**: OCR has physical limits on small image text and handwritten signatures; compound company names and rare personal names may be missed (Zone ③ custom words serve as the safety net);
- **Recommendation**: Always **manually review** sanitized documents before external sharing.

---

## 功能特性 | Key Features

| 功能 | 说明 |
| --- | --- |
| **五步闭环工作流** | 导入脱敏 → 内置通用匹配 → 填自定义敏感词 → 复核映射表 → 再次脱敏 |
| **内置通用识别** | 公司名（三段式流水线+jieba 词性校验）、人名（三通道+Excel 表头列整列识别）、金额、地址、手机号、身份证号、邮箱、证券代码、红色公章 |
| **图片层同等处理** | 扫描件与图片内文字走内置 OCR 引擎（PP-OCRv6-ONNX，零安装开箱即用）；红色公章按视觉特征检测后整体遮盖 |
| **PDF 原位遮罩** | 原生文本页做字符级坐标定位后从文件中**销毁原文**、原位白底替换；旋转页自动归一化；文件名同步脱敏 |
| **金额两种模式** | ① 全部隐去（`***`） ② 缩放脱敏（全批次 ×随机系数 K，勾稽关系保持，公式入映射表） |
| **映射表与台账** | 每批次生成汇总映射表 CSV + 逐笔遮蔽台账；支持编辑映射表后二次处理（命中时零 OCR 秒级重放） |
| **候选词推荐** | 自动扫描高频专名，按置信分列出供勾选，一键写入 brands.json；全程内存零持久化 |
| **自定义词代号** | 敏1、敏2…可区分短代号，映射表自带「原文→代号」对照 |
| **仅指定词模式** | 跳过全部自动识别，只处理③区指定词（速度快、零误伤） |
| **安全边界** | 输出到独立文件夹，**原文件绝不修改**；单文件失败不中断批量任务 |

| Feature | Description |
| --- | --- |
| Five-step workflow | Import → Matching → Custom words → Review → Re-run |
| Built-in detection | Company (3-stage pipeline + jieba), person (3-channel + Excel header column), amounts, addresses, phones, ID cards, emails, stock codes, red-seal stamps |
| Image-layer OCR | Scanned/embedded images via built-in PP-OCRv6-ONNX (zero-install); visual seal detection |
| PDF in-place masking | Character-level targeting → destroy original → white-background replacement; rotation normalization; filename sanitization |
| Amount modes | Full mask / Scale (×K ratio-preserving, formula in mapping table) |
| Mapping & ledger | Per-batch CSV + per-op audit; edit-and-rerun with zero-OCR replay |
| Candidate discovery | Auto-scan high-frequency proper nouns; memory-only; one-click write to brands.json |
| Custom word codes | Distinguishable codes (敏1, 敏2, …) with "original→code" legend |
| Words-only mode | Skip all auto-detection, process only specified words |
| Safety | Independent output folder; **originals never modified**; batch continues on single-file failure |

---

## 安装 | Installation

### 方式一：下载发行包（exe，推荐普通用户）| Option 1: Release (exe)

1. 从本项目 [Releases](../../releases) 页面下载发行 zip 包；
2. 解压到任意本地文件夹（OCR 模型权重已内置，开箱即用）；
3. 双击 `财税文档脱敏工具.exe` 运行。

说明：

- 程序未做代码签名，首次运行遇 Windows SmartScreen 提示时，点「更多信息 → 仍要运行」；
- Word（.docx）等文档处理所需的运行组件已随包内置，一般无需另装运行库；个别精简系统报「缺少可选组件」时，安装 [Microsoft VC++ 2015-2022 x64 可再发行组件包](https://aka.ms/vs/17/release/vc_redist.x64.exe) 后重试；
- 发行包内的 `setup_cpu_deps_env.bat` 可一键检测并补齐可选依赖（exe 模式加 `/exe` 参数做完整性自检）；
- 处理老版 `.xls` 需本机装有 Excel 或 WPS（保格式转换）；其余格式开箱即用。

### 方式二：从源码运行 | Option 2: From source

环境要求：Windows 10/11，Python 3.10+。

```bat
:: 1) 获取代码
git clone https://github.com/Zhu-Guang-Scion/financial-doc-masker.git
cd financial-doc-masker

:: 2) 安装依赖（均为可选依赖，按需安装；建议一次装齐）
pip install -r requirements.txt

:: 3) 准备内置 OCR 引擎的模型权重（见下方「OCR 模型权重获取」）

:: 4) 启动
python main.py
```

没有任何第三方依赖时界面也能启动，缺失的组件对应功能自动降级并在启动日志中提示安装命令。`python selftest.py` 可做无界面的引擎自检；测试运行方式见 `docs/CONTRIBUTING.md`。

#### OCR 模型权重获取 | OCR Model Weights

内置 OCR 引擎（PP-OCRv6-ONNX）需要两个权重文件：`models/onnx_v6/PP-OCRv6_det_small.onnx` 与 `models/onnx_v6/PP-OCRv6_rec_small.onnx`（合计约 31MB）。

- **发行包用户**：权重已内置在 zip 中，无需任何操作；
- **源码用户**：`models/` 目录不入 Git 仓库，需自行获取——从 [RapidAI/RapidOCR（ModelScope）](https://modelscope.cn/models/RapidAI/RapidOCR) 的 `onnx/PP-OCRv6/` 路径，或 [SWHL/RapidOCR（HuggingFace）](https://huggingface.co/SWHL/RapidOCR) 下载 PP-OCRv6 的 det/rec **small** 档 ONNX 权重，按上述文件名放入 `models/onnx_v6/`。

---

## 快速上手 | Quick Start

> **一句话记住**：导入脱敏 → 内置通用匹配 → 填自定义敏感词 → 复核映射表 → 再次脱敏
> **In one sentence**: Import → Matching → Custom words → Review → Re-run

**① 导入脱敏 | Import & Mask** —— 拖入文件/文件夹（或点导入按钮），勾选脱敏选项后点「开始处理」。输出到独立文件夹「脱敏输出_任务时间」，原文件绝不修改。

**② 内置通用匹配 | Built-in Matching** —— 公司名、人名、证件、地址、金额、印章自动识别遮盖，并生成映射表 CSV 记录每处「原文→替换」。

**③ 填自定义敏感词 | Custom Words（必做）** —— 内置规则不是万能的：词组型公司字号、生僻人名、品牌/项目代号、文档特有的第三方名称都可能漏检；图片小字与手写签名存在识别上限。把确定要遮的词逐行填入界面③区，引擎按最高优先级精确遮盖——这一步直接决定最终呈现效果。拿不准该填什么时，用③区的「从文档发现候选词」让引擎替你扫一遍。

**④ 复核映射表 | Review Mapping** —— 点「复核映射表...」逐条核对：可改写「替换为」、删除误报行。映射表含敏感原文，请按密件单独保管。

**⑤ 再次脱敏 | Re-run** —— 编辑后的 CSV 自动设为「映射对照表覆盖」，对原始文件重跑：改写的行按新内容替换，删除的行不再遮盖。至此脱敏闭环完成。

脱敏后的文档建议抽页人工复核后再对外提供。

---

## brands.json：登记你自己公司的名称与简称 | Register Your Company

软件可按配置遮盖「你自己公司」的名称、简称与邮箱域名特征。配置文件 `brands.json` 与程序放在同一文件夹；源码仓库内附空白模板 `brands_示例.json`，复制一份改名为 `brands.json` 填入内容即可。没有此文件时软件按空配置正常运行。

完整样例（换成自己公司的信息即可照抄，全部为虚构示例）：

```json
{
  "brands": ["星辰家居股份有限公司"],
  "aliases": ["星辰家居", "星辰"],
  "domain_hints": ["xingchen"],
  "org_suffixes": ["卫生室", "分院"],
  "org_suffixes_remove": ["院", "中心"]
}
```

| 键 | 作用 |
| --- | --- |
| `brands` | 公司全称或品牌名（整词遮盖） |
| `aliases` | 简称/别名（同样整词遮盖） |
| `domain_hints` | 邮箱域名特征片段：邮箱地址中出现该片段时整个邮箱全部遮盖，防止凭域名反推公司 |
| `org_suffixes` | 组织后缀增词（进阶，可不填）：行业特有后缀词被误遮时填进来即放行 |
| `org_suffixes_remove` | 组织后缀删词白名单（进阶，可不填）：撤回某个内置后缀的放行 |

填写要点：

- 全称和常用简称都填上——正文里往往只出现简称，只填全称会漏；
- 保存为 UTF-8 编码；
- `org_suffixes` 增词务必填「后缀词」本身，绝不能填机构/品牌专名；
- 防呆保护：过短（不足 2 字）或过于通用的词（如「公司」「定制」）会被拒绝加载并提示。

---

## OCR 引擎与模式 | OCR Engine & Modes

扫描件与图片内文字默认开箱即用：程序内置 PP-OCRv6-ONNX 引擎，无需安装 Python、Paddle 或任何组件，勾选「图片内文字（OCR）」直接处理即可。文字型 PDF / Word / Excel / PPT 一直开箱即用。

四种引擎模式在「设置」页下拉选择：

| 模式 | 说明 |
| --- | --- |
| 自动（默认） | 内置 PP-OCRv6-ONNX：CPU 自动用 small 档，检测到可用 NVIDIA 显卡时自动用 medium 档 |
| 仅 CPU | 强制 v6-small@CPU；GPU 异常时的逃生通道 |
| GPU-v5（高级） | 需本机已安装 Paddle GPU 环境 |
| GPU-v6（高级） | 需先双击运行 `setup_v6_gpu_env.bat` 一键创建外部 GPU 环境 |

统一兜底：所选模式环境不满足时，软件提示原因并自动回退 CPU 模式继续工作，不会报错中断。

---

## 性能参考 | Performance Reference

实际耗时取决于页数、图片密度与机器配置，下表仅为参考值（默认自动模式）：

| 项目 | CPU（普通电脑） | GPU（NVIDIA 显卡） |
| --- | --- | --- |
| 纯 OCR 单页 | ≈2.3 秒 | ≈0.6 秒 |
| 真实文档全流程 | 11~37 秒/页 | ≈3.6 秒/页（GPU-v6 外部环境） |
| 复核轮（缓存命中） | 14 页同样本 69 秒（全量重跑 127 秒） | —— |

测试环境：RTX 4060 Laptop 8GB / Intel 24 核 32 线程 / 13.7GB 内存；CPU 档为禁用独显模拟普通客户机的口径。低配机器耗时相应增加，功能不受影响。

---

## 隐私承诺 | Privacy Commitment

- **完全本地离线**：文档处理全程在本机完成，不访问网络。唯一例外是可选 OCR 组件的安装与模型首次下载（完成后完全离线）。
- **无遥测、无账号**：不收集、不上报任何使用数据、崩溃数据或文档内容。
- **原文件只读**：输入文件绝不被修改，全部输出写入独立的「脱敏输出_任务时间」文件夹。
- **候选词零持久化**：候选词推荐功能扫描到的候选词、频次、上下文全程只在内存里，关闭窗口即丢弃。
- **敏感产物由你保管**：映射表、遮蔽台账与分析缓存包含文档明文敏感信息，敏感等级等同原文——请勿随脱敏文档一同分发。

---

## 许可证 | License

本项目以 **GNU Affero 通用公共许可证第 3 版（AGPL-3.0-only 或任意更新版本）** 发布，许可证全文见仓库根目录 [LICENSE](LICENSE)。

选择 AGPL 的原因：PDF 原生文本遮罩核心使用 PyMuPDF（AGPL-3.0 与 Artifex 商业许可双许可发行）。本项目随 PyMuPDF 以 AGPL-3.0 同证开源。

按 AGPL-3.0 §6 要求，本仓库完整包含对应源码、依赖清单（`requirements.txt`）与构建脚本（`财税文档脱敏工具.spec`）。第三方组件与模型权重的许可声明见 [docs/THIRD-PARTY-NOTICES.md](docs/THIRD-PARTY-NOTICES.md)。

---

## 鸣谢 | Acknowledgments

| 项目 | 用途 | 许可证 |
| --- | --- | --- |
| [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR) | PP-OCR 系列识别模型与外接引擎 | Apache-2.0 |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) | ONNX 预转换权重与推理封装 | Apache-2.0 |
| [PyMuPDF](https://pymupdf.readthedocs.io/) | PDF 解析、渲染与原生遮罩 | AGPL-3.0 |
| [openpyxl](https://openpyxl.readthedocs.io/) / [python-docx](https://python-docx.readthedocs.io/) / [python-pptx](https://python-pptx.readthedocs.io/) | Excel / Word / PPT 处理 | MIT |
| [jieba](https://github.com/fxsjy/jieba) | 人名词边界交叉校验 | MIT |
| [onnxruntime](https://onnxruntime.ai/) / [opencv-python](https://github.com/opencv/opencv-python) / [Pillow](https://python-pillow.org/) | OCR 推理、印章检测与图像解码 | MIT / Apache-2.0 / HPND |

欢迎参与贡献，工程纪律与回归判定口径见 [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)。

---
