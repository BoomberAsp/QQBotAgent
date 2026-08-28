"""
Character Detail — Lookup and render Ark Re:Code character details.

Loads data/wiki_cache/character_details.json (produced by
WikiScraper.refresh_characters()) and renders a readable Chinese text card
covering stats (growth ratios), skills, multipliers, discipline and potential.

Entry points:
- lookup_character(query): resolve a name/alias/id to a detail entry.
- format_character(entry): render an entry to text.
- character_detail(character_name): agent-tool entry combining both.
"""

import asyncio
import json
import os
import sys

from tools.name_resolver import get_resolver


_CACHE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "wiki_cache", "character_details.json"
)

# Module-level lazy index (rebuilt only once per process)
_data: dict | None = None
_by_cn_name: dict = {}
_by_cn_name_norm: dict = {}
_by_title: dict = {}
_by_id: dict = {}


def _normalize_name(name: str) -> str:
    """Normalize a character name for tolerant matching.

    Strips whitespace and the decorative particle ``的`` (users type 新生的伊娥丝
    for canonical 新生伊娥丝, 新婚的伊娥丝 for 新婚伊娥丝, …) and lowercases, so
    minor spelling variants still land on the right entry.
    """
    return "".join(ch for ch in (name or "").lower() if ch not in "的 \u3000·")

# In-flight guard so a slow background refresh isn't triggered concurrently
# by multiple incoming requests (LLM translation of ~200 chars is expensive).
_refreshing = False

# One-shot-per-process flag for the image backfill check (existence checks are
# cheap but there is no reason to repeat them on every request).
_images_ensured = False


def _load() -> dict:
    """Lazily load character_details.json and rebuild lookup indexes."""
    global _data, _by_cn_name, _by_cn_name_norm, _by_title, _by_id
    if _data is not None:
        return _data

    _data = {}
    if os.path.exists(_CACHE_PATH):
        try:
            with open(_CACHE_PATH, "r", encoding="utf-8") as f:
                cache = json.load(f)
            _data = cache.get("data", {})
        except Exception:
            _data = {}

    # Canonical names from the alias dictionary (character_dic.json maps
    # H-codes / English titles / nicknames → canonical Chinese name). Cached
    # name_cn values can drift from these (old LLM translations), which breaks
    # alias lookup — so also index every entry under its canonical name.
    try:
        alias_map = get_resolver().character_alias_map()
    except Exception:
        alias_map = {}

    _by_cn_name = {}
    _by_cn_name_norm = {}
    _by_title = {}
    _by_id = {}
    for entry in _data.values():
        if not isinstance(entry, dict):
            continue
        cn = entry.get("name_cn") or entry.get("title")
        if cn:
            _by_cn_name[cn] = entry
        title = entry.get("title")
        if title:
            _by_title[title.strip().lower()] = entry
        cid = entry.get("id")
        if cid:
            _by_id[str(cid).strip().upper()] = entry
        # Canonical cross-reference (setdefault: a true name_cn owner wins).
        canonical = alias_map.get(str(cid).strip().lower()) or \
            (alias_map.get(title.strip().lower()) if title else None)
        if canonical:
            _by_cn_name.setdefault(canonical, entry)
            _by_cn_name_norm.setdefault(_normalize_name(canonical), entry)
        if cn:
            _by_cn_name_norm.setdefault(_normalize_name(cn), entry)

    return _data


def lookup_character(query: str) -> dict | None:
    """Resolve a character name/alias/id to a detail entry, or None."""
    if not query or not query.strip():
        return None
    q = query.strip()

    _load()

    # 1. Exact Chinese name / canonical name
    if q in _by_cn_name:
        return _by_cn_name[q]

    # 2. Exact ID match (case-insensitive)
    if q.upper() in _by_id:
        return _by_id[q.upper()]

    # 3. Exact English title match
    if q.lower() in _by_title:
        return _by_title[q.lower()]

    # 4. Normalized match (ignores 的/whitespace: 新生的伊娥丝 → 新生伊娥丝)
    qn = _normalize_name(q)
    if qn and qn in _by_cn_name_norm:
        return _by_cn_name_norm[qn]

    # 5. Fuzzy alias via pinyin resolver (also retry without 的 so canonical
    #    aliases like 新婚伊娥丝 match inputs like 新婚的伊娥丝)
    resolver = get_resolver()
    candidates = [q]
    q_stripped = q.replace("的", "")
    if q_stripped != q:
        candidates.append(q_stripped)
    for cand in candidates:
        try:
            resolved = resolver.resolve_character(cand)
        except Exception:
            resolved = None
        if resolved:
            entry = _by_cn_name.get(resolved) or _by_cn_name_norm.get(
                _normalize_name(resolved))
            if entry:
                return entry

    return None


