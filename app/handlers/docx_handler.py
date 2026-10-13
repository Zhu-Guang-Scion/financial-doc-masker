# -*- coding: utf-8 -*-
"""Word(.docx) 处理器（python-docx + XML 级 w:t 全收集）。

策略：run 级原位替换，保留全部格式（字体/字号/颜色/加粗等）。

覆盖范围（XML 级 w:t 全收集）：正文、表格（含嵌套）、sdt 内容
块、文本框（w:txbxContent，含 mc:AlternateContent Choice/Fallback 双份）、
超链接（w:hyperlink 内 run，python-docx 的 paragraph.runs 不枚举）、TOC 域
缓存条目、页眉页脚全部件（含首页/奇偶页）。

根因背景（某扫描样本类别 A）：旧实现只走 doc.paragraphs/doc.tables
——sdt/文本框段落不在 doc.paragraphs（封面标题/编制行从未送检）；TOC 缓存
条目文本在 w:hyperlink 内而 paragraph.runs 不含 hyperlink run（目录首行以
空串早退）。本实现改为对每个部件根元素 iter() 全部 w:p，按段拼接自有
w:r（不下钻嵌套段落），送检后按 run 原位写回，与既有遮蔽写回机制
（text_replacement.rewrite_runs）完全同路径。

TOC 域回滚防护：遮蔽命中所在的 TOC 域整体转静态文本——剥 fldChar/
instrText 结构 run、解开 w:hyperlink 包裹、保留全部文本 run。目录是域缓存
（w:fldChar begin/instrText「TOC…」/separate/条目/end），Word/WPS 按 F9 或
打印预览刷新会从正文标题重算目录，直接改写缓存文本会被整体回滚；转静态后
域不复存在，刷新无从发生（域缓存改写路径经选型验证）。
未命中的域（含非 TOC 域）一律不动，零结构破坏。
"""
from __future__ import annotations

import re
from pathlib import Path

from ..utils import MissingDependencyError
from .text_replacement import rewrite_runs

# TOC 域指令识别：instrText 形如 ' TOC \o "1-2" \h \z \u '。
# \b 词边界保证不误配 PAGEREF _Toc… 书签名（_ 与 T 间无词边界）。
_TOC_INSTR = re.compile(r"\bTOC\b")

# mc 命名空间（python-docx nsmap 不含 mc，用 Clark 记法直书）
_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_MC_ALTERNATE = f"{{{_MC_NS}}}AlternateContent"
_MC_CHOICE = f"{{{_MC_NS}}}Choice"
_MC_FALLBACK = f"{{{_MC_NS}}}Fallback"


def process(src: Path, dst: Path, engine, options, report) -> dict:
    try:
        from docx import Document
    except ImportError as e:
        raise MissingDependencyError(
            f"处理 .docx 需要 python-docx：pip install python-docx。组件已安装仍报此错时，多为依赖库 DLL 加载失败（请安装 Microsoft VC++ 2015-2022 x64 运行库后重试）；内部原因：{e}"
        ) from e

    report.log("阶段 1/2：解析 Word 文档结构 …")
    doc = Document(str(src))
    counter = {"hits": 0, "paragraphs": 0, "toc_static": 0}

    # 主文档 + 各节页眉页脚（含首页/奇偶页部件），按 partname 去重
    # （48 节常见共享同一物理页眉部件，重复处理会双重计数）。
    seen_parts = set()
    for kind, part in _collect_parts(doc, report):
        if part.partname in seen_parts:
            continue
        seen_parts.add(part.partname)
        _process_part(part.element, kind, engine, report, counter)

    report.log(f"段落 {counter['paragraphs']} 个，命中敏感信息 {counter['hits']} 处")
    if counter["toc_static"]:
        report.log(f"目录域转静态文本 {counter['toc_static']} 处"
                   f"（遮蔽命中域防 F9 刷新回滚）")
    report.log("阶段 2/2：写出脱敏副本 …")
    doc.save(str(dst))
    return {"masked": counter["hits"]}


# ---------------------------------------------------------------------------
# 部件收集：主文档 + 页眉页脚全部件
# ---------------------------------------------------------------------------

