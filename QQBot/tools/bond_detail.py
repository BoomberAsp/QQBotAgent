"""
Bond Detail — Lookup and render Ark Re:Code bond (羁绊) details.

Loads data/wiki_cache/bond_details.json (produced by
WikiScraper.refresh_bonds()) and renders a readable Chinese text card covering
class/stars/ATK/HP, description, bond skill, notes, and the economy footer
(obtain / sell price / XP value / release date).

Entry points:
- lookup_bond(query): resolve a name/alias/id to a detail entry.
- format_bond(entry): render an entry to text.
- bond_detail(bond_name): agent-tool entry combining both.
"""

import asyncio
import json
import os

from tools.name_resolver import get_resolver


_CACHE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "data", "wiki_cache", "bond_details.json"
)

# Module-level lazy index (rebuilt only once per process)
_data: dict | None = None
_by_cn_name: dict = {}
_by_title: dict = {}
_by_id: dict = {}

# In-flight guard so a slow background refresh isn't triggered concurrently.
_refreshing = False


def _load() -> dict:
    """Lazily load bond_details.json and rebuild lookup indexes."""
    global _data, _by_cn_name, _by_title, _by_id
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

    _by_cn_name = {}
    _by_title = {}
    _by_id = {}
    for entry in _data.values():
        if not isinstance(entry, dict):
            continue
        cn = entry.get("name_cn")
        if cn:
            _by_cn_name[cn] = entry
        title = entry.get("title")
        if title:
            _by_title[title.strip().lower()] = entry
        cid = entry.get("id")
        if cid:
            _by_id[str(cid).strip().upper()] = entry

    return _data


def lookup_bond(query: str) -> dict | None:
    """Resolve a bond name/alias/id to a detail entry, or None."""
    if not query or not query.strip():
        return None
    q = query.strip()

    _load()

    # 1. Exact Chinese name
    if q in _by_cn_name:
        return _by_cn_name[q]

    # 2. Exact ID match (case-insensitive)
    if q.upper() in _by_id:
        return _by_id[q.upper()]

    # 3. Exact English title match
    if q.lower() in _by_title:
        return _by_title[q.lower()]

    # 4. Fuzzy alias via pinyin resolver
    try:
        resolved = get_resolver().resolve_bond(q)
    except Exception:
        resolved = None
    if resolved and resolved in _by_cn_name:
        return _by_cn_name[resolved]

    return None


def _indent(text: str, prefix: str = "    ") -> str:
    """Indent every non-empty line of a multi-line string."""
    if not text:
        return ""
    return "\n".join(prefix + ln for ln in text.splitlines() if ln)


def format_bond(entry: dict) -> str:
    """Render a bond detail entry to a readable Chinese text card."""
    lines = []

    name = entry.get("name_cn") or entry.get("title", "")
    stars = "★" * int(entry.get("stars", 0) or 0)
    header = f"【{name}】{stars}"

    meta_parts = [x for x in (entry.get("class_cn"),) if x]
    if meta_parts:
        header += " | " + " · ".join(meta_parts)
    lines.append(header)

    idv = entry.get("id", "")
    if idv:
        lines.append(f"ID: {idv}")

    if entry.get("member"):
        lines.append(f"关联角色: {entry['member']}")

    atk = f"{entry.get('atk_base', '')} / {entry.get('atk_max', '')}".strip(" /")
    hp = f"{entry.get('hp_base', '')} / {entry.get('hp_max', '')}".strip(" /")
    stat_parts = [p for p in (f"攻击 {atk}" if atk else "", f"生命 {hp}" if hp else "") if p]
    if stat_parts:
        lines.append(" | ".join(stat_parts))

    if entry.get("desc"):
        lines.append(f"简介: {entry['desc']}")

    if entry.get("effect"):
        lines.append("羁绊技能:")
        lines.append(_indent(entry["effect"]))

    if entry.get("notes"):
        lines.append(f"备注: {entry['notes']}")

    footer = []
    if entry.get("obtain"):
        footer.append(f"获取方式: {entry['obtain']}")
    if entry.get("unsellable"):
        footer.append("出售价格: 不可出售")
    else:
        footer.append(
            f"出售价格: 金币 +{entry.get('sell_gold', '')} · 记忆碎片 +{entry.get('sell_fragment', '')}"
        )
        footer.append(f"经验值: {entry.get('xp_value', '')}")
    if entry.get("release"):
        footer.append(f"上线时间: {entry['release']}")
    if footer:
        lines.append("\n".join(footer))

    return "\n".join(lines)


async def bond_detail(bond_name: str) -> str:
    """Agent tool: return the detailed bond card for a name/alias.

    Renders a PNG card and sends it directly to the chat (via the ``_send_msg``
    contextvar, only available inside the agent loop), while still returning the
    text detail so the LLM can reason over it.

    Args:
        bond_name: Bond name or alias (e.g. 「驰骋的快感」「复活甲」).
    """
    text, card_path = await bond_detail_with_card(bond_name)
    if card_path:
        await _send_card_image(card_path)
        text += (
            "\n\n（羁绊卡片图片已直接发送到聊天，回复时无需重复详情内容，"
            "简短说明即可。）"
        )
    return text


async def bond_detail_with_card(bond_name: str) -> tuple[str, str | None]:
    """Return ``(text_detail, card_png_path_or_None)`` for a name/alias.

    Shared by the agent tool and the direct ``/羁绊详情`` command so both
    render the card through the same lookup + refresh path.
    """
    await _maybe_refresh_bonds()

    entry = lookup_bond(bond_name)
    if not entry:
        return (
            f"未找到羁绊「{bond_name}」的详情。\n"
            "你可以尝试使用其他别名（如英文名、称号、谐音），或检查名称是否有误。"
            "（若羁绊库为空，可能正在首次拉取数据，请稍后重试。）",
            None,
        )
    return format_bond(entry), render_card(entry)


def render_card(entry: dict) -> str | None:
    """Render a bond card PNG; return its path, or None on failure."""
    try:
        from tools.card_renderer import render_bond_card
        return render_bond_card(entry)
    except Exception:
        return None


async def _send_card_image(card_path: str):
    """Send a card image to QQ via the ``_send_msg`` contextvar (tool context)."""
    try:
        from agent.context import _send_msg
        from nonebot.adapters.onebot.v11 import MessageSegment
        send = _send_msg.get()
        if send is not None:
            await send(MessageSegment.image(f"file://{card_path}"))
    except Exception:
        pass


def _invalidate():
    """Drop the in-memory index so the next lookup reloads the fresh cache."""
    global _data
    _data = None


async def _maybe_refresh_bonds():
    """Kick off a background refresh if the bond cache is stale.

    Fire-and-forget (like character_detail's background refresh) so the current
    request isn't blocked by the slow scrape+translate pipeline. Any failure
    here is swallowed — the lookup still proceeds with cached data.
    """
    global _refreshing
    if _refreshing:
        return

    try:
        from tools.wiki_scraper import WikiScraper
        from lib.model_router import model_router

        scraper = WikiScraper(llm_client=model_router.flash_client)
        if not scraper.is_bonds_stale():
            return

        _refreshing = True
        asyncio.create_task(_do_refresh(scraper))
    except Exception:
        pass


async def _do_refresh(scraper):
    """Run refresh_bonds(), then invalidate the in-memory index."""
    global _refreshing
    try:
        result = await scraper.refresh_bonds()
        if result:
            _invalidate()
    except Exception:
        pass
    finally:
        _refreshing = False
