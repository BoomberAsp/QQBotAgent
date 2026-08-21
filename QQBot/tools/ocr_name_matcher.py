"""
OCR Name Matcher — corrects visual OCR misreads of character names.

OCR errors are *visual* (咲→咩, 乌→鸟, 兔→鬼), so pinyin-based matching
(``name_resolver``) cannot catch them.  This module renders each character
with the bundled CJK font and compares glyphs via Stroke-IoU.

Pipeline position::

    OCR text → [this module: glyph correction] → corrected text
             → [name_resolver: alias resolution] → canonical name

Matching strategies by query length (see test/frame_detection_report.md):
  - exact / alias hit  → use directly
  - 1 char             → IoU vs single-char names (≥0.45, margin ≥0.10)
  - 2 chars            → split-char matching vs two-char names
                         (score gap ≥0.05) OR cross-length recall (≥0.60,
                         margin ≥0.05) for e.g. 鸟德 → 乌尔德
  - ≥3 chars           → full-name F1 (≥0.85 margin ≥0.15 auto-correct;
                         0.60-0.85 margin ≥0.05 correct with warning)
  - uncertain          → multimodal LLM fallback with the FULL candidate
                         list (~200-300 tokens), or top-3 candidates

The name index is built dynamically from the wiki cache
(``character_details.json``), falling back to the vendored
``character_dic.json`` — new characters are picked up after
``refresh_characters()`` without code changes.
"""

from __future__ import annotations

import json
import os
from difflib import SequenceMatcher

from lib.ocr_engine import render_glyph_mask, stroke_iou

_CHAR_DETAIL_CACHE = os.path.join(
    os.path.dirname(__file__), "..", "data", "wiki_cache",
    "character_details.json",
)
_CHAR_DIC_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "characters",
    "character_dic.json",
)

# ── Thresholds (from frame_detection_report.md decision table) ──────
_ONE_CHAR_MIN, _ONE_CHAR_MARGIN = 0.45, 0.10
_TWO_CHAR_SPLIT_MARGIN = 0.05
_TWO_CHAR_F1_MIN = 0.60
# cross-length recall needs a tighter margin than the old F1 did — the
# recall scale separates candidates more sharply (鸟德: 乌尔德 .92 vs
# 奥德雅 .86)
_TWO_CHAR_CROSS_MARGIN = 0.05
_LONG_AUTO_MIN, _LONG_AUTO_MARGIN = 0.85, 0.15
_LONG_WARN_MIN, _LONG_WARN_MARGIN = 0.60, 0.05
_LOW_OCR_CONF = 0.30
# vision fallback: glyph plausibility floor for the model's proposal
_VISION_GLYPH_MIN = 0.50

# ── Glyph cache ──────────────────────────────────────────────────────

_glyph_cache: dict = {}


def _glyph(ch: str):
    if ch not in _glyph_cache:
        _glyph_cache[ch] = render_glyph_mask(ch)
    return _glyph_cache[ch]


def _char_sim(a: str, b: str) -> float:
    return 1.0 if a == b else stroke_iou(_glyph(a), _glyph(b))


def _align_score(query: str, target: str) -> float:
    """SequenceMatcher alignment numerator: equal chars +1, replacements
    their glyph Stroke-IoU, indels 0."""
    sm = SequenceMatcher(None, query, target)
    score = 0.0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            score += i2 - i1
        elif tag == "replace":
            for i, j in zip(range(i1, i2), range(j1, j2)):
                score += _char_sim(query[i], target[j])
    return score


def _cross_score(query: str, target: str) -> float:
    """Cross-length similarity: recall of the query chars, gated on a
    precision floor so a short query cannot latch onto an unrelated long
    name.  Ranking by recall (not F1) lets 鸟德 → 乌尔德 beat 米德 — the
    dropped char must not dilute the per-query-char score.
    """
    score = _align_score(query, target)
    if score <= 0:
        return 0.0
    precision = score / len(target)
    if precision < 0.55:
        return 0.0
    return score / len(query)


def _f1_score(query: str, target: str) -> float:
    """Name-level F1 similarity via SequenceMatcher alignment.

    Equal chars score 1.0, replacements score their glyph Stroke-IoU,
    insertions/deletions score 0.  The raw score is normalised as an
    F1 of precision (vs target length) and recall (vs query length), so
    a short query matching part of a long target is not over-penalised.
    """
    score = _align_score(query, target)
    if score <= 0:
        return 0.0
    precision = score / len(target)
    recall = score / len(query)
    if precision + recall <= 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


# ── Matcher ──────────────────────────────────────────────────────────


