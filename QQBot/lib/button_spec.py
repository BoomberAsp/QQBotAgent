"""
Platform-agnostic button DSL — foundation of the QQ official keyboard
(buttons-design §6.2/§6.3, stage P0+P1).

Pure data + pure functions, zero project-internal dependencies: the
kernel (turn.py helpers), the renderers (agent/session_render.py) and
the adapters (adapters/qq_official*.py) all import this module, so it
must stay a leaf.

A keyboard is ``Rows = list[list[Btn]]`` (≤5 rows × ≤3 per row by
default, hot-read from env — the platform hard-limits 5 rows × 5
buttons and 15 total, but our layouts stay narrow for phone screens).
``validate()`` is the single gate every send path calls before a
keyboard reaches the wire; ``to_text()`` renders the L2 degradation
(plain-text command list) used when markdown/keyboard are rejected.

Action kinds mirror the official API (docs/qqbot-api-v2-full-docs.md §50):
  - "cmd" → type=2 指令按钮: the client types/sends ``data`` for the
    user (@bot-prefixed in groups). No callback, no 3s ack — anything
    needing the LLM or >3s must be this kind (design §2.4 推论 C).
  - "cb"  → type=1 回调按钮: platform pushes INTERACTION_CREATE; the
    bot must PUT an ack within 3s. Only for millisecond-scale local
    state changes. ``data`` is our own ascii payload (encode_cb in
    adapters/qq_official_buttons.py) matching _CB_PAYLOAD_RE.
  - "url" → type=0 跳转按钮: opens an https link.
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass

# 按钮不可用时客户端展示的提示（unsupport_tips）。
DEFAULT_TIP = "当前QQ版本不支持该按钮，请发送 /帮助"

# 回调载荷格式（严格 ascii，防注入/防超长）：
#   qb1:<verb>[.<sub>][:arg[:arg[:arg]]]   每段 arg ≤24 字符
# 中文允许出现在指令按钮 data（如 "/剧情 初见"），但回调载荷必须过这个
# 正则——decode_cb 同样用它校验，畸形载荷直接拒绝。
_CB_PAYLOAD_RE = re.compile(
    r"^qb1:([a-z_]+(?:\.[a-z_]+)?)(?::([A-Za-z0-9_.#-]{1,24})){0,3}$")
# Public alias — adapters/qq_official_buttons.py encode/decode use the
# exact same pattern, so there is one definition of "valid payload".
CB_PAYLOAD_RE = _CB_PAYLOAD_RE

# 平台硬上限（docs §50）；我们的默认排版上限更窄，env 可调。
_PLATFORM_MAX_ROWS = 5
_PLATFORM_MAX_PER_ROW = 5
_PLATFORM_MAX_TOTAL = 15
_CMD_DATA_LIMIT = 60      # 指令按钮 data 保守上限（含 @bot 前缀余量）
_CB_DATA_LIMIT = 64       # 回调 data 上限（平台 64 字符）
_URL_LIMIT = 200
_LABEL_WIDTH = 10         # 显示宽度（1 汉字 = 2）


@dataclass
class Btn:
    """One button, described platform-agnostically (design §6.2).

    Field semantics follow the official Action/RenderData/Modal models;
    adapters/qq_official_buttons.py::to_message_keyboard does the only
    translation to wire format.
    """
    label: str                       # 按钮文字（显示宽度 ≤10）
    data: str = ""                   # cmd: 替用户发送的文本；cb: qb1 载荷；url: https 链接
    action: str = "cmd"              # "cmd" | "cb" | "url"
    style: int = 1                   # 平台 style 枚举：0=灰色线框 1=蓝色线框（其余值按 1 处理）
    enter: bool = True               # cmd only: 单聊点击后自动发送 data（群聊平台自动忽略，只插入输入框）
    reply: bool = False              # cmd only: data 是否带引用回复本消息（API action.reply，版本 8983+）
    anchor: bool = False             # cmd only: True → anchor=1 唤起手Q选图器并忽略 enter（仅手机端 8983+ 单聊）
    modal: str = ""                  # cb only: 非空 → 点击先弹确认框（内容为该文本）
    confirm: str = "确认"            # modal 确认按钮文字
    cancel: str = "取消"             # modal 取消按钮文字
    group: str = ""                  # cb only: 互斥组 id（同组内点击后置灰）
    admin_only: bool = False         # cb only: permission type=1（仅管理员可点）
    visited: str = ""                # 点击后的 label（缺省同 label）
    tip: str = DEFAULT_TIP           # unsupport_tips
    id: str = ""                     # 按钮 id（缺省 b.{row}.{col}）


Rows = list[list[Btn]]


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def max_rows() -> int:
    """排版行数上限（env 热读，绝不超过平台硬上限 5）。"""
    return min(_env_int("QQ_OFFICIAL_BUTTONS_MAX_ROWS", 5), _PLATFORM_MAX_ROWS)


def max_per_row() -> int:
    """每行按钮数上限（env 热读，绝不超过平台硬上限 5）。"""
    return min(_env_int("QQ_OFFICIAL_BUTTONS_MAX_PER_ROW", 3),
               _PLATFORM_MAX_PER_ROW)


def display_width(text: str) -> int:
    """按钮标签显示宽度：全角/宽字符（汉字等）= 2，其余 = 1。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in text)


def fit_label(text: str, width: int | None = None) -> str:
    """Truncate a label to the display-width cap, adding … when cut.

    Command buttons for named entities (人格/剧情 titles) carry display
    names longer than the 10-unit platform cap — every render site funnels
    labels through here so validate() never rejects on width.
    """
    limit = width if width is not None else _LABEL_WIDTH
    text = (text or "").strip()
    if display_width(text) <= limit:
        return text
    out = ""
    used = 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + cw > limit - 1:   # leave 1 unit for the ellipsis
            break
        out += ch
        used += cw
    return out + "…"


