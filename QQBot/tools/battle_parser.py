"""
Battle Screenshot Parser — OCR-based extraction of character action values.

Takes 1-2 battle screenshots from Ark Re:Code, runs qwen3.5-ocr + banner
colour sampling to extract structured character data (name, side, action
value), and outputs a format compatible with ``calculate_speed``.

Extraction (per PLAN.md Idea 8):
  1. One qwen3.5-ocr call per screenshot over the left half of the panel
     (0-50% of frame width) — names + action values come back interleaved
     per row; the glyph matcher (``ocr_name_matcher``) corrects visual
     misreads.
  2. Side (ally/enemy) from the row banner colour (red/blue band scan).
  3. Multimodal LLM fallback for names the matcher leaves uncertain.

Entry point:
  parse_battle_screenshots(paths: list[str]) -> str
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import sys
from typing import Any

import httpx
import numpy as np
from PIL import Image


# ── Character Skill Index ───────────────────────────────────────────

# Path to the character detail cache (produced by wiki_scraper.refresh_characters).
_CHAR_DETAIL_CACHE = os.path.join(
    os.path.dirname(__file__), "..", "data", "wiki_cache", "character_details.json"
)

# Lazily built index: {name_cn: {has_entry_buff, has_entry_morale_focus, ...}}
_skill_index: dict | None = None


def _load_character_skill_index() -> dict:
    """Lazily load character_details.json and build a per-character skill index.

    Returns {} when the cache file is missing (e.g. dev machine with no crawled
    data) so callers degrade gracefully.
    """
    global _skill_index
    if _skill_index is not None:
        return _skill_index

    _skill_index = {}

    if not os.path.exists(_CHAR_DETAIL_CACHE):
        return _skill_index

    try:
        with open(_CHAR_DETAIL_CACHE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        data = cache.get("data", {})
    except Exception:
        return _skill_index

    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        name = entry.get("name_cn") or entry.get("title", "")
        if not name:
            continue

        skills = entry.get("skills") or []
        entry_buff = False
        entry_morale = False
        immunity = False
        action_gauge = False
        ag_skills: list[dict] = []

        for s in skills:
            # Combine all text fields for keyword scanning
            text = " ".join(
                str(s.get(k, ""))
                for k in ("name", "name_en", "des", "des_en", "des2", "des2_en")
            )
            text_lower = text.lower()

            # ── Entry-battle buff: "at battle start" + buff keyword ──
            if not entry_buff:
                if _match_entry_buff(text, text_lower):
                    entry_buff = True

            # ── Entry-battle morale/focus ──
            if not entry_morale:
                if _match_entry_morale(text, text_lower):
                    entry_morale = True

            # ── Immunity skill ──
            if not immunity:
                if _match_immunity(text, text_lower):
                    immunity = True

            # ── Action gauge skill ──
            if _match_action_gauge(text, text_lower):
                action_gauge = True
                ag_skills.append({
                    "name": s.get("name") or s.get("name_en", ""),
                    "des": s.get("des") or s.get("des_en", ""),
                })

        _skill_index[name] = {
            "has_entry_buff": entry_buff,
            "has_entry_morale_focus": entry_morale,
            "has_immunity_skill": immunity,
            "has_action_gauge_skill": action_gauge,
            "action_gauge_skills": ag_skills,
        }

    return _skill_index


def _invalidate_skill_index():
    """Drop the cached skill index so the next lookup reloads it."""
    global _skill_index
    _skill_index = None


# ── Keyword matchers ─────────────────────────────────────────────────

# Phrases that indicate a buff is granted at the start of battle.
_ENTRY_BATTLE_PHRASES = [
    "at the start of battle",
    "at the beginning of battle",
    "when battle begins",
    "when the battle begins",
    "at the start of the battle",
    "at the beginning of the battle",
    "start of the battle",
    "beginning of the battle",
    "enter battle",
    "entering battle",
    "进入战斗时",
    "战斗开始时",
    "战斗开始",
]

# Phrases that indicate a buff is being granted.
_BUFF_GRANT_PHRASES = [
    "grants",
    "gains",
    "applies",
    "granted",
    "获得",
    "赋予",
    "施加",
    "得到",
    "增加",
    "提升",
]

# Morale / Focus keywords.
_MORALE_FOCUS_KEYWORDS = [
    "morale",
    "focus",
    "战意",
    "集中力",
    "气魄",
]

# Immunity keywords.
_IMMUNITY_KEYWORDS = [
    "immunity",
    "immune",
    "免疫",
]

# Action-gauge keywords (拉条/推条).
_ACTION_GAUGE_KEYWORDS = [
    "combat readiness",
    "action gauge",
    "拉条",
    "推条",
    "increase combat readiness",
    "decrease combat readiness",
    "push back",
    "push forward",
    "reduce combat readiness",
    "行动值",
]


def _match_entry_buff(text: str, text_lower: str) -> bool:
    """True if *text* describes a buff granted at the start of battle."""
    has_entry = any(p in text_lower for p in _ENTRY_BATTLE_PHRASES)
    if not has_entry:
        return False
    has_buff = any(p in text_lower for p in _BUFF_GRANT_PHRASES)
    return has_buff


def _match_entry_morale(text: str, text_lower: str) -> bool:
    """True if *text* describes Morale/Focus gained at the start of battle."""
    has_entry = any(p in text_lower for p in _ENTRY_BATTLE_PHRASES)
    if not has_entry:
        return False
    has_morale = any(kw in text_lower for kw in _MORALE_FOCUS_KEYWORDS)
    return has_morale


def _match_immunity(text: str, text_lower: str) -> bool:
    """True if *text* mentions an immunity-granting effect."""
    return any(kw in text_lower for kw in _IMMUNITY_KEYWORDS)


def _match_action_gauge(text: str, text_lower: str) -> bool:
    """True if *text* mentions action-gauge manipulation (拉条/推条)."""
    return any(kw in text_lower for kw in _ACTION_GAUGE_KEYWORDS)


def get_character_skill_info(name: str) -> dict | None:
    """Look up a character's skill flags by name (Chinese or English).

    Returns None when the character is not found in the index.
    """
    idx = _load_character_skill_index()
    if name in idx:
        return idx[name]
    # Try case-insensitive English title match
    name_lower = name.strip().lower()
    for cn, info in idx.items():
        if cn.lower() == name_lower:
            return info
    return None


# ── Public API ──────────────────────────────────────────────────────


async def parse_battle_screenshots(paths: list[str]) -> str:
    """Extract structured battle data from 1-2 screenshots.

    Args:
        paths: 1 or 2 image file paths (1=current state only, 2=pre+post).

    Returns:
        A JSON string with keys:
        - ``characters``: list of {name, side, action_value, acting}
        - ``phase``: detected from action values — "pre" when ALL values
          are ≤ 5% (乱速 just completed), else "post"; "pair" in
          two-screenshot mode, where the per-screenshot phases are in
          ``screenshot_phases`` and order mismatches raise a warning
        - ``mode``: "single" | "pair"
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
            result = await _parse_single(path)
            await _resolve_uncertain_names(result, path)
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
        result = _format_single_result(all_results[0], warnings)
        # Add action gauge skills for single-screenshot mode
        result_dict = json.loads(result)
        ag_skills = _summarize_action_gauge_skills(
            all_results[0].get("characters", [])
        )
        if ag_skills:
            result_dict["action_gauge_skills"] = ag_skills
        return json.dumps(result_dict, ensure_ascii=False, indent=2)

    # Phase sanity check: the first screenshot should be pre (all values
    # ≤ 5% after 乱速), the second post (values have grown).
    phases = [r.get("phase", "unknown") for r in all_results]
    if phases == ["post", "pre"]:
        warnings.append(
            "截图顺序疑似颠倒：第一张行动值均>5%（像跑条后），"
            "第二张均≤5%（像跑条前）。请确认顺序后重新发送。"
        )
    elif phases[0] == "post":
        warnings.append(
            "第一张截图含行动值>5%的角色，不像跑条前截图（乱速后应全员≤5%）。"
        )
    elif phases[1] == "pre":
        warnings.append(
            "第二张截图行动值均≤5%，不像跑条后截图（跑条后应有角色>5%）。"
        )

    # Two-screenshot mode: validate pre-screenshot + action gauge skills
    pre_valid, validity_warnings = _validate_pre_screenshot(
        all_results[0], all_results[1]
    )
    warnings.extend(validity_warnings)

    ag_skills = _summarize_action_gauge_skills(
        all_results[0].get("characters", []) + all_results[1].get("characters", [])
    )

    return _format_pair_result(
        all_results[0], all_results[1], warnings,
        pre_valid=pre_valid,
        action_gauge_skills=ag_skills,
    )


