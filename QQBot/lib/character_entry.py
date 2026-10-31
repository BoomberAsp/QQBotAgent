"""游戏数据 → 角色卡片 entry（供 bot 运行时使用）。

`tools/character_detail.py` 原来的卡片数据只来自 wiki 缓存
（`character_details.json`），导致：
  * wiki 还没收录的角色（如 H806 蜜拉贝儿）查不到 → 卡面直接失败；
  * 已收录角色用的是 wiki 文案/面板，与解包数据不一致。

这里复用 `scripts/render_skill_cards.py` 的组装逻辑（同一份 join + 系数换算），
让运行时与批量渲染产出完全一致的卡片。
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

from lib.paths import wiki_cache_dir


def _gd_dir() -> str:
    return os.environ.get("GAMEDATA_DIR") or ""


@lru_cache(maxsize=1)
def _bundle() -> dict:
    """加载 gamedata + wiki 详情，构建一次映射（进程内缓存）。"""
    gd = _gd_dir()
    if not gd:
        return {}
    try:
        from scripts.render_skill_cards import build_entry, build_maps, load_gamedata
    except Exception:  # noqa: BLE001
        return {}
    try:
        data = load_gamedata(gd)
    except Exception:  # noqa: BLE001
        return {}
    wiki_entries = {}
    path = os.path.join(wiki_cache_dir(), "character_details.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                wiki_entries = (json.load(f) or {}).get("data") or {}
        except Exception:  # noqa: BLE001
            wiki_entries = {}
    try:
        build_maps(data.get("roles") or {}, wiki_entries)
    except Exception:  # noqa: BLE001
        pass
    return {"data": data, "wiki": wiki_entries, "build_entry": build_entry}


def invalidate() -> None:
    _bundle.cache_clear()
    _name_index.cache_clear()


@lru_cache(maxsize=1)
def _name_index() -> dict:
    """中文名/英文名 → 角色 ID（精确匹配优先于别名，避免 蜜拉贝儿→圣诞的蜜拉贝儿）。"""
    b = _bundle()
    roles = ((b.get("data") or {}).get("roles")) or {}
    idx: dict[str, str] = {}
    for rid, g in roles.items():
        for nm in (g.get("names") or {}).values():
            if nm:
                idx.setdefault(str(nm).strip(), rid)
    return idx


def resolve(name_or_id: str) -> str | None:
    if not name_or_id:
        return None
    b = _bundle()
    roles = ((b.get("data") or {}).get("roles")) or {}
    t = name_or_id.strip()
    if t in roles:
        return t
    # ① 官方名精确匹配（「蜜拉贝儿」应当命中 H806 而不是 H146 圣诞的蜜拉贝儿）
    hit = _name_index().get(t)
    if hit:
        return hit
    # ② 别名解析（面板 character_dic + 产物别名）
    try:
        from lib.gamedata_source import get_source
        return get_source().resolve_role(t)
    except Exception:  # noqa: BLE001
        return None


def build_entry(name_or_id: str) -> dict | None:
    """返回可直接喂给 card_renderer 的 entry；解析不到/数据不足时 None。"""
    b = _bundle()
    if not b:
        return None
    rid = resolve(name_or_id)
    if not rid:
        return None
    roles = (b["data"].get("roles") or {})
    g = roles.get(rid)
    if not g:
        return None
    wiki = None
    for _name, e in (b.get("wiki") or {}).items():
        if (e.get("id") or "").strip() == rid:
            wiki = e
            break
    try:
        entry = b["build_entry"](rid, g, wiki, b["data"])
    except Exception:  # noqa: BLE001
        return None
    if not entry.get("skills") and not wiki:
        return None
    return entry
