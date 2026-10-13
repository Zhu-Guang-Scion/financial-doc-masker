# -*- coding: utf-8 -*-
"""分析层缓存（app/analysis_cache.py，v1.9.0）单元测试。

覆盖：
- site_key：同输入同键 / 异 dpi 异键 / 异图像异键；
- content_hash：稳定性与内容敏感性（改一字节 hash 即变）；
- brands_config_hash：无配置 → "none"；FINMASK_BRANDS 指向文件 → 内容
  hash；改一条即变（维护者失效规格「brands 一字之变全量失效」的单测化）；
- 头部强校验：schema_version / app_version / engine_name / brands_hash
  任一篡改 → miss_all（回退真引擎，loaded=False）；
- 命中回放：真引擎（OnnxV6Engine small@CPU，本机权重）对 fitz 生成的小
  PNG 识别——直连一次、缓存代理记录+save 一次、新代理 consume 回放一次，
  断言 OCRPageResult 的 lines 字段级全等（text/bbox/confidence 精确相等），
  并断言回放轮零真引擎调用、bbox 还原为 tuple；
- 损坏 JSON → 静默回退不抛异常；
- consume=False 只写不读（预置哨兵条目不被回放，当轮结果为真引擎产出）；
- resolve_output_root / cache_path_for：批目录名反推输出根与缓存文件命名。

运行：python tests/test_analysis_cache.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import analysis_cache as ac
from app import app_config
from app.ocr_engine import OCRLine, OCRPageResult, OnnxV6Engine
from app.utils import APP_VERSION as _REAL_APP_VERSION
from app.utils import program_data_dir as _real_program_data_dir


class _Case:
    """极简用例收集器（与既有回归套件的 run() 风格一致）。"""

    def __init__(self):
        self.failed = []
        self.total = 0

    def check(self, name, cond, detail=""):
        self.total += 1
        if cond:
            print(f"  PASS {name}")
        else:
            self.failed.append(name)
            print(f"  FAIL {name} {detail}")


class _FakeEngine:
    """确定性假 OCR 引擎：可计数调用，输出仅依赖输入字节。"""

    def __init__(self, tag="fake", name="fake-ocr(test)"):
        self.tag = tag
        self.name = name
        self.calls = 0

    def recognize(self, image_bytes: bytes, dpi: int) -> OCRPageResult:
        self.calls += 1
        out = OCRPageResult()
        out.lines.append(OCRLine(
            text=f"{self.tag}-{dpi}-{len(image_bytes)}",
            bbox=(1.0, 2.5, 30.25, 4.0),  # 含非整数值，考验 JSON float 往返
            confidence=0.87654))
        out.lines.append(OCRLine(text="第二行", bbox=(0.0, 10.0, 12.5, 14.25),
                                 confidence=0.5))
        return out


def _tmpdir(prefix="_acache_test_") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def _record_cache(engine_name, path, tag="round1", app_version=None,
                  consume=False, sites=1):
    """用假引擎走一遍 记录→save，返回 (代理, 引擎)。"""
    eng = _FakeEngine(tag=tag, name=engine_name)
    proxy = ac.CachedOcrEngine(eng, path, app_version=app_version or _REAL_APP_VERSION,
                               consume=consume)
    for i in range(sites):
        proxy.recognize(bytes([i]) * 64, 200)
    proxy.save()
    return proxy, eng


# ---------------------------------------------------------------------------
# ① site_key
# ---------------------------------------------------------------------------

def test_site_key(c: _Case):
    k1 = ac.site_key(b"same-image", 200)
    k2 = ac.site_key(b"same-image", 200)
    c.check("同输入同键", k1 == k2)
    c.check("键为 64 位 hex（sha256）", len(k1) == 64 and all(ch in "0123456789abcdef" for ch in k1))
    c.check("异 dpi 异键", k1 != ac.site_key(b"same-image", 600))
    c.check("异图像异键", k1 != ac.site_key(b"other-image", 200))
    c.check("dpi 进键（200 vs 2000 不串）", k1 != ac.site_key(b"same-image", 2000))


# ---------------------------------------------------------------------------
# ② content_hash
# ---------------------------------------------------------------------------

def test_content_hash(c: _Case):
    d = _tmpdir()
    try:
        p = d / "src.pdf"
        p.write_bytes(b"AAAAABBBBBCCCCC")
        h1 = ac.content_hash(p)
        h2 = ac.content_hash(p)
        c.check("同文件重复计算稳定", h1 == h2)
        c.check("hash 为 64 位 hex", len(h1) == 64)
        with open(p, "r+b") as f:  # 只改一个字节
            f.seek(2)
            f.write(b"X")
        h3 = ac.content_hash(p)
        c.check("改一字节 hash 即变", h1 != h3)
        big = d / "big.bin"
        big.write_bytes(os.urandom(3 * 1024 * 1024 + 7))  # 跨多个 1MB 块
        c.check("大文件流式 hash 稳定", ac.content_hash(big) == ac.content_hash(big))
    finally:
        pass  # 临时目录留给系统清理


# ---------------------------------------------------------------------------
# ③ brands_config_hash（维护者失效规格单测化）
# ---------------------------------------------------------------------------

def test_brands_config_hash(c: _Case):
    from app import brand_config
    d = _tmpdir()
    brands_file = d / "brands.json"
    brands_file.write_text(json.dumps({"brands": ["云舒雅"], "aliases": [],
                                       "domain_hints": []}, ensure_ascii=False),
                           encoding="utf-8")
    old_env = os.environ.get(brand_config.ENV_OVERRIDE)
    old_pdd = app_config.program_data_dir
    try:
        app_config.program_data_dir = lambda: d / "nonexistent-program-dir"
        os.environ[brand_config.ENV_OVERRIDE] = str(brands_file)
        h1 = ac.brands_config_hash()
        import hashlib
        c.check("FINMASK_BRANDS 指向文件 → 内容 hash",
                h1 == hashlib.sha256(brands_file.read_bytes()).hexdigest())

        # 改一条（一字节级差异）→ hash 变
        brands_file.write_text(json.dumps({"brands": ["云舒木"], "aliases": [],
                                           "domain_hints": []}, ensure_ascii=False),
                               encoding="utf-8")
        c.check("brands 改一条 hash 即变", ac.brands_config_hash() != h1)

        # 无配置（env 清空 + 程序目录无 brands.json）→ "none"
        os.environ.pop(brand_config.ENV_OVERRIDE, None)
        c.check("无配置 → 'none'", ac.brands_config_hash() == "none")
    finally:
        if old_env is None:
            os.environ.pop(brand_config.ENV_OVERRIDE, None)
        else:
            os.environ[brand_config.ENV_OVERRIDE] = old_env
        app_config.program_data_dir = old_pdd


# ---------------------------------------------------------------------------
# ④ 头部强校验（逐项篡改 → miss_all）
# ---------------------------------------------------------------------------

def test_header_validation(c: _Case):
    d = _tmpdir()
    path = d / "cache" / "f1.json"
    for field, newval in (("schema_version", 99),
                          ("app_version", "0.0.1-test-old"),
                          ("engine_name", "PP-OCRv6-ONNX-medium(gpu)"),
                          ("brands_hash", "deadbeef")):
        _record_cache("fake-ocr(test)", path)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["header"][field] = newval
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        eng2 = _FakeEngine(tag="round2")
        proxy = ac.CachedOcrEngine(eng2, path, app_version=_REAL_APP_VERSION,
                                   report_log=lambda m: None, consume=True)
        st = proxy.stats()
        c.check(f"篡改 {field} → miss_all（loaded=False）",
                st["loaded"] is False and st["miss_reason"] != "", f"stats={st}")
        proxy.recognize(b"probe" * 16, 200)
        c.check(f"篡改 {field} → 回退真引擎识别", eng2.calls == 1)
        proxy.save()
        data2 = json.loads(path.read_text(encoding="utf-8"))
        c.check(f"篡改 {field} → save 后头部按当前值重建",
                data2["header"][field] == {"schema_version": ac.CACHE_SCHEMA_VERSION,
                                           "app_version": _REAL_APP_VERSION,
                                           "engine_name": "fake-ocr(test)",
                                           "brands_hash": data2["header"]["brands_hash"]}[field])

    # 哨兵验证：头部不匹配时旧站点绝不回放（防异源产出污染）
    _record_cache("fake-ocr(test)", path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["header"]["engine_name"] = "另一引擎"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    eng3 = _FakeEngine(tag="round3")
    proxy3 = ac.CachedOcrEngine(eng3, path, app_version=_REAL_APP_VERSION, consume=True)
    r = proxy3.recognize(bytes([0]) * 64, 200)  # 旧缓存里有这个键
    c.check("头部不符时同键站点不回放旧值", r.lines[0].text.startswith("round3-"))


# ---------------------------------------------------------------------------
# ⑤ 真引擎命中回放（OnnxV6Engine small@CPU + fitz 生成 PNG）
# ---------------------------------------------------------------------------

def _render_test_png(d: Path) -> bytes:
    import fitz
    doc = fitz.open()
    try:
        page = doc.new_page(width=280, height=150)
        page.insert_text((30, 55), "云舒雅家居股份有限公司", fontsize=13,
                         fontname="china-s")
        page.insert_text((30, 95), "联系人：张三 13800138000", fontsize=11,
                         fontname="china-s")
        pix = page.get_pixmap(matrix=fitz.Matrix(200 / 72.0, 200 / 72.0), alpha=False)
        png_path = d / "probe.png"
        png_path.write_bytes(pix.tobytes("png"))
        return png_path.read_bytes()
    finally:
        doc.close()


class _CountingEngine:
    """真引擎计数包装：验证命中轮零底层调用、未命中轮正常透传。"""

    def __init__(self, inner):
        self._inner = inner
        self.calls = 0

    @property
    def name(self):
        return self._inner.name

    def recognize(self, image_bytes: bytes, dpi: int) -> OCRPageResult:
        self.calls += 1
        return self._inner.recognize(image_bytes, dpi)


def _lines_snapshot(result) -> list:
    return [(ln.text, tuple(ln.bbox), ln.confidence) for ln in result.lines]


def test_real_engine_replay(c: _Case):
    try:
        eng = OnnxV6Engine(tier="small", use_gpu=False)  # CPU 推理保证逐字节确定
    except Exception as e:  # noqa: BLE001  组件缺失环境跳过（本机权重在，正常可用）
        print(f"  INFO 跳过真引擎回放用例（OnnxV6 不可用：{type(e).__name__}: {e}）")
        return
    d = _tmpdir()
    path = d / "cache" / "probe.json"
    logs = []
    png = _render_test_png(d)
    dpi = 200

    direct = eng.recognize(png, dpi)
    c.check("真引擎直连产出非空行", len(direct.lines) >= 1,
            f"lines={len(direct.lines)}")

    # 第一轮：consume=False 记录（auto 轮语义）+ save
    proxy1 = ac.CachedOcrEngine(eng, path, app_version=_REAL_APP_VERSION,
                                report_log=logs.append, consume=False)
    r1 = proxy1.recognize(png, dpi)
    proxy1.save()
    c.check("记录轮与直连结果一致（透传真 OCR）",
            _lines_snapshot(r1) == _lines_snapshot(direct))
    c.check("缓存文件已落盘", path.is_file())

    # 第二轮：consume=True 回放（映射复核轮语义）
    eng2 = _CountingEngine(OnnxV6Engine(tier="small", use_gpu=False))
    proxy2 = ac.CachedOcrEngine(eng2, path, app_version=_REAL_APP_VERSION,
                                report_log=logs.append, consume=True)
    r2 = proxy2.recognize(png, dpi)
    st = proxy2.stats()
    a, b = _lines_snapshot(direct), _lines_snapshot(r2)
    c.check("回放轮命中（hits=1, misses=0）",
            st["hits"] == 1 and st["misses"] == 0, f"stats={st}")
    c.check("回放轮零真引擎调用", eng2.calls == 0, f"底层调用 {eng2.calls} 次")
    c.check("回放轮缓存已载入（loaded=True）", st["loaded"] is True)
    c.check("行数全等", len(a) == len(b), f"{len(a)} vs {len(b)}")
    c.check("text 逐行全等", [x[0] for x in a] == [x[0] for x in b])
    c.check("bbox 逐行精确全等（float 往返无损）",
            all(x[1] == y[1] for x, y in zip(a, b)))
    c.check("confidence 逐行精确全等",
            all(x[2] == y[2] for x, y in zip(a, b)))
    c.check("回放 bbox 为 tuple（与引擎惯例同形）",
            all(type(ln.bbox) is tuple for ln in r2.lines),
            f"types={[type(ln.bbox).__name__ for ln in r2.lines]}")
    c.check("table_cells 回放为空表", r2.table_cells == [])
    c.check("consume 命中日志待 process 末尾输出（stats 口径正确）",
            st["consume"] and st["hits"] > 0)
    # 未命中站点照常透传真引擎（键绑定图像内容）
    r3 = proxy2.recognize(png + b"\x00", dpi)
    c.check("异图像未命中透传真引擎", eng2.calls == 1 and r3 is not None)


# ---------------------------------------------------------------------------
# ⑥ 损坏 JSON → 静默回退
# ---------------------------------------------------------------------------

def test_corrupt_json(c: _Case):
    d = _tmpdir()
    path = d / "cache" / "broken.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{这不是JSON[[", encoding="utf-8")
    eng = _FakeEngine(tag="rebuild")
    proxy = ac.CachedOcrEngine(eng, path, app_version=_REAL_APP_VERSION,
                               report_log=lambda m: None, consume=True)
    st = proxy.stats()
    c.check("损坏文件 loaded=False 且有原因", st["loaded"] is False and "损坏" in st["miss_reason"],
            f"stats={st}")
    r = proxy.recognize(b"img-bytes", 200)  # 不抛异常即为过
    c.check("损坏文件回退真引擎不抛", eng.calls == 1 and r.lines)
    proxy.save()
    data = json.loads(path.read_text(encoding="utf-8"))
    c.check("save 后文件重建为合法缓存", isinstance(data.get("sites"), dict)
            and len(data["sites"]) == 1)

    # 空文件 / 非对象 JSON 同口径
    path.write_text("", encoding="utf-8")
    proxy2 = ac.CachedOcrEngine(_FakeEngine(), path, app_version=_REAL_APP_VERSION,
                                consume=True)
    c.check("空文件 miss_all", proxy2.stats()["loaded"] is False)
    path.write_text("[1,2,3]", encoding="utf-8")
    proxy3 = ac.CachedOcrEngine(_FakeEngine(), path, app_version=_REAL_APP_VERSION,
                                consume=True)
    c.check("非对象 JSON miss_all", proxy3.stats()["loaded"] is False)


# ---------------------------------------------------------------------------
# ⑦ consume=False 只写不读
# ---------------------------------------------------------------------------

def test_consume_false_never_reads(c: _Case):
    d = _tmpdir()
    path = d / "cache" / "w.json"
    # 预置一份同头部、同键、但内容为哨兵的合法缓存
    _, eng0 = _record_cache("fake-ocr(test)", path, tag="SENTINEL")
    png = bytes([0]) * 64
    key = ac.site_key(png, 200)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert key in data["sites"]
    data["sites"][key]["lines"] = [{"text": "SENTINEL-不应回放",
                                    "bbox": [0.0, 0.0, 1.0, 1.0],
                                    "confidence": 0.99}]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    eng = _FakeEngine(tag="fresh", name="fake-ocr(test)")
    proxy = ac.CachedOcrEngine(eng, path, app_version=_REAL_APP_VERSION, consume=False)
    r = proxy.recognize(png, 200)
    st = proxy.stats()
    c.check("consume=False 不读缓存（hits=0）", st["hits"] == 0 and eng.calls == 1)
    c.check("consume=False 返回真引擎当轮结果（哨兵未回放）",
            r.lines[0].text.startswith("fresh-"))
    proxy.save()
    data2 = json.loads(path.read_text(encoding="utf-8"))
    c.check("save 后同键被当轮结果覆盖", data2["sites"][key]["lines"][0]["text"].startswith("fresh-"))
    # 头部匹配时 save 合并磁盘旧站点（同键必同值，覆盖只为防御手改）
    c.check("站点数在 save 后 ≥ 记录数", len(data2["sites"]) >= 1)


# ---------------------------------------------------------------------------
# ⑧ 回放对象同形性（dict/tuple 类型级断言）
# ---------------------------------------------------------------------------

def test_replay_shape(c: _Case):
    d = _tmpdir()
    path = d / "cache" / "shape.json"
    _record_cache("fake-ocr(test)", path, tag="shape")
    proxy = ac.CachedOcrEngine(_FakeEngine(tag="never", name="fake-ocr(test)"),
                               path, app_version=_REAL_APP_VERSION, consume=True)
    png = bytes([0]) * 64
    r = proxy.recognize(png, 200)
    c.check("回放为 OCRPageResult", isinstance(r, OCRPageResult))
    c.check("回放行为 OCRLine", all(isinstance(ln, OCRLine) for ln in r.lines))
    c.check("bbox 类型为 tuple", all(type(ln.bbox) is tuple for ln in r.lines))
    c.check("bbox 元素为 float", all(isinstance(v, float) for ln in r.lines for v in ln.bbox))
    c.check("name 透传真引擎", proxy.name == "fake-ocr(test)")


# ---------------------------------------------------------------------------
# ⑨ 输出根解析与缓存文件命名（接线契约）
# ---------------------------------------------------------------------------

class _Opts:
    """MaskOptions 最小替身（仅 output_dir / output_root 属性）。"""

    def __init__(self, output_dir="", output_root=None):
        self.output_dir = output_dir
        if output_root is not None:
            self.output_root = output_root


def test_output_root_and_cache_path(c: _Case):
    d = _tmpdir()
    src = d / "sub" / "报告.pdf"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"pdf-content")

    # run_batch 已把 output_dir 改写为批目录（dispatcher v1.6.3 命名）→ 反推父目录
    root = ac.resolve_output_root(src, _Opts(output_dir=str(d / "输出根" / "脱敏输出_20260908_1200")))
    c.check("批目录名反推输出根", root == str(d / "输出根"), f"root={root}")

    # 显式 output_root（调用方改写前捕获）优先
    root2 = ac.resolve_output_root(src, _Opts(
        output_dir=str(d / "x" / "脱敏输出_20260908_1201"), output_root=str(d / "显根")))
    c.check("显式 output_root 优先", root2 == str(d / "显根"))

    # 普通目录原样 / 未设置 → 源文件父目录
    c.check("普通输出目录原样作为输出根",
            ac.resolve_output_root(src, _Opts(output_dir=str(d / "普通"))) == str(d / "普通"))
    c.check("未设置 → 源文件父目录",
            ac.resolve_output_root(src, _Opts()) == str(src.parent))

    cp = ac.cache_path_for(src, str(d / "输出根"))
    import hashlib
    c.check("缓存文件名 = 源文件内容 sha256",
            cp.name == hashlib.sha256(src.read_bytes()).hexdigest() + ".json",
            f"name={cp.name}")
    c.check("缓存路径 = <输出根>/cache/<hash>.json",
            cp.parent == (d / "输出根" / "cache"))

    # RuleEngine.mapping_only 只读属性（接线 consume 开关）
    from app.rules_engine import MaskOptions, RuleEngine
    eng_auto = RuleEngine(MaskOptions())
    c.check("auto 态 mapping_only=False", eng_auto.mapping_only is False)
    csvp = d / "map.csv"
    csvp.write_text("原文,替换为\n云舒雅,ABC\n", encoding="utf-8")
    eng_map = RuleEngine(MaskOptions(mapping_csv=str(csvp)))
    c.check("映射态 mapping_only=True", eng_map.mapping_only is True)


def run():
    print("== 分析层缓存（v1.9.0 analysis_cache）测试 ==")
    c = _Case()
    test_site_key(c)
    test_content_hash(c)
    test_brands_config_hash(c)
    test_header_validation(c)
    test_real_engine_replay(c)
    test_corrupt_json(c)
    test_consume_false_never_reads(c)
    test_replay_shape(c)
    test_output_root_and_cache_path(c)
    print(f"\n共 {c.total} 条用例，失败 {len(c.failed)} 条")
    return not c.failed


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
