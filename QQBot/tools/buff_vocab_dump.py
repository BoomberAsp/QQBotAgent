#!/usr/bin/env python3
"""Buff vocabulary dump — extract buff/debuff names from character skill text.

Scans ``data/wiki_cache/character_details.json`` for status effects granted by
skills (located via their turn-duration markers), and aligns each distinct name
against the two existing translation tables:

* ``lib/status_icons.py: STATUS_ICON_CN``  (icon file name → Chinese label)
* ``tools/wiki_scraper.py: _STATUS_TERM_CN`` (English term → Chinese term)

Output reveals the alias gaps — skill-text names that do not map cleanly onto an
icon label — which is the raw material for building the character→buff-icon
mapping used to shrink BuffDetector's template set.

Usage:
    python3 tools/buff_vocab_dump.py [--cache path/to/character_details.json]
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections import Counter, defaultdict

_THIS = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_THIS)
sys.path.insert(0, _PROJECT)

from lib.status_icons import STATUS_ICON_CN  # noqa: E402  (filename -> CN label)

_DEFAULT_CACHE = os.path.join(_PROJECT, "data", "wiki_cache", "character_details.json")
_WIKI_SCRAPER = os.path.join(_THIS, "wiki_scraper.py")


def _extract_module_list(path: str, varname: str) -> list:
    """Extract a module-level list literal without importing (avoids deps)."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == varname:
                    return ast.literal_eval(node.value)
    return []


# ── Duration markers ─────────────────────────────────────────────────
# Parenthesised/bracketed: 速度下降(2回合) / 晕眩[1回合] / SPD Down (2 turns)
_PAREN_DUR = re.compile(r"[(\[]\s*\d+\s*[-–~]?\s*\d*\s*(?:回合|turns?)\s*[)\]]")
# Bare: 持续2回合 / 持续时间为2回合 (no parentheses)
_BARE_DUR = re.compile(r"(?:持续|持续时间为)?\s*\d+\s*[-–~]?\s*\d*\s*回合")

# A window this wide before the marker is enough to contain the buff name.
_WINDOW = 14

# Strip these trailing fragments when a known label fails to match, so the
# "raw tail" shown for UNMATCHED entries reads as the buff name itself.
_TAIL_CLEAN = re.compile(
    r"(?:自身|己方|我方|敌方|敌人|目标|所有敌人|全体敌人|全体我方|我方全体|"
    r"随机|1个|2个|一个|两个|数|多个|数个)?"
    r"(?:造成|附加|施加|赋予|获得|得到|使|令|授予|给予)?$"
)


def _known_labels() -> tuple[dict[str, str], set[str], set[str]]:
    icon_cn = dict(STATUS_ICON_CN)              # filename -> CN
    terms = _extract_module_list(_WIKI_SCRAPER, "_STATUS_TERM_CN")
    term_en_to_cn = {en: cn for en, cn in terms}
    icon_set = set(icon_cn.values())
    term_set = set(term_en_to_cn.values())
    return icon_cn, icon_set, term_set


def _match_label(window: str, labels_by_len: list[str]) -> str | None:
    """Longest known-label substring match in *window* (canonical name)."""
    for lab in labels_by_len:
        if lab in window:
            return lab
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Dump buff vocabulary from skill text.")
    ap.add_argument("--cache", default=_DEFAULT_CACHE)
    args = ap.parse_args()

    raw = open(args.cache, "rb").read()
    i = raw.index(b"{")  # tolerate a leading BOM / stray prefix
    data = json.loads(raw[i:].decode("utf-8"))["data"]

    icon_cn, icon_set, term_set = _known_labels()
    labels_by_len = sorted(icon_set | term_set, key=len, reverse=True)

    # distinct raw tail -> {count, category, example}
    found: dict[str, dict] = {}
    # category -> counter of canonical labels
    cats: dict[str, Counter] = defaultdict(Counter)

    for ch in data.values():
        for sk in ch.get("skills", []):
            for field in ("des", "des2", "burst"):
                text = str(sk.get(field) or "")
                for m in _PAREN_DUR.finditer(text):
                    win = text[max(0, m.start() - _WINDOW): m.start()]
                    lab = _match_label(win, labels_by_len)
                    if lab is None:
                        tail = _TAIL_CLEAN.sub("", win)[-6:]
                        key = f"??{tail}"
                        cat = "UNMATCHED"
                    else:
                        key = lab
                        if lab in icon_set and lab in term_set:
                            cat = "both"
                        elif lab in icon_set:
                            cat = "icon"
                        else:
                            cat = "term(需映射)"
                    e = found.setdefault(
                        key, {"count": 0, "cat": cat, "example": ""})
                    e["count"] += 1
                    if not e["example"]:
                        e["example"] = text
                    cats[cat][lab or tail] += 1

    # Also count bare-duration occurrences to judge whether they matter.
    bare_total = 0
    for ch in data.values():
        for sk in ch.get("skills", []):
            for field in ("des", "des2", "burst"):
                bare_total += len(_BARE_DUR.findall(str(sk.get(field) or "")))

    print(f"characters: {len(data)}")
    print(f"paren-duration buff occurrences: {sum(v['count'] for v in found.values())}")
    print(f"bare-duration occurrences (持续N回合): {bare_total}")
    print(f"distinct names: {len(found)}\n")

    order = ["icon", "both", "term(需映射)", "UNMATCHED"]
    for cat in order:
        items = [(k, v) for k, v in found.items() if v["cat"] == cat]
        if not items:
            continue
        print(f"=== [{cat}] ({len(items)}) ===")
        for k, v in sorted(items, key=lambda kv: -kv[1]["count"]):
            print(f"  {v['count']:4d}  {k!r}   e.g. {v['example'][:60]!r}")

    print("\n=== summary ===")
    for cat in order:
        c = cats.get(cat)
        if c:
            print(f"{cat:14s} {sum(c.values()):4d} occurrences, "
                  f"{len(c):3d} distinct: {sorted(c, key=c.get, reverse=True)[:20]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