async def _resolve_uncertain_names(result: dict, path: str) -> None:
    """Multimodal fallback: names the glyph matcher left ``uncertain``
    are re-read by a vision LLM from the cropped name region.  No-op when
    the multimodal client is unavailable (names keep their raw reading).
    """
    uncertain = [
        c for c in result.get("characters", [])
        if c.get("name_status") == "uncertain"
    ]
    if not uncertain:
        return

    from tools.ocr_name_matcher import get_name_matcher

    matcher = get_name_matcher()
    image = Image.open(path).convert("RGB")
    for c in uncertain:
        try:
            name = await matcher.resolve_with_vision(
                c["raw_name"], image, c["bbox"]
            )
        except Exception:
            name = None
        if name:
            c["name"] = name
            c["name_status"] = "vision"


# ── qwen3.5-ocr panel extraction ────────────────────────────────────

_QWEN_OCR_MODEL = "qwen3.5-ocr"
_QWEN_OCR_TIMEOUT = 90.0

# Left half of the panel: covers the longest (8-char) character names and
# the action-value column while excluding the skill/buff icon overlays
# (regions C/D).  A single call returns names and values interleaved per
# row — the old 0-32% name crop clipped the last glyph of 8-char names
# (璀璨誓约的伊娥丝), and the separate digit-only value pass is what made
# easyOCR's "%" misreads need per-digit template matching.
_PANEL_CROP_FRAC = 0.50