class OcrNameMatcher:
    """Corrects OCR-misread character names via glyph visual similarity."""

    def __init__(self) -> None:
        self._cache_mtime = None
        self._names: list[str] = []
        self._aliases: dict = {}
        self._by_length: dict = {}
        self._refresh_index()

    # ── Index construction ──────────────────────────────────────────

    def _refresh_index(self) -> None:
        mtime = None
        if os.path.exists(_CHAR_DETAIL_CACHE):
            mtime = os.path.getmtime(_CHAR_DETAIL_CACHE)
        if mtime == self._cache_mtime and self._names:
            return
        self._cache_mtime = mtime

        names: set = set()

        # Primary source: wiki cache (contains the newest characters)
        if os.path.exists(_CHAR_DETAIL_CACHE):
            try:
                with open(_CHAR_DETAIL_CACHE, "r", encoding="utf-8") as f:
                    cache = json.load(f)
                for entry in cache.get("data", {}).values():
                    if not isinstance(entry, dict):
                        continue
                    name = entry.get("name_cn") or entry.get("title") or ""
                    if name:
                        names.add(name)
            except Exception:
                pass

        # Fallback / supplement: vendored openrubi alias dictionary
        self._aliases = {}
        if os.path.exists(_CHAR_DIC_PATH):
            try:
                with open(_CHAR_DIC_PATH, "r", encoding="utf-8") as f:
                    dic = json.load(f)
                for alias, canon in dic.items():
                    self._aliases[alias.strip().lower()] = canon
                    names.add(canon)
            except Exception:
                pass

        self._names = sorted(names)
        self._by_length = {}
        for name in self._names:
            self._by_length.setdefault(len(name), []).append(name)

    @property
    def names(self) -> list[str]:
        return self._names

    # ── Matching ────────────────────────────────────────────────────

    def match(self, query: str, ocr_confidence: float = 1.0) -> dict:
        """Match an OCR-read name against the known character names.

        Returns a dict:
          ``name``      resolved canonical name (None when uncertain)
          ``status``    exact | corrected | warning | uncertain
          ``candidates`` top-3 [(name, score)] when uncertain
        """
        self._refresh_index()
        q = query.strip()
        if not q:
            return {"name": None, "status": "uncertain", "candidates": []}

        # Exact hit on canonical name or known alias
        canon = self._exact(q)
        if canon:
            return {"name": canon, "status": "exact", "candidates": []}

        # Truncated long-name rescue: OCR sometimes drops the tail of a
        # long name when the last glyph is clipped at a crop edge
        # (璀璨誓约的伊娥 → 璀璨誓约的伊娥丝).  A ≥4-char reading that is a
        # prefix of exactly one known name (allowing 1-2 dropped chars)
        # is corrected directly.
        if len(q) >= 4:
            pref = [
                n for n in self._names
                if n.startswith(q) and len(n) <= len(q) + 2
            ]
            if len(pref) == 1:
                return {"name": pref[0], "status": "corrected", "candidates": []}

        if not self._names or ocr_confidence < _LOW_OCR_CONF:
            return {"name": None, "status": "uncertain", "candidates": []}

        if len(q) == 1:
            return self._match_one(q)
        if len(q) == 2:
            return self._match_two(q)
        return self._match_long(q)

    def _exact(self, q: str) -> str | None:
        if q in self._names:
            return q
        return self._aliases.get(q.lower())

    def _match_one(self, q: str) -> dict:
        ranked = sorted(
            ((n, _char_sim(q, n)) for n in self._by_length.get(1, [])),
            key=lambda kv: -kv[1],
        )
        if not ranked:
            return {"name": None, "status": "uncertain", "candidates": []}
        top = ranked[0]
        margin = top[1] - (ranked[1][1] if len(ranked) > 1 else 0.0)
        if top[1] >= _ONE_CHAR_MIN and margin >= _ONE_CHAR_MARGIN:
            return {"name": top[0], "status": "corrected", "candidates": []}
        return {"name": None, "status": "uncertain", "candidates": ranked[:3]}

    def _match_two(self, q: str) -> dict:
        # Split-char matching against two-char names
        two = self._by_length.get(2, [])
        ranked = sorted(
            (
                (n, _char_sim(q[0], n[0]) + _char_sim(q[1], n[1]))
                for n in two
            ),
            key=lambda kv: -kv[1],
        )
        split_ok = False
        if ranked:
            margin = ranked[0][1] - (ranked[1][1] if len(ranked) > 1 else 0.0)
            split_ok = margin >= _TWO_CHAR_SPLIT_MARGIN
            split_target, split_conf = ranked[0][0], ranked[0][1] / 2

        # Cross-length recall (catches 鸟德 → 乌尔德)
        f1_ranked = sorted(
            ((n, _cross_score(q, n)) for n in self._names),
            key=lambda kv: -kv[1],
        )
        f1_target, f1_conf = f1_ranked[0]
        f1_margin = f1_conf - (f1_ranked[1][1] if len(f1_ranked) > 1 else 0.0)

        if split_ok and (
            f1_conf < _TWO_CHAR_F1_MIN
            or f1_margin < _TWO_CHAR_CROSS_MARGIN
            or f1_target == split_target
            or split_conf >= f1_conf
        ):
            return {
                "name": split_target, "status": "corrected", "candidates": [],
            }
        if f1_conf >= _TWO_CHAR_F1_MIN and f1_margin >= _TWO_CHAR_CROSS_MARGIN:
            status = "corrected" if f1_conf >= _LONG_AUTO_MIN else "warning"
            return {"name": f1_target, "status": status, "candidates": []}
        if split_ok:
            return {
                "name": split_target, "status": "corrected", "candidates": [],
            }
        return {
            "name": None, "status": "uncertain",
            "candidates": [(n, round(s / 2, 3)) for n, s in ranked[:3]],
        }

    def _match_long(self, q: str) -> dict:
        ranked = sorted(
            ((n, _f1_score(q, n)) for n in self._names),
            key=lambda kv: -kv[1],
        )
        top, second = ranked[0], (ranked[1][1] if len(ranked) > 1 else 0.0)
        margin = top[1] - second
        if top[1] >= _LONG_AUTO_MIN and margin >= _LONG_AUTO_MARGIN:
            return {"name": top[0], "status": "corrected", "candidates": []}
        if top[1] >= _LONG_WARN_MIN and margin >= _LONG_WARN_MARGIN:
            return {"name": top[0], "status": "warning", "candidates": []}
        return {
            "name": None, "status": "uncertain",
            "candidates": [(n, round(s, 3)) for n, s in ranked[:3]],
        }

    # ── Multimodal fallback ─────────────────────────────────────────

    def candidate_list_text(self, lengths: set[int] | None = None) -> str:
        """Candidate names grouped by length (~200-300 tokens for the
        full list).  ``lengths`` restricts the groups — the vision
        fallback uses ±1 of the query length, since a 1-char label can
        never be a 4-char name (full list invited cross-length
        hallucinations: 咩 → 天宫咲夜)."""
        lines = []
        for length in sorted(self._by_length):
            if lengths is not None and length not in lengths:
                continue
            label = {1: "单字", 2: "双字", 3: "三字"}.get(length, "多字")
            lines.append(
                f"{label}名称：" + "、".join(self._by_length[length])
            )
        return "\n".join(lines)

    async def resolve_with_vision(
        self, query: str, image, bbox
    ) -> str | None:
        """Multimodal fallback: send the cropped name region + the full
        candidate list to a vision LLM.  Returns a canonical name or None.
        """
        try:
            from lib.multimodal_client import MultimodalClient
        except ImportError:
            return None
        client = MultimodalClient()
        if not client.is_available():
            return None

        import tempfile

        pad = 5
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        crop = image.crop((
            max(0, min(xs) - pad), max(0, min(ys) - pad),
            max(xs) + pad, max(ys) + pad,
        ))
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        crop.save(tmp.name)

        # A label of N chars cannot be a name of a very different length;
        # restrict candidates to ±1 to suppress cross-length hallucination.
        allowed = {max(1, len(query) - 1), len(query), len(query) + 1}
        prompt = (
            "这是游戏 Ark Re:Code 战斗截图中的角色名称区域。"
            "请识别图片中的角色名。以下是可能的角色名：\n"
            f"{self.candidate_list_text(allowed)}\n"
            "请从中选择最匹配的一个，只回复角色名，不要其他内容。"
        )
        try:
            answer = await client.analyze_image(tmp.name, prompt)
        except Exception:
            return None
        finally:
            os.unlink(tmp.name)

        if not answer:
            return None
        # Validate: first allowed-length candidate verbatim in the answer
        # whose glyphs plausibly match the OCR reading (vision proposes,
        # glyph similarity disposes — rejects e.g. 咩 → 伊娃).
        for name in sorted(
            (n for n in self._names if len(n) in allowed),
            key=len, reverse=True,
        ):
            if name in answer and _cross_score(query, name) >= _VISION_GLYPH_MIN:
                return name
        return None


_matcher: OcrNameMatcher | None = None


def get_name_matcher() -> OcrNameMatcher:
    global _matcher
    if _matcher is None:
        _matcher = OcrNameMatcher()
    return _matcher
