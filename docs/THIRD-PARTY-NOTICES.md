# 第三方组件与模型权重声明（THIRD-PARTY NOTICES）

本文件声明「财税文档脱敏工具」分发包（发行 exe 闭包与源码依赖清单）所包含的第三方组件与模型权重的许可信息。本文件为归属与合规提示，不构成法律意见；各组件的许可条款以随附的许可证原文为准。

- 声明口径：以 2026-09 对发行闭包（`dist` 产物 `_internal` 目录实测）与运行时依赖元数据的逐项审计为准；源码运行场景以 `requirements.txt` 为准（两者组件面一致，闭包另含打包所需的底层 DLL）。
- 范围外：Microsoft VC++ 可再分发组件（VCRUNTIME140\*、MSVCP140、VCOMP140、api-ms-win-crt-\*）按微软再分发条款随包提供，免于逐项声明；Windows UCRT 为操作系统组件。
- 本项目自有资产（`app/`、`models/` 的工程代码、文档、构建脚本）不属于第三方声明范围。

## 1. 重点组件：PyMuPDF（AGPL-3.0）

| 组件 | 版本（审计实测） | 许可证 | 用途 |
| --- | --- | --- | --- |
| PyMuPDF（import fitz） | 1.28.2 | **AGPL-3.0 或 Artifex 商业许可（双许可）** | PDF 解析、渲染、原生文本遮罩 |

