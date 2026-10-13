# -*- coding: utf-8 -*-
"""v1.9.4 首轮冻结 plan（app/masking_plan.py）单元测试。

覆盖（任务书单测项 8 之 plan 部分）：
- 序列化/反序列化 roundtrip：PlanBuilder 装配 → save → load_plan 逐字段一致
  （四形态指令 native_spans/jspan/rect/filename + 页级元数据 mode/low_conf/
  raster/hits + ocr.items）；
- 头部四键逐字段校验：plan_schema_version / app_version / src_content_hash /
  brands_hash 任一篡改 → miss + 人话原因；页数不一致由 pdf_handler 校验；
- 原子写：save 后目录内无 .tmp 残留；重复 save 幂等（内容一致）；
- 增行防呆四闸（契约评审决策 #3）：长度<2 / 三 stop 表 / 纯度 / jieba 频次
  （真实 jieba）逐一拒绝+原因；正常键放行；
- find_add_occurrences：全部出现定位 + 与幸存区间重叠豁免；
- apply_plan_edits：删除按原文全局（native/jspan/rect/文件名对全通道）+
  删除清单；改值只更新真编辑项；
- merge_filename_pairs：仅首轮快照（编辑已由 apply_plan_edits 烤入；引擎/
  编辑表派生对不再并入——覆盖轮引擎=整张编辑表，碎片键上名=文件名误改名），
  长度降序。

运行：python tests/test_masking_plan.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 预设依赖用例：品牌预设已外置（与其他回归套件一致）
os.environ.setdefault("FINMASK_BRANDS", str(
    Path(__file__).resolve().parents[1] / "_eval" / "brands_test.json"))

from app import masking_plan as mp
from app.analysis_cache import brands_config_hash, content_hash
from app.rules_engine import MaskOptions, RuleEngine
from app.utils import APP_VERSION

# pytest 次要入口：把 test_*(…, root) 的 root 形参暴露为 module 级 fixture，
# 与直跑 main() 的单一共享 root 同名同构（同前缀临时根、模块内共享）。
# python 直跑不经过 pytest 收集，这里的 import/fixture 对直跑口径零影响。
try:
    import pytest
except ImportError:  # pragma: no cover - 直跑环境无 pytest
    pytest = None

if pytest is not None:
    @pytest.fixture(scope="module")
    def root():
        return Path(tempfile.mkdtemp(prefix="v194plan_"))


class _Case:
    def __init__(self):
        self.failed = []
        self.total = 0

    def check(self, name, cond, detail=""):
        self.total += 1
        if not cond:
            self.failed.append(f"{name}: {detail}")
            print(f"  ✘ {name} {detail}")
        else:
            print(f"  ✔ {name}")


def _make_src(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    src = root / "样例文件.pdf"
    src.write_bytes(b"%PDF-1.4\n%%EOF\n")  # 内容哈希用占位 PDF（不参与解析）
    return src


def _builder(src: Path, options: MaskOptions) -> "mp.PlanBuilder":
    b = mp.PlanBuilder(src, options)
    # 页 0：native
    b.set_page(0, "native", 3, 0)
    b.set_native_text(0, "云舒雅家居股份有限公司与王建国的协议\n第二行条款")
    b.add_native_spans(0, [(0, 12, "company", "ABC股份有限公司", "高",
                            "云舒雅家居股份有限公司")])
    b.add_op_rect(0, "redact_draw", (10.0, 20.0, 30.0, 40.0), "***", "***",
                  "550", "amount", "第1页(列对齐)")
    b.add_op_rect(0, "stamp", (50.0, 60.0, 90.0, 100.0), "██", "██",
                  "红色印章区域#1", "stamp", "第1页")
    # 页 1：ocr
    b.set_page(1, "ocr", 2, 1)
    b.mark_raster(1)
    b.ensure_ocr(1)["low_conf"] = 1
    b.set_ocr_items(1, [("姓名王建国", (70.0, 60.0, 180.0, 80.0), 0.95),
                        ("特批客户订单确认函补贴81.00",
                         (70.0, 170.0, 350.0, 190.0), 0.92)])
    b.add_op_jspan(1, 2, 5, "person", "王**", "高", "王建国", "(OCR)", "m")
    b.add_op_jspan(1, 14, 19, "amount", "***", "", "81.00", "(OCR)", "a")
    b.snapshot_filename([("云舒雅家居", "ABC"), ("王建国", "王*")])
    b.snapshot_table_rows([
        ("云舒雅家居股份有限公司", "ABC股份有限公司", "company", "第1页"),
        ("王建国", "王*", "person", "第1页"),
        ("81.00", "***", "amount", "第2页(OCR)"),
    ])
    return b


def test_roundtrip_and_header(c: _Case, root: Path):
    print("· 序列化 roundtrip 与头部四键")
    src = _make_src(root)
    out_root = root / "out"
    options = MaskOptions(output_dir=str(out_root))
    b = _builder(src, options)
    path = b.save(mp.output_root(src, options))
    c.check("plan 落盘于 cache/plans/<内容哈希>.json",
            path == out_root / "cache" / "plans" / f"{content_hash(src)}.json",
            str(path))
    c.check("无 .tmp 残留", not list(path.parent.glob("*.tmp")))
    plan, reason = mp.load_plan(src, options)
    c.check("装载命中", plan is not None, reason)
    if plan is None:
        return
    c.check("header 四键齐全",
            all(k in plan.header for k in
                ("plan_schema_version", "app_version",
                 "src_content_hash", "brands_hash")))
    c.check("schema 版本", plan.header["plan_schema_version"] == mp.PLAN_SCHEMA_VERSION)
    c.check("app_version", plan.header["app_version"] == APP_VERSION)
    c.check("src_content_hash=文件名双保险",
            plan.header["src_content_hash"] == content_hash(src)
            == path.stem)
    c.check("brands_hash", plan.header["brands_hash"] == brands_config_hash())
    c.check("不含 engine_name", "engine_name" not in plan.header)
    c.check("页数", plan.page_count == 2
            and plan.header["page_count"] == 2)
    # 四形态指令逐字段
    pg0 = plan.page_by_pno(0)
    c.check("页 0 元数据", pg0["mode"] == "native" and pg0["hits"] == 3
            and pg0["raster"] is False)
    c.check("native_spans 六元组",
            pg0["native_spans"] == [[0, 12, "company", "ABC股份有限公司",
                                     "高", "云舒雅家居股份有限公司"]])
    kinds = [op["k"] for op in pg0["ops"]]
    c.check("页 0 rect 指令（redact_draw+stamp 按应用序）",
            kinds == ["redact_draw", "stamp"], str(pg0["ops"]))
    c.check("rect 成品矩形",
            pg0["ops"][0]["rect"] == [10.0, 20.0, 30.0, 40.0])
    pg1 = plan.page_by_pno(1)
    c.check("页 1 元数据（ocr+raster+low_conf）",
            pg1["mode"] == "ocr" and pg1["raster"] is True
            and pg1["low_conf"] == 1)
    c.check("ocr.items 快照",
            [it[0] for it in pg1["ocr"]["items"]] ==
            ["姓名王建国", "特批客户订单确认函补贴81.00"])
    jspans = [op for op in pg1["ops"] if op["t"] == "jspan"]
    c.check("jspan 指令（m/a preset）",
            [sp["p"] for sp in jspans] == ["m", "a"]
            and jspans[0]["orig"] == "王建国"
            and jspans[1]["e"] - jspans[1]["s"] == 5)
    c.check("filename 对快照",
            plan.filename["pairs"] == [["云舒雅家居", "ABC"], ["王建国", "王*"]])
    c.check("table_rows 剔除 amount 行",
            [r[0] for r in plan.table_rows()] ==
            ["云舒雅家居股份有限公司", "王建国"])
    # 原子写幂等
    data_before = json.loads(path.read_text(encoding="utf-8"))
    b.save(mp.output_root(src, options))
    data_after = json.loads(path.read_text(encoding="utf-8"))
    c.check("重复 save 幂等", data_before == data_after)


def test_header_validation(c: _Case, root: Path):
    print("· 头部四键逐字段篡改 → miss + 人话原因")
    src = _make_src(root / "v")
    out_root = root / "v" / "out"
    options = MaskOptions(output_dir=str(out_root))
    _builder(src, options).save(mp.output_root(src, options))
    path = mp.plan_path_for(src, mp.output_root(src, options))
    base = json.loads(path.read_text(encoding="utf-8"))
    cases = {
        "plan_schema_version": 99,
        "app_version": "1.9.3",
        "src_content_hash": "0" * 64,
        "brands_hash": "deadbeef",
    }
    for field, bad in cases.items():
        data = json.loads(json.dumps(base))
        data["header"][field] = bad
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        plan, reason = mp.load_plan(src, options)
        c.check(f"篡改 {field} → miss", plan is None, str(reason))
        c.check(f"篡改 {field} → 人话原因", field in reason, reason)
    # 结构损坏
    path.write_text("{not json", encoding="utf-8")
    plan, reason = mp.load_plan(src, options)
    c.check("损坏 JSON → miss", plan is None and "损坏" in reason)
    src2 = _make_src(root / "无plan")  # 源存在但从未落盘 plan
    plan, reason = mp.load_plan(src2, MaskOptions(output_dir=str(root / "无plan")))
    c.check("plan 不存在 → miss", plan is None and "不存在" in reason, reason)


def test_add_key_gates(c: _Case):
    print("· 增行防呆四闸（契约评审决策 #3，真实 jieba）")
    engine = RuleEngine(MaskOptions())
    cases = [
        ("关", "长度"),                       # 闸 1：strip 后长度 <2
        ("公司", "停用表"),                   # 闸 2：stop 表
        ("有限公司", "停用表"),               # 闸 2：stop 表
        ("（普通合伙）", "纯度或停用表"),     # 闸 2/3：括号归一化后纯后缀
        ("条款", "频次"),                     # 闸 4：jieba freq=830≥20 且词性 n
    ]
    for key, expect in cases:
        ok, reason = mp.add_key_allowed(key, engine)
        c.check(f"拒绝「{key}」（{expect}）", not ok, f"ok={ok} reason={reason}")
        c.check(f"「{key}」有拒绝原因", bool(reason))
    ok, reason = mp.add_key_allowed("宁柯贸易", engine)  # freq=0、非 stop、非纯后缀
    c.check("正常键「宁柯贸易」放行", ok, reason)
    ok, reason = mp.add_key_allowed("  云舒雅家居  ", engine)
    c.check("带空白正常键放行（strip 后判定）", ok, reason)


def test_find_add_occurrences(c: _Case):
    print("· find_add_occurrences 定位与重叠豁免")
    text = "鑫彤建材与鑫彤建材二组签订鑫彤建材协议"
    occ = mp.find_add_occurrences(text, "鑫彤建材", [])
    c.check("全部出现定位", occ == [(0, 4), (5, 9), (13, 17)], str(occ))
    occ2 = mp.find_add_occurrences(text, "鑫彤建材", [(0, 12)])
    c.check("与幸存区间重叠豁免", occ2 == [(13, 17)], str(occ2))
    c.check("空键安全", mp.find_add_occurrences(text, "", []) == [])


def test_apply_plan_edits(c: _Case, root: Path):
    print("· apply_plan_edits：删除按原文全局、改值真编辑")
    src = _make_src(root / "e")
    out_root = root / "e" / "out"
    options = MaskOptions(output_dir=str(out_root))
    b = _builder(src, options)
    b.save(mp.output_root(src, options))
    plan, reason = mp.load_plan(src, options)
    assert plan is not None, reason
    logs = []
    stat = mp.apply_plan_edits(plan, deletes={"王建国"},
                               changes={"云舒雅家居股份有限公司":
                                        "华为技术有限公司"},
                               report_log=logs.append)
    pg0 = plan.page_by_pno(0)
    c.check("native span 按原文删除（王建国无 native span，云舒雅保留）",
            len(pg0["native_spans"]) == 1
            and pg0["native_spans"][0][5] == "云舒雅家居股份有限公司"
            and stat["deleted"] == ["王建国"])
    c.check("jspan 按原文删除",
            all(op.get("orig") != "王建国" for op in plan.page_by_pno(1)["ops"]))
    c.check("改值全局更新 native span 绘制文本",
            pg0["native_spans"] == [[0, 12, "company", "华为技术有限公司",
                                     "高", "云舒雅家居股份有限公司"]])
    kept = [op for op in plan.page_by_pno(1)["ops"] if op["t"] == "jspan"
            and op.get("rule") != "amount"]
    c.check("删除清单落日志（含通道说明）",
            any("王建国" in ln for ln in logs), str(logs))
    c.check("文件名对按原文删除",
            plan.filename["pairs"] == [["云舒雅家居", "ABC"]])
    # amount 行（orig=81.00）不受删除影响
    c.check("未删除指令保持",
            any(op.get("orig") == "81.00" for op in plan.page_by_pno(1)["ops"]))
    # 真编辑才改：同值不产生 changed
    plan2, _ = mp.load_plan(src, options)
    stat2 = mp.apply_plan_edits(plan2, deletes=set(), changes={
        "云舒雅家居股份有限公司": "ABC股份有限公司"})  # 与 plan 行同值
    c.check("同值不记改值", stat2["changed"] == [], str(stat2))


def test_merge_filename_pairs(c: _Case, root: Path):
    print("· merge_filename_pairs：仅首轮快照（引擎/编辑表派生对不再并入），长度降序")
    src = _make_src(root / "f")
    out_root = root / "f" / "out"
    options = MaskOptions(output_dir=str(out_root),
                          custom_words=["宁柯贸易"])
    engine = RuleEngine(options)
    engine.find_spans("王建国在此签字")  # 运行期注册表（覆盖轮为整张编辑表）
    plan_pairs = [["云舒雅家居", "ABC"], ["王建国", "王*"],
                  ["特批客户订单确认函", "已核实"]]
    pairs = mp.merge_filename_pairs(plan_pairs)
    merged = dict(pairs)
    c.check("快照对全保留（含引擎注册过的同原文键）",
            merged.get("云舒雅家居") == "ABC"
            and merged.get("特批客户订单确认函") == "已核实"
            and merged.get("王建国") == "王*", str(merged))
    # M4 缺陷 1 修复口径：函数不再收引擎——覆盖轮引擎 _mapping=整张编辑表，
    # 碎片键（限公司→××× 类）与新注册键一律不上文件名（防 18/39 误改名复现）
    engine._mapping["限公司"] = "×××"
    engine.find_spans("刘新华另行签批")  # 运行期新注册人名（plan 没有的键）
    c.check("引擎编辑表碎片键/新注册键不再并入（文件名防误改名）",
            mp.merge_filename_pairs(plan_pairs) == pairs,
            str(mp.merge_filename_pairs(plan_pairs)))
    lens = [len(o) for o, _r in pairs]
    c.check("按原文长度降序", lens == sorted(lens, reverse=True), str(lens))
    c.check("空快照返回空表", mp.merge_filename_pairs(None) == []
            and mp.merge_filename_pairs([]) == [])


def main():
    print("== v1.9.4 冻结 plan（masking_plan）测试 ==")
    c = _Case()
    root = Path(tempfile.mkdtemp(prefix="v194plan_"))
    test_roundtrip_and_header(c, root)
    test_header_validation(c, root)
    test_add_key_gates(c)
    test_find_add_occurrences(c)
    test_apply_plan_edits(c, root)
    test_merge_filename_pairs(c, root)
    print(f"\n共 {c.total} 条用例，失败 {len(c.failed)} 条")
    return not c.failed


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