def cb_payload_ok(data: str) -> bool:
    """回调载荷是否符合 qb1 严格格式。"""
    return bool(_CB_PAYLOAD_RE.match(data or ""))


def validate(rows: Rows, *, max_rows_: int | None = None,
             max_per_row_: int | None = None) -> list[str]:
    """Return a list of human-readable problems ([] = sendable).

    Every send path (kernel _safe_send_kb, adapter send_keyboard,
    callback回执) calls this BEFORE the wire; a non-empty result means
    "degrade to markdown-only + WARNING", never "crash the turn".
    Limits default to the env hot-read so a config change needs no
    code deploy; callers may pin them (tests).
    """
    problems: list[str] = []
    if not rows:
        return ["keyboard is empty"]
    row_cap = max_rows_ if max_rows_ is not None else max_rows()
    col_cap = max_per_row_ if max_per_row_ is not None else max_per_row()
    if len(rows) > row_cap:
        problems.append(f"too many rows: {len(rows)} > {row_cap}")
    total = 0
    for ri, row in enumerate(rows):
        if not row:
            problems.append(f"row {ri} is empty")
            continue
        if len(row) > col_cap:
            problems.append(f"row {ri} has {len(row)} buttons > {col_cap}")
        total += len(row)
        for bi, btn in enumerate(row):
            where = f"row {ri} btn {bi}"
            if not (btn.label or "").strip():
                problems.append(f"{where}: empty label")
            elif display_width(btn.label) > _LABEL_WIDTH:
                problems.append(
                    f"{where}: label too wide "
                    f"({display_width(btn.label)} > {_LABEL_WIDTH}): "
                    f"{btn.label!r}")
            if btn.action == "cmd":
                if not (btn.data or "").strip():
                    problems.append(f"{where}: cmd button without data")
                elif len(btn.data) > _CMD_DATA_LIMIT:
                    problems.append(
                        f"{where}: cmd data too long "
                        f"({len(btn.data)} > {_CMD_DATA_LIMIT})")
                if btn.modal:
                    problems.append(f"{where}: modal is cb-only")
            elif btn.action == "cb":
                if not cb_payload_ok(btn.data):
                    problems.append(
                        f"{where}: cb payload malformed: {btn.data[:40]!r}")
                elif len(btn.data) > _CB_DATA_LIMIT:
                    problems.append(f"{where}: cb data too long")
            elif btn.action == "url":
                if not (btn.data or "").startswith("https://"):
                    problems.append(f"{where}: url button needs https://")
                elif len(btn.data) > _URL_LIMIT:
                    problems.append(f"{where}: url too long")
                if btn.modal:
                    problems.append(f"{where}: modal is cb-only")
            else:
                problems.append(f"{where}: unknown action {btn.action!r}")
    if total > _PLATFORM_MAX_TOTAL:
        problems.append(f"too many buttons total: {total} > "
                        f"{_PLATFORM_MAX_TOTAL}")
    return problems


def clip_rows(rows: Rows) -> Rows:
    """Hard-clip rows to the platform envelope (≤5 rows × ≤5, total ≤15).

    Used for the 40034029/304062 clip-and-retry degradation step
    (design §8.2): the platform said "layout too big", so trim the tail
    rather than losing the whole keyboard.
    """
    out: Rows = []
    total = 0
    for row in rows[:_PLATFORM_MAX_ROWS]:
        kept: list[Btn] = []
        for btn in row[:_PLATFORM_MAX_PER_ROW]:
            if total >= _PLATFORM_MAX_TOTAL:
                break
            kept.append(btn)
            total += 1
        if kept:
            out.append(kept)
    return out


def to_text(rows: Rows) -> str:
    """L2 degradation: render the keyboard as a plain-text command list.

    Appended to the markdown body when md+keyboard AND md-only were both
    rejected, and used by OneBot's send_keyboard fallback. cb buttons
    have no text-command equivalent (they are server-side state ops) and
    are skipped; url buttons render as bare links.
    """
    lines: list[str] = []
    n = 0
    for row in rows or []:
        for btn in row:
            if btn.action == "cmd" and (btn.data or "").strip():
                n += 1
                lines.append(f"{n}. {btn.label} → 发送「{btn.data.strip()}」")
            elif btn.action == "url" and (btn.data or "").strip():
                n += 1
                lines.append(f"{n}. {btn.label} → {btn.data.strip()}")
    if not lines:
        return ""
    return "按钮暂不可用，可直接发送命令：\n" + "\n".join(lines)


_MD_LINE_ESCAPE_RE = re.compile(r"^(\s*)([#>*\-+])(\s)")


def sanitize_markdown_text(text: str) -> str:
    """Escape line-leading markdown markers in plain prose.

    For the rare case where untrusted/plain content must share a
    msg_type=2 payload with a keyboard (a keyboard can only ride on
    markdown) — without this, a leading "#" would render as a heading.
    """
    return _MD_LINE_ESCAPE_RE.sub(r"\1\\\2\3", text or "")


def stamp(names) -> str:
    """4-hex version stamp of an ordered name list (design §6.6).

    Callback payloads embed the stamp of the list they were rendered
    from; a mismatch means the list changed under the user (someone
    deleted item 3 already) → the handler refuses and re-renders.
    """
    joined = "|".join(str(x) for x in (names or []))
    return hashlib.blake2s(joined.encode("utf-8"), digest_size=8
                           ).hexdigest()[:4]
