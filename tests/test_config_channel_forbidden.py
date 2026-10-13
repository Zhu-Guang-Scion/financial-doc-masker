# -*- coding: utf-8 -*-
"""配置通道停用集「特例差集」参数化收敛守卫（开源治理清洗批 C1）。

背景：配置通道停用集的「预设历史语料特例」差集表达式曾在四处逐字私拷
（brand_config.write_forbidden_set / rules_engine 装载点 / candidate_scanner
两处），与「同源禁止私拷」的既有工程红线相悖。本测试锁定收敛后的语义：

- 唯一权威：差集特例词面只允许存在于 brand_config 的权威常量定义点
  （CONFIG_CHANNEL_STOP_EXCEPTION），派生常量
  CONFIG_CHANNEL_FORBIDDEN_WORDS = 引擎停用全集 − 特例例外；
- 四处消费点同源引用：rules_engine 装载点 / candidate_scanner 两处 /
  brand_config 写入端均不再私拷字面量或差集表达式；
- 语义逐元素不变：write_forbidden_set() 与收敛前的旧差集表达式逐元素相等
  （54 份冻结重放 md5 全等的判定前提）。

运行：pytest tests/test_config_channel_forbidden.py
"""
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import brand_config as bc
from app import candidate_scanner as cs
from app import rules_engine as re_mod


def _code_lines(src: str) -> list:
    """源码 → 代码体行（剥离文档串与整行/行尾注释）。

    词表 v3 清洗把原外置判例词面替换为虚构替身，落点全在
    注释与文档串（形态史注释）。配置通道的字面量唯一性断言只对代码体
    有效——注释中的词面属合法历史注释（与词表外置口径一致），不计。
    """
    import ast
    tree = ast.parse(src)
    doc = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            b = getattr(node, "body", None)
            if (b and isinstance(b[0], ast.Expr)
                    and isinstance(b[0].value, ast.Constant)
                    and isinstance(b[0].value.value, str)):
                doc.update(range(b[0].lineno,
                                 (b[0].end_lineno or b[0].lineno) + 1))
    out = []
    for i, ln in enumerate(src.splitlines(), 1):
        if i in doc:
            continue
        s = ln.split("#", 1)[0].strip()  # 行尾注释剥离（三文件字符串面无 # 字面）
        if s:
            out.append(s)
    return out


def test_authority_constant_defined_in_brand_config():
    """权威常量存在且语义 = 引擎停用全集 − 特例例外（两常量互为补集）。

    权威常量结构唯一口径（词表 v3 演练批）：特例条目随词表数据外置工程
    迁至外置档 config_stop_exception 键——有档={司南}、开源缺档=空集。
    常量结构恒为 frozenset（结构唯一），字面内容随档在/档缺，不再锚定
    len==1。
    """
    exc = bc.CONFIG_CHANNEL_STOP_EXCEPTION
    assert isinstance(exc, frozenset), exc
    ccw = bc.CONFIG_CHANNEL_FORBIDDEN_WORDS
    assert isinstance(ccw, frozenset)
    # 特例词面被排除在配置通道停用集之外（允许经配置通道登记）
    assert ccw.isdisjoint(exc)
    # 并集还原引擎停用全集：除特例词面外逐元素相等
    assert ccw == re_mod._BRAND_STOP_ALL - exc  # 差集还原（注入例外不必在全集）
    # 特例词面属于引擎停用全集（学习通道仍不得复活）
    assert exc <= re_mod._BRAND_STOP_ALL
    # 同一常量重复访问为同一对象（模块级缓存，同源引用语义）
    assert bc.CONFIG_CHANNEL_FORBIDDEN_WORDS is ccw


def test_write_forbidden_set_element_equal_to_legacy_expression():
    """write_forbidden_set() 与旧差集表达式逐元素相等（含 extra_forbidden）。"""
    legacy = ((re_mod._BRAND_STOP_ALL - bc.CONFIG_CHANNEL_STOP_EXCEPTION)
              | frozenset(re_mod.NON_NAME_BLACKLIST))
    assert bc.write_forbidden_set() == legacy
    extra = bc.write_forbidden_set(["示例追加词", 123])
    assert extra == legacy | {"示例追加词"}


def test_rules_engine_consumes_authority_constant():
    """rules_engine 装载点引用权威常量，代码体不再私拷特例字面量/差集表达式。

    「司南」为合法词面出现在注释/文档串（词表 v3 清洗把原判例词
    替换落点在注释形态史）——字面量唯一断言按代码体口径（_code_lines）。
    """
    src = inspect.getsource(re_mod)
    assert "CONFIG_CHANNEL_STOP_EXCEPTION" in src
    assert "CONFIG_CHANNEL_FORBIDDEN_WORDS" in src
    assert "司南" not in "\n".join(_code_lines(src))


def test_candidate_scanner_consumes_single_source():
    """candidate_scanner 两处差集改由 brand_config 单一来源派生（代码体口径）。"""
    src = inspect.getsource(cs)
    assert "write_forbidden_set" in src
    assert "司南" not in "\n".join(_code_lines(src))


def test_no_duplicate_literal_outside_authority():
    """全 app/ 范围内特例字面量（代码体口径）仅允许出现在 brand_config
    权威定义点；注释/文档串中的词面属合法形态史，不计。"""
    app_dir = ROOT / "app"
    offenders = []
    for py in app_dir.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if py.name == "brand_config.py":
            continue
        if "司南" in "\n".join(_code_lines(text)):
            offenders.append(py.name)
    assert offenders == [], offenders
    # 词表外置工程（2026-09-21）：特例词面随外置档迁出代码——入库源码
    # （含 brand_config）零字面量；权威形态断言改为「运行时经外置档注入」
    assert "司南" not in inspect.getsource(bc)


if __name__ == "__main__":
    import traceback
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print("FAILURES:", failures)
    sys.exit(1 if failures else 0)