本项目整体以 AGPL-3.0 授权（见仓库根 `LICENSE`），与 PyMuPDF 同证兼容。若你需要在闭源场景集成 PyMuPDF，请自行评估 AGPL 义务或向 [Artifex](https://artifex.com/licensing) 购买商业授权。

## 2. 模型权重声明（Apache-2.0）

| 权重 | 来源链路 | 许可证 |
| --- | --- | --- |
| `models/onnx_v6/PP-OCRv6_det_small.onnx`、`PP-OCRv6_rec_small.onnx` | PaddleOCR **PP-OCRv6** 官方权重 → RapidOCR 官方预转换 ONNX（small 档） | Apache-2.0（PaddleOCR 项目整体；RapidOCR 转换权重仓库同） |
| `rapidocr_onnxruntime` 随包 cls 模型（`ch_ppocr_mobile_v2.0_cls_infer.onnx`） | PaddleOCR v2 方向分类器 → rapidocr_onnxruntime wheel 内嵌 | Apache-2.0 |

归属与变更声明（Apache-2.0 §4(b) 要求）：

> PP-OCRv6 det/rec small 检测/识别权重 © PaddlePaddle（PaddleOCR 项目），以 Apache-2.0 提供；由 RapidOCR 转换为 ONNX 格式，本分发包未对其做任何修改（unmodified，small 档）；方向分类器权重取自 rapidocr_onnxruntime 1.4.4 wheel 内嵌文件。
>
> PaddleOCR: <https://github.com/PaddlePaddle/PaddleOCR>
> RapidOCR（RapidAI）: <https://github.com/RapidAI/RapidOCR>（权重镜像：ModelScope `RapidAI/RapidOCR`、HuggingFace `SWHL/RapidOCR`）

依据 Apache-2.0 再分发的义务（附许可证全文、保留版权与 NOTICE、声明变更）已由本节与上游仓库链接满足。若未来引入其他系列模型（如对话式/文心系模型），须逐个核对其模型卡许可后另行增补声明。

## 3. 运行时在用组件（permissive 系列简表）

以下组件按各自 permissive 许可证随包再分发，各组件的完整许可文本可在其发行包（`*.dist-info`）或上游仓库查阅：

| 包 | 审计版本 | 许可证 | 用途 |
| --- | --- | --- | --- |
| rapidocr-onnxruntime | 1.4.4 | Apache-2.0 | PP-OCRv6 ONNX 推理封装（默认 OCR 引擎链） |
| onnxruntime | 1.20.1 | MIT | ONNX 推理后端 |
| opencv-python | 4.10.0.84 | Apache-2.0（wheel 内嵌 FFmpeg DLL 为 LGPL-2.1 构建，见第 4 节） | 红色印章检测 / OCR 图像解码 |
| Pillow | 10.0.1 | HPND（MIT 型宽松许可，OSI 认可） | 图像解码与处理 |
| numpy | 1.24.3 | BSD-3-Clause | 数值计算 |
| jieba | 0.42.1 | MIT | 人名词边界交叉校验（词典数据随包同证） |
| python-docx | 1.2.0 | MIT | Word .docx 处理 |
| python-pptx | 1.0.2 | MIT | PPT .pptx 处理 |
| openpyxl | 3.0.10 | MIT | Excel .xlsx 处理 |
| lxml | 4.9.3 | BSD-3-Clause | docx/pptx 底层 XML 解析 |
| xlrd | （源码依赖清单） | BSD 类 | 老版 .xls 降级读取 |
| pywin32 | 305.1 | PSF-2.0 | .xls 调用本机 Excel/WPS（COM） |
| tkinterdnd2 | 0.6.2 | MIT（内嵌 tkdnd 二进制为 Tcl/Tk 风格 BSD 类许可） | 文件/文件夹拖拽导入 |
| Shapely | 2.1.2 | BSD-3-Clause | rapidocr 传递依赖 |
| pyclipper | 1.4.0 | MIT | rapidocr 传递依赖 |
| PyYAML | 6.0.2 | MIT | rapidocr 配置 |
| tqdm | 4.65.0 | MPL-2.0 / MIT | 进度条（MPL-2.0 为文件级 copyleft，取 MIT 路径或按文件级条款，均与本项目兼容） |
| certifi | 2023.11.17 | MPL-2.0 | CA 证书束（传递依赖） |

打包闭包中随带的其他开发/传递组件（构建与评测工具链，如 PyInstaller、modelscope、pytest 及 conda 环境混入包）许可均为 MIT / BSD / Apache-2.0 / PSF / Unlicense / ZPL-2.1 等 permissive 许可，不改变上述结论；发行闭包的每次重组（剔除死重后）应以同口径审计复核并更新本清单。

## 4. LGPL 组件动态链接声明

以下三件组件以 **LGPL** 许可发行，均以**独立动态链接库（DLL）形态**与本程序**动态链接**后随包分发，未静态链入本程序可执行文件。依据相应 LGPL 条款，这是合规的分发方式；同时按条款要求声明如下：使用者可依据 LGPL 对这些库进行反编译/修改/替换，且替换该库后程序仍可运行。

### 4.1 ZeroMQ（libzmq）

- 文件：`libzmq-mt-4_3_4.dll`
- 许可证：**LGPL-3.0**（含静态链接例外条款；本项目仅动态链接）
- 用途：pyzmq（BSD-3）的底层库，GPU-v6 外部环境子进程通信链路
- 上游与源码：<https://github.com/zeromq/libzmq>
- 声明：本程序以动态链接方式使用该库；你可获取该库源码并依 LGPL-3.0 修改、替换（将该 DLL 换成自行构建的同名库即可）。

### 4.2 GNU libiconv

- 文件：`iconv.dll`、`charset.dll`
- 许可证：**LGPL-2.1**（及更新版本）
- 用途：字符编码转换（随打包运行组件闭包带入）
- 上游与源码：<https://www.gnu.org/software/libiconv/>
- 声明：本程序以动态链接方式使用该库；该库源码可自上游获取，你可依 LGPL-2.1 修改并以兼容版本替换。

### 4.3 FFmpeg（opencv-python wheel 内嵌构建）

- 文件：`opencv_videoio_ffmpeg4100_64.dll`（随 opencv-python 4.10.0.84 wheel 内嵌）
- 许可证：**LGPL-2.1**（该构建未启用需 GPL 的编解码配置）
- 用途：视频/图像 I/O 后端（opencv-python 官方 wheel 按此方式分发）
- 上游与源码：FFmpeg <https://ffmpeg.org>；opencv-python 的构建说明 <https://github.com/opencv/opencv-python>
- 声明：本程序经 opencv-python 以动态链接方式使用该库；FFmpeg 源码可自上游获取，你可依 LGPL-2.1 重新构建并替换该 DLL。

## 5. 打包闭包随带的底层 DLL（上游归属简表）

发行 exe 闭包为满足「开箱即用」随带了以下底层库，许可证均为 permissive（附各上游仓库可查得许可原文）：

| DLL / 库 | 上游项目 | 许可证 |
| --- | --- | --- |
| freetype.dll | FreeType | FTL（FreeType License，GPL-2 双许可，按 FTL 路径分发；FTL 含署名条款，许可全文见 <https://www.freetype.org/license.html>，随包二进制内亦有附载） |
| libxml2 / libxslt / libexslt.dll | libxml2 / libxslt | MIT |
| tcl86t.dll / tk86t.dll | Tcl/Tk | Tcl/Tk 许可（BSD 类） |
| hdf5 / hdf5_hl.dll | HDF5 | HDF5 许可（BSD 类） |
| libbz2.dll | bzip2 | bzip2 许可（BSD 类，含署名条款） |
| libsodium.dll | libsodium | ISC |
| snappy.dll | Google Snappy | BSD-3 |
| sqlite3.dll | SQLite | Public Domain（保留上游免责声明） |
| xxhash.dll | xxHash | BSD-2 |
| openjp2.dll | OpenJPEG | BSD-2 |
| tiff.dll | libtiff | HPND 类宽松许可 |
| libwebp / libwebpmux / libwebpdemux / libsharpyuv.dll | libwebp | BSD-3 |
| deflate.dll | zlib | zlib 许可 |
| Lerc.dll | LERC | Apache-2.0 |
| liblzma.dll | XZ Utils | Public Domain |
| zstd.dll | Zstandard | BSD-3 |
| libpng16.dll | libpng | libpng 许可 |
| ffi.dll | libffi | MIT 类 |

## 6. 免责声明

本程序按「现状」提供（见 AGPL-3.0 第 15、16 条担保与责任限制）。上述第三方组件各自按其许可证提供，本项目的维护者不对任何第三方组件的适销性、适用性或不侵权作出担保。各组件的版权与许可归属其各自的著作权人。

---

*维护说明：依赖面变化（新增/升级/剔除组件、更换模型）时，须同步更新本文件并复核许可证兼容性；建议以闭包实测审计（dist-info 逐项）为更新口径。*