def _indent(text: str, prefix: str = "    ") -> str:
    """Indent every non-empty line of a multi-line string."""
    if not text:
        return ""
    return "\n".join(prefix + ln for ln in text.splitlines() if ln)


def format_character(entry: dict) -> str:
    """Render a character detail entry to a readable Chinese text card."""
    lines = []

    name = entry.get("name_cn") or entry.get("title", "")
    stars = "★" * int(entry.get("stars", 0) or 0)
    header = f"【{name}】{stars}"

    meta_parts = [x for x in (entry.get("element"), entry.get("class_cn")) if x]
    if entry.get("constellation"):
        meta_parts.append(entry.get("constellation"))
    if meta_parts:
        header += " | " + " · ".join(meta_parts)
    lines.append(header)

    # Identity
    idv = entry.get("id", "")
    if idv:
        lines.append(f"ID: {idv}")

    # Personal info
    personal = []
    for label, key in (
        ("身高", "height"),
        ("体重", "weight"),
        ("生日", "birthday"),
        ("胸围", "breast"),
        ("上线", "release"),
    ):
        val = entry.get(key)
        if val:
            personal.append(f"{label} {val}")
    if personal:
        lines.append(" | ".join(personal))

    # Description
    if entry.get("desc"):
        lines.append(f"简介: {entry['desc']}")

    # Stats (growth ratios + Lv.60 max)
    stats = entry.get("stats") or {}
    stats_max = entry.get("stats_max") or {}
    if stats_max:
        max_parts = [f"{k} {v}" for k, v in stats_max.items() if v]
        ratio_parts = [f"{k} {v}" for k, v in stats.items() if v]
        if max_parts:
            lines.append("面板(Lv.60): " + " | ".join(max_parts))
        if ratio_parts:
            lines.append("成长系数: " + " | ".join(ratio_parts))
    else:
        stat_parts = [f"{k} {v}" for k, v in stats.items() if v]
        if stat_parts:
            lines.append("面板(成长系数): " + " | ".join(stat_parts))

    # Skills
    skills = entry.get("skills") or []
    if skills:
        lines.append("技能:")
        for i, sk in enumerate(skills, 1):
            sk_name = sk.get("name") or sk.get("name_en", "")
            sk_type = sk.get("type", "")
            meta = []
            if sk_type:
                meta.append(sk_type)
            if sk.get("soul"):
                meta.append(f"星尘 {sk['soul']}")
            if sk.get("cd"):
                meta.append(f"冷却 {sk['cd']}")
            if sk.get("focus"):
                meta.append(f"专注 {sk['focus']}")
            head = f"  [{i}] {sk_name}"
            if meta:
                head += " (" + " | ".join(meta) + ")"
            lines.append(head)

            if sk.get("des"):
                lines.append(_indent(sk["des"]))
            if sk.get("des2"):
                lines.append(_indent(sk["des2"]))
            if sk.get("multi"):
                lines.append("    倍率:")
                lines.append(_indent(sk["multi"], "      "))
            if sk.get("burst"):
                burst = f"    Burst: {sk['burst']}"
                if sk.get("burst_cost"):
                    burst += f" (消耗 {sk['burst_cost']})"
                lines.append(burst)

    # Discipline (talent)
    discs = entry.get("discs") or []
    shown = [(i + 1, d) for i, d in enumerate(discs) if d]
    if shown:
        lines.append("天赋:")
        for lv, d in shown:
            lines.append(f"  {lv}. {d}")

    # Potential
    pot_parts = []
    if entry.get("team_pot"):
        pot_parts.append(f"团队 {entry['team_pot']}")
    if entry.get("self_pot"):
        pot_parts.append(f"自身 {entry['self_pot']}")
    if pot_parts:
        lines.append("潜能: " + " / ".join(pot_parts))

    return "\n".join(lines)


