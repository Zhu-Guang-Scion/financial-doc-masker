# -*- coding: utf-8 -*-
"""候选词扫描引擎（app/candidate_scanner.py）测试集（G2/S1）。

覆盖（任务书交付物 3，夹具一律判例词+合成串，零真实语料上下文）：
- 四通道召回：云舒雅/司南/慕蓝朵/江图/江城市图书馆/江城图书馆/广州文汇
  研究中心/中共江城图书馆委员会 全部在榜且可见；
- 频次精确回填：全称嵌于更长串（江城图书馆内部/中共江城图书馆委员会）时
  freq 等于全语料精确计数（贪婪配对不吞频次）；
- 四闸链逐闸：动词短语灭（DN4/词性尾验）、单字后缀低证据灭、财务管理中心
  类部门词灭、图书馆 纯后缀隐藏、判例词存活；
- 功能字剥头（合流修正一）：中共X 存活、剥后不足灭；
- R-C1 声明式：低频简称经声明配对存活（J18 例外）；R-C3 剥离派生（云舒雅）；
- 打分排序（score 降序+同分频次降序）、env 覆盖与非法回退、上下文数字打码、
  ScanSession.close() 清空、visible() 排除 hidden 与 person_likely。

运行：pytest tests/test_candidate_scanner.py 或 python tests/test_candidate_scanner.py
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.candidate_scanner import (  # noqa: E402
    CandidateWord,
    ScannerConfig,
    ScanSession,
    scan_sources,
    _func_core,
    _func_core_ok,
)

# 九判例词（云住良材属 freq=1 统计不可达区，其声明例外由专用夹具以「住材」
# 等价验证；任务书验收线为九词全存活，冒烟面见 _goal_work/g2_s1_report.md）
EXEMPLARS = ("云舒雅", "司南", "慕蓝朵", "江图", "云舒雅家居",
             "江城图书馆", "云舒雅家居股份有限公司", "中共江城图书馆委员会")


# --------------------------------------------------------------------------
# 合成夹具（判例词 + 通用形态词；重复一律加括号控制，避免相邻字面量优先级陷阱）
# --------------------------------------------------------------------------
_F1 = ("江城图书馆（以下简称“江图”）是广州市的公共图书馆。"   # R-C1 声明对
       + "江城图书馆章程明确职责。" * 4
       + "云舒雅家居发布服务指南。" * 4
       + "云舒雅家居股份有限公司编制年鉴。" * 4
       + "中共江城图书馆委员会召开全体会议。" * 4
       + "财务管理中心组织培训。" * 4          # DN2 部门词形态
       + "库存待处清单公示。" * 4              # DN4 单字后缀（处）
       + "严格执行考勤制度。" * 3              # DN4 单字后缀（行，低频）
       + "联系人：张三主持会签。"        # person_likely 夹具（三处语境各异，
       + "经张三复核通过。"              #  避免等频长 n-gram 容器抢先触发 DN5，
       + "张三签批归档。")               #  保住 person 闸路径的端到端覆盖）
_F2 = ("云舒雅家居股份有限公司年报。"
       + "云舒雅体验馆开放。" * 2
       + "司南之家优惠。" * 2
       + "慕蓝朵之家促销。" * 2
       + "江城图书馆联盟章程。" * 2)
_F3 = ("司南品牌工坊开放。"
       + "司南荟。" * 2
       + "江城图书馆讲座排期。" * 2
       + "江图发布。" * 2)
_F4 = ("慕蓝朵直营专卖店开业。"
       + "慕蓝朵设计师联名。" * 2
       + "云舒雅康养基地。" * 2
       + "云舒雅家居分馆开放。" * 2)
_F5 = ("慕蓝朵旗舰店。"
       + "云舒雅家居生活。" * 2
       + "江图讲堂。" * 2
       + "司南荟登陆。" * 2)

_MAIN_NAMES = ["合成甲.docx", "合成乙.docx", "合成丙.docx", "合成丁.docx", "合成戊.docx"]
_MAIN_TEXTS = [_F1, _F2, _F3, _F4, _F5]
_MAIN_PATHS = [Path(n) for n in _MAIN_NAMES]

_CACHE: dict = {}


def _main_session() -> ScanSession:
    """主夹具会话（只读复用；close 用例单独构建）。"""
    if "main" not in _CACHE:
        _CACHE["main"] = scan_sources(_MAIN_PATHS, reuse_texts=dict(zip(_MAIN_PATHS, _MAIN_TEXTS)))
    return _CACHE["main"]


def _by_word(session: ScanSession) -> dict:
    return {c.word: c for c in session.candidates}


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """brands 配置与 G2 调试 env 全隔离（同源装载走空配置 = 代码表原样）。"""
    monkeypatch.setenv("FINMASK_BRANDS", "__no_such_brands__.json")
    for k in ("FINMASK_G2_FREQ_MIN", "FINMASK_G2_NGRAM_MAX",
              "FINMASK_G2_SINGLE_SUFFIX_MIN", "FINMASK_G2_DERIVE_MIN_FREQ"):
        monkeypatch.delenv(k, raising=False)


# --------------------------------------------------------------------------
# 四通道召回 + schema 面
# --------------------------------------------------------------------------
def test_four_channel_exemplar_recall():
    """八判例词全在榜且可见（hidden=None、person=False），频次=全语料精确计数。"""
    sess = _main_session()
    by = _by_word(sess)
    for w in EXEMPLARS:
        assert w in by, f"判例词不在榜: {w}"
        c = by[w]
        assert c.hidden_reason is None, (w, c.hidden_reason)
        assert c.person_likely is False, w
        assert c.freq == sum(t.count(w) for t in _MAIN_TEXTS), w
        assert c.files_n == sum(1 for t in _MAIN_TEXTS if w in t), w
        assert c.already_configured is None  # S1 恒 None（契约修正一）
        assert set(c.channels) <= {"ngram", "suffix", "abbrev", "declared", "jieba_nt"}


def test_channel_evidence():
    """通道证据：江图=声明+简称+ngram；江城图书馆/江城市图书馆=双通道；
    中共江城图书馆委员会=机构形态（合流修正二提名后缀召回 10 字全称）。"""
    by = _by_word(_main_session())
    assert {"declared", "abbrev", "ngram"} <= set(by["江图"].channels)
    assert by["江图"].declared is True
    assert {"ngram", "suffix"} <= set(by["江城图书馆"].channels)
    assert {"ngram"} <= set(by["云舒雅家居"].channels)  # ngram 通道（suffix 覆盖由江城图书馆断言承载）
    assert "suffix" in by["中共江城图书馆委员会"].channels
    assert by["中共江城图书馆委员会"].org_form is True


def test_scores_sorted_and_schema():
    """score 降序（J15），同分频次降序；分值域与 schema 字段完整。"""
    sess = _main_session()
    ranked = [(c.score, c.freq) for c in sess.candidates]
    assert ranked == sorted(ranked, key=lambda kv: (-kv[0], -kv[1]))
    for c in sess.candidates:
        assert 0.0 <= c.score <= 100.0
        assert isinstance(c, CandidateWord)
        assert isinstance(c.freq, int) and c.freq >= 0
        assert isinstance(c.files_n, int) and c.files_n >= 0
        assert c.first_loc == {} or set(c.first_loc) == {"file_idx", "start", "end"}


def test_visible_excludes_hidden_and_person():
    """visible() 排除 hidden 与 person_likely 两组。"""
    sess = _main_session()
    vis_words = {c.word for c in sess.visible()}
    by = _by_word(sess)
    assert "张三" in by and by["张三"].person_likely is True   # nr+联系人职务上下文
    assert "张三" not in vis_words
    assert by["图书馆"].hidden_reason == "纯组织后缀"
    assert "图书馆" not in vis_words
    for c in sess.candidates:
        if c.hidden_reason is None and not c.person_likely:
            assert c.word in vis_words


# --------------------------------------------------------------------------
# 频次精确回填（回填纠偏）
# --------------------------------------------------------------------------
def test_refill_counts_embedded_occurrences():
    """江城图书馆 167→502 类形态的小夹具：全称仅以更长串（中共江城图书馆
    委员会/江城图书馆内部）嵌现时，回填 freq = 全语料精确计数。"""
    text = ("中共江城图书馆委员会。" * 2
            + "江城图书馆内部治理。" * 2
            + "江城图书馆章程一次。")
    sess = scan_sources([Path("回填甲.docx")], reuse_texts={Path("回填甲.docx"): text})
    by = _by_word(sess)
    assert "江城图书馆" in by
    c = by["江城图书馆"]
    assert c.freq == text.count("江城图书馆") == 5
    assert c.files_n == 1
    # 后缀通道贪婪配对只提名更长形（中共江城图书馆委员会/江城图书馆内部），
    # 精确频次只能来自回填——通道级提名计数必然小于 5
    assert c.first_loc == {"file_idx": 0, "start": 2, "end": 7}


# --------------------------------------------------------------------------
# 四闸链逐闸 + 功能字剥头
# --------------------------------------------------------------------------
def test_dn4_single_suffix_low_evidence():
    """库存待处类单字后缀低证据灭（J3：len≤4 且 count<10）。"""
    by = _by_word(_main_session())
    assert by["库存待处"].hidden_reason == "单字后缀低证据"
    assert by["严格执行"].hidden_reason == "单字后缀低证据"  # freq 3 <10


def test_dn2_generic_dept_word():
    """财务管理中心类部门词灭（G 表覆盖切分）；判例词全部存活。"""
    by = _by_word(_main_session())
    assert by["财务管理中心"].hidden_reason == "通用部门词"
    for w in EXEMPLARS:
        assert by[w].hidden_reason is None, w


def test_pure_suffix_hidden():
    """图书馆：整词即组织后缀 → 纯后缀隐藏（勾了也遮不掉，v1.9.3 同源语义）。"""
    by = _by_word(_main_session())
    assert by["图书馆"].hidden_reason == "纯组织后缀"


def test_pos_tail_verb_phrases():
    """日起施行/严格执行类动词短语灭（词性尾验；高频仍灭）；江城图书馆内部
    已被 DN5 残片闸前置拦截（出现全被「江城图书馆内部治理」解释）。"""
    sent = ("日起施行。" * 6 + "严格执行。" * 6 + "江城图书馆内部治理。" * 6)
    texts = [sent, sent, sent]  # 三份文件（files_n≥3 免人名信号干扰）
    paths = [Path(f"词性{i}.docx") for i in range(3)]
    sess = scan_sources(paths, reuse_texts=dict(zip(paths, texts)))
    by = _by_word(sess)
    for w in ("日起施行", "严格执行"):
        assert w in by, w
        assert by[w].hidden_reason == "词性尾验", (w, by[w].hidden_reason)
        assert by[w].freq == 18  # 3 文件 × 6 次（occurrence 级精确计数）


def test_dn5_fragment_hidden():
    """DN5 长词内部残片闸（S1 后续修复·实考驱动）：独立计数 eff=freq−容器
    频 ≤0 → 残片隐藏；判例词「仅全称内嵌」形态同理（无独立遮蔽价值）。"""
    texts = ["江城图书馆内部治理。" * 6] * 2
    paths = [Path("残片甲.docx"), Path("残片乙.docx")]
    sess = scan_sources(paths, reuse_texts=dict(zip(paths, texts)))
    by = _by_word(sess)
    assert by["江城图书馆内部"].hidden_reason == "长词内部残片"
    # 嵌现全称内部的裸品牌形：派生仍发生（abbrev 通道留痕），但 eff=0 → 隐藏
    texts2 = ["云舒雅家居股份有限公司公告。" * 4] * 3
    paths2 = [Path(f"残片丙{i}.docx") for i in range(3)]
    sess2 = scan_sources(paths2, reuse_texts=dict(zip(paths2, texts2)))
    by2 = _by_word(sess2)
    assert by2["云舒雅"].hidden_reason == "长词内部残片"
    assert "abbrev" in by2["云舒雅"].channels
    # 部分独立出现 → 存活（主夹具 云舒雅 7 次中 0 次被 ≥3 频容器解释）
    assert _by_word(_main_session())["云舒雅"].hidden_reason is None


def test_person_fragment_propagation():
    """人名传播（J19）：人名词内部子串同标 person 组（家明类残片不可达榜）。"""
    from app.candidate_scanner import _Tally, _propagate_person
    stage3 = ["张三丰", "张三", "三丰", "图书馆"]
    tally = {w: _Tally() for w in stage3}
    tally["张三丰"].person = True
    n = _propagate_person(stage3, tally)
    assert tally["张三"].person is True and tally["三丰"].person is True
    assert tally["图书馆"].person is False
    assert n == 2


def test_func_core_semantics():
    """功能字剥头（合流修正一）：中共X 剥头后实体核 ≥2 存活；剥后不足灭。"""
    assert _func_core("中共江城图书馆委员会") == "共江城图书馆委员会"
    assert _func_core_ok("中共江城图书馆委员会") is True
    assert _func_core("进行了") == "进行"
    assert _func_core_ok("的执") is False          # 剥后不足 2 字 → 灭
    assert _func_core_ok("严格执行") is True       # 无功能字头尾（词性尾验负责灭）
    by = _by_word(_main_session())
    assert by["中共江城图书馆委员会"].hidden_reason is None


# --------------------------------------------------------------------------
# 通道 C：R-C1 声明式 + R-C3 剥离派生
# --------------------------------------------------------------------------
def test_declared_lowfreq_exception():
    """R-C1：低频简称经声明配对存活（J18 频次下限的声明例外）。"""
    text = ("云住良材试点项目启动。以下简称“住材”。住材专班负责推进。"
            "某服务中心挂牌。")
    sess = scan_sources([Path("声明甲.docx")], reuse_texts={Path("声明甲.docx"): text})
    by = _by_word(sess)
    assert "住材" in by
    c = by["住材"]
    assert c.declared is True and c.freq == 2
    assert c.hidden_reason is None and c.person_likely is False
    assert "declared" in c.channels
    # J18：非声明的低频提名放弃（不入榜、有计数）
    assert "某服务中心" not in by
    assert sess.meta["low_freq_dropped"] >= 1


def test_rc3_stripping_derivation():
    """R-C3：云舒雅家居股份有限公司（freq≥10，len>ngram_max 走精确计数资格）
    → 剥法定后缀+行业尾词 → 云舒雅（abbrev 通道）。派生发生且留痕，但本夹具
    云舒雅仅以全称内嵌出现（eff=0）→ DN5 残片隐藏（无独立遮蔽价值）。"""
    text = "云舒雅家居股份有限公司公告。" * 4
    texts = [text, text, text]
    paths = [Path(f"派生{i}.docx") for i in range(3)]
    sess = scan_sources(paths, reuse_texts=dict(zip(paths, texts)))
    by = _by_word(sess)
    assert "云舒雅" in by
    assert "abbrev" in by["云舒雅"].channels
    assert by["云舒雅"].freq == 12  # 回填：全称内嵌出现计入
    assert by["云舒雅"].hidden_reason == "长词内部残片"


# --------------------------------------------------------------------------
# 会话面：上下文打码 / source_files / close / 进度回调 / reuse
# --------------------------------------------------------------------------
def test_context_text_digit_mask():
    """上下文 ≥7 位数字打码「〔数字×N〕」（B 口径），换行渲染为 ⏎。"""
    text = ("江城图书馆文号穗财12345678号发布。" * 3   # ×3 过频次门限；首现在 0 位
            + "附录联系方式。")
    sess = scan_sources([Path("打码甲.docx")], reuse_texts={Path("打码甲.docx"): text})
    by = _by_word(sess)
    ctx = sess.context_text(by["江城图书馆"], span=15)
    assert "12345678" not in ctx
    assert "〔数字×8〕" in ctx


def test_source_files_and_progress():
    """source_files 前 N 文件名；on_progress 逐文件回调。"""
    calls = []
    sess = scan_sources(_MAIN_PATHS, reuse_texts=dict(zip(_MAIN_PATHS, _MAIN_TEXTS)),
                        on_progress=lambda done, total: calls.append((done, total)))
    by = _by_word(sess)
    names = sess.source_files(by["江城图书馆"], limit=5)
    assert len(names) == 3 and set(names) <= set(_MAIN_NAMES)
    assert calls[-1] == (5, 5) and len(calls) == 5


def test_close_clears_everything():
    """close() 清空全部内存文本与候选（零持久化闭环）。"""
    sess = scan_sources([Path("关闭甲.docx")],
                        reuse_texts={Path("关闭甲.docx"): "云舒雅家居股份有限公司章程。" * 5})
    assert sess.candidates and sess.visible()
    sess.close()
    assert sess.candidates == []
    assert sess.visible() == []
    assert sess.context_text(CandidateWord(word="江城图书馆",
                                           first_loc={"file_idx": 0, "start": 0, "end": 5})) == ""
    assert sess.source_files(CandidateWord(word="江城图书馆")) == []


def test_scan_session_default_init():
    """ScanSession 可空构造（S2 单测 mock 面）。"""
    s = ScanSession()
    assert s.candidates == [] and s.visible() == [] and s.meta == {}


# --------------------------------------------------------------------------
# 环境变量覆盖与非法回退
# --------------------------------------------------------------------------
def test_env_overrides():
    """FINMASK_G2_* 合法值覆盖 J1-J4 默认。"""
    import os
    os.environ["FINMASK_G2_FREQ_MIN"] = "5"
    os.environ["FINMASK_G2_NGRAM_MAX"] = "7"
    os.environ["FINMASK_G2_SINGLE_SUFFIX_MIN"] = "20"
    os.environ["FINMASK_G2_DERIVE_MIN_FREQ"] = "7"
    try:
        cfg = ScannerConfig()
        assert (cfg.freq_min, cfg.ngram_max, cfg.single_suffix_min,
                cfg.derive_min_freq) == (5, 7, 20, 7)
    finally:
        for k in ("FINMASK_G2_FREQ_MIN", "FINMASK_G2_NGRAM_MAX",
                  "FINMASK_G2_SINGLE_SUFFIX_MIN", "FINMASK_G2_DERIVE_MIN_FREQ"):
            os.environ.pop(k, None)


def test_env_invalid_fallback_with_warning():
    """非法值回退默认并 warnings 告警（band 几何 env 先例）。"""
    import os
    os.environ["FINMASK_G2_FREQ_MIN"] = "abc"
    os.environ["FINMASK_G2_NGRAM_MAX"] = "0"      # 低于下限 2
    try:
        with pytest.warns(UserWarning, match="FINMASK_G2_FREQ_MIN"):
            cfg = ScannerConfig()
        assert cfg.freq_min == 3
        with pytest.warns(UserWarning, match="FINMASK_G2_NGRAM_MAX"):
            cfg2 = ScannerConfig()
        assert cfg2.ngram_max == 9
    finally:
        os.environ.pop("FINMASK_G2_FREQ_MIN", None)
        os.environ.pop("FINMASK_G2_NGRAM_MAX", None)


def test_defaults_match_ruling():
    """默认值=评审决策 J1-J6 固化（contract §0 红线 4）。"""
    cfg = ScannerConfig()
    assert (cfg.freq_min, cfg.ngram_max, cfg.single_suffix_min,
            cfg.derive_min_freq, cfg.weights, cfg.top_page) == \
        (3, 9, 10, 10, (0.35, 0.20, 0.30, 0.15), 50)


# --------------------------------------------------------------------------
# 闸链计数与零持久化面
# --------------------------------------------------------------------------
def test_meta_gate_counters():
    """每闸拦截计数留痕（响亮优于静默；meta 只计数）。"""
    meta = _main_session().meta
    gh = meta["gate_hidden"]
    assert gh["纯组织后缀"] >= 1
    assert gh["通用部门词"] >= 1
    assert gh["单字后缀低证据"] >= 2
    assert gh["长词内部残片"] >= 1  # DN5（图书 类纯内部残片）
    assert meta["nominees_n"] >= len(_by_word(_main_session()))


def test_nomination_suffix_extra_constant():
    """合流修正二：_NOMINATION_SUFFIX_EXTRA 仅含三词（J20 冻结面）。"""
    from app import candidate_scanner as cs
    assert cs._NOMINATION_SUFFIX_EXTRA == frozenset({"委员会", "党委", "党委会"})


# --------------------------------------------------------------------------
# 2026-09-16 碎词治理批：DN6 扩 / DN7 自包含修复 / DN8 对称化 / DN9 邻字闸 /
# configured_words 外部容器。形态依据=54 份实考（_eval/g2_frag_audit.py 与
# g2_frag_diag*.py 逐词归因，判例词邻字集中度全部 ≤0.74 留在安全带）。
# --------------------------------------------------------------------------
def _scan_texts(texts, configured=None):
    paths = [Path(f"合成{n}.docx") for n in range(len(texts))]
    return scan_sources(paths, reuse_texts=dict(zip(paths, texts)),
                        configured_words=configured)


def test_dn9_edge_fragment_gate():
    """DN9 边界残片：容器被功能字生成期滤除（应收账款-「应」∈FUNC）时，
    残片出现点 CJK 左邻集中度 ≥0.90 且绝对量 ≥5 → 长词内部残片隐藏。"""
    texts = ["应收账款清查工作开始。" * 9          # 「收账款」9 次 pre=应
             + "另收账款单独核销登记一次。"]        # 1 次 pre=另（独立出现）
    sess = _scan_texts(texts)
    c = _by_word(sess).get("收账款")
    assert c is not None and c.hidden_reason == "长词内部残片"


def test_dn9_real_entity_survives_edge_gate():
    """DN9 误杀防线：真实体邻字分散（独立句首=BOUND 多样）且独立出现
    充分，不受邻字闸影响；低频真实体被 edge_min=5 绝对量豁免。"""
    texts = ["江城图书馆。" * 6
             + "到江城图书馆借阅的读者很多。" * 2
             + "云舒雅橱柜。" * 3]
    sess = _scan_texts(texts)
    by = _by_word(sess)
    for w in ("江城图书馆", "云舒雅橱柜"):
        c = by.get(w)
        assert c is not None and c.hidden_reason is None, w


def test_dn8_head_glue_variants():
    """DN8 头缀方向（对称化）：「成立江城图书馆」剥头后=池内实体（freq≥10）
    → 粘连变体隐藏；多层头（本办法+适用+功能字于）同杀。"""
    texts = ["江城图书馆章程明确职责。" * 10
             + "成立江城图书馆。开展业务合作。" * 4
             + "本办法适用于云舒雅家居股份有限公司。" * 3
             + "云舒雅家居股份有限公司。" * 10]
    sess = _scan_texts(texts)
    by = _by_word(sess)
    assert by["成立江城图书馆"].hidden_reason == "粘连变体"
    assert by["本办法适用于云舒雅家居股份有限公司"].hidden_reason == "粘连变体"
    # 判例词本体不受头缀闸影响
    assert by["云舒雅家居股份有限公司"].hidden_reason is None


def test_dn7_selfcontain_fix_no_gluetail_kills_fullname():
    """DN8 rem2 收窄回归锚（判例误杀回退判例）：「云舒雅家居股份有限公司」=
    云舒雅家居 + 股份有限公司（G 可覆盖段）——法定后缀段是全名组成部分，
    不得判粘连；「江城图书馆委员会」同理（实体+委员会）。"""
    texts = ["云舒雅家居股份有限公司。" * 10
             + "江城图书馆委员会。" * 10]
    sess = _scan_texts(texts)
    by = _by_word(sess)
    assert by["云舒雅家居股份有限公司"].hidden_reason is None
    assert by["江城图书馆委员会"].hidden_reason is None


def test_dn6_zongze_docno():
    """DN6 扩：总则/附则第X条 章节条款粘连形态 → 公文条款号隐藏。"""
    sess = _scan_texts(["总则第一条明确适用范围。附则第五条明确解释权。"] * 4)
    c = _by_word(sess).get("总则第一条")
    assert c is not None and c.hidden_reason == "公文条款号"


def test_pos_tail_recheck_ntok2():
    """POS 尾验 tail 复核放宽 ntok≤2：词典 nr 误标双 token 形态
    （必须/d + 严格执行/nr——54 份实考）尾部词性复核 v → 词性尾验隐藏。"""
    sess = _scan_texts(["必须严格执行。" * 5])
    c = _by_word(sess).get("必须严格执行")
    assert c is not None and c.hidden_reason == "词性尾验"


def test_configured_words_external_container():
    """configured_words 外部可信容器（维护者指令「结合已输入自定义敏感词
    筛选」）：双容器分摊形态（应/税——「应」被功能字滤除致容器缺席，
    「税收账款」池内仅覆盖一半）零配置时残片存活；注入配置词后 DN5 区间
    覆盖隐藏。「了」∈FUNC 断右扩展容器，隔离「收账款账龄」类右容器干扰。
    真实语料实测（54 份）：舒雅 224/云舒雅家居 134/城图书馆 512 均被外部
    容器隐藏，独立简称「云舒雅」222 次保留（全名 find 命不中独立简称）。"""
    texts = ["应收账款了结。" * 6
             + "税收账款了结。" * 6]
    base = _scan_texts(texts)
    c0 = _by_word(base).get("收账款")
    assert c0 is not None and c0.hidden_reason is None  # 零配置基线：容器分摊+邻字 0.5
    sess = _scan_texts(texts, configured=["应收账款"])
    c1 = _by_word(sess).get("收账款")
    assert c1 is not None and c1.hidden_reason == "长词内部残片"


def test_configured_words_zero_behavior_default():
    """零行为默认（考场口径不受影响）：configured_words=None 与不传参
    的候选榜逐词 hidden_reason 全等。"""
    paths = _MAIN_PATHS
    kw = dict(reuse_texts=dict(zip(paths, _MAIN_TEXTS)))
    s_none = scan_sources(paths, configured_words=None, **kw)
    s_omit = scan_sources(paths, **kw)
    m_none = {c.word: c.hidden_reason for c in s_none.candidates}
    m_omit = {c.word: c.hidden_reason for c in s_omit.candidates}
    assert m_none == m_omit


def test_env_edge_params():
    """DN9 参数 env 覆盖（沿 J1-J4 先例）：EDGE_RATIO/EDGE_MIN 可调，
    非法值回退默认+告警。"""
    import warnings as _w
    from app import candidate_scanner as cs
    texts = ["应收账款清查工作开始。" * 9 + "另收账款单独核销登记一次。"]
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FINMASK_G2_EDGE_RATIO", "0.99")
        sess = _scan_texts(texts)  # 9/10=0.90 < 0.99 → 不隐藏
        c = _by_word(sess).get("收账款")
        assert c is not None and c.hidden_reason is None
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FINMASK_G2_EDGE_MIN", "99")
        sess = _scan_texts(texts)  # 绝对量 9 < 99 → 不隐藏
        c = _by_word(sess).get("收账款")
        assert c is not None and c.hidden_reason is None
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("FINMASK_G2_EDGE_RATIO", "abc")
        with _w.catch_warnings(record=True) as rec:
            sess = _scan_texts(texts)
        assert any("FINMASK_G2_EDGE_RATIO" in str(x.message) for x in rec)
        c = _by_word(sess).get("收账款")
        assert c is not None and c.hidden_reason == "长词内部残片"  # 回退默认 0.90


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


def test_scan_stop_protocol_partial_results():
    """v2.1.4 停止协议：on_progress 返回 False → 已提取部分照常出结果，
    meta interrupted/files_n/files_total 诚实标注（此前回调返回值被忽略，
    「正在停止/部分结果」文案不实——阶段 1 review 实锤）。"""
    paths = [Path("停甲.docx"), Path("停乙.docx"), Path("停丙.docx")]
    texts = ["江城图书馆文献信息采选管理办法施行细则。"] * 3
    calls = []

    def on_progress(done, total):
        calls.append(done)
        return done < 2   # False=请求停止（第 2 个文件完成后）

    sess = scan_sources(paths, on_progress=on_progress,
                        reuse_texts=dict(zip(paths, texts)))
    assert calls == [1, 2]                      # 第 3 个不再回调
    assert sess.meta["interrupted"] is True
    assert sess.meta["files_n"] == 2 and sess.meta["files_total"] == 3
    assert all(c.hidden_reason is None for c in sess.visible())  # 部分结果可用
    sess.close()


def test_scan_stop_protocol_not_triggered_on_none():
    """回调返回 None（UI 节流路径）不得触发停止——仅显式 False 停。"""
    paths = [Path("续甲.docx"), Path("续乙.docx")]
    texts = ["慕蓝朵直营专卖店财务细则施行。"] * 2
    sess = scan_sources(paths, on_progress=lambda d, t: None,
                        reuse_texts=dict(zip(paths, texts)))
    assert sess.meta["interrupted"] is False
    assert sess.meta["files_n"] == 2
    sess.close()
