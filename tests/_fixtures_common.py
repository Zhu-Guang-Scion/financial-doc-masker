# -*- coding: utf-8 -*-
"""测试夹具路径统一口径（conftest.py 与各回归套件共用；仅标准库、不依赖 pytest）。

- 品牌夹具（FINMASK_BRANDS）：本地回归配置 ``_eval/brands_test.json`` 存在
  则用之（本地判例词配置，不入开源库）；缺失（干净 clone / 开源环境）时
  回退开源内置虚构词夹具 ``tests/fixtures/brands_oss.json``——统一经
  :func:`resolve_brands_fixture` 取值，不再静默降级为空品牌表。
- 本地真实语料根（CORPUS_DIR）：默认取仓库同级「测试文件夹」目录，
  环境变量 ``FINMASK_CORPUS_DIR`` 可覆盖；目录不存在时依赖它的用例
  自行 skip（本地语料未配置时跳过）。
"""
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

LOCAL_BRANDS_JSON = REPO_ROOT / "_eval" / "brands_test.json"  # 不入开源库
OSS_BRANDS_JSON = Path(__file__).resolve().parent / "fixtures" / "brands_oss.json"


def resolve_brands_fixture() -> str:
    """FINMASK_BRANDS 统一选取：本地回归配置优先，缺失回退开源虚构词夹具。"""
    if LOCAL_BRANDS_JSON.is_file():
        return str(LOCAL_BRANDS_JSON)
    return str(OSS_BRANDS_JSON)


# 本地真实语料根（env FINMASK_CORPUS_DIR 可覆盖；默认不存在即 skip）
CORPUS_DIR = Path(os.environ.get(
    "FINMASK_CORPUS_DIR", str(REPO_ROOT.parent / "测试文件夹")))


def resolve_wordlists_fixture() -> str:
    """词表外置档路径（_goal_work/wordlists_private/ 存在则用，否则空串=缺档态）。"""
    import sys as _sys
    from pathlib import Path as _Path
    root = _Path(__file__).resolve().parents[1]
    p = root / "_goal_work" / "wordlists_private" / "wordlists_private.json"
    return str(p) if p.is_file() else ""