_PROMPT_PANEL = (
    "这是游戏战斗界面截图的角色行动顺序列表，每行包含角色名与行动值"
    "（白色百分数）。请从上到下逐行输出所有角色名和行动值，每行一个，"
    "不要其他内容。"
)

# qwen3.5-ocr reads action values cleanly as "NN%".
_QWEN_VALUE_RE = re.compile(r"^(\d{1,3})\s*[%％]$")

# Some images make the model fuse a row into one line ("露娜 88%"); the
# format is deterministic per image, so split fused lines explicitly.
_QWEN_MERGED_RE = re.compile(r"^(?P<name>.+?)\s+(?P<val>\d{1,3})\s*[%％]$")


def _parse_qwen_panel_message(
    message: dict, crop_w: int, crop_h: int
) -> list[dict]:
    """Extract ``[{y, text}]`` rows from a qwen3.5-ocr response.

    The response format is deterministic per image, two variants: a
    ```json fenced array of ``{"rotate_rect": [cx, cy, h, w, angle],
    "text": ...}`` whose coordinates are normalised to a 0-1000 grid
    (scaled here to crop pixels), or plain text lines via
    ``ocr_result.processed_text`` (no coordinates → ``y=None``).
    """
    content = message.get("content") or ""
    arr = None
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", content, re.S)
    candidates = [m.group(1)] if m else [content]
    for raw in candidates:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                arr = parsed
                break
        except (json.JSONDecodeError, TypeError):
            continue

    entries: list[dict] = []
    if arr is not None:
        for item in arr:
            if not isinstance(item, dict):
                continue
            rect = item.get("rotate_rect") or item.get("bbox") or []
            y = float(rect[1]) / 1000.0 * crop_h if len(rect) >= 2 else None
            entries.append({"y": y, "text": str(item.get("text", "")).strip()})
        return entries

    pt = (message.get("ocr_result") or {}).get("processed_text")
    if pt:
        for line in str(pt).splitlines():
            line = line.strip()
            if line:
                entries.append({"y": None, "text": line})
    return entries


async def _qwen_ocr_panel(crop: Image.Image, cfg: dict) -> list[dict]:
    """One qwen3.5-ocr call over the panel crop.  Returns ``[{y, text}]``."""
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    body = {
        "model": _QWEN_OCR_MODEL,
        "temperature": 0.0,
        "max_tokens": 2048,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _PROMPT_PANEL},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }],
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }
    url = f"{cfg['api_base']}/chat/completions"
    for attempt in range(3):
        try:
            async with httpx.AsyncClient() as http:
                resp = await http.post(
                    url, headers=headers, json=body, timeout=_QWEN_OCR_TIMEOUT
                )
                if resp.status_code == 429 and attempt < 2:
                    await asyncio.sleep(3.0 * (attempt + 1))
                    continue
                resp.raise_for_status()
                result = resp.json()
            return _parse_qwen_panel_message(
                result["choices"][0]["message"], crop.size[0], crop.size[1],
            )
        except Exception as e:
            if attempt >= 2:
                raise RuntimeError(f"qwen3.5-ocr 调用失败: {e}") from e
            await asyncio.sleep(2.0 * (attempt + 1))
    return []