def _collect_parts(doc, report=None):
    """产出 (位置前缀, part)：正文 + 每节默认/首页/奇偶页眉页脚。

    report：部件读取失败留痕用（尽力携带 partname，
    取不到时回退节序+属性名）；None 时静默降级（兼容旧调用口径）。
    """
    yield "正文", doc.part
    for si, section in enumerate(doc.sections, 1):
        for kind, attr in (("页眉", "header"), ("页脚", "footer"),
                           ("页眉", "first_page_header"), ("页脚", "first_page_footer"),
                           ("页眉", "even_page_header"), ("页脚", "even_page_footer")):
            hf = None
            try:
                hf = getattr(section, attr)
                yield kind, hf.part
            except Exception as e:  # 单节部件缺失不阻断（如无首页页眉）
                # v2.1.9：该 except 此前静默 continue——页眉页脚
                # 部件文字可能漏遮且无任何痕迹。补一行留痕（含 partname）；
                # 刻意保留宽捕获：WPS/异形包在部件链路上抛出的异常面收窄
                # 有漏网风险，控制流语义不变（照旧跳过该部件）。
                if report is not None:
                    try:
                        _pn = ""
                        try:
                            _part = getattr(hf, "part", None) if hf is not None else None
                            _pn = str(getattr(_part, "partname", "") or "")
                        except Exception:
                            pass
                        _where = f"partname={_pn}" if _pn else f"节属性 {attr}"
                        report.log(f"⚠ 第{si}节{kind}部件读取失败已跳过"
                                   f"（{_where}）：[{type(e).__name__}] {e}"
                                   f"——该部件文字可能未经脱敏，请人工复核。")
                    except Exception:
                        pass
                continue


# ---------------------------------------------------------------------------
# 单部件处理：全量 w:p 收集 → 送检 → run 原位写回 → 命中 TOC 域转静态
# ---------------------------------------------------------------------------

def _process_part(root, kind: str, engine, report, counter) -> None:
    from docx.oxml.ns import qn

    # 单次遍历快照：lxml 元素代理对象的 id() 仅在引用存活期间稳定，
    # 全部坐标系（元素序/段落表/表格表）必须由同一份快照派生。
    all_elems = list(root.iter())
    if not any(el.tag == qn("w:p") for el in all_elems):
        return

    # 元素文档序索引：域范围判定 / 表格行列号的统一坐标系
    eidx = {id(el): i for i, el in enumerate(all_elems)}
    paragraphs = [el for el in all_elems if el.tag == qn("w:p")]
    p_seq = {id(p): i for i, p in enumerate(paragraphs)}
    fields, fldsimple_tocs = _scan_fields(all_elems, eidx)
    toc_fields = [f for f in fields if f["is_toc"]]
    dirty_toc = set()          # 待转静态的命中 TOC 域（fields 下标）
    tbl_elems = [el for el in all_elems if el.tag == qn("w:tbl")]
    choice_texts: dict = {}    # id(AlternateContent) -> Choice 侧已记账段落全文

    for p in paragraphs:
        runs = _own_runs(p)
        if not runs:
            continue  # 纯容器段（嵌套段落宿主），自有文本为空
        full = "".join(r.text or "" for r in runs)
        if not full.strip():
            continue
        counter["paragraphs"] += 1
        spans = engine.find_spans(full)
        # mc:AlternateContent Choice/Fallback 双份去重：Fallback 副本照常
        # 改写（两份必须同步，否则新旧阅读器呈现不一致），但不再重复记账
        # ——视觉一份、台账一笔。
        ac_kind, ac_id = _alternate_content_branch(p)
        record = not (ac_kind == "fallback" and full in choice_texts.get(ac_id, ()))
        if not spans:
            continue
        loc = _location(p, p_seq[id(p)], kind, eidx, tbl_elems, toc_fields, qn)
        if ac_kind == "choice":
            choice_texts.setdefault(ac_id, set()).add(full)
        # run 级原位写回（与旧路径同一机制；CT_R 自带 text get/set，
        # xml:space 等细节由 python-docx 统一处理）
        rewrite_runs(runs, spans)
        for sp in spans:
            if record:
                report.record(sp.text, sp.replacement, sp.rule, loc)
        counter["hits"] += len(spans)
        # 遮蔽命中落在 TOC 域范围内 → 该域标记待转静态
        p_i = eidx[id(p)]
        for fi, f in enumerate(fields):
            if f["is_toc"] and _in_field(p, p_i, f):
                dirty_toc.add(fi)
        # run 级 fldSimple 型 TOC：本段被改写的 run 位于其内 → 待转静态
        for fi, fs in enumerate(fldsimple_tocs):
            if any(fs in r.iterancestors() for r in runs):
                dirty_toc.add(len(fields) + fi)

    # 遮蔽已全部写回后，统一把命中 TOC 域转静态（剥域结构保文本）
    for fi in sorted(dirty_toc):
        if fi < len(fields):
            _convert_field_to_static(all_elems, fields[fi], eidx, qn)
        else:
            _unwrap(fldsimple_tocs[fi - len(fields)])
        counter["toc_static"] += 1


