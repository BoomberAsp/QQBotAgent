"""
Name Resolver — Fuzzy character/bond name matching via pinyin index.

Builds a pinyin lookup table from OpenRubi's manually maintained alias
dictionaries. Query converts input to pinyin (no tone, lowercase), then
does O(1) dictionary lookup — no LLM involvement.

Usage:
    resolver = NameResolver()
    name = resolver.resolve_character("狼团长")  # → "夏妮"
    name = resolver.resolve_character("Shani")    # → "夏妮"
    name = resolver.resolve_character("瞎泥")      # → "夏妮" (homophone)
"""

import json
import os
import re

from pypinyin import Style, pinyin


# ── Paths ────────────────────────────────────────────────────────

_OPENRUBI_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "openrubi", "arkrecode"
)
_CHAR_DIC_PATH = os.path.join(_OPENRUBI_DIR, "members", "character_dic.json")
_BOND_DIC_PATH = os.path.join(_OPENRUBI_DIR, "bonds", "bonds_search_dic.json")
_PINYIN_CACHE_PATH = os.path.join(_OPENRUBI_DIR, "members", "pinyin_choices.json")
# QQBotAgent's own translation mapping (supplements OpenRubi)
_NAME_MAPPING_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "wiki_cache", "name_mapping.json"
)

# Cached index on disk (built once, loaded at startup)
_INDEX_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "name_index")
_CHAR_INDEX_PATH = os.path.join(_INDEX_DIR, "char_pinyin.json")
_BOND_INDEX_PATH = os.path.join(_INDEX_DIR, "bond_pinyin.json")


# ── Pinyin Helpers ────────────────────────────────────────────────

def _load_pinyin_choices() -> dict:
    """Load OpenRubi's manually curated multi-sound character cache."""
    if os.path.exists(_PINYIN_CACHE_PATH):
        with open(_PINYIN_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _to_pinyin(text: str, choices: dict) -> str:
    """Convert Chinese text to pinyin (no tone, lowercase, no spaces).

    Multi-sound characters use the cached choice from OpenRubi;
    unregistered characters fall back to pypinyin's default.
    """
    result = []
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            if ch in choices:
                result.append(choices[ch])
            else:
                py = pinyin(ch, style=Style.NORMAL, heteronym=False)
                result.append(py[0][0] if py else ch)
        elif ch.isascii() and ch.isalpha():
            result.append(ch.lower())
        elif ch.isascii() and ch.isdigit():
            result.append(ch)
        # else: skip punctuation/spaces
    return "".join(result)


# ── Index Builder ─────────────────────────────────────────────────

def build_index() -> tuple[dict, dict]:
    """Build pinyin→canonical_name indices for characters and bonds.

    Called once to generate char_pinyin.json and bond_pinyin.json.
    Returns (char_index, bond_index).
    """
    choices = _load_pinyin_choices()
    char_index = {}  # pinyin_key → canonical_name
    bond_index = {}

    # 1. OpenRubi character aliases (authoritative)
    if os.path.exists(_CHAR_DIC_PATH):
        with open(_CHAR_DIC_PATH, "r", encoding="utf-8") as f:
            char_dic = json.load(f)
        for alias, name in char_dic.items():
            py = _to_pinyin(alias, choices)
            if py:
                char_index[py] = name

    # 2. OpenRubi bond aliases
    if os.path.exists(_BOND_DIC_PATH):
        with open(_BOND_DIC_PATH, "r", encoding="utf-8") as f:
            bond_dic = json.load(f)
        for alias, name in bond_dic.items():
            py = _to_pinyin(alias, choices)
            if py:
                bond_index[py] = name

    # 3. QQBotAgent name_mapping (supplementary English→Chinese)
    if os.path.exists(_NAME_MAPPING_PATH):
        with open(_NAME_MAPPING_PATH, "r", encoding="utf-8") as f:
            name_map = json.load(f)
        for en_name, cn_name in name_map.items():
            py = _to_pinyin(en_name, choices)
            if py:
                # Don't overwrite OpenRubi entries
                if py not in char_index and py not in bond_index:
                    char_index[py] = cn_name

    os.makedirs(_INDEX_DIR, exist_ok=True)
    with open(_CHAR_INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(char_index, f, ensure_ascii=False, indent=2)
    with open(_BOND_INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(bond_index, f, ensure_ascii=False, indent=2)

    return char_index, bond_index


# ── Name Resolver ─────────────────────────────────────────────────

class NameResolver:
    """Fuzzy name resolver via pinyin index lookup.

    Loads pre-built pinyin indices at init time. No LLM, no network —
    pure O(1) dictionary lookup.
    """

    def __init__(self):
        self._choices = _load_pinyin_choices()

        # Load pre-built indices, or build on first use
        if os.path.exists(_CHAR_INDEX_PATH) and os.path.exists(_BOND_INDEX_PATH):
            with open(_CHAR_INDEX_PATH, "r", encoding="utf-8") as f:
                self._char_index = json.load(f)
            with open(_BOND_INDEX_PATH, "r", encoding="utf-8") as f:
                self._bond_index = json.load(f)
        else:
            self._char_index, self._bond_index = build_index()

        # Also keep direct alias lookup for exact English-name matches
        self._char_alias = {}
        if os.path.exists(_CHAR_DIC_PATH):
            with open(_CHAR_DIC_PATH, "r", encoding="utf-8") as f:
                for alias, name in json.load(f).items():
                    self._char_alias[alias.strip().lower()] = name

        self._bond_alias = {}
        if os.path.exists(_BOND_DIC_PATH):
            with open(_BOND_DIC_PATH, "r", encoding="utf-8") as f:
                for alias, name in json.load(f).items():
                    self._bond_alias[alias.strip().lower()] = name

    # ── Public API ────────────────────────────────────────────────

    def resolve_character(self, query: str) -> str | None:
        """Resolve a fuzzy character name to its canonical Chinese name.

        Returns None if no match found (caller should use original query).
        """
        if not query or not query.strip():
            return None

        q = query.strip()

        # 1. Direct exact match (case-insensitive)
        low = q.lower()
        if low in self._char_alias:
            return self._char_alias[low]

        # 2. Pinyin lookup
        py = _to_pinyin(q, self._choices)
        if py and py in self._char_index:
            return self._char_index[py]

        return None

    def resolve_bond(self, query: str) -> str | None:
        """Resolve a fuzzy bond name to its canonical Chinese name."""
        if not query or not query.strip():
            return None

        q = query.strip()
        low = q.lower()

        if low in self._bond_alias:
            return self._bond_alias[low]

        py = _to_pinyin(q, self._choices)
        if py and py in self._bond_index:
            return self._bond_index[py]

        return None


# ── Module-level singleton ────────────────────────────────────────

_resolver: NameResolver | None = None


def get_resolver() -> NameResolver:
    """Get or create the module-level NameResolver singleton."""
    global _resolver
    if _resolver is None:
        _resolver = NameResolver()
    return _resolver
