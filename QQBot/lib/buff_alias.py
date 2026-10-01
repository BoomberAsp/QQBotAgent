"""Buff name → icon-label aliasing and extraction.

Skill descriptions name status effects inconsistently relative to the icon
template labels in ``lib/status_icons.STATUS_ICON_CN`` (e.g. text 速度下降 vs
icon 速度降低, 晕眩 vs 眩晕, 无法恢复 vs 禁疗).  This module normalises a
buff name found in skill text onto the canonical Chinese icon label so that the
character→buff mapping can shrink ``BuffDetector``'s template set.

Two public pieces:
* :data:`CANONICAL_LABELS` — the set of icon template keys (all manual + wiki
  status icons; the same keys ``BuffDetector`` uses).
* :func:`extract_skill_buff_labels` — scan a skill's Chinese/English text for
  buff names (located by their turn-duration markers) and return the set of
  canonical labels the skill can apply.

Precision is intentionally sacrificed for recall: a buff that is mentioned but
does not actually render an icon is harmless (it only keeps the template set a
little larger).  Missing a real buff is what breaks validation, so the aliasing
errs on the inclusive side.
"""

from __future__ import annotations

import re
from typing import Any

from lib.status_icons import STATUS_ICON_CN

# ── Canonical labels ─────────────────────────────────────────────────

CANONICAL_LABELS: set[str] = set(STATUS_ICON_CN.values())

# Skill-text phrasing → canonical icon label.  Only non-identity entries are
# listed; identity (text name == icon label) is handled by :func:`normalize`.
_BUFF_ALIAS: dict[str, str] = {
    # 眩晕 (icon) — text writes 晕眩
    "晕眩": "眩晕",
    # 沉睡 (icon) — text writes 睡眠
    "睡眠": "沉睡",
    # 速度降低 (icon) — text writes 速度下降
    "速度下降": "速度降低",
    # 攻击力降低 (icon) — text writes 攻击力下降
    "攻击力下降": "攻击力降低",
    # 命中率降低 (icon) — text writes 命中率下降
    "命中率下降": "命中率降低",
    # 禁疗 (icon) — text writes 无法恢复
    "无法恢复": "禁疗",
    # 妨碍 (icon) — text writes 妨害
    "妨害": "妨碍",
    # 效果命中提升 (icon) — text writes 状态命中提升
    "状态命中提升": "效果命中提升",
    # 效果抵抗提升 (icon) — text writes 状态抗性提升
    "状态抗性提升": "效果抵抗提升",
    # 暴击抵抗 (icon) — text writes 暴击抗性提升 / 暴击抵抗提升
    "暴击抗性提升": "暴击抵抗",
    "暴击抵抗提升": "暴击抵抗",
    # 闪避 (icon) — text writes 闪避提升
    "闪避提升": "闪避",
    # （基础）命中率提升 (icon) — text writes 命中率提升
    "命中率提升": "（基础）命中率提升",
    # 鬼跳南瓜 (icon, H172 S2) — text writes 跳跳南瓜
    "跳跳南瓜": "鬼跳南瓜",
}

# Every name the suffix-probe may resolve, longest first.
_NAME_DICT: list[str] = sorted(set(CANONICAL_LABELS) | set(_BUFF_ALIAS), key=len, reverse=True)


def normalize(name: str) -> str | None:
    """Map a skill-text buff name to its canonical icon label, or None."""
    if name in CANONICAL_LABELS:
        return name
    return _BUFF_ALIAS.get(name)


# ── Extraction ───────────────────────────────────────────────────────

# Turn-duration markers: (2回合) / [2回合] / (2 turns) / [2 turns].
_CN_DUR = re.compile(r"[(\[]\s*\d+\s*[-–~]?\s*\d*\s*回合\s*[)\]]")
_EN_DUR = re.compile(r"[(\[]\s*\d+\s*[-–~]?\s*\d*\s*turns?\s*[)\]]")

# Trailing CJK run before a marker (the buff name sits at its end).
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,12}$")

def _load_en_terms() -> list[tuple[str, str]]:
    """English status term → Chinese pairs from the editable glossary.

    Read through ``tools.wiki_glossary.load_glossary()`` (light json/os deps
    only, mtime-cached) instead of the old AST parse of wiki_scraper's source,
    so panel edits to ``config/translation_glossary.json`` take effect on the
    next battle-text scan without a bot restart.
    """
    try:
        from tools.wiki_glossary import load_glossary
        return [(en, cn) for en, cn in load_glossary()["status_terms"]]
    except Exception:
        return []


def _extract_cn_labels(text: str) -> set[str]:
    """Extract canonical labels from Chinese text via duration markers."""
    labels: set[str] = set()
    for m in _CN_DUR.finditer(text):
        run = _CJK_RUN.search(text[: m.start()])
        if not run:
            continue
        token = run.group(0)
        # Longest trailing substring that resolves to a known label wins.
        for start in range(len(token) - 1):
            label = normalize(token[start:])
            if label is not None:
                labels.add(label)
                break
    return labels


def _extract_en_labels(text: str) -> set[str]:
    """Extract canonical labels from English text via duration markers."""
    terms = _load_en_terms()
    if not terms:
        return set()
    labels: set[str] = set()
    for m in _EN_DUR.finditer(text):
        window = text[max(0, m.start() - 60): m.start()]
        best: tuple[int, str] | None = None
        for en, cn in terms:
            idx = window.rfind(en)
            if idx >= 0 and (best is None or idx > best[0]):
                best = (idx, cn)
        if best is not None:
            label = normalize(best[1])
            if label is not None:
                labels.add(label)
    return labels


def extract_skill_buff_labels(skill: dict[str, Any]) -> set[str]:
    """Return the set of canonical buff labels one skill can apply.

    Scans both the Chinese and English description fields so skills whose
    ``des2``/``burst`` fields are English-only are still covered.
    """
    labels: set[str] = set()
    for field in ("des", "des2", "burst"):
        labels |= _extract_cn_labels(str(skill.get(field) or ""))
    for field in ("des_en", "des2_en", "burst_en"):
        labels |= _extract_en_labels(str(skill.get(field) or ""))
    return labels