# ---------------------------------------------------------------------------
# 段落自有 run 收集（不下钻嵌套段落）
# ---------------------------------------------------------------------------

def _own_runs(p):
    """段落自有 w:r：全部后代 run（覆盖 w:hyperlink / w:sdt / w:smartTag /
    w:ins 等包裹层），但不下钻嵌套 w:p（文本框 txbxContent 内段落单独立段
    处理，避免重复送检），也不下钻已收集 run 的内部（文本框内嵌套段落属于
    内层 w:p）。"""
    from docx.oxml.ns import qn

    runs = []

    def walk(el):
        for child in el:
            tag = child.tag
            if tag == qn("w:p"):
                continue  # 嵌套段落（文本框内等）由 iter() 另案处理
            if tag == qn("w:r"):
                runs.append(child)
                continue  # 不下钻 run：其内嵌 txbxContent 段落不属本段
            walk(child)

    walk(p)
    return runs


def _alternate_content_branch(p):
    """段落是否位于 mc:AlternateContent 的 Choice / Fallback 分支。"""
    el = p.getparent()
    while el is not None:
        if el.tag == _MC_ALTERNATE:
            for child in el:
                if child.tag in (_MC_CHOICE, _MC_FALLBACK) \
                        and any(node is child for node in p.iterancestors()):
                    return ("choice" if child.tag == _MC_CHOICE else "fallback",
                            id(el))
        el = el.getparent()
    return None, None


# ---------------------------------------------------------------------------
# 域结构扫描（fldChar begin/separate/end 栈配对 + fldSimple）
# ---------------------------------------------------------------------------

def _scan_fields(all_elems, eidx):
    """按文档序扫描域结构。返回 (复杂域列表, TOC 型 fldSimple 列表)。

    复杂域：{bi, ei, is_toc, host_ids}——begin/end 元素文档序区间（含嵌套
    域，如 TOC 缓存条目内的 PAGEREF）+ 域首尾宿主段落（begin/end 所在段落
    本体的序位先于其子元素，不在 [bi, ei] 区间内，须单列）。栈配对保证区间
    完整，转换时整域剥除不破坏配对。
    """
    from docx.oxml.ns import qn

    fields = []
    fldsimple_tocs = []
    stack = []
    instr_parts: list = []
    fld_char = qn("w:fldChar")
    fld_type = qn("w:fldCharType")
    instr_text = qn("w:instrText")
    fld_simple = qn("w:fldSimple")
    w_p = qn("w:p")
    for el in all_elems:
        tag = el.tag
        if tag == fld_char:
            t = el.get(fld_type)
            if t == "begin":
                stack.append((eidx[id(el)], _paragraph_hosts(el, w_p)))
                instr_parts.append([])
            elif t == "end" and stack:
                bi, begin_hosts = stack.pop()
                instr = "".join(instr_parts.pop())
                fields.append({
                    "bi": bi, "ei": eidx[id(el)],
                    "is_toc": bool(_TOC_INSTR.search(instr)),
                    "host_ids": begin_hosts | _paragraph_hosts(el, w_p),
                })
        elif tag == instr_text and stack:
            instr_parts[-1].append(el.text or "")
        elif tag == fld_simple:
            instr = el.get(qn("w:instr")) or ""
            if _TOC_INSTR.search(instr):
                fldsimple_tocs.append(el)
    return fields, fldsimple_tocs