# ── Row banner (side) detection ─────────────────────────────────────


def _detect_row_bands(
    image: Image.Image, frame: tuple[int, int, int, int]
) -> list[dict]:
    """Detect the coloured character-row banners (red=enemy, blue=ally).

    For every pixel row in the name/value window (13-45% of frame width,
    right of the portraits) counts red and blue pixels — enemy banners are
    red ~ (90, 30, 45), ally banners blue ~ (40, 70, 110) — and run-length-
    encodes the rows where a colour dominates.  White name glyphs, the
    grey NEXT ON banner and the panel background contribute almost no
    red/blue pixels, so character rows come back top-to-bottom with their
    side even when a long name crosses the window.
    """
    left, top, right, bottom = frame
    frame_w = right - left
    frame_h = bottom - top
    x0 = left + int(frame_w * 0.13)
    x1 = left + int(frame_w * 0.45)
    win = np.array(image)[top:bottom, x0:x1].astype(int)
    r, g, b = win[:, :, 0], win[:, :, 1], win[:, :, 2]
    thr = int(frame_w * 0.08)
    red_n = ((r > g + 35) & (r > b + 25)).sum(axis=1)
    blue_n = ((b > r + 40) & (b > g + 20)).sum(axis=1)
    lab = np.where(red_n > thr, 1, np.where(blue_n > thr, 2, 0))

    # The acting (top, 0%) row's banner is partly covered by white name
    # glyphs and turn markers, splitting its colour run; bridge gaps up to
    # ~3.5% of frame height (glyph gaps ≈ 3%, banner-to-banner gaps ≈ 8%).
    gap_tol = max(8, int(frame_h * 0.035))
    min_h = max(12, int(frame_h * 0.035))

    bands: list[dict] = []
    side = y0 = y1 = gap = 0

    def _flush():
        if side and y1 - y0 >= min_h:
            bands.append({
                "side": "enemy" if side == 1 else "ally",
                "y0": top + y0, "y1": top + y1,
            })

    for i in range(lab.shape[0]):
        s = int(lab[i])
        if s and s == side:
            y1 = i + 1
            gap = 0
        elif s:
            _flush()
            side, y0, y1, gap = s, i, i + 1, 0
        else:
            gap += 1
            if side and gap > gap_tol:
                _flush()
                side, gap = 0, 0
    _flush()
    return bands


# ── Single-image parsing ────────────────────────────────────────────


