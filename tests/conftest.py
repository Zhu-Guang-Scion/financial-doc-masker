# -*- coding: utf-8 -*-
"""pytest 收集护栏与测试夹具统一口径。

== 1. 脚本式套件哨兵：把「空转假绿」变成显式 skip ==

机理：tests/ 下有一批自运行脚本式套件（如 test_address_regression.py），全部
断言位于 ``if __name__ == "__main__":`` 直跑块内，模块级 ``def test_*`` 计数为
0。pytest 按 test_*.py 模式导入这些文件后收集到零条用例，于是在汇总里显示为
「无声通过」（空转假绿）。官方验证链路是 python 逐文件直跑
（``python tests/test_xxx.py``），pytest 只是次要入口。

本 conftest 在收集期集中识别这类文件（pytest_collect_file）：对 pytest 本轮
要收集、文件名匹配 test_*.py、且 AST 静态计数确认模块级零用例的脚本式套件，
改用一个自定义 Module 收集器装载，其 ``collect()`` 只产出一条哨兵用例；哨兵
运行时显式 ``pytest.skip`` 并注明原因。效果：

- pytest 直跑不再对这类文件静默放行，汇总行可见 ``9 skipped``（-ra 可见原因）；
- python 直跑零变化：``python tests/test_xxx.py`` 不加载 conftest.py；
- 有模块级 ``def test_*`` 的真测试文件不受影响（计数>0 → 走默认收集）。

== 2. 品牌夹具与语料根的统一选取（与 tests/_fixtures_common.py 同源） ==

- 收集期即对 ``FINMASK_BRANDS`` 做 setdefault：本地回归配置存在则用之，
  否则回退开源内置虚构词夹具 ``tests/fixtures/brands_oss.json``（各套件
  自己的 setdefault 因此变为同值 no-op，干净 clone 不再静默降级为空品牌表）；
- 本地真实语料根常量 ``CORPUS_DIR``（env ``FINMASK_CORPUS_DIR`` 可覆盖），
  供依赖真实语料的套件引用，目录不存在时自行 skip（本地语料未配置时跳过）。

== 3. 开源首轮排除钩子（OSS_FIRST_ROUND=1 时生效）与品牌夹具缺失自动 skip ==

环境变量 ``OSS_FIRST_ROUND=1`` 表示「开源首轮测试集」口径：人名夹具虚构化
重构尚未完成的耦合件在收集期整文件 skip 并注明原因，保证开源仓库
``pytest tests/`` 直跑全绿（被排除件显式 skipped，其余全过）。

v2.1.9 R3-P1-2 起，同一批耦合件不再依赖该隐藏 env 开关：收集期检测本地
回归配置 ``_eval/brands_test.json`` 是否存在——不存在（干净 clone 特征）
即自动 skip 并注明「品牌夹具缺失（干净 clone）」；存在（本地）则照常
运行、零变化。``OSS_FIRST_ROUND=1`` 的显式排除行为原样保留（优先于自动
skip 判定）。本地语料口径：品牌词配置 ``_eval/brands_test.json``、词表
外置档 ``_goal_work/wordlists_private/wordlists_private.json``、真实语料
根 ``FINMASK_CORPUS_DIR``（默认仓库同级「测试文件夹」）——三者均不入
开源库，缺失时相关套件按各自口径 skip 或回退开源夹具。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from _fixtures_common import CORPUS_DIR, LOCAL_BRANDS_JSON, resolve_brands_fixture

# 收集期统一品牌夹具：本地配置优先、缺失回退开源虚构词夹具（见模块 docstring）
os.environ.setdefault("FINMASK_BRANDS", resolve_brands_fixture())
# 词表外置档（2026-09-21 外置工程）：本地私档存在则注入（同 brands 降级模式）；
# 干净 clone 无档=缺档警告+通用默认词表（语料条目依赖的测试应 skip 而非红）
from _fixtures_common import resolve_wordlists_fixture  # noqa: E402
os.environ.setdefault("FINMASK_WORDLISTS", resolve_wordlists_fixture())

_SKIP_REASON = (
    "脚本式套件哨兵：本文件无任何 pytest 可收集的用例（模块级 def test_* 计数=0，"
    "断言全部位于 if __name__ == '__main__' 直跑块内），pytest 下导入即通过属空转假绿；"
    "官方验证方式为 python 直跑本文件（python tests/test_xxx.py 逐文件口径）"
)

_SENTINEL_NAME = "test__script_suite_needs_python_direct_run"

# 开源首轮（OSS_FIRST_ROUND=1）整文件排除名单：人名夹具虚构化重构未完成
# 的耦合件（映射表 §四，开源后续轮收回）。_v204_adversarial.py 为共享样本
# 库模块（不被 pytest 收集），列入仅为让排除口径自文档完整。
_OSS_FIRST_ROUND_EXCLUDED = {
    "_v204_adversarial.py",
    "test_f4_label_anchor.py",
    "test_f4_layout_list.py",
    "test_person_list_layout_p0.py",
    "test_person_name_regression.py",
}

# v2.1.9 R3-P1-2：品牌夹具耦合件（上面名单去掉非收集的共享模块）——
# 这些套件显式指向不入开源库的本地回归配置 _eval/brands_test.json，
# 干净 clone 上必然红。此前要靠隐藏 env 开关 OSS_FIRST_ROUND=1 才全绿
# （文档负担），现按「夹具在不在」自动判定：在（本地）照常跑、零变化；
# 不在（干净 clone）自动 skip 并注明口径。
_BRAND_FIXTURE_COUPLED = frozenset(
    _OSS_FIRST_ROUND_EXCLUDED - {"_v204_adversarial.py"})
_BRAND_FIXTURE_MISSING_REASON = (
    "品牌夹具缺失（干净 clone）：本套件依赖不入库的本地回归配置"
    " _eval/brands_test.json；本地语料口径见 conftest 头注")


def _count_collectable_tests(path: Path) -> int:
    """按 pytest 默认收集语义静态计数：模块级 ``def test*`` 与 ``Test*`` 类内
    的 ``def test*`` 方法。不进入 If/Try/With/For 等块体（直跑块内定义不算）、
    不进入函数体（嵌套定义不被 pytest 收集）。"""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    count = 0

    def scan(body) -> None:
        nonlocal count
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test"):
                    count += 1
            elif isinstance(node, ast.ClassDef):
                if node.name.startswith("Test"):
                    scan(node.body)

    scan(tree.body)
    return count


class _ScriptSuiteSentinel(pytest.Item):
    """为「零用例脚本式套件」生成的显式 skip 哨兵。

    reportinfo 的 lineno 取 0 而非 None：marker 式整文件 skip（开源首轮排除
    钩子）叠加到哨兵项时，pytest 生成 skip 报告需要可解析的文件位置。
    """

    def runtest(self) -> None:
        pytest.skip(_SKIP_REASON)

    def reportinfo(self):
        return self.path, 0, _SENTINEL_NAME


class _ScriptSuiteModule(pytest.Module):
    """装载零用例脚本式套件的 Module 收集器：只产出一条哨兵用例。"""

    def collect(self):
        yield _ScriptSuiteSentinel.from_parent(self, name=_SENTINEL_NAME)


def pytest_collect_file(file_path, parent):
    """对 pytest 本轮要收集的 test_*.py 做 AST 预检：模块级零用例 → 用哨兵
    收集器接管；其余（含全部真测试文件）返回 None 交回默认收集。"""
    fp = Path(file_path)
    if fp.suffix != ".py" or not fp.name.startswith("test_"):
        return None
    try:
        has_tests = _count_collectable_tests(fp) > 0
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None  # 解析不了的不接管，交回默认行为
    if has_tests:
        return None
    return _ScriptSuiteModule.from_parent(parent, path=fp)


def pytest_collection_modifyitems(config, items):
    """开源首轮口径（env OSS_FIRST_ROUND=1）：人名夹具耦合件整文件 skip，
    保证开源仓库 ``pytest tests/`` 直跑全绿（被排除件显式 skipped）。

    v2.1.9 R3-P1-2：OSS_FIRST_ROUND 未设时，品牌夹具缺失（干净 clone
    特征：``_eval/brands_test.json`` 不存在）同样对耦合件自动 skip——
    开源首轮全绿不再依赖隐藏 env 开关的文档负担；本地（夹具在）零变化。
    OSS_FIRST_ROUND=1 时沿用其原有排除口径与文案（行为不变，优先于
    自动 skip 判定）。"""
    oss = os.environ.get("OSS_FIRST_ROUND") == "1"
    clean_clone = not LOCAL_BRANDS_JSON.is_file()
    for item in items:
        name = Path(item.fspath).name
        if oss and name in _OSS_FIRST_ROUND_EXCLUDED:
            item.add_marker(pytest.mark.skip(
                reason="开源首轮排除：人名夹具虚构化重构未完成"
                       "（TODO 开源后续轮，映射表 §四）"))
        elif clean_clone and name in _BRAND_FIXTURE_COUPLED:
            item.add_marker(pytest.mark.skip(reason=_BRAND_FIXTURE_MISSING_REASON))


@pytest.fixture(scope="session")
def brands_json():
    """当前生效的品牌夹具 JSON 路径（本地配置优先，缺失回退开源虚构词夹具）。"""
    return resolve_brands_fixture()


@pytest.fixture(scope="session")
def corpus_dir():
    """本地真实语料根；未配置（目录不存在）时显式 skip。"""
    if not CORPUS_DIR.is_dir():
        pytest.skip(f"本地语料未配置（FINMASK_CORPUS_DIR 可指定，当前不存在）：{CORPUS_DIR}")
    return CORPUS_DIR


# ---------------------------------------------------------------------------
# v2.0.2 搭车项：`def test_xxx(c: _Case)` 形参的 pytest 化
# ---------------------------------------------------------------------------

class _CaseFallback:
    """与各套件模块级 ``_Case`` 同构的兜底用例收集器（仅当某模块未定义
    ``_Case`` 时启用；现有 9 个受影响套件均自带同名类，走不到这里）。"""

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


@pytest.fixture
def c(request):
    """为 ``test_xxx(c: _Case)`` 形参提供「每条用例独立实例」的 _Case。

    直跑口径（python 逐文件）里 ``c`` 是 run()/main() 内的单一实例、套件尾
    统一判败；pytest 口径改为每条用例独立实例 + teardown 汇总评审决策：单条
    用例内 check 失败仍逐条收集打印、不中断后续检查（与直跑同构），teardown
    时若有失败则以 AssertionError 判本条用例失败——「该用例有失败检查 ⇔
    该用例判败」与直跑逐条等价。实例类取自用例所在模块的同名 ``_Case``
    （9 个套件各自定义、接口一致：failed/total/check(name, cond, detail)），
    输出风格与直跑逐字一致。

    python 直跑不加载 conftest.py，本 fixture 对官方验证链路零影响。
    """
    case_cls = getattr(request.module, "_Case", None)
    case = case_cls() if case_cls is not None else _CaseFallback()
    yield case
    if case.failed:
        raise AssertionError(
            f"{len(case.failed)}/{case.total} 条检查失败: {case.failed}")


def pytest_configure(config):
    # test_frozen_replay.test_m1_frozen_replay 与 test_masking_ledger
    # .test_round1_integrity 的返回值是直跑 main() 链式注入的载体（pytest 侧
    # 由 _m1_artifacts fixture 复刻产物、返回值弃用）。pytest 7 起对「用例
    # 返回非 None」发 PytestReturnNotNoneWarning，此豁免仅消该条告警，
    # 不涉及任何断言语义。
    config.addinivalue_line(
        "filterwarnings", "ignore::pytest.PytestReturnNotNoneWarning")
