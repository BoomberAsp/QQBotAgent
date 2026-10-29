"""
card.md 解析与切片编辑 — **纯函数，零 IO**。

设计依据：``docs/character-admin-ui-design.md`` §6.2。

四条硬约束（每条都能在实现里找到对应位置）：

1. **不重渲染，只切片**。解析产出每个段落在原文中的字符区间；改一段 = 在区间上拼接。
   没被改的段落在字符串里根本没被碰过，于是「未改动内容字节级不变」由构造保证，
   而不是靠小心翼翼的模板复现。往返不变量：
   ``replace_body(doc, i, section_body(doc, i)) == doc.raw``。
2. **围栏感知**。````` ``` ``` / ``~~~`` 代码块内的 ``#`` 不算标题 —— 用户在卡里贴
   prompt 示例、贴对话示例非常常见，不做状态机会把示例里的 ``## 对话示例`` 切开。
3. **只按 level 1–2 切段**，``###`` 及更深归父段（``## Bond Tiers`` 下的
   ``### Lv3 · 档案Ⅰ`` 必须留在该段里）。
4. **无 H1 / 无元数据注释也能工作**：``set_h1`` 会补 H1，``set_meta`` 会在 H1 后插入注释。

边界语义：
- 行尾统一按 ``\\n`` 计算区间；``\\r\\n`` 只在**写入文本**时归一化，原文区间不动。
- 元数据注释 = H1 之后**第一处** ``<!-- ... -->``（允许跨行），值是 ``k: v`` 用 ``|`` 分隔。
- 段落按**索引**寻址，不按标题名字（允许重名段落）。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

Span = Tuple[int, int]

#: 「不存在」的区间哨兵（无 H1 / 无元数据注释时使用）
ABSENT: Span = (-1, -1)

_HEADING_RE = re.compile(r"^(#{1,6})([ \t]+)(.*)$")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)

#: 结构化编辑器识别的段落标题（缺失只告警，不阻断 —— 用户卡允许自由形态）
KNOWN_SECTIONS = (
    "Core Personality",
    "Worldview",
    "Relationship with User",
    "Communication Style",
    "Appearance Anchor",
    "Gift Preferences",
    "Bond Levels",
    "Bond Tiers",
)


# ── 数据结构 ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class Section:
    """一个 level-2 段落。``body_span`` 不含标题行。"""

    index: int
    level: int
    heading: str
    prefix: str            # 原文的 "# " / "## " / "##\t" —— 重写时原样保留
    heading_span: Span     # 标题行（含行尾换行）
    body_span: Span
    bytes: int             # 正文区间字节数（含段落间空行）

    def as_dict(self) -> dict:
        return {"index": self.index, "level": self.level, "heading": self.heading,
                "bytes": self.bytes}


@dataclass(frozen=True)
class CardDoc:
    """一张卡片的解析结果。所有 ``*_span`` 都是对 ``raw`` 的区间。"""

    raw: str
    h1: str
    h1_span: Span
    h1_prefix: str
    meta: Dict[str, str]      # 保序（dict 3.7+ 保插入顺序）
    meta_raw: str
    meta_span: Span
    preamble_span: Span
    sections: Tuple[Section, ...] = ()

    # ── 便捷属性 ──
    @property
    def preamble(self) -> str:
        span = self.preamble_span
        if span[0] < 0 or span[1] <= span[0]:
            return ""
        return self.raw[span[0]:span[1]].strip("\n")

    def section(self, index: int) -> Section:
        return self.sections[index]

    def find(self, heading: str) -> Optional[Section]:
        """按标题名找段落（大小写/首尾空白不敏感，取第一个）。"""
        target = (heading or "").strip().lower()
        for sec in self.sections:
            if sec.heading.strip().lower() == target:
                return sec
        return None


# ── 词法扫描 ──────────────────────────────────────────────────────

def _iter_lines(raw: str) -> Iterable[Tuple[int, int, str]]:
    """Yield ``(start, end_with_newline, text_without_newline)``."""
    pos, n = 0, len(raw)
    while pos < n:
        nl = raw.find("\n", pos)
        if nl < 0:
            yield pos, n, raw[pos:].rstrip("\r")
            return
        yield pos, nl + 1, raw[pos:nl].rstrip("\r")
        pos = nl + 1


def _fence_flags(lines: Sequence[Tuple[int, int, str]]) -> List[bool]:
    """每个行对象是否属于围栏代码块（含开/闭定界行本身）。"""
    flags: List[bool] = []
    fence: Optional[Tuple[str, int]] = None      # (定界符字符, 长度)
    for _start, _end, text in lines:
        if fence is None:
            m = _FENCE_RE.match(text)
            if m:
                seq = m.group(1)
                fence = (seq[0], len(seq))
                flags.append(True)
            else:
                flags.append(False)
        else:
            flags.append(True)
            m = _FENCE_RE.match(text)
            if m:
                seq = m.group(1)
                if (seq[0] == fence[0] and len(seq) >= fence[1]
                        and not text.strip()[len(seq):].strip()):
                    fence = None
    return flags


def _mask_fenced(raw: str, lines, flags) -> str:
    """把围栏块内容替换成空格（保留换行与偏移），用于安全地正则搜索注释。"""
    if not any(flags):
        return raw
    buf = list(raw)
    for (start, end, _text), is_fenced in zip(lines, flags):
        if not is_fenced:
            continue
        for i in range(start, end):
            if buf[i] != "\n":
                buf[i] = " "
    return "".join(buf)


# ── 元数据注释 ────────────────────────────────────────────────────

def parse_meta_comment(inner: str) -> Dict[str, str]:
    """把注释内文 ``mode: split | task_voice: character`` 解析成有序 dict。"""
    out: Dict[str, str] = {}
    for token in (inner or "").split("|"):
        token = token.strip()
        if not token:
            continue
        if ":" in token:
            key, _, value = token.partition(":")
        elif "=" in token:
            key, _, value = token.partition("=")
        else:
            continue
        key = key.strip().lower()
        if key:
            out[key] = value.strip()
    return out


def render_meta_comment(meta: Dict[str, str]) -> str:
    """有序 dict → ``<!-- k: v | k2: v2 -->``（空 dict 得空串）。"""
    items = [(k, v) for k, v in (meta or {}).items() if v is not None and str(v).strip()]
    if not items:
        return ""
    return "<!-- " + " | ".join(f"{k}: {str(v).strip()}" for k, v in items) + " -->"


# ── 解析 ──────────────────────────────────────────────────────────

@dataclass
class _Heading:
    level: int
    prefix: str
    title: str
    span: Span


def parse_card(raw: str) -> CardDoc:
    """解析 card.md 全文。永不抛异常（畸形卡也能得到可用的结果）。"""
    raw = raw or ""
    lines = list(_iter_lines(raw))
    flags = _fence_flags(lines)

    headings: List[_Heading] = []
    for (start, end, text), is_fenced in zip(lines, flags):
        if is_fenced:
            continue
        m = _HEADING_RE.match(text)
        if not m:
            continue
        headings.append(_Heading(
            level=len(m.group(1)),
            prefix=m.group(1) + m.group(2),
            title=m.group(3).strip(),
            span=(start, end),
        ))

    h1 = next((h for h in headings if h.level == 1), None)

    # 段落：所有 level-2 标题；正文止于**下一个 level<=2 标题**（含第二个 H1）
    boundaries = [h for h in headings if h.level <= 2]
    sections: List[Section] = []
    for order, head in enumerate(h for h in boundaries if h.level == 2):
        nxt = next((b for b in boundaries if b.span[0] > head.span[0]), None)
        body_start = head.span[1]
        body_end = nxt.span[0] if nxt is not None else len(raw)
        sections.append(Section(
            index=order, level=2, heading=head.title, prefix=head.prefix,
            heading_span=head.span, body_span=(body_start, body_end),
            bytes=max(0, body_end - body_start),
        ))

    # 元数据注释（围栏内的不算）
    search_from = h1.span[1] if h1 is not None else 0
    masked = _mask_fenced(raw, lines, flags)
    meta_span: Span = ABSENT
    meta: Dict[str, str] = {}
    meta_raw = ""
    m = _COMMENT_RE.search(masked, search_from)
    if m:
        meta_span = (m.start(), m.end())
        meta_raw = raw[m.start():m.end()]
        meta = parse_meta_comment(m.group(1))

    # 前言：H1/注释之后 → 首个 ## 之前
    pre_start = meta_span[1] if meta_span != ABSENT else (
        h1.span[1] if h1 is not None else 0)
    pre_end = sections[0].heading_span[0] if sections else len(raw)
    if pre_end < pre_start:
        pre_end = pre_start
    preamble_span: Span = (pre_start, pre_end)

    return CardDoc(
        raw=raw,
        h1=h1.title if h1 is not None else "",
        h1_span=h1.span if h1 is not None else ABSENT,
        h1_prefix=h1.prefix if h1 is not None else "# ",
        meta=meta,
        meta_raw=meta_raw,
        meta_span=meta_span,
        preamble_span=preamble_span,
        sections=tuple(sections),
    )


# ── 切片编辑 ──────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n")


def _body_text(text: str) -> str:
    """编辑框文本 → 落盘文本（CRLF 归一、去首尾空行）。"""
    return _normalize(text).strip("\n")


def _region_text(region: str) -> str:
    """原文区间 → 可见正文（去首尾所有换行，含 CRLF）。"""
    return _normalize(region).strip("\n")


def _render_region(region: str, text: str, at_eof: bool) -> str:
    """在原区间 ``region`` 的位置渲染新正文。

    - **原区间有可见文本**：沿用它的前导/尾部换行（保持作者的排版习惯）
    - **原区间只有空行**：用规范格式（1 个空行 + 正文 + 段落间 1 个空行，EOF 前不留空行）
    """
    body = _body_text(text)
    if region.strip("\r\n"):
        lead = region[:len(region) - len(region.lstrip("\r\n"))]
        trail = region[len(region.rstrip("\r\n")):]
        return _normalize(lead) + body + _normalize(trail)
    return "\n" + body + ("\n" if at_eof else "\n\n")


def _replace_region(raw: str, span: Span, text: str) -> str:
    """把 ``span`` 区间替换成 ``text``；内容没变则**一个字节都不动**。

    一旦真的改动，整篇的行尾统一为 LF（设计 §6.6 L12：CRLF 在保存时归一），
    避免出现「一半 CRLF 一半 LF」的混合行尾。
    """
    if span[0] < 0 or span[1] < span[0]:
        return raw
    old = raw[span[0]:span[1]]
    if _region_text(old) == _body_text(text):
        return raw
    new = _render_region(old, text, at_eof=(span[1] >= len(raw)))
    return _normalize(raw[:span[0]] + new + raw[span[1]:])


def section_body(doc: CardDoc, index: int) -> str:
    """段落正文（已去掉首尾换行，用于编辑器）。"""
    span = doc.sections[index].body_span
    return _region_text(doc.raw[span[0]:span[1]])


def replace_body(doc: CardDoc, index: int, body: str) -> str:
    """替换第 ``index`` 段的正文，其余字节不变。"""
    return _replace_region(doc.raw, doc.sections[index].body_span, body)


def replace_preamble(doc: CardDoc, text: str) -> str:
    """替换 H1/注释与首个段落之间的前言（扮演指令段）。"""
    return _replace_region(doc.raw, doc.preamble_span, text)


def apply_section_edits(doc: CardDoc, edits: Dict[int, str]) -> str:
    """一次应用多段修改，返回新全文。

    - 先把待改区间与目标文本比较，**全都没变就直接返回原文**（保住字节）
    - 有改动时：先归一为 LF 再重解析（CRLF→LF 会移动偏移，不能沿用旧 span），
      然后**按索引降序**应用 —— 先改靠后的段，前面段的区间始终有效。
    """
    pairs = {int(i): b for i, b in (edits or {}).items()
             if isinstance(i, int) and 0 <= i < len(doc.sections)}
    if not pairs:
        return doc.raw
    changed = any(section_body(doc, i) != _body_text(b) for i, b in pairs.items())
    if not changed:
        return doc.raw

    raw = _normalize(doc.raw)
    work = doc if raw == doc.raw else parse_card(raw)
    for index in sorted(pairs, reverse=True):
        if index >= len(work.sections):
            continue
        span = work.sections[index].body_span
        old = raw[span[0]:span[1]]
        new = _render_region(old, pairs[index], at_eof=(span[1] >= len(raw)))
        raw = raw[:span[0]] + new + raw[span[1]:]
    return raw


def set_h1(doc: CardDoc, title: str) -> str:
    """重写 H1 标题文本（保留原前缀与行尾）。缺失时在最前面补一个 H1。"""
    title = _normalize(title).strip()
    if not title:
        raise ValueError("标题不能为空")
    if doc.h1_span == ABSENT:
        return f"# {title}\n\n" + doc.raw.lstrip("\n")
    line = doc.raw[doc.h1_span[0]:doc.h1_span[1]]
    ending = line[len(line.rstrip("\r\n")):]
    new_line = f"{doc.h1_prefix}{title}{ending}"
    if new_line == line:
        return doc.raw
    return doc.raw[:doc.h1_span[0]] + new_line + doc.raw[doc.h1_span[1]:]


def set_meta(doc: CardDoc, **values) -> str:
    """就地重写元数据注释。

    - 值为 ``None`` / 空串 = **删除该键**（``voice_id`` 删掉即「恢复自动分配」）
    - 未知键与顺序原样保留
    - 无注释时插到 H1 之后；全部删空时把整行注释去掉
    """
    items: Dict[str, str] = dict(doc.meta)
    for key, value in values.items():
        key = (key or "").strip().lower()
        if not key:
            continue
        if value is None or str(value).strip() == "":
            items.pop(key, None)
        else:
            items[key] = str(value).strip()

    if items == doc.meta and doc.meta_span != ABSENT:
        return doc.raw                     # 值没变 → 一个字节都不动

    comment = render_meta_comment(items)

    if not comment:                        # 删空：整行移除（并把相邻空行收敛成一个）
        if doc.meta_span == ABSENT:
            return doc.raw
        start, end = doc.meta_span
        line_start = doc.raw.rfind("\n", 0, start) + 1
        line_end = doc.raw.find("\n", end)
        line_end = len(doc.raw) if line_end < 0 else line_end + 1
        before, after = doc.raw[:line_start], doc.raw[line_end:]
        b = len(before) - len(before.rstrip("\n"))      # 前文已有的行尾换行
        a = len(after) - len(after.lstrip("\n"))        # 后文已有的前导换行
        keep_a = min(a, max(0, 2 - b))                  # 合并后只保留一个空行
        return _normalize(before + after[a - keep_a:])

    if doc.meta_span != ABSENT:
        return doc.raw[:doc.meta_span[0]] + comment + doc.raw[doc.meta_span[1]:]

    pos = doc.h1_span[1] if doc.h1_span != ABSENT else 0
    return doc.raw[:pos] + comment + "\n" + doc.raw[pos:]


def insert_section(doc: CardDoc, heading: str, body: str = "",
                   after: Optional[int] = None) -> str:
    """在 ``after`` 段之后插入一个新段落（``after=None`` = 末尾追加）。"""
    heading = _normalize(heading).strip().lstrip("#").strip()
    if not heading:
        raise ValueError("段落标题不能为空")
    body = _normalize(body).strip()
    block = f"## {heading}\n\n{body}\n" if body else f"## {heading}\n"

    if after is not None and 0 <= after + 1 < len(doc.sections):
        pos = doc.sections[after + 1].heading_span[0]
        return doc.raw[:pos] + block + "\n" + doc.raw[pos:]

    if not doc.raw:
        return block
    raw = doc.raw
    if raw.endswith("\n\n"):
        prefix = ""
    elif raw.endswith("\n"):
        prefix = "\n"
    else:
        prefix = "\n\n"
    return raw + prefix + block


def remove_section(doc: CardDoc, index: int) -> str:
    """删除整段（标题行 + 正文）。"""
    sec = doc.sections[index]
    return doc.raw[:sec.heading_span[0]] + doc.raw[sec.body_span[1]:]


# ── 差异 ──────────────────────────────────────────────────────────

def unified_diff(old: str, new: str,
                 fromfile: str = "当前", tofile: str = "新内容",
                 context: int = 3) -> List[str]:
    """保存前预览用的统一 diff（按行，已去掉行尾换行）。"""
    return list(difflib.unified_diff(
        (old or "").splitlines(), (new or "").splitlines(),
        fromfile=fromfile, tofile=tofile, lineterm="", n=context,
    ))