async def _parse_single(path: str) -> dict:
    """Extract characters from one battle screenshot.

    One qwen3.5-ocr call reads the left half of the panel (0-50% of frame
    width); names and action values come back interleaved top-to-bottom.
    Rows are built by y-clustering when the response carries coordinates,
    else by order (names and values are each in row order).  The side of
    each row comes from the banner colour bands (``_detect_row_bands``).

    Returns a dict with keys: phase, characters, frame, row_buffs.
    """
    image = Image.open(path).convert("RGB")
    w, h = image.size

    from lib.ocr_engine import detect_frame

    frame = detect_frame(image)
    if frame is None:
        # Fallback: use a generous central crop
        margin_x = int(w * 0.12)
        margin_y = int(h * 0.08)
        frame = (margin_x, margin_y, w - margin_x, h - margin_y)

    left, top, right, bottom = frame
    frame_w = right - left
    frame_h = bottom - top

    from lib.multimodal_client import MultimodalClient

    client = MultimodalClient()
    if not client.is_available():
        raise RuntimeError("MULTIMODAL_MODEL 未配置，qwen3.5-ocr 不可用")
    cfg = dict(client._config)
    cfg["model"] = _QWEN_OCR_MODEL

    # 1. qwen3.5-ocr over the panel crop (names + values in one call)
    crop = image.crop((left, top, left + int(frame_w * _PANEL_CROP_FRAC), bottom))
    entries = await _qwen_ocr_panel(crop, cfg)

    name_items: list[dict] = []
    value_items: list[dict] = []
    for e in entries:
        text = e["text"].strip()
        m = _QWEN_VALUE_RE.match(text)
        if m and 0 <= int(m.group(1)) <= 250:
            value_items.append(e)
            continue
        mg = _QWEN_MERGED_RE.match(text)
        if mg and 0 <= int(mg["val"]) <= 250 and _clean_name(mg["name"]):
            name_items.append({"y": e["y"], "text": mg["name"]})
            value_items.append({"y": e["y"], "text": mg["val"] + "%"})
            continue
        if _clean_name(text):
            name_items.append(e)
    if not name_items:
        return {
            "phase": "unknown",
            "characters": [],
            "frame": frame,
            "row_buffs": [],
        }

    # 2. Pair names and values into rows
    rows: list[dict] = []
    orphans: list[dict] = []
    has_y = all(e["y"] is not None for e in name_items + value_items)
    if has_y:
        tol = max(10.0, frame_h * 0.03)
        pool = [dict(e, _kind="n") for e in name_items] + [
            dict(e, _kind="v") for e in value_items
        ]
        pool.sort(key=lambda e: e["y"])
        cluster = [pool[0]]
        clusters = []

        def _flush_cluster(cur):
            n = next((e for e in cur if e["_kind"] == "n"), None)
            v = next((e for e in cur if e["_kind"] == "v"), None)
            if n:
                rows.append({"name_e": n, "value_e": v})
            elif v:
                orphans.append(v)

        for e in pool[1:]:
            if e["y"] - cluster[-1]["y"] <= tol:
                cluster.append(e)
            else:
                _flush_cluster(cluster)
                cluster = [e]
        _flush_cluster(cluster)
        # A value whose y drifted off its row (model coordinate quirk,
        # seen once next to the NEXT ON banner) pairs by row order.
        for row in rows:
            if row["value_e"] is None and orphans:
                row["value_e"] = orphans.pop(0)
    else:
        for i, e in enumerate(name_items):
            rows.append({
                "name_e": e,
                "value_e": value_items[i] if i < len(value_items) else None,
            })

    # 3. Side per row from the banner colour bands
    bands = _detect_row_bands(image, frame)

    from tools.ocr_name_matcher import get_name_matcher

    matcher = get_name_matcher()

    characters = []
    buff_rows: list[list[dict]] = []
    for i, row in enumerate(rows):
        ne = row["name_e"]
        raw_name = _clean_name(ne["text"].strip())
        res = matcher.match(raw_name)
        name = res["name"] or raw_name

        band = None
        if ne["y"] is not None:
            y_abs = top + ne["y"]
            cands = [b for b in bands if b["y0"] - 30 <= y_abs <= b["y1"] + 30]
            if cands:  # adjacent banners can both reach: take the nearest
                band = min(cands, key=lambda b: abs((b["y0"] + b["y1"]) / 2 - y_abs))
        elif i < len(bands):
            band = bands[i]
        if band:
            side, by0, by1 = band["side"], band["y0"], band["y1"]
        else:
            side = "unknown"
            by0 = top + int(ne["y"]) - 20 if ne["y"] is not None else top
            by1 = by0 + 40
        bbox = [
            [left + int(frame_w * 0.12), by0 + 4],
            [left + int(frame_w * 0.30), by0 + 4],
            [left + int(frame_w * 0.30), by1 - 4],
            [left + int(frame_w * 0.12), by1 - 4],
        ]

        value = None
        ve = row["value_e"]
        if ve:
            m = _QWEN_VALUE_RE.match(ve["text"].strip())
            if m and 0 <= int(m.group(1)) <= 250:
                value = float(int(m.group(1)))

        characters.append({
            "name": name,
            "raw_name": raw_name,
            "name_status": res["status"],
            "action_value": value,
            "side": side,
            "acting": False,
            "bbox": bbox,
        })
        buff_rows.append([{"bbox": bbox}])

    # 4. Post-process: identify acting character, sort, dedup, cap/side
    characters = _post_process(characters, image)

    # 5. Buff detection per row (for validity checks)
    row_buffs: list[dict] = []
    try:
        import cv2
        img_bgr = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
        from lib.ocr_engine import BuffDetector
        detector = BuffDetector()
        row_buffs = detector.detect_in_rows(img_bgr, buff_rows, frame)
    except Exception as e:
        print(f"[battle_parser] Buff detection skipped: {type(e).__name__}: {e}",
              file=sys.stderr)

    return {
        "phase": _detect_phase(characters),
        "characters": characters,
        "frame": frame,
        "row_buffs": row_buffs,
    }


