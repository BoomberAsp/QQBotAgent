"""
Battle Screenshot Parser — OCR-based extraction of character action values.

Takes 1-2 battle screenshots from Ark Re:Code, runs OCR + colour sampling to
extract structured character data (name, side, action value), and outputs
a format compatible with ``calculate_speed``.

Two-layer architecture (per PLAN.md Idea 8):
  1. OCR + pixel colour sampling (fast, cheap, no LLM)
  2. Multimodal LLM fallback (when OCR fails or confidence is low)

Entry point:
  parse_battle_screenshots(paths: list[str]) -> str
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

from PIL import Image


# ── Public API ──────────────────────────────────────────────────────


async def parse_battle_screenshots(paths: list[str]) -> str:
    """Extract structured battle data from 1-2 screenshots.

    Args:
        paths: 1 or 2 image file paths (1=current state only, 2=pre+post).

    Returns:
        A JSON string with keys:
        - ``characters``: list of {name, side, action_value, acting}
        - ``phase``: "pre" | "post" | "single"
        - ``warnings``: list of human-readable caveats
        - ``raw_format``: calculate_speed-compatible text block
    """
    if not paths:
        return json.dumps({"error": "未提供截图路径"}, ensure_ascii=False)

    if len(paths) > 2:
        return json.dumps({"error": "最多支持 2 张截图（跑条前 + 跑条后）"}, ensure_ascii=False)

    all_results: list[dict] = []
    warnings: list[str] = []

    for i, path in enumerate(paths):
        if not os.path.exists(path):
            return json.dumps(
                {"error": f"截图文件不存在: {path}"}, ensure_ascii=False
            )
        try:
            result = _parse_single(path)
            result["_index"] = i
            all_results.append(result)
        except Exception as e:
            warnings.append(f"截图 {i + 1} 解析失败: {type(e).__name__}: {e}")

    if not all_results:
        # All OCR failed → try multimodal LLM fallback
        return json.dumps({
            "error": "所有截图 OCR 解析失败",
            "fallback": "multimodal",
            "paths": paths,
            "warnings": warnings,
        }, ensure_ascii=False)

    if len(all_results) == 1:
        return _format_single_result(all_results[0], warnings)

    return _format_pair_result(all_results[0], all_results[1], warnings)


# ── Single-image parsing ────────────────────────────────────────────


def _parse_single(path: str) -> dict:
    """Extract characters from one battle screenshot.

    Returns a dict with keys: phase, characters, frame, ocr_blocks.
    """
    image = Image.open(path).convert("RGB")
    w, h = image.size

    # 1. Detect the central dark panel
    from lib.ocr_engine import detect_frame, extract_text_blocks, sample_side_colour

    frame = detect_frame(image)
    if frame is None:
        # Fallback: use a generous central crop
        margin_x = int(w * 0.12)
        margin_y = int(h * 0.08)
        frame = (margin_x, margin_y, w - margin_x, h - margin_y)

    # 2. OCR within the frame
    blocks = extract_text_blocks(image, frame)
    if not blocks:
        return {"phase": "unknown", "characters": [], "frame": frame, "ocr_blocks": []}

    # 3. Cluster blocks into rows by Y coordinate
    rows = _cluster_rows(blocks, y_tolerance=int(h * 0.015))

    # 4. Within each row, extract character name + action value
    characters = []
    for row_blocks in rows:
        char = _extract_character(row_blocks, image)
        if char:
            characters.append(char)

    # 5. Post-process: identify acting character, sort by action value
    characters = _post_process(characters, image)

    return {
        "phase": "unknown",  # caller sets pre/post
        "characters": characters,
        "frame": frame,
        "ocr_blocks": blocks,
    }


# ── Row clustering ──────────────────────────────────────────────────


def _cluster_rows(
    blocks: list[dict], y_tolerance: int = 15
) -> list[list[dict]]:
    """Cluster OCR text blocks into rows by vertical proximity.

    Blocks whose vertical centre is within *y_tolerance* pixels are placed
    in the same row. Rows are sorted top-to-bottom.
    """
    if not blocks:
        return []

    # Sort by Y centre
    def _y_centre(b: dict) -> float:
        ys = [p[1] for p in b["bbox"]]
        return (min(ys) + max(ys)) / 2

    sorted_blocks = sorted(blocks, key=_y_centre)

    rows: list[list[dict]] = []
    current_row: list[dict] = [sorted_blocks[0]]
    current_y = _y_centre(sorted_blocks[0])

    for b in sorted_blocks[1:]:
        y = _y_centre(b)
        if abs(y - current_y) <= y_tolerance:
            current_row.append(b)
            # Update current_y to the weighted centre
            current_y = sum(_y_centre(x) for x in current_row) / len(current_row)
        else:
            rows.append(current_row)
            current_row = [b]
            current_y = y

    rows.append(current_row)
    return rows


# ── Character extraction from a row ─────────────────────────────────


# Regex for action value: matches "XX.X%" or "XX%"
_ACTION_RE = re.compile(r"^(\d{1,3}(?:\.\d)?)\s*%$")


def _extract_character(row_blocks: list[dict], image: Image.Image) -> dict | None:
    """Extract a single character's data from one OCR row.

    Returns ``{name, action_value, side, acting, bbox}`` or None if the row
    doesn't contain a recognisable character entry.
    """
    if not row_blocks:
        return None

    # Separate action-value candidates from name candidates
    action_blocks = []
    name_blocks = []
    for b in row_blocks:
        text = b["text"].strip()
        if _ACTION_RE.match(text):
            action_blocks.append(b)
        else:
            name_blocks.append(b)

    if not name_blocks:
        return None

    # Action value: take the rightmost percentage
    action_value = None
    if action_blocks:
        # Pick the rightmost (highest X) action value block
        best = max(action_blocks, key=lambda b: max(p[0] for p in b["bbox"]))
        m = _ACTION_RE.match(best["text"].strip())
        if m:
            action_value = float(m.group(1))

    # Character name: leftmost text block that isn't a percentage
    # Sort by X position
    name_blocks.sort(key=lambda b: min(p[0] for p in b["bbox"]))
    name = name_blocks[0]["text"].strip() if name_blocks else ""

    # Clean up common OCR artifacts
    name = _clean_name(name)

    if not name:
        return None

    # Side determination via colour sampling on the name block
    from lib.ocr_engine import sample_side_colour

    side = sample_side_colour(image, name_blocks[0]["bbox"])

    return {
        "name": name,
        "action_value": action_value,
        "side": side,
        "acting": False,
        "bbox": name_blocks[0]["bbox"],
    }


def _clean_name(raw: str) -> str:
    """Remove common OCR artifacts from character names."""
    # Remove leading/trailing punctuation, single chars, obvious non-names
    name = raw.strip("，。,.、:：·-—|/\\[]{}()（） ")
    # Remove HP / LV / Lv fragments
    for noise in ["HP", "hp", "Lv", "LV", "lv", "Lv.", "Next", "NEXT", "ON"]:
        if name == noise:
            return ""
        # Remove leading noise
        if name.startswith(noise + " "):
            name = name[len(noise) + 1 :]
        if name.startswith(noise):
            name = name[len(noise) :]
    # Remove leading digits (often OCR fragment from HP bar)
    name = re.sub(r"^\d+\s*", "", name)
    # Remove trailing digits with dot (fragment)
    name = re.sub(r"\s*\d+\.?\d*$", "", name)
    return name.strip()


# ── Post-processing ─────────────────────────────────────────────────


def _post_process(characters: list[dict], image: Image.Image) -> list[dict]:
    """Refine extracted characters: identify acting char, sort, deduplicate."""
    if not characters:
        return []

    # Identify the acting character: action value ≈ 0% and at the top
    for c in characters:
        av = c.get("action_value")
        if av is not None and av <= 0.5:
            c["acting"] = True

    # Sort: acting character first, then by action value descending
    characters.sort(
        key=lambda c: (
            not c.get("acting", False),
            -(c.get("action_value") or 0),
        )
    )

    # Deduplicate by name (keep highest confidence / first occurrence)
    seen = set()
    unique = []
    for c in characters:
        if c["name"] not in seen:
            seen.add(c["name"])
            unique.append(c)

    return unique


# ── Output formatting ───────────────────────────────────────────────


def _format_single_result(result: dict, warnings: list[str]) -> str:
    """Format a single-screenshot result as JSON."""
    characters = result.get("characters", [])
    allies = [c for c in characters if c.get("side") == "ally"]
    enemies = [c for c in characters if c.get("side") == "enemy"]
    unknown = [c for c in characters if c.get("side") == "unknown"]

    raw_lines = _build_raw_format(characters, allies, enemies)

    return json.dumps({
        "phase": "single",
        "characters": characters,
        "allies": allies,
        "enemies": enemies,
        "unknown_side": unknown,
        "warnings": warnings,
        "raw_format": raw_lines,
        "note": "仅提供了一张截图，缺少初始行动值对比。如需测速请提供两张截图（跑条前+跑条后）。",
    }, ensure_ascii=False, indent=2)


def _format_pair_result(
    pre: dict, post: dict, warnings: list[str]
) -> str:
    """Format a two-screenshot (pre+post) result as JSON."""
    pre_chars = {c["name"]: c for c in pre.get("characters", [])}
    post_chars = {c["name"]: c for c in post.get("characters", [])}

    merged = []
    all_names = set(pre_chars.keys()) | set(post_chars.keys())

    for name in all_names:
        pre_c = pre_chars.get(name)
        post_c = post_chars.get(name)
        side = (pre_c or post_c).get("side", "unknown")
        merged.append({
            "name": name,
            "side": side,
            "init_action_value": pre_c.get("action_value") if pre_c else None,
            "current_action_value": post_c.get("action_value") if post_c else None,
        })

    allies = [c for c in merged if c.get("side") == "ally"]
    enemies = [c for c in merged if c.get("side") == "enemy"]
    unknown = [c for c in merged if c.get("side") == "unknown"]

    # Check for name mismatches between pre and post
    pre_only = set(pre_chars.keys()) - set(post_chars.keys())
    post_only = set(post_chars.keys()) - set(pre_chars.keys())
    if pre_only or post_only:
        mismatch = []
        if pre_only:
            mismatch.append(f"仅跑条前出现: {', '.join(sorted(pre_only))}")
        if post_only:
            mismatch.append(f"仅跑条后出现: {', '.join(sorted(post_only))}")
        warnings.extend(mismatch)

    raw_lines = _build_raw_format(merged, allies, enemies)

    return json.dumps({
        "phase": "pair",
        "characters": merged,
        "allies": allies,
        "enemies": enemies,
        "unknown_side": unknown,
        "warnings": warnings,
        "raw_format": raw_lines,
    }, ensure_ascii=False, indent=2)


def _build_raw_format(
    characters: list[dict],
    allies: list[dict],
    enemies: list[dict],
) -> str:
    """Build a text block compatible with ``calculate_speed``'s parser.

    Format::

        我方
        <name> <init_av> <current_av> 0
        ...

        敌方
        <name> <init_av> <current_av>
        ...
    """
    lines = []

    if allies:
        lines.append("我方")
        for c in allies:
            init_av = c.get("init_action_value", c.get("action_value", 0)) or 0
            cur_av = c.get("current_action_value", c.get("action_value", 0)) or 0
            lines.append(f"{c['name']} {init_av} {cur_av} 0")

    if enemies:
        lines.append("敌方")
        for c in enemies:
            init_av = c.get("init_action_value", c.get("action_value", 0)) or 0
            cur_av = c.get("current_action_value", c.get("action_value", 0)) or 0
            lines.append(f"{c['name']} {init_av} {cur_av}")

    return "\n".join(lines)


# ── Multimodal LLM fallback (placeholder) ───────────────────────────


async def _multimodal_fallback(paths: list[str]) -> str:
    """Fallback: use multimodal LLM to extract character data from screenshots.

    Called when OCR fails entirely (e.g. blurry image, non-standard UI).
    Placeholder — the PLAN.md one-shot prompt strategy is implemented here.
    """
    # TODO: implement one-shot multimodal LLM extraction
    return json.dumps({
        "error": "多模态 LLM 降级尚未实现",
        "paths": paths,
    }, ensure_ascii=False)