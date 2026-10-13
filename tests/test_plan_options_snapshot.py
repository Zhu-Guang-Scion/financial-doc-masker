# -*- coding: utf-8 -*-
"""v2.0.3 WF3：plan 快照头部全量类别开关键（F5 可观测性）测试。

背景（D3 诊断「开关取证」节）：现场 plan 快照 first_round_options 仅 5 键
（mask_amount/mask_stamp/deep_recheck/ocr_image_text/ocr_min_confidence，
masking_plan.py 旧 202-209 行），人名/企业/联系人/地址等类别开关盘上不可证
——台账/审计体系要求开关态可追溯。评审决策：plan 头部补记 MaskOptions
全部类别开关（以 MaskOptions 实际字段为准全量；内容/路径类字段
custom_words/whitelist/mapping_csv/output_dir 非开关、含敏感清单，不记，
其检测语义已冻结进 plan 指令本身）。

兼容契约（本套件固化）：
- 新 plan：first_round_options 含 16 个全量开关键（5 旧键原样保留 + 11 新键）；
- 旧 plan（无新键夹具）：load_plan 头部强校验只看四键、first_round_options
  不参与校验、重放读取点（pdf_handler 漂移提示）按 `key in fropts` 容忍
  缺失 → 装载不失效（v1.9.4 装载兼容语义沿用）；
- 新增键不参与 plan 缓存键（路径=src_content_hash；头部校验四键含
  brands_hash/app_version）→ 存量 plan 缓存零失效。

运行：python tests/test_plan_options_snapshot.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 品牌夹具统一选取（本地配置优先，缺失回退开源虚构词夹具 tests/fixtures/）
from _fixtures_common import resolve_brands_fixture

os.environ.setdefault("FINMASK_BRANDS", resolve_brands_fixture())

from app import masking_plan as mp
from app.analysis_cache import content_hash
from app.rules_engine import MaskOptions
from app.utils import APP_VERSION

try:
    import pytest
except ImportError:  # pragma: no cover - 直跑环境无 pytest
    pytest = None

if pytest is not None:
    @pytest.fixture(scope="module")
    def root():
        return Path(tempfile.mkdtemp(prefix="v203plan_"))


class _Case:
    def __init__(self):
        self.failed = []
        self.total = 0

    def check(self, name, cond, detail=""):
        self.total += 1
        if cond:
            print(f"  PASS {name}")
        else:
            self.failed.append(f"{name}: {detail}")
            print(f"  ✘ {name} {detail}")


# MaskOptions 全量标量类别开关（声明序；5 旧键在前与其历史次序一致不强求，
# 以 masking_plan 实现为准——本表是测试侧的独立期望，防实现漂移）
EXPECTED_OPTION_KEYS = (
    # 旧 5 键（v2.0.1 起既有）
    "mask_amount", "mask_stamp", "deep_recheck", "ocr_image_text",
    "ocr_min_confidence",
    # 新增（v2.0.3 F5）：类别开关 + 行为标量
    "mask_company", "mask_person", "mask_contact", "mask_address",
    "amount_mode", "amount_partial", "amount_threshold",
    "keep_excel_formula", "include_institutions", "person_loose",
    "xls_to_xlsx",
    # v2.1.8 新增：仅指定词模式位
    "words_only",
)

# 非开关字段（内容清单/路径）：不进 plan 快照的口径固化
NON_SWITCH_FIELDS = ("custom_words", "whitelist", "mapping_csv", "output_dir")


def _make_src(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    src = root / "样例文件.pdf"
    src.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return src


def test_new_plan_has_full_option_keys(c: _Case, root: Path):
    print("· 新 plan：first_round_options 含 MaskOptions 全量类别开关")
    out_root = root / "new"
    # 各开关取非默认值，证明记录的是「当轮实际值」而非默认值
    options = MaskOptions(
        output_dir=str(out_root),
        mask_company=False, mask_person=False, mask_contact=False,
        mask_address=False, mask_amount=True, amount_mode="round_wan",
        amount_partial=True, amount_threshold=1000.0,
        keep_excel_formula=False, include_institutions=True,
        person_loose=False, mask_stamp=True, ocr_image_text=True,
        xls_to_xlsx=True, deep_recheck=False, ocr_min_confidence=0.75)
    b = mp.PlanBuilder(_make_src(root / "new_src"), options)
    b.set_page(0, "native", 0, 0)
    b.set_native_text(0, "占位文本")
    path = b.save(mp.output_root(b._src, options))
    data = json.loads(path.read_text(encoding="utf-8"))
    fropts = data["header"]["first_round_options"]
    missing = [k for k in EXPECTED_OPTION_KEYS if k not in fropts]
    c.check("16 键全量在场（无缺失）", not missing, f"missing={missing}")
    if missing:
        return
    c.check("类别开关记当轮实际值（非默认值）",
            fropts["mask_company"] is False
            and fropts["mask_person"] is False
            and fropts["mask_contact"] is False
            and fropts["mask_address"] is False
            and fropts["mask_amount"] is True
            and fropts["amount_mode"] == "round_wan"
            and fropts["amount_partial"] is True
            and fropts["amount_threshold"] == 1000.0
            and fropts["keep_excel_formula"] is False
            and fropts["include_institutions"] is True
            and fropts["person_loose"] is False
            and fropts["mask_stamp"] is True
            and fropts["ocr_image_text"] is True
            and fropts["xls_to_xlsx"] is True
            and fropts["deep_recheck"] is False
            and fropts["ocr_min_confidence"] == 0.75,
            str(fropts))
    c.check("旧 5 键原样保留（键名/语义不变）",
            all(k in fropts for k in EXPECTED_OPTION_KEYS[:5]))
    c.check("内容/路径字段不进快照",
            all(k not in fropts for k in NON_SWITCH_FIELDS), str(sorted(fropts)))
    # 旧读取点兼容：pdf_handler 漂移提示按 `key in fropts` 取 4 键 + 阈值，
    # 新键在场不改变其行为（固定键元组遍历）
    c.check("头四键强校验不受影响（装载命中）",
            mp.load_plan(b._src, options)[0] is not None)


def test_old_plan_without_new_keys_loads(c: _Case, root: Path):
    print("· 旧 plan（first_round_options 仅 5 键夹具）：装载不失效")
    out_root = root / "old"
    src = _make_src(root / "old_src")
    options = MaskOptions(output_dir=str(out_root))
    # 手工构造 v2.0.1 形态 plan：仅 5 旧键，无任何 F5 新键
    header = {
        "plan_schema_version": mp.PLAN_SCHEMA_VERSION,
        "app_version": str(APP_VERSION),
        "src_content_hash": content_hash(src),
        "brands_hash": __import__("app.analysis_cache", fromlist=["brands_config_hash"]).brands_config_hash(),
        "src_name": src.name,
        "created": "2026-09-11T00:00:00",
        "page_count": 1,
        "first_round_options": {
            "mask_amount": False,
            "mask_stamp": True,
            "deep_recheck": True,
            "ocr_image_text": True,
            "ocr_min_confidence": 0.6,
        },
    }
    data = {"header": header,
            "filename": {"src_stem": src.stem, "pairs": []},
            "table_rows": [],
            "pages": [{"pno": 0, "mode": "native", "hits": 0, "low_conf": 0,
                       "raster": False, "native_spans": [], "ops": []}]}
    path = mp.plan_path_for(src, mp.output_root(src, options))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    plan, reason = mp.load_plan(src, options)
    c.check("旧 plan 装载命中（新键缺失不作废）", plan is not None, reason)
    if plan is None:
        return
    fropts = plan.header.get("first_round_options") or {}
    c.check("旧 plan 开关键保持 5 键原样", len(fropts) == 5, str(sorted(fropts)))
    c.check("页表/指令可读（重放契约不破坏）",
            plan.page_count == 1 and plan.page_by_pno(0) is not None)


def test_schema_version_no_bump_added_optional_key(c: _Case, root: Path):
    print("· plan_schema_version 不 bump 自查：加可选键非结构变更")
    c.check("PLAN_SCHEMA_VERSION 保持 1（可选键不改变结构/装载契约）",
            mp.PLAN_SCHEMA_VERSION == 1, str(mp.PLAN_SCHEMA_VERSION))
    # 论证锚点：装载强校验四键集合不含 first_round_options 的任何键；
    # 新旧 plan 双向可读（上两用例），不存在需要 bump 隔离的读取分叉。
    options = MaskOptions(output_dir=str(root / "schemav"))
    src = _make_src(root / "schemav_src")
    b = mp.PlanBuilder(src, options)
    b.set_page(0, "native", 0, 0)
    b.save(mp.output_root(src, options))
    plan, reason = mp.load_plan(src, options)
    c.check("新 plan 在 PLAN_SCHEMA_VERSION=1 下自洽装载",
            plan is not None and plan.header["plan_schema_version"] == 1,
            reason)


def main() -> bool:
    print("== v2.0.3 WF3 plan 快照全量开关键（F5）测试 ==")
    c = _Case()
    root = Path(tempfile.mkdtemp(prefix="v203plan_"))
    test_new_plan_has_full_option_keys(c, root)
    test_old_plan_without_new_keys_loads(c, root)
    test_schema_version_no_bump_added_optional_key(c, root)
    print(f"\n共 {c.total} 条检查，失败 {len(c.failed)} 条")
    return not c.failed


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(0 if main() else 1)