# Known UI labels that are not character names.
_UI_LABELS = {
    "行动顺序", "action order", "NEXT", "Next", "next", "ON", "On", "on",
    "NEXTON", "NEXT ON", "Next On", "-NEXT ON",
    "HP", "hp", "Hp", "Lv", "LV", "lv", "Lv.",
    "SPD", "spd", "ATK", "atk", "DEF", "def",
    "回合", "2回合", "4回合",
}


def _clean_name(raw: str) -> str:
    """Remove common OCR artifacts from character names.

    Returns an empty string if the text is clearly not a character name.
    """
    name = raw.strip("，。,.、:：·-—|/\\[]{}()（） ")

    # Reject strings with common OCR-garbled UI-label fragments
    if any(ch in name for ch in ("|", "「", "」", "〔", "〕", "【", "】")):
        return ""

    if not name:
        return ""

    # Reject known UI labels (case-insensitive)
    if name in _UI_LABELS or name.upper() in _UI_LABELS:
        return ""

    # Reject single characters (OCR artifacts like "U", "/", ".") —
    # but keep single CJK chars: 咲 / 珍 / 空 are valid character names.
    if len(name) <= 1:
        if not name or not ("一" <= name <= "鿿"):
            return ""

    # Remove HP / LV / Lv fragments
    for noise in ["HP", "hp", "Lv", "LV", "lv", "Lv.", "Next", "NEXT", "ON"]:
        if name == noise:
            return ""
        if name.startswith(noise + " "):
            name = name[len(noise) + 1 :]
        if name.startswith(noise):
            name = name[len(noise) :]

    # Remove leading digits (often OCR fragment from HP bar)
    name = re.sub(r"^\d+\s*", "", name)
    # Remove trailing digits with dot (fragment)
    name = re.sub(r"\s*\d+\.?\d*$", "", name)

    name = name.strip()
    if not name:
        return ""
    if len(name) == 1 and not ("一" <= name <= "鿿"):
        return ""
    return name


# ── Phase detection ─────────────────────────────────────────────────

# 乱速 gives every character a random 0.0%-5.0% starting action value, so a
# pre-battle screenshot has ALL values ≤ 5%; after the gauge run at least
# one character has accumulated more.
_PRE_AV_MAX = 5.0


def _detect_phase(characters: list[dict]) -> str:
    """Detect the screenshot phase from extracted action values.

    ``pre``  — every action value ≤ 5% (乱速 just completed);
    ``post`` — at least one action value > 5% (gauge has been running);
    ``unknown`` — no action values were extracted at all.
    """
    values = [
        c["action_value"] for c in characters
        if c.get("action_value") is not None
    ]
    if not values:
        return "unknown"
    return "pre" if all(v <= _PRE_AV_MAX for v in values) else "post"


# ── Post-processing ─────────────────────────────────────────────────


def _post_process(characters: list[dict], image: Image.Image) -> list[dict]:
    """Refine extracted characters: identify acting char, sort, deduplicate.

    Also caps characters per side at 3 (game mechanic: max 3 allies + 3 enemies).
    Excess characters on a side are likely OCR false positives.
    """
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

    # Deduplicate by (name, side): the same character may legitimately
    # appear on both sides (mirror matches), so name-only dedup dropped
    # real rows.  Keep the first occurrence per (name, side).
    seen = set()
    unique = []
    for c in characters:
        key = (c["name"], c.get("side"))
        if key not in seen:
            seen.add(key)
            unique.append(c)

    # Cap at 3 characters per side (game mechanic: max 3 allies + 3 enemies).
    # Unknown-side characters are preserved (they may be the only data we have).
    allies = [c for c in unique if c.get("side") == "ally"]
    enemies = [c for c in unique if c.get("side") == "enemy"]
    unknown = [c for c in unique if c.get("side") == "unknown"]

    if len(allies) > 3:
        allies = allies[:3]
    if len(enemies) > 3:
        enemies = enemies[:3]

    return allies + enemies + unknown


# ── Pre-screenshot validity ──────────────────────────────────────────


