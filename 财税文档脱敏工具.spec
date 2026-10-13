# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all, collect_data_files

# 构建机 Anaconda 根目录：默认 D:\Anaconda3，可用环境变量 FINMASK_CONDA_ROOT
# 覆盖（例：set FINMASK_CONDA_ROOT=E:\Miniconda3 后再执行 pyinstaller）。
# 仅影响构建期 DLL 收集路径，默认值与历史硬编码逐字相同（零行为变化）。
_CONDA_ROOT = os.environ.get('FINMASK_CONDA_ROOT', r'D:\Anaconda3')

datas = [('使用说明.txt', '.')]  # v1.7.5：使用说明随包分发
binaries = []
hiddenimports = [
    'jieba',  # v1.3 人名词边界交叉校验（纯 Python，词典数据见下方 datas）
    # 外部 Paddle 环境接入时，paddle 栈会用到 packaging 的完整子模块；
    # packaging 被部分打包会遮住外部版本，故补全（体积可忽略）。
    'packaging', 'packaging.requirements', 'packaging.markers',
    'packaging.specifiers', 'packaging.utils', 'packaging.version',
    # paddle 栈（aiohttp/bce-sdk 等）用到的 http 子模块同理补全
    'http.cookies', 'http.client',
    # 标准库中未被本项目引用、但外部 Paddle 栈运行需要的模块（体积均可忽略）
    'uuid', 'base64', 'secrets', 'json', 'gettext', 'queue',
    'urllib.request', 'urllib.parse', 'urllib.error',
    'xml.etree.ElementTree', 'xml.dom.minidom', 'xml.sax',
    'logging.config', 'logging.handlers',
    'zoneinfo', 'mimetypes', 'difflib', 'dataclasses', 'numbers', 'calendar',
    'symtable', 'token', 'tokenize', 'platform', 'tempfile', 'shutil',
    'fnmatch', 'glob', 'string', 'textwrap', 'weakref', 'copyreg', 'pickle',
    'inspect', 'dis', 'opcode',
    # distutils 在 Python 自身 Lib 下（不在 site-packages），外部栈需要 → 必须随包
    'distutils', 'distutils.util', 'distutils.version',
    'plistlib', 'sysconfig', 'unittest', 'unittest.mock',
    'wave', 'chunk', 'aifc', 'sunau', 'audioop',
]
tmp_ret = collect_all('tkinterdnd2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# 外部 Paddle 栈会用到 PIL 的几乎全部子模块，部分打包会遮住外部完整版 → 整包收集
tmp_ret = collect_all('PIL')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# v1.7.3：使用说明随包分发（exe 同目录，注明版本号与更新日期）
datas += [('使用说明.txt', '.')]
# v1.3：jieba 词典数据（dict.txt 等）必须随包，否则冻结环境下分词不可用
datas += collect_data_files('jieba')
# Anaconda 特有：_ctypes.pyd 依赖 Library\bin\ffi.dll，构建时不在 PATH 会被漏掉
binaries += [(os.path.join(_CONDA_ROOT, 'Library', 'bin', 'ffi.dll'), '.')]
# Anaconda 特有：_tkinter.pyd 依赖 Library\bin 下的 tcl/tk 运行库（PyInstaller 只收集了数据目录）
binaries += [(os.path.join(_CONDA_ROOT, 'Library', 'bin', 'tcl86t.dll'), '.'),
             (os.path.join(_CONDA_ROOT, 'Library', 'bin', 'tk86t.dll'), '.')]
# v1.8.0：conda 版 Pillow 的 _imaging.pyd 依赖 Library\bin 下 12 个 DLL，构建时
# 未被解析（33 条 Library not found 警告）——v6 是第一条在 frozen 内 import 包内
# PIL 的链路，缺失即崩（冒烟实证）。显式补入（与 ffi.dll 同法）。
binaries += [(os.path.join(_CONDA_ROOT, 'Library', 'bin', name), '.')
             for name in ('openjp2.dll', 'tiff.dll', 'freetype.dll',
                          'libwebp.dll', 'libwebpmux.dll', 'libwebpdemux.dll',
                          'deflate.dll', 'Lerc.dll', 'liblzma.dll',
                          'zstd.dll', 'libpng16.dll', 'libsharpyuv.dll')]
# 2026-09-17：conda 动态链残留闭包（测试机实锤根因）——lxml 的 etree.pyd 依赖
# libxml2/libxslt/libexslt（→iconv/charset），另有 hdf5/bz2/sqlite3/zmq/snappy/
# xxhash（→libsodium）系 pyd 同款缺口。构建机因 PATH/conda 机制能解析全部依赖
# 而全程无感（构建/冒烟/自检三过=盲区），裸机处理 docx 即报「DLL load failed
# while importing etree: 找不到指定的模块」——装 VC++ redist 不能治（libxml2
# 不在 redist 内）。pefile 导入表全 dist 扫描+迭代闭包定稿，与 Pillow 12 DLL
# 同法显式补入。
# v2.1.4 收缩：hdf5×2/libzmq/libsodium/snappy/xxhash 六件的宿主 pyd 包
# （h5py/pyzmq/PyNaCl/python-snappy/xxhash）已随死重批出闭包（见下方
# excludes），对应 DLL 同步移出补收表；保留 7 件（libxml2 系 5+bz2+sqlite3）。
binaries += [(os.path.join(_CONDA_ROOT, 'Library', 'bin', name), '.')
             for name in ('libxml2.dll', 'libxslt.dll', 'libexslt.dll',
                          'iconv.dll', 'charset.dll',
                          'libbz2.dll', 'sqlite3.dll')]
# v1.8.0：PP-OCRv6-ONNX 闭包——仅 CPU 版 onnxruntime（1.20.1）+ rapidocr 1.4.4。
# rapidocr 随包默认 v4 的 det/rec 模型过滤掉（省 ~15MB 且避免误用）；
# cls 模型放行——rapidocr 1.4.4 初始化硬依赖它（use_cls=False 也不免，
# 冒烟实证 ImportError）；其余数据（config 等）照收。
# 严禁打入 onnxruntime-gpu（见 excludes 兜底）。
tmp_ret = collect_all('rapidocr_onnxruntime')
for _src, _dst in tmp_ret[0]:
    _norm = _src.lower().replace('/', os.path.sep)
    if _norm.endswith('.onnx') and f'{os.path.sep}models{os.path.sep}' in _norm \
            and ('det_' in _norm or 'rec_' in _norm):
        continue  # rapidocr 随包默认 v4 det/rec 模型，排除（cls 保留）
    datas.append((_src, _dst))
binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('onnxruntime')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
hiddenimports += ['onnxruntime', 'rapidocr_onnxruntime']
# v2.1.4：xlrd 入包（P1 修复）——.xls 备用读取链路（xls_handler._process_
# with_xlrd 函数内 import）。此前 requirements 有 xlrd 但闭包未收，exe 内
# 无 Excel/WPS COM 时的 .xls 降级链路实际不可用（Z2 闭包审计实锤）。
hiddenimports += ['xlrd']
# v6 small 权重（det 9.9MB + rec 21.2MB ≈ 31.2MB）；medium/tiny 不入包
datas += [(r'models\onnx_v6\PP-OCRv6_det_small.onnx', 'models/onnx_v6'),
          (r'models\onnx_v6\PP-OCRv6_rec_small.onnx', 'models/onnx_v6')]
# v1.8.2：GPU-v6 模式 worker 脚本随包（落 _internal/app/，由外部 v6_gpu_env
# 的 python.exe 执行；纯标准库脚本，不引入任何 GPU 依赖入包，
# onnxruntime_gpu/modelscope 的 excludes 兜底保持不变）
datas += [('app/v6_gpu_worker.py', 'app')]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['paddle', 'paddleocr', 'paddlex', 'modelscope', 'aistudio_sdk', 'botocore', 'boto3', 's3transfer', 'panel', 'bokeh', 'llvmlite', 'numba', 'scipy', 'babel', 'PyQt5', 'qtpy', 'pandas', 'pyarrow', 'matplotlib', 'pkg_resources', 'setuptools', '_distutils_hack',
              # v1.8.0：exe 仅内置 onnxruntime CPU 闭包，严禁打入 onnxruntime-gpu
              'onnxruntime_gpu', 'onnxruntime.training', 'torch', 'tensorflow',
              # v2.1.4 死重批（Z2 闭包审计 §1.2+§4，app 直连/传递 import 边
              # 全量 grep 甄别，保留项见注释）：开发工具类（运行时零 import）
              'IPython', 'notebook', 'sphinx', 'docutils', 'alabaster',
              'jedi', 'parso', 'wcwidth', 'pytest', 'black', 'blib2to3',
              'isort',
              # ML 全家桶（transformers 33MB 及专属依赖链）
              'transformers', 'tokenizers', 'safetensors', 'datasets',
              'huggingface_hub', 'dask', 'distributed', 'dill',
              # Web/DB 栈（外部 Paddle 环境的依赖在外部环境内，exe 闭包无关）
              'sqlalchemy', 'tornado', 'aiohttp', 'yarl', 'multidict',
              'frozenlist', 'brotli',
              # 冷门二进制（其专属 DLL 已同步移出补收表）
              'h5py', 'lmdb', 'nacl', 'bcrypt', 'snappy', 'xxhash',
              'zstandard', 'greenlet', 'wrapt', 'zope', 'menuinst', 'zmq', 'pyzmq',
              # 科学配套（宿主 pandas/sklearn 均不在闭包）
              'sklearn', 'dateutil', 'tzdata', 'regex', 'markupsafe', 'onnx',
              # 甄别保留（勿加入 excludes）：
              # 保留：numpy/PIL/cv2/jieba/lxml/openpyxl/docx/pptx/xlrd/yaml/
              #       tkinterdnd2/pywin32（核心）；psutil（rapidocr 引用 10 处）；
              #       requests+certifi+urllib3+charset_normalizer（requests
              #       生态，rapidocr 引用）；pydantic（rapidocr 配置生态）；
              #       filelock/jsonschema/importlib_metadata（小件，保守）。
              #       protobuf 已随 onnx 一并排除（onnx 唯一消费者）。
              'protobuf'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='财税文档脱敏工具',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='财税文档脱敏工具',
)