async def character_detail(character_name: str) -> str:
    """Agent tool: return the detailed character card for a name/alias.

    Renders a PNG card and sends it directly to the chat (via the ``_send_msg``
    contextvar, only available inside the agent loop), while still returning the
    text detail so the LLM can reason over it.

    Args:
        character_name: Character name or alias (e.g. 「夏妮」「狼团长」「Shani」).
    """
    text, card_path = await character_detail_with_card(character_name)
    if card_path:
        sent = await _send_card_image(card_path)
        if sent:
            text += (
                "\n\n（角色卡片图片已直接发送到聊天，回复时无需重复详情内容，"
                "简短说明即可。）"
            )
    return text


async def character_detail_with_card(character_name: str) -> tuple[str, str | None]:
    """Return ``(text_detail, card_png_path_or_None)`` for a name/alias.

    Shared by the agent tool and the direct ``/角色详情`` command so both
    render the card through the same lookup + refresh path.
    """
    await _maybe_refresh_characters()

    entry = lookup_character(character_name)
    if not entry:
        return (
            f"未找到角色「{character_name}」的详情。\n"
            "你可以尝试使用其他别名（如英文名、称号、谐音），或检查名称是否有误。"
            "（若角色库为空，可能正在首次拉取数据，请稍后重试。）",
            None,
        )
    return format_character(entry), render_card(entry)


def render_card(entry: dict) -> str | None:
    """Render a character card PNG (if stale); return its path, or None."""
    try:
        from tools.card_renderer import render_character_card_if_stale
        return render_character_card_if_stale(entry)
    except Exception:
        return None


async def _send_card_image(card_path: str) -> bool:
    """Send a card image to QQ via the ``_send_msg`` contextvar (tool context).

    Returns True on success, False if the contextvar is missing or the send
    fails (logged so a silent "text says sent, no image arrived" mismatch is
    diagnosable).
    """
    try:
        from agent.context import _send_msg
        from nonebot.adapters.onebot.v11 import MessageSegment
        from pathlib import Path
        send = _send_msg.get()
        if send is None:
            print("[character_detail] _send_msg contextvar not set; image not sent",
                  file=sys.stderr)
            return False
        await send(MessageSegment.image(Path(card_path)))
        return True
    except Exception as e:
        print(f"[character_detail] card image send failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        return False


def _invalidate():
    """Drop the in-memory index so the next lookup reloads the fresh cache."""
    global _data
    _data = None


async def _maybe_refresh_characters():
    """Refresh the character cache if it's stale or missing.

    Stale-but-present cache → background (fire-and-forget) refresh so the
    current request isn't blocked. Missing cache (first build) → block so the
    current request returns real data instead of "not found". Fresh cache →
    one-shot image backfill check (downloads only run during the weekly
    refresh otherwise, so a filename-convention fix would never reach disk).
    """
    global _refreshing, _images_ensured
    if _refreshing:
        return

    try:
        from tools.wiki_scraper import WikiScraper
        from lib.model_router import model_router

        scraper = WikiScraper(llm_client=model_router.flash_client)
        if not scraper.is_characters_stale():
            if not _images_ensured:
                _images_ensured = True
                asyncio.create_task(_ensure_images(scraper))
            return

        _refreshing = True
        if os.path.exists(_CACHE_PATH):
            asyncio.create_task(_do_refresh(scraper))
        else:
            await _do_refresh(scraper)
    except Exception:
        _refreshing = False


async def _ensure_images(scraper):
    """Fire-and-forget backfill of missing character art + card re-render."""
    try:
        await scraper.ensure_character_images()
    except Exception as e:
        print(f"[character_detail] image backfill failed: "
              f"{type(e).__name__}: {e}", file=sys.stderr)


async def _do_refresh(scraper):
    """Run refresh_characters(), then invalidate the in-memory index."""
    global _refreshing
    try:
        result = await scraper.refresh_characters()
        if result:
            _invalidate()
    except Exception:
        pass
    finally:
        _refreshing = False
