"""
Bond Content — 角色卡养成内容解析（Bond-System-Design.md §4.4/§5.3/§6，P2）。

从角色卡 markdown 中解析三类养成素材，全部是纯文本解析、无副作用、无
nonebot 依赖，可独立单测：

- **Gift Preferences**（礼物偏好表）：喜爱 / 普通 / 讨厌三档清单，送礼时
  按礼物名做子串匹配决定档位与 EXP。
- **Bond Tiers**（分段解锁档案）：``### Lv{N} · {标题}`` 子段，等级到达后
  注入角色面动态层，让角色「自然地透露」背景故事（方舟式档案解锁）。
- **称呼演化**：按好感等级给出一句 prompt 指令（纯 prompt diff，无状态）。

约定：本模块只读角色卡文本，不关心它是内置人格还是 UGC；调用方（agent_router）
负责取卡与注入。
"""

import re
from typing import Dict, List, Optional

# 礼物偏好三档 → EXP 档位名（与 bond.py 配置键对应）
GIFT_TIERS = ("loved", "normal", "disliked")

# 中/英文档位标签 → 内部 tier 名
_GIFT_LABEL_MAP = {
    "喜爱": "loved", "喜欢": "loved", "loved": "loved", "like": "loved", "favorite": "loved",
    "普通": "normal", "一般": "normal", "normal": "normal", "neutral": "normal",
    "讨厌": "disliked", "不喜歡": "disliked", "不喜欢": "disliked",
    "disliked": "disliked", "hate": "disliked",
}

_SPLIT_RE = re.compile(r"[、,，;；/]+")
_TIER_HEADER_RE = re.compile(r"^Lv\s*(\d+)\b", re.IGNORECASE)


def _section(card: str, heading: str) -> str:
    """Return the body of a ``## {heading}`` section (up to the next ``## ``)."""
    if not card:
        return ""
    lines = card.splitlines()
    out: List[str] = []
    capturing = False
    target = heading.strip().lower()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            title = stripped[3:].strip().lower()
            if capturing:
                # next top-level section starts → stop
                break
            if title == target or title.startswith(target):
                capturing = True
                continue
        elif capturing:
            out.append(line)
    return "\n".join(out).strip()


# ── Gift Preferences ──────────────────────────────────────────────

def parse_gift_preferences(card: str) -> Dict[str, List[str]]:
    """Parse ``## Gift Preferences`` into {loved, normal, disliked} lists."""
    prefs: Dict[str, List[str]] = {"loved": [], "normal": [], "disliked": []}
    body = _section(card, "Gift Preferences")
    if not body:
        return prefs
    for raw in body.splitlines():
        line = raw.strip().lstrip("-*· ").strip()
        if not line:
            continue
        # split label from values on first : or ：
        m = re.split(r"[:：]", line, maxsplit=1)
        if len(m) != 2:
            continue
        label = m[0].strip().lower()
        tier = _GIFT_LABEL_MAP.get(label)
        if tier is None:
            # allow labels like "喜爱礼物"
            for key, t in _GIFT_LABEL_MAP.items():
                if key in label:
                    tier = t
                    break
        if tier is None:
            continue
        values = m[1].strip()
        if not values or "未设定" in values or "未設定" in values:
            continue
        for item in _SPLIT_RE.split(values):
            item = item.strip().strip("。.、")
            if item and item not in prefs[tier]:
                prefs[tier].append(item)
    return prefs


def match_gift(gift_name: str, prefs: Dict[str, List[str]]) -> str:
    """Match a gift against the preference table.

    Returns "loved" / "normal" / "disliked". Substring match either direction,
    loved checked first (most rewarding), then disliked, then normal.
    Unmatched gifts default to "normal" (benefit of the doubt).
    """
    name = (gift_name or "").strip().lower()
    if not name:
        return "normal"
    for tier in ("loved", "disliked", "normal"):
        for item in prefs.get(tier, []):
            it = item.strip().lower()
            if not it:
                continue
            if it in name or name in it:
                return tier
    return "normal"


# ── Bond Tiers（分段解锁档案）──────────────────────────────────────

def parse_bond_tiers(card: str) -> List[Dict[str, object]]:
    """Parse ``## Bond Tiers`` into [{level:int, title:str, body:str}], by level.

    Each subsection header looks like ``### Lv3 · 档案Ⅰ`` (separators ·/：/:/-
    all accepted). Body is the text under that subsection.
    """
    body = _section(card, "Bond Tiers")
    if not body:
        return []
    tiers: List[Dict[str, object]] = []
    cur: Optional[Dict[str, object]] = None
    buf: List[str] = []
    for raw in body.splitlines():
        line = raw.rstrip()
        st = line.strip()
        if st.startswith("### "):
            if cur is not None:
                cur["body"] = "\n".join(buf).strip()
                tiers.append(cur)
            header = st[4:].strip()
            m = _TIER_HEADER_RE.match(header)
            if not m:
                cur = None
                buf = []
                continue
            level = int(m.group(1))
            rest = header[m.end():].strip()
            title = re.sub(r"^[·:：\-\s]+", "", rest).strip() or f"Lv{level}"
            cur = {"level": level, "title": title, "body": ""}
            buf = []
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        cur["body"] = "\n".join(buf).strip()
        tiers.append(cur)
    tiers.sort(key=lambda t: t["level"])  # type: ignore[arg-type]
    return tiers


def unlocked_tiers(card: str, level: int) -> List[Dict[str, object]]:
    """Tiers whose unlock level ≤ current level."""
    return [t for t in parse_bond_tiers(card) if int(t["level"]) <= level]


def tiers_unlocking_at(card: str, level: int) -> List[Dict[str, object]]:
    """Tiers that unlock exactly at ``level`` (for unlock events/notices)."""
    return [t for t in parse_bond_tiers(card) if int(t["level"]) == level]


def unlocked_tiers_text(card: str, level: int) -> str:
    """Dynamic-layer text of all unlocked Bond Tiers (empty if none)."""
    parts = []
    for t in unlocked_tiers(card, level):
        body = str(t["body"]).strip()
        title = str(t["title"]).strip()
        if body:
            parts.append(f"【{title}】\n{body}")
        else:
            parts.append(f"【{title}】")
    return "\n\n".join(parts)


# ── 称呼演化（纯 prompt diff，无状态）───────────────────────────────

def address_directive(level: int) -> str:
    """Level-appropriate guidance on how the character addresses the user.

    好感越高，称呼越亲昵——这只是给 LLM 的一句语气指令，具体称呼仍由
    角色卡与对话历史决定。
    """
    if level >= 7:
        return (
            "称呼演化: 你们已是最亲密的人，用最亲昵的称呼与语气对待用户，"
            "可以自然流露依恋、关心与占有欲。"
        )
    if level >= 5:
        return (
            "称呼演化: 关系已很深厚，可以用亲密、随意的称呼，语气放松而温暖。"
        )
    if level >= 3:
        return (
            "称呼演化: 你们已经熟悉，可以自然地用昵称或名字称呼用户，语气亲近。"
        )
    return (
        "称呼演化: 你们刚认识不久，礼貌而略带试探地称呼用户"
        "（用其昵称/名字或通用称呼即可），保持分寸感。"
    )
