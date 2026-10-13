# -*- coding: utf-8 -*-
"""文本替换工具：纯文本替换与富文本 run 级替换（Word / PPT 通用）。"""
from __future__ import annotations


def apply_replacements(text: str, spans) -> str:
    """按 spans（start 升序、互不重叠）把 replacement 拼回纯文本。"""
    if not spans:
        return text
    out = []
    last = 0
    for sp in sorted(spans, key=lambda s: (s.start, s.end)):
        out.append(text[last:sp.start])
        out.append(sp.replacement)
        last = sp.end
    out.append(text[last:])
    return "".join(out)


def rewrite_runs(runs, spans) -> int:
    """富文本 run 级原位替换：保留全部字体/颜色/加粗等格式。

    策略：替换文本写入“命中起点所在 run”（继承该 run 的格式），
    其余受影响的 run 只删除被遮盖的部分。

    返回发生文本变化的 run 数量。
    """
    spans = sorted(spans, key=lambda s: (s.start, s.end))
    texts = [r.text or "" for r in runs]
    full = "".join(texts)
    if not spans or not full:
        return 0

    # 计算 run 的字符区间边界（基于替换前的全文坐标）
    bounds = [0]
    for t in texts:
        bounds.append(bounds[-1] + len(t))

    changed = 0
    span_idx = 0
    for ri, t in enumerate(texts):
        rs, re_ = bounds[ri], bounds[ri + 1]
        if re_ == rs:
            continue  # 空 run 保持原样
        buf = []
        i = rs
        while i < re_:
            while span_idx < len(spans) and spans[span_idx].end <= i:
                span_idx += 1
            sp = spans[span_idx] if span_idx < len(spans) else None
            if sp is not None and sp.start <= i < sp.end:
                if sp.start >= rs:
                    buf.append(sp.replacement)  # 挂在起点所在 run，保留其格式
                i = min(sp.end, re_)
            else:
                buf.append(full[i])
                i += 1
        new_text = "".join(buf)
        if new_text != t:
            runs[ri].text = new_text
            changed += 1
    return changed