def _paragraph_hosts(el, w_p):
    """el 的全部 w:p 祖先（宿主段落；元素代理引用存活期内的 id 稳定）。"""
    return {id(a) for a in el.iterancestors() if a.tag == w_p}


def _in_field(p, p_i, field) -> bool:
    """段落是否位于域范围内：区间内（整段都在域中）或为域首尾宿主段。"""
    return field["bi"] <= p_i <= field["ei"] or id(p) in field["host_ids"]


def _convert_field_to_static(all_elems, field, eidx, qn):
    """命中 TOC 域 → 静态文本：解范围内 hyperlink 包裹 + 剥 fldChar/
    instrText 结构 run，保留全部文本 run（已遮蔽形态原样落纸）。

    元素集取自处理前快照 all_elems（写回阶段新增/移除的 w:t 不在判定面）。"""
    bi, ei = field["bi"], field["ei"]

    def in_range(el):
        i = eidx.get(id(el))
        return i is not None and bi <= i <= ei

    hyperlink = qn("w:hyperlink")
    fld_char = qn("w:fldChar")
    instr_text = qn("w:instrText")
    # 1) 解开范围内 w:hyperlink（TOC 条目超链接壳，保文本与 run）
    for h in [el for el in all_elems if el.tag == hyperlink and in_range(el)]:
        _unwrap(h)
    # 2) 剥范围内域结构元素（含嵌套 PAGEREF 的 begin/instr/separate/end）
    for el in [el for el in all_elems
               if el.tag in (fld_char, instr_text) and in_range(el)]:
        _strip_structural(el)


def _unwrap(el):
    """把 el 子节点上提至父层后移除 el（保序）。"""
    parent = el.getparent()
    if parent is None:
        return
    idx = parent.index(el)
    for child in list(el):
        parent.insert(idx, child)
        idx += 1
    parent.remove(el)


def _strip_structural(el):
    """移除域结构元素；宿主 run 因此只剩 rPr 时连 run 一并移除。"""
    from docx.oxml.ns import qn

    parent = el.getparent()
    if parent is None:
        return
    if parent.tag == qn("w:r"):
        parent.remove(el)
        if not any(ch.tag != qn("w:rPr") for ch in parent):
            gp = parent.getparent()
            if gp is not None:
                gp.remove(parent)
    else:
        parent.remove(el)


# ---------------------------------------------------------------------------
# 位置标注（容器/段落上下文，供台账与映射表定位）
# ---------------------------------------------------------------------------

def _location(p, seq, kind, eidx, tbl_elems, toc_fields, qn) -> str:
    p_i = eidx[id(p)]
    in_textbox = any(a.tag == qn("w:txbxContent") for a in p.iterancestors())
    tbl = next((a for a in p.iterancestors() if a.tag == qn("w:tbl")), None)
    if in_textbox:
        return f"{kind}文本框第{seq + 1}段"
    if any(_in_field(p, p_i, f) for f in toc_fields):
        return f"{kind}目录第{seq + 1}行"
    if tbl is not None:
        ti = tbl_elems.index(tbl) + 1
        tr = next((a for a in p.iterancestors() if a.tag == qn("w:tr")), None)
        tc = next((a for a in p.iterancestors() if a.tag == qn("w:tc")), None)
        ri = (_sibling_index(tr, qn) + 1) if tr is not None else 0
        ci = (_sibling_index(tc, qn) + 1) if tc is not None else 0
        prefix = "" if kind == "正文" else kind
        return f"{prefix}表格{ti} 第{ri}行第{ci}列"
    return f"{kind}第{seq + 1}段"


def _sibling_index(el, qn) -> int:
    name = qn("w:tr") if el.tag == qn("w:tr") else qn("w:tc")
    return sum(1 for sib in el.itersiblings(preceding=True) if sib.tag == name)