def _validate_pre_screenshot(
    pre: dict,
    post: dict | None,
) -> tuple[bool, list[str]]:
    """Check whether the pre-battle screenshot is valid (乱速 completed).

    Implements the three rules from PLAN.md Idea 8:

    1. **Entry-battle buffs**: Characters with skills that grant buffs at the
       start of battle must show buff icons in the pre-screenshot.  If the
       character has no such skill, the row is vacuously valid.
    2. **Entry-battle Morale/Focus**: Same pattern — only characters whose
       skills grant Morale/Focus *at battle start* are checked.
    3. **Immunity gear**: Characters with 免疫 in the post-screenshot who do
       NOT have an immunity skill are wearing 免疫 gear.  Those characters
       must show 免疫 in the pre-screenshot.

    Args:
        pre: Result dict from ``_parse_single`` for the pre-screenshot.
        post: Result dict from ``_parse_single`` for the post-screenshot
              (may be None for single-screenshot mode).

    Returns:
        ``(is_valid, warnings)`` — *is_valid* is True only when all three
        rules pass. *warnings* contains human-readable explanations for
        each failing rule.
    """
    warnings: list[str] = []
    pre_chars = pre.get("characters", [])
    pre_row_buffs = pre.get("row_buffs", [])
    post_chars = post.get("characters", []) if post else []
    post_row_buffs = post.get("row_buffs", []) if post else []

    # Build name→buffs maps (align rows to characters by index)
    def _buffs_for_char(chars, row_buffs, name):
        """Find the row_buffs entry for a character by name (best-effort)."""
        for i, c in enumerate(chars):
            if c.get("name") == name and i < len(row_buffs):
                return row_buffs[i]
        return {}

    # ── Rule 1: Entry-battle buffs ──
    rule1_ok = True
    for c in pre_chars:
        name = c.get("name", "")
        info = get_character_skill_info(name)
        if info is None:
            # Character not in index — can't validate, assume OK
            continue
        if not info.get("has_entry_buff"):
            continue  # vacuously valid

        # Character has entry-battle buff skill → must see buffs
        row_buffs = _buffs_for_char(pre_chars, pre_row_buffs, name)
        if not row_buffs:
            warnings.append(
                f"规则1（进战buff）：角色「{name}」拥有进战buff技能，"
                f"但跑条前截图中未检测到buff图标（可能乱速尚未完成）"
            )
            rule1_ok = False

    # ── Rule 2: Entry-battle Morale/Focus ──
    rule2_ok = True
    for c in pre_chars:
        name = c.get("name", "")
        info = get_character_skill_info(name)
        if info is None:
            continue
        if not info.get("has_entry_morale_focus"):
            continue  # vacuously valid

        # Character has entry-battle morale/focus → must see 气魄/战意 icon
        row_buffs = _buffs_for_char(pre_chars, pre_row_buffs, name)
        has_morale_icon = any(
            kw in buff_name.lower()
            for buff_name in row_buffs
            for kw in ("气魄", "morale", "战意", "集中力", "focus")
        )
        if not has_morale_icon:
            warnings.append(
                f"规则2（进战战意/集中力）：角色「{name}」拥有进战获得战意/集中力"
                f"的技能，但跑条前截图中未检测到对应图标（可能乱速尚未完成）"
            )
            rule2_ok = False

    # ── Rule 3: Immunity gear ──
    rule3_ok = True
    if post:
        # Step A: Find characters in post-screenshot with 免疫 but no 免疫 skill
        gear_immune_names: set[str] = set()
        for c in post_chars:
            name = c.get("name", "")
            info = get_character_skill_info(name)
            if info and info.get("has_immunity_skill"):
                continue  # immunity comes from skill, not gear
            row_buffs = _buffs_for_char(post_chars, post_row_buffs, name)
            if any("免疫" in bn for bn in row_buffs):
                gear_immune_names.add(name)

        # Step B: For each gear-immune character, check pre-screenshot
        for name in gear_immune_names:
            row_buffs = _buffs_for_char(pre_chars, pre_row_buffs, name)
            if not any("免疫" in bn for bn in row_buffs):
                warnings.append(
                    f"规则3（免疫套装）：角色「{name}」穿戴免疫套装（跑条后检测到免疫，"
                    f"且无免疫技能），但跑条前截图中未检测到免疫图标（可能乱速尚未完成）"
                )
                rule3_ok = False

    is_valid = rule1_ok and rule2_ok and rule3_ok
    return is_valid, warnings


# ── Action gauge skill summary ──────────────────────────────────────


def _summarize_action_gauge_skills(characters: list[dict]) -> list[dict]:
    """Return a list of characters with action-gauge (拉条/推条) skills.

    Each entry: ``{"name": str, "skills": [{"name": str, "des": str}]}``.
    """
    result: list[dict] = []
    for c in characters:
        name = c.get("name", "")
        info = get_character_skill_info(name)
        if info and info.get("has_action_gauge_skill"):
            result.append({
                "name": name,
                "skills": info.get("action_gauge_skills", []),
            })
    return result


# ── Output formatting ───────────────────────────────────────────────


def _format_single_result(result: dict, warnings: list[str]) -> str:
    """Format a single-screenshot result as JSON."""
    characters = result.get("characters", [])
    allies = [c for c in characters if c.get("side") == "ally"]
    enemies = [c for c in characters if c.get("side") == "enemy"]
    unknown = [c for c in characters if c.get("side") == "unknown"]

    raw_lines = _build_raw_format(characters, allies, enemies)

    return json.dumps({
        "phase": result.get("phase", "unknown"),
        "mode": "single",
        "characters": characters,
        "allies": allies,
        "enemies": enemies,
        "unknown_side": unknown,
        "warnings": warnings,
        "raw_format": raw_lines,
        "note": "仅提供了一张截图，缺少初始行动值对比。如需测速请提供两张截图（跑条前+跑条后）。",
    }, ensure_ascii=False, indent=2)


def _format_pair_result(
    pre: dict, post: dict, warnings: list[str],
    pre_valid: bool | None = None,
    action_gauge_skills: list[dict] | None = None,
) -> str:
    """Format a two-screenshot (pre+post) result as JSON."""
    # Key by (name, side) so mirror/团战 rows — where the SAME character
    # legitimately appears on both 我方 and 敌方 — are preserved as distinct
    # entries instead of collapsing into one ambiguous row.
    def _key(c):
        return (c.get("name", ""), c.get("side", "unknown"))

    pre_chars = {_key(c): c for c in pre.get("characters", [])}
    post_chars = {_key(c): c for c in post.get("characters", [])}

    merged = []
    all_keys = set(pre_chars.keys()) | set(post_chars.keys())

    for key in all_keys:
        pre_c = pre_chars.get(key)
        post_c = post_chars.get(key)
        name, side = key
        merged.append({
            "name": name,
            "side": side,
            "init_action_value": pre_c.get("action_value") if pre_c else None,
            "current_action_value": post_c.get("action_value") if post_c else None,
        })

    allies = [c for c in merged if c.get("side") == "ally"]
    enemies = [c for c in merged if c.get("side") == "enemy"]
    unknown = [c for c in merged if c.get("side") == "unknown"]

    # Check for name mismatches between pre and post (keyed by name+side).
    pre_only = set(pre_chars.keys()) - set(post_chars.keys())
    post_only = set(post_chars.keys()) - set(pre_chars.keys())
    if pre_only or post_only:
        def _fmt(k):
            name, side = k
            side_cn = {"ally": "我方", "enemy": "敌方"}.get(side, side)
            return f"{name}({side_cn})"

        mismatch = []
        if pre_only:
            mismatch.append(f"仅跑条前出现: {', '.join(sorted(_fmt(k) for k in pre_only))}")
        if post_only:
            mismatch.append(f"仅跑条后出现: {', '.join(sorted(_fmt(k) for k in post_only))}")
        warnings.extend(mismatch)

    raw_lines = _build_raw_format(merged, allies, enemies)

    result = {
        "phase": "pair",
        "mode": "pair",
        "screenshot_phases": [
            pre.get("phase", "unknown"), post.get("phase", "unknown")
        ],
        "characters": merged,
        "allies": allies,
        "enemies": enemies,
        "unknown_side": unknown,
        "warnings": warnings,
        "raw_format": raw_lines,
    }

    if pre_valid is not None:
        result["pre_valid"] = pre_valid
        if not pre_valid:
            result["pre_valid_note"] = (
                "跑条前截图可能无效（乱速尚未完成），"
                "初始行动值不可靠，无法用于测速计算。请等待乱速完成后重新截图。"
            )

    if action_gauge_skills:
        result["action_gauge_skills"] = action_gauge_skills
        result["action_gauge_note"] = (
            "部分角色拥有拉条/推条技能（见 action_gauge_skills），"
            "行动值差值可能需要修正后才能用于测速。"
            "请根据技能描述判断是否影响了本次跑条的行动值变化。"
        )

    return json.dumps(result, ensure_ascii=False, indent=2)


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