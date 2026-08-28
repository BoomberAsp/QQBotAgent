"""
Wiki Scraper — Scrape Ark Re:Code Wiki for gacha data.

Fetches character and bond data from the official wiki using MediaWiki API,
caches results locally, and refreshes only when the cache is older than
Beijing time yesterday 18:00.

Data sources:
  - Template:Member (208 pages) → character pools
  - Template:Bond   (138 pages) → 3★/4★ bond pools (5★ bonds derived from characters)
  - Static config/gacha_data.json → banner probability weights (merged)
"""

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from curl_cffi import requests as cffi_requests

from lib.status_icons import STATUS_ICON_CN, STATUS_ICON_DIR


# ── Constants ────────────────────────────────────────────────────

WIKI_API = "https://arkrecodewiki.miraheze.org/w/api.php"
# Outbound HTTP proxy used to reach the wiki. Miraheze blocks the Tencent Cloud
# IP at the Cloudflare layer (HTTP 403 even with browser TLS impersonation), so
# requests are routed through the sing-box HTTP inbound on the host
# (127.0.0.1:1081). Set WIKI_PROXY="" to force a direct connection.
WIKI_PROXY = os.environ.get("WIKI_PROXY", "http://127.0.0.1:1081")
STATIC_CONFIG = os.path.join(
    os.path.dirname(__file__), "..", "config", "gacha_data.json"
)
CACHE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data", "wiki_cache"
)
CACHE_FILE = os.path.join(CACHE_DIR, "gacha.json")
TRANSLATION_CACHE = os.path.join(CACHE_DIR, "name_mapping.json")
CHARACTER_DETAIL_CACHE = os.path.join(CACHE_DIR, "character_details.json")
BOND_DETAIL_CACHE = os.path.join(CACHE_DIR, "bond_details.json")
BATTLE_MECHANICS_CACHE = os.path.join(CACHE_DIR, "battle_mechanics.json")

# Character art caches (portrait / head icon / skill icons), keyed by id/title.
PORTRAIT_DIR = os.path.join(CACHE_DIR, "portraits")
ICON_DIR = os.path.join(CACHE_DIR, "icons")
SKILL_ICON_DIR = os.path.join(CACHE_DIR, "skill_icons")
BOND_ICON_DIR = os.path.join(CACHE_DIR, "bond_icons")

BATCH_SIZE = 50  # MediaWiki pages per revisions query
BEIJING = timezone(timedelta(hours=8))

# Element → pool prefix mapping
LIGHT_DARK_ELEMENTS = {"Light", "Dark"}
THREE_COLOR_ELEMENTS = {"Flame", "Water", "Nature"}

# Vendored openrubi reference data — used as few-shot examples + name lookup
# for LLM translation. These are static reference files, NOT the data source.
OPENRUBI_CHAR_DIR = os.path.join(
    os.path.dirname(__file__), "..", "config", "characters"
)
OPENRUBI_CHAR_DIC = os.path.join(OPENRUBI_CHAR_DIR, "character_dic.json")
OPENRUBI_MEMBERS_INFO = os.path.join(OPENRUBI_CHAR_DIR, "members_info.json")
OPENRUBI_BONDS_INFO = os.path.join(OPENRUBI_CHAR_DIR, "bonds_info.json")

# Status-effect / game-term → Chinese (deterministic, applied before LLM translation).
# Longer phrases first so "ATK Down" is replaced before "ATK" would match inside it.
_STATUS_TERM_CN = [
    ("Effect Resistance", "效果抵抗"),
    ("Effect Hit Rate", "效果命中"),
    ("SPD Down", "速度下降"),
    ("ATK Down", "攻击力下降"),
    ("DEF Down", "防御力下降"),
    ("SPD Up", "速度提升"),
    ("ATK Up", "攻击力提升"),
    ("DEF Up", "防御力提升"),
    ("Provoke", "嘲讽"),
    ("Immunity", "免疫"),
    ("Shield", "护盾"),
    ("Stun", "眩晕"),
    ("Silence", "沉默"),
    ("Poison", "中毒"),
    ("Burn", "灼烧"),
    ("Bleed", "流血"),
    ("Recovery", "持续恢复"),
    ("Speed", "速度"),
    ("ATK", "攻击力"),
    ("DEF", "防御力"),
    ("HP", "生命值"),
    ("Additional damage", "追加伤害"),
    ("Upon hit", "技能命中时"),
    ("ACC", "（基础）命中率"),
    ("Lock On", "锁定"),
    ("Foresight", "看破"),
    ("Ignore Effect RES", "无视效果抵抗"),
    ("Astrogen", "星源力"),
    ("Flanking", "追加攻击"),
    ("Extra Turn", "额外回合"),
    ("Stealth", "潜伏"),
    ("Penetrate", "贯穿（一定防御力）"),
    ("Extinction", "灭绝"),
    ("Increase the Action Gauge", "行动值提升"),
    ("damage distribution effects", "伤害分配（分摊）效果"),
    ("ACC Up", "（基础）命中率提升"),
    ("Morale", "战意"),
    ("Injury", "创伤"),
    ("restore HP", "回复生命值"),
    ("Vigor", "气魄"),
    ("Hits", "命中的攻击"),
    ("Crit", "暴击"),
    ("Unbuffable", "无法强化"),
    ("Immortal", "不屈"),
    ("Revived", "复活"),
    ("fatal blow", "致命伤害"),
    ("removing all buffs", "驱散所有正向状态"),
    ("Seal/Passiveless", "被动无效"),
    ("ACC Down", "（基础）命中率下降"),
    ("damage taken is reduced", "伤害量下降"),
    ("Counter", "反击"),
    ("Evasion Up", "闪避率提升"),
    ("reduce the Cooldown", "冷却减少"),
    ("At the start of the battle", "进入战斗时"),
    ("Invincible", "无敌"),
    ("stealing (one/two/etc.) buff(s)", "窃取（一个/两个/等）正向状态"),
    ("Blink", "瞬动"),
    ("Bomb", "炸弹"),
    ("lower their Action Gauge", "造成行动值降低"),
    ("Ignite (the burn and bomb)", "激发"),
    ("DMG RED effect", "伤害量下降效果"),
    ("Curse", "诅咒"),
    ("Unhealable", "禁疗"),
    ("Resurgence", "回生"),
    ("Unremovable", "不可解除"),
    ("Lifesteal", "吸血"),
    ("increase their Skill Cooldowns", "技能冷却时间延长"),
    ("Sleep", "沉睡"),
    ("Confusion", "迷乱"),
    ("Focus", "集中力"),
    ("Crit RES Up", "暴击抵抗"),
    ("Defiant", "遇强则强"),
    ("Hinder", "妨碍"),
    ("Skill Nullifier", "技能免疫"),
    ("Restrict", "拘禁"),
    ("Frostburn", "冰灼"),
    ("Stellar Sigil", "星链标记"),
    ("Flanking Boost", "追击强化"),
    ("Flanking", "追击"),
    ("decreasing the duration of their buffs", "减少正向状态时间"),
    ("Guard", "守护"),
    ("fixed damage", "固定伤害"),
    ("copy buffs", "复制正向状态"),
    ("Random Buff", "随机正向状态"),
    ("extending the duration of all debuffs", "负向状态延长"),
    ("Immobilizing Debuffs", "无法行动类型负向状态"),
    ("Pain Threshold", "受伤上限"),
    ("Hibiscus Morning Dew", "扶桑晓露"),
    ("Crit DMG Up", "暴击伤害提升"),
    ("Performance Mode", "公演模式"),
    ("Arrogant Bullying", "自视甚高的欺侮"),
    ("Flow State", "心流状态"),
    ("Active", "主动技"),
    ("Passive", "被动技"),
    ("Resuscitate", "复苏"),
    ("ADD DMG RED", "追加伤害下降"),
    ("Shield Conversion", "护盾转换"),
    ("Cluster", "凝聚"),
    ("reducing the debuffs", "负向状态时间减少"),
    ("Jumpy Pumpkins", "鬼跳南瓜"),
    ("FXXK YXU", "FXXK YXU"),
    ("Targeted Taunt", "指定嘲讽"),
    ("Transfer", "转移"),
    ("rebound", "反弹"),
    ("favorable attribute", "有利属性"),
    ("attribute counter", "不利属性")
]

# Wiki stat multipliers: the wiki's {{Member}} template computes level-60 stats
# from growth ratios with these constants (verified against all 198 openrubi
# characters — zero mismatches across 792 stat values).
#   ATK:   round(ATK_ratio * 608)
#   HP:    round(HP_ratio  * 4960)
#   DEF:   round(DEF_ratio * 617)
#   SPD:   round(SPD_ratio * 100)
_STAT_MULTIPLIERS = {"ATK": 608, "HP": 4960, "DEF": 617, "SPD": 100}

# Deterministic element/class translation maps (user-confirmed)
ELEMENT_CN = {"Flame": "火", "Water": "水", "Nature": "木", "Light": "光", "Dark": "暗"}
CLASS_CN = {
    "Warrior": "战士",
    "Caster": "术士",
    "Defender": "重装",
    "Medic": "医疗",
    "Sniper": "狙击",
    "Vanguard": "先锋",
}

# Western zodiac constellation names (identity field)
CONSTELLATION_CN = {
    "Aries": "白羊座", "Taurus": "金牛座", "Gemini": "双子座",
    "Cancer": "巨蟹座", "Leo": "狮子座", "Virgo": "处女座",
    "Libra": "天秤座", "Scorpio": "天蝎座", "Scorpius": "天蝎座",
    "Sagittarius": "射手座", "Capricorn": "摩羯座", "Capricornus": "摩羯座",
    "Aquarius": "水瓶座", "Pisces": "双鱼座",
}

# openrubi Discipline stat keys → (Chinese label, is_percentage).
# Used to render openrubi's numeric Discipline dict into Chinese talent text
# for seeded entries (no LLM), closing the discs gap openrubi leaves English.
DISCIPLINE_STAT_CN = {
    "ATK%": ("攻击力", True),
    "HP%": ("生命值", True),
    "DEF%": ("防御力", True),
    "Speed": ("速度", False),
    "Effect_Hit_Rate": ("效果命中", True),
    "Effect_RES": ("效果抵抗", True),
    "Crit_Rate": ("暴击率", True),
    "Crit_DMG": ("暴击伤害", True),
}

# Bond economy values are deterministic by star count (from Template:Bond's
# {{#switch: {{{Stars}}}} markup). No wiki field stores them — they are
# computed at render time, so we reproduce the mapping here.
BOND_SELL_GOLD = {"5": "12500", "4": "4200", "3": "2100"}
BOND_SELL_FRAGMENT = {"5": "30", "4": "8", "3": "1"}
BOND_XP_VALUE = {"5": "1030", "4": "850", "3": "680"}

# Potential growth params are deterministic (never LLM-translated), so they are
# excluded from the re-translation source hash. Otherwise introducing them would
# invalidate every stored ``_src_hash`` and force a full re-translation of all
# ~208 characters. They are backfilled onto unchanged entries during refresh and
# are still part of ``_entry_card_hash``, so cards re-render when they change.
_POT_DETAIL_FIELDS = (
    "team_pot_rate", "team_pot_base", "team_pot_add",
    "self_pot_rate", "self_pot_base", "self_pot_add",
)


# ── WikiScraper ──────────────────────────────────────────────────

class WikiScraper:
    """Scrapes Ark Re:Code Wiki for gacha pool and bond data."""

    def __init__(self, llm_client=None):
        self._cache_dir = os.path.abspath(CACHE_DIR)
        self._cache_file = os.path.abspath(CACHE_FILE)
        self._translation_cache = os.path.abspath(TRANSLATION_CACHE)
        self._character_detail_cache = os.path.abspath(CHARACTER_DETAIL_CACHE)
        self._bond_detail_cache = os.path.abspath(BOND_DETAIL_CACHE)
        self._battle_mechanics_cache = os.path.abspath(BATTLE_MECHANICS_CACHE)
        self._static_config = os.path.abspath(STATIC_CONFIG)
        self.llm_client = llm_client  # For name translation (can be set later)

    def set_client(self, client):
        """Set or update the LLM client (for lazy initialization)."""
        self.llm_client = client

    # ── Public API ────────────────────────────────────────────────

    def get_cached_data(self) -> dict | None:
        """Synchronously load cached gacha data.

        Returns None only if the cache file doesn't exist or is corrupt.
        If the cache exists (even if stale), returns it — the caller gets
        the best available data. Staleness is handled separately by
        is_stale() + background refresh in gacha_pull().
        """
        if not os.path.exists(self._cache_file):
            return None

        try:
            with open(self._cache_file, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            return None

        data = cache.get("data", {})
        pools = data.get("pools", {})
        total_items = sum(len(p.get("items", [])) for p in pools.values())
        if total_items == 0:
            print("[WikiScraper] Cached data is empty, ignoring", file=sys.stderr)
            return None

        scraped_at = cache.get("scraped_at", 0)
        if self._is_stale(scraped_at):
            print(f"[WikiScraper] Using stale cache (will try background refresh)",
                  file=sys.stderr)

        return data

    def is_stale(self) -> bool:
        """Check whether the cache is stale (without loading full data)."""
        if not os.path.exists(self._cache_file):
            return True
        try:
            with open(self._cache_file, "r", encoding="utf-8") as f:
                cache = json.load(f)
            return self._is_stale(cache.get("scraped_at", 0))
        except Exception:
            return True

    def is_characters_stale(self) -> bool:
        """Check whether character_details.json is stale or missing.

        Stale = scraped_at earlier than the most recent Wednesday 20:00
        (Beijing), so character details refresh at most once a week, keyed to
        the weekly Wednesday 20:00 boundary (mirrors the gacha/redeem-cache
        pattern, just with a weekly instead of daily anchor).
        """
        if not os.path.exists(self._character_detail_cache):
            return True
        try:
            with open(self._character_detail_cache, "r", encoding="utf-8") as f:
                cache = json.load(f)
            scraped_at = cache.get("scraped_at", 0)
        except Exception:
            return True
        return scraped_at < self._most_recent_wed_20()

    def is_bonds_stale(self) -> bool:
        """Check whether bond_details.json is stale or missing.

        Same weekly Wednesday-20:00 anchor as character details.
        """
        if not os.path.exists(self._bond_detail_cache):
            return True
        try:
            with open(self._bond_detail_cache, "r", encoding="utf-8") as f:
                cache = json.load(f)
            scraped_at = cache.get("scraped_at", 0)
        except Exception:
            return True
        return scraped_at < self._most_recent_wed_20()

    def is_battle_mechanics_stale(self) -> bool:
        """Check whether battle_mechanics.json is stale or missing.

        Same weekly Wednesday-20:00 anchor as character/bond details.
        """
        if not os.path.exists(self._battle_mechanics_cache):
            return True
        try:
            with open(self._battle_mechanics_cache, "r", encoding="utf-8") as f:
                cache = json.load(f)
            scraped_at = cache.get("scraped_at", 0)
        except Exception:
            return True
        return scraped_at < self._most_recent_wed_20()

    @staticmethod
    def _most_recent_wed_20() -> float:
        """Unix timestamp of the most recent Wednesday 20:00 (Beijing).

        ``(weekday - 2) % 7`` counts days back to Wednesday; if today is
        Wednesday but before 20:00, fall back to the previous Wednesday.
        """
        now = datetime.now(BEIJING)
        days_back = (now.weekday() - 2) % 7
        wed = now.date() - timedelta(days=days_back)
        wed_20 = datetime.combine(
            wed, datetime.min.time().replace(hour=20), tzinfo=BEIJING
        )
        if now < wed_20:
            wed_20 -= timedelta(days=7)
        return wed_20.timestamp()

    async def refresh(self) -> dict:
        """Scrape wiki, merge with static config, write cache, return data."""
        # 1. Fetch all Member and Bond pages
        member_wikitexts = await self._fetch_template_pages("Member")
        bond_wikitexts = await self._fetch_template_pages("Bond")

        # 2. Parse templates
        members = []
        for title, wikitext in member_wikitexts:
            parsed = self._parse_member_template(title, wikitext)
            if parsed:
                members.append(parsed)

        bonds = []
        for title, wikitext in bond_wikitexts:
            parsed = self._parse_bond_template(title, wikitext)
            if parsed:
                bonds.append(parsed)

        print(f"[WikiScraper] Parsed {len(members)} members, {len(bonds)} bonds", file=sys.stderr)

        # 3. Classify into pools
        pools = self._classify_members(members)
        self._classify_bonds(bonds, pools)

        total_items = sum(len(p["items"]) for p in pools.values())
        print(f"[WikiScraper] Classified into {len(pools)} pools, {total_items} total items", file=sys.stderr)

        # Guard: don't cache empty results (indicates a fetch failure)
        if total_items == 0:
            print("[WikiScraper] WARNING: No items scraped, discarding empty result", file=sys.stderr)
            return self._load_static_fallback()

        # 4. Translate English names to Chinese (hybrid: cache + LLM)
        await self._translate_names(pools)

        # 5. Merge with static config (banner probabilities)
        result = self._merge_with_static(pools)

        # 6. Write cache
        cache = {
            "scraped_at": time.time(),
            "scraped_at_iso": datetime.now(BEIJING).isoformat(),
            "source_url": WIKI_API,
            "data": result,
        }
        os.makedirs(self._cache_dir, exist_ok=True)
        with open(self._cache_file, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

        return result

    # ── Staleness ─────────────────────────────────────────────────

    def _is_stale(self, scraped_at: float) -> bool:
        """Return True if scraped_at is before Beijing time yesterday 18:00."""
        now = datetime.now(BEIJING)
        yesterday_18 = datetime.combine(
            now.date() - timedelta(days=1),
            datetime.min.time().replace(hour=18),
            tzinfo=BEIJING,
        )
        cache_time = datetime.fromtimestamp(scraped_at, tz=BEIJING)
        return cache_time < yesterday_18

    # ── Fetch ─────────────────────────────────────────────────────

    @staticmethod
    async def _fetch_json(url: str, params: dict) -> dict | None:
        """Make a GET request and return parsed JSON.

        Uses curl_cffi with browser TLS fingerprint impersonation (chrome116)
        to bypass Cloudflare bot detection. Both httpx and urllib are blocked
        by Cloudflare's TLS fingerprinting. Requests are routed through the
        outbound proxy (WIKI_PROXY) since Miraheze additionally blocks the
        server's China IP regardless of TLS fingerprint.
        """
        loop = asyncio.get_running_loop()

        def _run():
            try:
                resp = cffi_requests.get(
                    url,
                    params=params,
                    impersonate="chrome116",
                    timeout=30,
                    proxy=WIKI_PROXY or None,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                                      " AppleWebKit/537.36 (KHTML, like Gecko)"
                                      " Chrome/116.0.0.0 Safari/537.36",
                    },
                )
                if resp.status_code != 200:
                    preview = resp.text[:200].replace("\n", " ")
                    print(f"[WikiScraper] HTTP {resp.status_code}: {preview}",
                          file=sys.stderr)
                    return None
                return resp.json()
            except Exception as e:
                print(f"[WikiScraper] HTTP error: {type(e).__name__}: {e}",
                      file=sys.stderr)
                return None

        return await loop.run_in_executor(None, _run)

    async def _fetch_page_wikitext(self, title: str) -> str:
        """Fetch the raw wikitext of a single page via the revisions API.

        Returns the page's latest ``slots.main["*"]`` content, or ``""`` on
        any failure (offline, missing page, HTTP error).
        """
        data = await self._fetch_json(WIKI_API, {
            "action": "query",
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "titles": title,
            "format": "json",
        })
        if not data:
            return ""
        for _pid, info in data.get("query", {}).get("pages", {}).items():
            revisions = info.get("revisions", [])
            if revisions:
                return revisions[0].get("slots", {}).get("main", {}).get("*", "")
        return ""

    async def _resolve_image_url(self, filename: str, width: int | None = None) -> str | None:
        """Resolve a ``File:`` filename to a (thumbnail) URL via imageinfo.

        Using the API avoids reconstructing MediaWiki's MD5 hash directory
        (``thumb/1/11/...``). ``width`` requests a thumbnail of that width
        (``thumburl``), otherwise the original file URL is returned.
        """
        params = {
            "action": "query",
            "prop": "imageinfo",
            "iiprop": "url",
            "titles": f"File:{filename}",
            "format": "json",
        }
        if width:
            params["iiurlwidth"] = width
        data = await self._fetch_json(WIKI_API, params)
        if not data:
            return None
        pages = data.get("query", {}).get("pages", {})
        for _pid, info in pages.items():
            for ii in info.get("imageinfo", []):
                return ii.get("thumburl") or ii.get("url")
        return None

    async def _download_image(self, url: str, dest: str) -> bool:
        """Download an image binary to ``dest`` via the outbound proxy."""
        loop = asyncio.get_running_loop()

        def _run():
            try:
                resp = cffi_requests.get(
                    url,
                    impersonate="chrome116",
                    timeout=30,
                    proxy=WIKI_PROXY or None,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                                      " AppleWebKit/537.36 (KHTML, like Gecko)"
                                      " Chrome/116.0.0.0 Safari/537.36",
                    },
                )
                if resp.status_code != 200 or not resp.content:
                    return False
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(resp.content)
                return True
            except Exception as e:
                print(f"[WikiScraper] image download error: {type(e).__name__}: {e}",
                      file=sys.stderr)
                return False

        return await loop.run_in_executor(None, _run)

    async def _background_downloads(self, tasks: list, label: str):
        """Await a batch of image downloads, swallowing any failure.

        Used by refresh_characters/refresh_bonds to run image download as a
        fire-and-forget task after the text cache has already been written.
        """
        try:
            await asyncio.gather(*tasks)
        except Exception as e:
            print(f"[WikiScraper] {label} download error: {type(e).__name__}: {e}",
                  file=sys.stderr)

    async def _background_render_cards(self, entries: list, kind: str):
        """Render character/bond cards in worker threads (fire-and-forget).

        Each entry is drawn only if its card PNG is missing or stale (content
        hash mismatch). PIL rendering is CPU-bound, so it runs via
        ``asyncio.to_thread`` with a small semaphore to keep the event loop
        responsive while cards are pre-rendered in the background.
        """
        try:
            if kind == "character":
                from tools.card_renderer import render_character_card_if_stale as render_fn
            else:
                from tools.card_renderer import render_bond_card_if_stale as render_fn
        except Exception as e:
            print(f"[WikiScraper] card renderer unavailable: {type(e).__name__}: {e}",
                  file=sys.stderr)
            return

        sem = asyncio.Semaphore(2)

        async def _one(entry: dict):
            try:
                async with sem:
                    await asyncio.to_thread(render_fn, entry)
            except Exception as e:
                print(f"[WikiScraper] card render error: {type(e).__name__}: {e}",
                      file=sys.stderr)

        await asyncio.gather(*(_one(e) for e in entries), return_exceptions=True)

    async def _background_download_then_render(self, download_tasks: list, label: str,
                                               entries: list, kind: str):
        """Download images first, then render cards — serialized, non-fatal.

        Rendering before the portrait/icon lands produces a placeholder tile
        (first char of name), and the content hash never changes when the image
        later arrives, so the placeholder would be cached permanently. Awaiting
        the downloads before rendering removes that race. Still fire-and-forget
        from the caller's perspective (wrapped in ``asyncio.create_task``).
        """
        await self._background_downloads(download_tasks, label)
        await self._background_render_cards(entries, kind)

    async def _download_character_images(self, entry: dict, semaphore: asyncio.Semaphore):
        """Download portrait / head icon / skill icons for one character.

        Idempotent: skips files already on disk. Filenames follow the wiki
        convention (``Template:Member`` renders the head icon as
        ``Icon_Head_S_{{{ID}}}.png``):
          - portrait : ``{ID}_90_Emo1_normal_LV1.png``
          - icon     : ``Icon_Head_S_{ID}.png``
          - skills   : ``Icon_Skill_{ID}_{001|002|003}.png``
        """
        cid = str(entry.get("id", "")).strip()
        title = str(entry.get("title", "")).strip()
        if not cid:
            return

        targets = []
        if cid:
            targets.append(
                (f"{cid}_90_Emo1_normal_LV1.png",
                 os.path.join(PORTRAIT_DIR, f"{cid}.png"), 600)
            )
        if cid:
            targets.append(
                (f"Icon_Head_S_{cid}.png",
                 os.path.join(ICON_DIR, f"{cid}.png"), None)
            )
        for n in range(1, 4):
            targets.append(
                (f"Icon_Skill_{cid}_{n:03d}.png",
                 os.path.join(SKILL_ICON_DIR, f"{cid}_{n}.png"), None)
            )

        for filename, dest, width in targets:
            if os.path.exists(dest):
                continue
            async with semaphore:
                url = await self._resolve_image_url(filename, width)
                if url:
                    await self._download_image(url, dest)

    @staticmethod
    def _character_art_missing(entry: dict) -> bool:
        """True if any expected character image file is absent on disk."""
        cid = str(entry.get("id", "")).strip()
        if not cid:
            return False
        expected = [os.path.join(PORTRAIT_DIR, f"{cid}.png"),
                    os.path.join(ICON_DIR, f"{cid}.png")]
        expected.extend(
            os.path.join(SKILL_ICON_DIR, f"{cid}_{n}.png") for n in range(1, 4)
        )
        return any(not os.path.exists(p) for p in expected)

    async def ensure_character_images(self) -> int:
        """Backfill missing character art without re-scraping or re-translating.

        Image downloads normally only run during the weekly cache refresh, so a
        filename-convention fix (or an interrupted download batch) leaves cards
        stuck on placeholder tiles until the next anchor. This method is
        idempotent and cheap (existence checks only) and downloads just the
        missing files, then re-renders the affected cards. Returns the number
        of characters that needed backfill.
        """
        existing = self._load_character_cache()
        if not existing:
            return 0
        missing = [
            e for e in existing.values()
            if isinstance(e, dict) and self._character_art_missing(e)
        ]
        if not missing:
            return 0
        print(f"[WikiScraper] backfilling art for {len(missing)} characters",
              file=sys.stderr)
        sem = asyncio.Semaphore(4)
        tasks = [self._download_character_images(e, sem) for e in missing]
        await self._background_download_then_render(
            tasks, "character art backfill", missing, "character"
        )
        return len(missing)

    async def _fetch_template_pages(self, template_name: str) -> list[tuple[str, str]]:
        """Fetch wikitext for all pages embedding a given template.

        Returns a list of (title, wikitext) tuples.
        """
        # Step 1: Get all page titles using embeddedin
        titles = await self._get_embedded_titles(template_name)
        print(f"[WikiScraper] Found {len(titles)} pages for Template:{template_name}", file=sys.stderr)
        if not titles:
            return []

        # Step 2: Batch-fetch wikitext
        results = []
        for i in range(0, len(titles), BATCH_SIZE):
            batch = titles[i : i + BATCH_SIZE]
            joined = "|".join(batch)
            data = await self._fetch_json(WIKI_API, {
                "action": "query",
                "prop": "revisions",
                "rvprop": "content",
                "rvslots": "main",
                "titles": joined,
                "format": "json",
            })
            if not data:
                continue

            pages = data.get("query", {}).get("pages", {})
            for _pageid, info in pages.items():
                title = info.get("title", "")
                revisions = info.get("revisions", [])
                if revisions:
                    content = revisions[0].get("slots", {}).get("main", {}).get("*", "")
                    results.append((title, content))

        return results

    async def _get_embedded_titles(self, template_name: str) -> list[str]:
        """Get all page titles that embed a given template."""
        titles = []
        base_params = {
            "action": "query",
            "list": "embeddedin",
            "eititle": f"Template:{template_name}",
            "eilimit": 500,
            "format": "json",
        }
        params = dict(base_params)
        while True:
            data = await self._fetch_json(WIKI_API, params)
            if not data:
                break

            pages = data.get("query", {}).get("embeddedin", [])
            for p in pages:
                titles.append(p["title"])

            # Handle continuation
            cont = data.get("continue")
            if not cont:
                break
            params = dict(base_params)
            params.update(cont)

        return titles

    # ── Parse ─────────────────────────────────────────────────────

    def _parse_member_template(self, title: str, wikitext: str) -> dict | None:
        """Parse a {{Member}} template from wikitext.

        Returns dict with keys: name, stars, element, bond, obtain, or None.
        """
        # Only parse the Member template block
        member_block = self._extract_template_block(wikitext, "Member")
        if not member_block:
            return None

        stars = self._extract_param(member_block, "Stars")
        element = self._extract_param(member_block, "Element")
        bond = self._extract_param(member_block, "Bond")
        obtain = self._extract_param(member_block, "Obtain")

        if not stars or not element:
            return None

        try:
            stars = int(stars)
        except (ValueError, TypeError):
            return None

        # Filter unreleased bonds: pure ID codes (A0287 etc.) indicate
        # characters/bonds not yet released — drop them to avoid leaking
        # unreleased content.
        bond_value = bond.strip() if bond else ""
        if re.match(r"^A\d{4}$", bond_value):
            bond_value = ""

        return {
            "name": title,
            "stars": stars,
            "element": element.strip(),
            "bond": bond_value,
            "obtain": obtain.strip() if obtain else "",
        }

    def _parse_bond_template(self, title: str, wikitext: str) -> dict | None:
        """Parse a {{Bond}} template from wikitext.

        Returns dict with keys: name, stars, or None.
        """
        bond_block = self._extract_template_block(wikitext, "Bond")
        if not bond_block:
            return None

        stars = self._extract_param(bond_block, "Stars")
        if not stars:
            return None

        try:
            stars = int(stars)
        except (ValueError, TypeError):
            return None

        return {"name": title, "stars": stars}

    def _extract_template_block(self, wikitext: str, template_name: str) -> str | None:
        """Extract the content block of a specific template from wikitext.

        Handles nested templates by tracking brace depth.
        """
        pattern = r"\{\{" + re.escape(template_name) + r"\b"
        match = re.search(pattern, wikitext)
        if not match:
            return None

        start = match.start()
        depth = 0
        i = start
        while i < len(wikitext) - 1:
            if wikitext[i : i + 2] == "{{":
                depth += 1
                i += 2
            elif wikitext[i : i + 2] == "}}":
                depth -= 1
                if depth == 0:
                    return wikitext[start + 2 : i]  # Content between {{ and }}
                i += 2
            else:
                i += 1

        return None

    def _extract_param_value(self, block: str, param_name: str) -> str | None:
        """Return the raw value of a template parameter (no markup cleaning).

        Matches |ParamName= value (until next | or end of block), tracking
        nested braces/brackets so `|` inside [[...]] / {{...}} doesn't end the
        value prematurely.
        """
        pattern = r"\|" + re.escape(param_name) + r"\s*=\s*"
        match = re.search(pattern, block)
        if not match:
            return None

        start = match.end()
        i = start
        brace_depth = 0
        while i < len(block):
            ch = block[i]
            if ch == "|" and brace_depth == 0:
                break
            elif ch in "{[(":
                brace_depth += 1
            elif ch in "}])":
                brace_depth -= 1
            i += 1

        return block[start:i].strip() or None

    def _extract_param(self, block: str, param_name: str) -> str | None:
        """Extract a template parameter value (legacy cleaning for gacha).

        Matches |ParamName= value (until next | or end of block).
        Handles multi-line values and nested templates/links.
        """
        value = self._extract_param_value(block, param_name)
        if value is None:
            return None
        # Remove HTML tags and wiki markup from the value
        value = re.sub(r"<[^>]+>", "", value)
        value = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+?)\]\]", r"\1", value)
        return value or None

    @staticmethod
    def _clean_markup(text: str) -> str:
        """Clean wikitext/HTML markup into plain text for translation/display.

        Resolves {{Status Tooltip|KEY|DISPLAY}} → DISPLAY (or KEY), strips
        bold/italic markers and image links, converts <br> to newlines, and
        removes remaining HTML tags.
        """
        if not text:
            return ""

        # {{Status Tooltip|KEY}} or {{Status Tooltip|KEY|DISPLAY}}
        text = re.sub(
            r"\{\{\s*Status Tooltip\s*(?:\|([^}|]*))?(?:\|([^}]*))?\s*\}\}",
            lambda m: (m.group(2) or m.group(1) or "").strip(),
            text,
        )
        # Deterministic game-term → Chinese (word-boundary match, longest first).
        # Runs here so both the English fallback and the LLM input see Chinese
        # terms, preventing mixed-language descriptions.
        for en, cn in _STATUS_TERM_CN:
            text = re.sub(r"\b" + re.escape(en) + r"\b", cn, text)
        # {{Example}} → 示例
        text = text.replace("{{Example}}", "示例")
        # Remaining simple templates {{X}} → X
        text = re.sub(r"\{\{\s*([^{}|]+)\s*\}\}", r"\1", text)
        # Image links [[File:...]] / [[Image:...]] → removed entirely
        text = re.sub(r"\[\[(?:File|Image):[^\]]*\]\]", "", text)
        # Wiki links [[Page|display]] (multi-| safe) → display
        text = re.sub(r"\[\[(?:[^|\]]*\|)*([^\]]+?)\]\]", r"\1", text)
        # Bold/italic markers
        text = text.replace("'''", "").replace("''", "")
        # <br> → newline
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        # Other HTML tags
        text = re.sub(r"<[^>]+>", "", text)
        # Collapse per-line whitespace, drop empty lines
        lines = []
        for ln in text.splitlines():
            ln = re.sub(r"[ \t]+", " ", ln).strip()
            if ln:
                lines.append(ln)
        return "\n".join(lines)

    # ── Character Detail (full member fields) ─────────────────────

    def _parse_member_full(self, title: str, wikitext: str) -> dict | None:
        """Parse a {{Member}} template into a full character-detail entry.

        Extracts identity, personal info, growth-ratio stats, discipline,
        potential, and S1/S2/S3 skills. Returns None if the template has no
        Stars (i.e. not a real character page). English fields are kept under
        ``*_en`` keys and translated later by _translate_character_details.
        """
        block = self._extract_template_block(wikitext, "Member")
        if not block:
            return None

        stars_raw = self._extract_param_value(block, "Stars")
        if not stars_raw:
            return None
        try:
            stars = int(stars_raw.strip())
        except (ValueError, TypeError):
            return None

        def p(name):
            return self._extract_param_value(block, name)

        entry = {
            "title": title,
            "id": (p("ID") or "").strip(),
            "stars": stars,
            "element_en": (p("Element") or "").strip(),
            "class_en": (p("Class") or "").strip(),
            "constellation": (p("Constellation") or "").strip(),
            "breast": (p("Breast") or "").strip(),
            "birthday": (p("Birthday") or "").strip(),
            "height": (p("Height") or "").strip(),
            "weight": (p("Weight") or "").strip(),
            "release": (p("Release") or "").strip(),
            "desc_en": self._clean_markup(p("Desc") or ""),
            "stats": {
                "ATK": (p("ATK") or "").strip(),
                "DEF": (p("DEF") or "").strip(),
                "HP": (p("HP") or "").strip(),
                "SPD": (p("SPD") or "").strip(),
            },
            "discs_en": [self._clean_markup(p(f"Disc{i}") or "") for i in range(1, 7)],
            "team_pot_en": self._clean_markup(p("TeamPot") or ""),
            "self_pot_en": self._clean_markup(p("SelfPot") or ""),
            # Potential growth params — deterministic (no LLM). The wiki renders
            # 5 tiers (B/A/S/SS/SSS) per side as Base + Add×k with k=2..6
            # (verified against Shani's rendered page: DEF +3.6% … +10.8% from
            # Base=0/Add=1.8).
            "team_pot_rate": (p("TeamPotRate") or "").strip(),
            "team_pot_base": (p("TeamPotBase") or "").strip(),
            "team_pot_add": (p("TeamPotAdd") or "").strip(),
            "self_pot_rate": (p("SelfPotRate") or "").strip(),
            "self_pot_base": (p("SelfPotBase") or "").strip(),
            "self_pot_add": (p("SelfPotAdd") or "").strip(),
            "skills": [],
        }

        for s in ("S1", "S2", "S3"):
            name = p(s)
            if not name:
                continue
            entry["skills"].append({
                "name_en": self._clean_markup(name),
                "type": (p(s + "Type") or "").strip(),
                "soul": (p(s + "Soul") or "").strip(),
                "cd": (p(s + "CD") or "").strip(),
                "focus": (p(s + "Focus") or "").strip(),
                "des_en": self._clean_markup(p(s + "Des") or ""),
                "des2_en": self._clean_markup(p(s + "Des2") or ""),
                "burst_en": self._clean_markup(p(s + "Burst") or ""),
                "burst_cost": (p(s + "BurstCost") or "").strip(),
                "multi": self._clean_markup(p(s + "Multi") or ""),
            })

        return entry

    async def refresh_characters(self) -> dict:
        """Scrape full character details, translate only changed/new entries.

        Incremental + openrubi-seeded to minimise LLM token cost:
        - A per-entry ``_src_hash`` of the raw English wiki fields detects
          change. Entries whose hash matches the cache are reused untouched.
        - On the first build (empty cache), entries are seeded from openrubi's
          already-translated members_info.json; only characters openrubi
          doesn't know are LLM-translated.
        - On later refreshes, only changed/new entries hit the LLM.

        Independent of the gacha refresh() — writes character_details.json.
        Returns {title: entry} (empty on failure).
        """
        member_wikitexts = await self._fetch_template_pages("Member")
        scraped = []
        for title, wikitext in member_wikitexts:
            parsed = self._parse_member_full(title, wikitext)
            if parsed:
                scraped.append(parsed)

        print(f"[WikiScraper] Parsed {len(scraped)} full character entries", file=sys.stderr)
        if not scraped:
            return {}

        existing = self._load_character_cache()
        # Seed from openrubi only when building the cache for the first time;
        # on incremental refreshes the existing translations are authoritative.
        seed_index = self._openrubi_seed_index() if not existing else None

        # Canonical Chinese names (openrubi dictionaries + own translation map).
        # Used to heal drifted name_cn values in cached entries: alias lookup
        # (character_dic → canonical name → _by_cn_name) only works when the
        # cached name_cn equals the canonical name the alias dictionary uses.
        canonical_names = self._openrubi_name_lookup()
        try:
            canonical_names.update({
                str(k).strip().lower(): v
                for k, v in self._load_translation_map().items() if v
            })
        except Exception:
            pass

        to_translate = []
        result = {}
        unchanged = seeded = changed = healed = 0

        for entry in scraped:
            title = entry["title"]
            h = self._source_hash(entry)
            old = existing.get(title)

            # Unchanged → reuse existing (already translated) entry as-is.
            if old is not None and old.get("_src_hash") == h:
                if self._heal_name_cn(old, canonical_names):
                    healed += 1
                # Early cache builds left seeded entries with untranslated
                # free text (desc/des2/burst still English). Queue those for
                # gap-translation so mixed English/Chinese cards self-heal.
                if self._has_untranslated_freetext(old):
                    to_translate.append(old)
                # Deterministic potential params are excluded from _source_hash
                # (no translation needed), so copy the fresh values onto the
                # reused entry — keeps them current without re-translation and
                # changes _card_hash so the card re-renders.
                for k in _POT_DETAIL_FIELDS:
                    if entry.get(k):
                        old[k] = entry[k]
                result[title] = old
                unchanged += 1
                continue

            # Changed or new.
            seed = self._match_seed(seed_index, entry) if seed_index else None
            if seed is not None:
                self._apply_deterministic_maps(entry)
                self._default_cn_fields(entry)
                entry = self._merge_seed(entry, seed)
                self._heal_name_cn(entry, canonical_names)
                entry["_src_hash"] = h
                result[title] = entry
                seeded += 1
                # openrubi has no bio/enhanced skill descriptions — gap-fill
                # the leftover English fields via LLM translation.
                if self._has_untranslated_freetext(entry):
                    to_translate.append(entry)
            else:
                entry["_src_hash"] = h
                to_translate.append(entry)
                result[title] = entry
                changed += 1

        print(f"[WikiScraper] {unchanged} unchanged ({healed} names healed), "
              f"{seeded} seeded from openrubi, "
              f"{changed} changed/new, {len(to_translate)} to translate",
              file=sys.stderr)

        if to_translate:
            await self._translate_character_details(to_translate)

        # Stamp each entry with its content hash so the card renderer can skip
        # re-drawing unchanged cards.
        for entry in result.values():
            entry["_card_hash"] = self._entry_card_hash(entry)

        # Write the text cache FIRST — lookups only need the JSON. The image
        # download below is slow (1000+ files via proxy) and must not delay
        # availability of character details.
        cache = {
            "scraped_at": time.time(),
            "scraped_at_iso": datetime.now(BEIJING).isoformat(),
            "source_url": WIKI_API,
            "data": result,
        }
        os.makedirs(self._cache_dir, exist_ok=True)
        with open(self._character_detail_cache, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

        # Fire-and-forget image download + card rendering, serialized so cards
        # are never drawn before their portrait/icon lands (idempotent and
        # non-fatal: cards degrade to placeholder tiles until images land).
        try:
            sem = asyncio.Semaphore(4)
            tasks = [self._download_character_images(e, sem) for e in result.values()]
            asyncio.create_task(
                self._background_download_then_render(
                    tasks, "character art", list(result.values()), "character"
                )
            )
        except Exception as e:
            print(f"[WikiScraper] character art download skipped: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)

        return result

    # ── Character Detail Diff / Seed ───────────────────────────────

    @staticmethod
    def _source_hash(entry: dict) -> str:
        """Fingerprint the raw English wiki fields of an entry.

        Changes to any source field (skills, stats, desc, discs, personal,
        element/class, id/stars) invalidate the stored translation, so the
        entry is re-translated on the next refresh. Deterministic potential
        growth params are excluded — they need no translation and are
        backfilled onto unchanged entries instead.
        """
        payload = {k: v for k, v in entry.items()
                   if k != "_src_hash" and k not in _POT_DETAIL_FIELDS}
        s = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    @staticmethod
    def _entry_card_hash(entry: dict) -> str:
        """Fingerprint the final (translated) entry for card-render invalidation.

        Unlike ``_source_hash`` (raw English fields, drives re-translation),
        this hashes the fully-populated entry so a re-translation or any field
        change also invalidates the cached PNG card. The card renderer uses
        this to skip re-drawing cards whose content is unchanged.
        """
        payload = {k: v for k, v in entry.items() if k != "_card_hash"}
        s = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    @staticmethod
    def _apply_deterministic_maps(entry: dict):
        """Fill element/class/constellation/stats_max via fixed maps (no LLM)."""
        entry["element"] = ELEMENT_CN.get(entry.get("element_en", ""), entry.get("element_en", ""))
        entry["class_cn"] = CLASS_CN.get(entry.get("class_en", ""), entry.get("class_en", ""))
        const = entry.get("constellation", "").strip()
        if const:
            entry["constellation"] = CONSTELLATION_CN.get(
                const[:1].upper() + const[1:], const
            )
        # Compute level-60 max stats from wiki growth ratios
        WikiScraper._compute_stats_max(entry)

    @staticmethod
    def _compute_stats_max(entry: dict):
        """Compute level-60 max stats from wiki growth ratios (deterministic).

        The wiki's ``{{Member}}`` template uses fixed multipliers:
        ``{{#expr:{{{ATK}}}*608 round0}}`` etc. The result is stored as
        ``entry["stats_max"]`` — always present for every character, regardless
        of whether an openrubi seed exists.
        """
        stats = entry.get("stats") or {}
        stats_max = {}
        for key, mult in _STAT_MULTIPLIERS.items():
            raw = stats.get(key, "")
            if raw:
                try:
                    stats_max[key] = str(round(float(raw) * mult))
                except (ValueError, TypeError):
                    stats_max[key] = ""
        if stats_max:
            entry["stats_max"] = stats_max

    @staticmethod
    def _default_cn_fields(entry: dict):
        """Fall back Chinese display fields to their English source.

        Uses setdefault so a later _merge_seed can overwrite the fields
        openrubi actually provides (name/skills/potential).
        """
        entry.setdefault("name_cn", entry.get("title", ""))
        entry.setdefault("desc", entry.get("desc_en", ""))
        entry.setdefault("discs", list(entry.get("discs_en", [])))
        entry.setdefault("team_pot", entry.get("team_pot_en", ""))
        entry.setdefault("self_pot", entry.get("self_pot_en", ""))
        for s in entry.get("skills", []):
            s.setdefault("name", s.get("name_en", ""))
            s.setdefault("des", s.get("des_en", ""))
            s.setdefault("des2", s.get("des2_en", ""))
            s.setdefault("burst", s.get("burst_en", ""))

    @staticmethod
    def _has_untranslated_freetext(entry: dict) -> bool:
        """True if any display free-text field still equals its English source.

        Detects leftovers from early builds where seeded entries never went
        through LLM translation (openrubi has no bio/enhanced descriptions, so
        ``desc``/``des2``/``burst`` kept their English fallback) — the source
        of cards mixing English and Chinese text for the same content.
        """
        desc_en = (entry.get("desc_en") or "").strip()
        if desc_en and (entry.get("desc") or "") == entry.get("desc_en"):
            return True
        for s in entry.get("skills", []) or []:
            for key in ("des", "des2", "burst"):
                en = (s.get(key + "_en") or "").strip()
                if en and s.get(key, "") == s.get(key + "_en"):
                    return True
        return False

    @staticmethod
    def _heal_name_cn(entry: dict, canonical_names: dict) -> bool:
        """Align ``name_cn`` with the canonical openrubi/translation-map name.

        Alias resolution depends on ``name_cn`` matching the canonical name in
        ``character_dic.json``; caches built before that alignment drift and
        make alias lookups fail. Returns True when the entry was changed.
        """
        title = str(entry.get("title", "")).strip()
        canonical = canonical_names.get(title.strip().lower()) if title else None
        if not canonical:
            return False
        if entry.get("name_cn") == canonical:
            return False
        entry["name_cn"] = canonical
        return True

    def _load_character_cache(self) -> dict:
        """Load existing {title: entry} from character_details.json."""
        if not os.path.exists(self._character_detail_cache):
            return {}
        try:
            with open(self._character_detail_cache, "r", encoding="utf-8") as f:
                cache = json.load(f)
            return cache.get("data", {})
        except Exception:
            return {}

    def _openrubi_seed_index(self) -> dict | None:
        """Index openrubi members_info.json for seeding (by id + by alias)."""
        if not os.path.exists(OPENRUBI_MEMBERS_INFO):
            return None
        try:
            with open(OPENRUBI_MEMBERS_INFO, "r", encoding="utf-8") as f:
                members = json.load(f)
        except Exception:
            return None
        by_id: dict = {}
        by_alias: dict = {}
        for m in members:
            mid = str(m.get("id", "")).strip().upper()
            if mid:
                by_id[mid] = m
            for key in [m.get("name"), m.get("e7_name"), *m.get("other_names", [])]:
                if key:
                    by_alias.setdefault(str(key).strip().lower(), m)
        return {"by_id": by_id, "by_alias": by_alias}

    @staticmethod
    def _match_seed(seed_index: dict | None, entry: dict) -> dict | None:
        """Match a scraped entry to an openrubi seed by id, then by title."""
        if not seed_index:
            return None
        eid = str(entry.get("id", "")).strip().upper()
        if eid and eid in seed_index["by_id"]:
            return seed_index["by_id"][eid]
        title = entry.get("title", "").strip().lower()
        if title and title in seed_index["by_alias"]:
            return seed_index["by_alias"][title]
        return None

    @staticmethod
    def _render_discipline_cn(discipline: dict) -> list[str]:
        """Render openrubi's Discipline numeric dict into Chinese talent text.

        openrubi stores talents as ``{"1": {"HP%": "3"}, "2": {...}, ...}``
        where level 3 is universally absent (a non-stat node). Returns a
        6-element list indexed by level-1, empty string for missing levels, so
        ``format_character`` can keep the true talent level numbers.
        """
        discs = [""] * 6
        if not isinstance(discipline, dict):
            return discs
        for lv, stats in discipline.items():
            try:
                idx = int(lv) - 1
            except (ValueError, TypeError):
                continue
            if idx < 0 or idx >= 6 or not isinstance(stats, dict):
                continue
            for key, val in stats.items():
                label, is_pct = DISCIPLINE_STAT_CN.get(key, (key, False))
                suffix = "%" if is_pct else ""
                discs[idx] = f"{label} +{val}{suffix}"
                break  # single-stat nodes; keep the first if ever multiple
        return discs

    @staticmethod
    def _merge_seed(entry: dict, seed: dict) -> dict:
        """Overwrite the fields openrubi translated, leaving the rest as-is.

        Skills are matched by position (S1/S2/S3 order matches openrubi's
        Skills list). openrubi has no bio/desc, so desc keeps its English
        fallback; the numeric Discipline dict is rendered into Chinese talent
        text deterministically. Level-60 max stats are merged from openrubi's
        ATK/HP/DEF/Speed fields alongside the wiki's growth ratios.
        """
        if seed.get("name"):
            entry["name_cn"] = seed["name"]

        seed_skills = seed.get("Skills", []) or []
        for i, sk in enumerate(entry.get("skills", [])):
            osk = seed_skills[i] if i < len(seed_skills) else None
            if not osk:
                continue
            if osk.get("Name"):
                sk["name"] = osk["Name"]
            if osk.get("Describe"):
                sk["des"] = osk["Describe"]
            if osk.get("Burst"):
                sk["burst"] = osk["Burst"]

        if seed.get("Discipline"):
            entry["discs"] = WikiScraper._render_discipline_cn(seed["Discipline"])

        pot = seed.get("Potential") or {}
        if pot.get("Team"):
            entry["team_pot"] = pot["Team"]
        if pot.get("Self"):
            entry["self_pot"] = pot["Self"]

        return entry

    # ── Character Detail Translation ──────────────────────────────

    async def _translate_character_details(self, chars: list[dict]):
        """Translate English character entries to Chinese (name + free-text).

        Called only on changed/new entries. Order: deterministic maps → field
        fallback → name lookup (openrubi dict, then gacha name_mapping, then
        LLM) → free-text LLM translation (desc/skills/discs) with openrubi
        few-shot.
        """
        for c in chars:
            self._apply_deterministic_maps(c)
            self._default_cn_fields(c)

        # Name translation
        openrubi_map = self._openrubi_name_lookup()
        name_map = self._load_translation_map()
        unmapped = []
        for c in chars:
            # Entries gap-filled from a seeded cache may already carry the
            # canonical openrubi name — never overwrite a real Chinese name.
            existing_cn = (c.get("name_cn") or "").strip()
            if existing_cn and existing_cn != c.get("title"):
                continue
            cn = openrubi_map.get(c["title"].strip().lower()) or name_map.get(c["title"])
            if cn:
                c["name_cn"] = cn
            else:
                c["name_cn"] = c["title"]
                unmapped.append(c["title"])

        if unmapped and self.llm_client:
            new_names = await self._llm_translate(unmapped)
            if new_names:
                for c in chars:
                    if new_names.get(c["title"]):
                        c["name_cn"] = new_names[c["title"]]
                name_map.update(new_names)
                self._save_translation_map(name_map)

        # Free-text LLM translation (desc, skill names/descriptions, discs)
        if self.llm_client:
            await self._llm_translate_details(chars)

    def _openrubi_name_lookup(self) -> dict:
        """Build lowercase alias → canonical Chinese name map from openrubi.

        character_dic.json is alias→canonical (e.g. 'Shani'→'夏妮'); also folds
        in members_info.json e7_name/other_names. Used to avoid LLM calls for
        names openrubi already knows.
        """
        lookup: dict = {}
        if os.path.exists(OPENRUBI_CHAR_DIC):
            try:
                with open(OPENRUBI_CHAR_DIC, "r", encoding="utf-8") as f:
                    dic = json.load(f)
                for alias, name in dic.items():
                    low = str(alias).strip().lower()
                    if low and name:
                        lookup.setdefault(low, name)
            except Exception:
                pass
        if os.path.exists(OPENRUBI_MEMBERS_INFO):
            try:
                with open(OPENRUBI_MEMBERS_INFO, "r", encoding="utf-8") as f:
                    members = json.load(f)
                for m in members:
                    cn = m.get("name")
                    if not cn:
                        continue
                    for key in [m.get("e7_name"), *m.get("other_names", [])]:
                        if key:
                            lookup.setdefault(str(key).strip().lower(), cn)
            except Exception:
                pass
        return lookup

    def _build_fewshot(self) -> str:
        """Build a few-shot string from openrubi members_info (2 example chars)."""
        examples = []
        if os.path.exists(OPENRUBI_MEMBERS_INFO):
            try:
                with open(OPENRUBI_MEMBERS_INFO, "r", encoding="utf-8") as f:
                    members = json.load(f)
                for m in members[:2]:
                    skill_lines = [
                        f"  - {s.get('Name', '')}：{s.get('Describe', '')}"
                        for s in m.get("Skills", [])[:3]
                    ]
                    examples.append(
                        f"角色「{m.get('name', '')}」（{m.get('Attribute', '')}/{m.get('Class', '')}）\n"
                        + "\n".join(skill_lines)
                    )
            except Exception:
                pass
        return "\n\n".join(examples)

    async def _llm_translate_details(self, chars: list[dict]):
        """Batch-translate desc/skill names+descriptions/discs via LLM.

        Sends characters in batches of 8, each with English fields; expects a
        JSON object keyed by title with translated fields. openrubi few-shot +
        a status-term glossary keep terminology consistent.
        """
        if not self.llm_client:
            return

        fewshot = self._build_fewshot()
        glossary = (
            "术语参考：ATK=攻击力，DEF=防御力，HP=生命值，Speed=速度，"
            "ATK Down=攻击力下降，DEF Down=防御力下降，SPD Down=速度下降，"
            "ATK Up=攻击力提升，DEF Up=防御力提升，SPD Up=速度提升，"
            "Provoke=嘲讽，Immunity=免疫，Shield=护盾，Stun=眩晕，Silence=沉默，"
            "Poison=中毒，Burn=灼烧，Bleed=流血，Barrier=屏障，Recovery=恢复，"
            "Effect Hit Rate=效果命中，Effect Resistance=效果抵抗。"
        )

        total = len(chars)
        for i in range(0, total, 8):
            batch = chars[i : i + 8]
            # Progress heartbeat: this phase runs 20+ sequential LLM calls and
            # is otherwise completely silent, which looks like a hang in logs.
            print(f"[WikiScraper] translating details {i + 1}-{i + len(batch)}/{total}",
                  file=sys.stderr)
            payload = []
            for c in batch:
                payload.append({
                    "title": c["title"],
                    "desc": c.get("desc_en", ""),
                    "skills": [
                        {
                            "name": s.get("name_en", ""),
                            "des": s.get("des_en", ""),
                            "des2": s.get("des2_en", ""),
                            "burst": s.get("burst_en", ""),
                        }
                        for s in c.get("skills", [])
                    ],
                    "discs": c.get("discs_en", []),
                })

            prompt = (
                "你是《Ark Re:Code》游戏本地化翻译，将以下角色的英文文本翻译为简体中文。\n"
                "保持游戏术语风格，方括号内的数值区间（如 [40-50%]）原样保留。\n"
                + glossary
                + "\n"
                + (("参考已翻译角色示例，保持术语与风格一致：\n" + fewshot + "\n") if fewshot else "")
                + "\n输入 JSON（英文）：\n"
                + json.dumps(payload, ensure_ascii=False, indent=1)
                + "\n\n返回 ONLY JSON（无 markdown 代码块、无解释），结构相同但字段翻译为中文：\n"
                '{"<title>": {"name_cn": "...", "desc": "...", "skills": [{"name":"...","des":"...","des2":"...","burst":"..."}], "discs": ["..."]}}'
            )

            try:
                result = await self.llm_client.chat_completion(prompt, timeout_set=60.0)
                translated = self._parse_translation_json(result)
            except Exception:
                translated = {}

            for c in batch:
                t = translated.get(c["title"])
                if not isinstance(t, dict):
                    continue
                # Merge guard: only fill fields that still equal their English
                # source (i.e. untranslated). Authoritative Chinese values
                # (openrubi-seeded names/descriptions) are never overwritten —
                # this is what lets seeded entries be gap-filled safely.
                if t.get("name_cn") and c.get("name_cn") == c.get("title"):
                    c["name_cn"] = t["name_cn"]
                if t.get("desc") and (c.get("desc") or "") == c.get("desc_en"):
                    c["desc"] = t["desc"]
                for idx, sk in enumerate(c.get("skills", [])):
                    ts = t.get("skills", [])
                    if isinstance(ts, list) and idx < len(ts) and isinstance(ts[idx], dict):
                        for key in ("name", "des", "des2", "burst"):
                            val = ts[idx].get(key)
                            if val and (sk.get(key) or "") == sk.get(key + "_en"):
                                sk[key] = val
                if isinstance(t.get("discs"), list) and c.get("discs") == c.get("discs_en"):
                    c["discs"] = t["discs"]

    @staticmethod
    def _parse_translation_json(text: str) -> dict:
        """Extract a JSON object from LLM output (handles nested braces)."""
        if not text:
            return {}
        text = text.strip()
        m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
        try:
            return json.loads(text)
        except Exception:
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                pass
        return {}

    # ── Bond Detail (full bond fields) ─────────────────────────────

    def _parse_bond_full(self, title: str, wikitext: str) -> dict | None:
        """Parse a {{Bond}} template into a full bond-detail entry.

        Extracts class/stars/ATK/HP, description, bond skill (effect), notes,
        obtain, and the related character. Sell price / XP value are NOT stored
        on the page — they are deterministic by Stars (see
        _apply_bond_deterministic). Release date lives in Template:Bond/Release.
        """
        block = self._extract_template_block(wikitext, "Bond")
        if not block:
            return None

        stars_raw = self._extract_param_value(block, "Stars")
        if not stars_raw:
            return None
        try:
            stars = int(stars_raw.strip())
        except (ValueError, TypeError):
            return None

        def p(name):
            return self._extract_param_value(block, name)

        # Bond id is embedded in the icon filename (Icon_Head_S_A0001.png).
        image = (p("Image") or "").strip()
        m = re.search(r"Icon_Head_S_([A-Za-z0-9]+)\.png", image, re.IGNORECASE)
        cid = m.group(1).upper() if m else ""

        return {
            "title": title,
            "id": cid,
            "stars": stars,
            "class_en": (p("Class") or "").strip(),
            "member_en": self._clean_markup(p("Member") or "").strip(),
            "atk_base": (p("ATK") or "").strip(),
            "atk_max": (p("ATK2") or "").strip(),
            "hp_base": (p("HP") or "").strip(),
            "hp_max": (p("HP2") or "").strip(),
            "desc_en": self._clean_markup(p("Desc") or ""),
            "effect_en": self._strip_list_markers(self._clean_markup(p("Effect") or "")),
            "notes_en": self._clean_markup(p("Notes") or ""),
            "obtain_en": self._clean_markup(p("Obtain") or ""),
            "unsellable": bool((p("Unsellable") or "").strip()),
        }

    @staticmethod
    def _strip_list_markers(text: str) -> str:
        """Strip leading ``*``/``#`` list markers from each line."""
        if not text:
            return ""
        lines = []
        for ln in text.splitlines():
            ln = re.sub(r"^\s*[*#]+\s*", "", ln)
            if ln.strip():
                lines.append(ln)
        return "\n".join(lines)

    @staticmethod
    def _normalize_release_date(s: str) -> str:
        """Normalise ``November 01, 2023`` / ``2023-11-01`` → ``2023-11-01``."""
        if not s:
            return ""
        s = s.strip()
        try:
            return datetime.strptime(s, "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            return s

    async def _fetch_bond_release_map(self) -> dict:
        """Fetch Template:Bond/Release → {title: release date string}."""
        data = await self._fetch_json(WIKI_API, {
            "action": "query",
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "titles": "Template:Bond/Release",
            "format": "json",
        })
        if not data:
            return {}
        content = ""
        for _pid, info in data.get("query", {}).get("pages", {}).items():
            revs = info.get("revisions", [])
            if revs:
                content = revs[0].get("slots", {}).get("main", {}).get("*", "")
        mapping = {}
        for m in re.finditer(r"\|\s*([^=|\n]+?)\s*=\s*([^|\n]+)", content):
            name = m.group(1).strip()
            date = m.group(2).strip()
            if name and date:
                mapping[name] = date
        return mapping

    async def refresh_bonds(self) -> dict:
        """Scrape full bond details, translate only changed/new entries.

        Mirrors refresh_characters(): incremental + openrubi-seeded to minimise
        LLM cost. Release dates come from Template:Bond/Release; sell price and
        XP value are computed deterministically from Stars. Writes
        bond_details.json. Returns {title: entry} (empty on failure).
        """
        bond_wikitexts = await self._fetch_template_pages("Bond")
        scraped = []
        for title, wikitext in bond_wikitexts:
            parsed = self._parse_bond_full(title, wikitext)
            if parsed:
                scraped.append(parsed)

        print(f"[WikiScraper] Parsed {len(scraped)} full bond entries", file=sys.stderr)
        if not scraped:
            return {}

        release_map = await self._fetch_bond_release_map()
        existing = self._load_bond_cache()
        seed_index = self._openrubi_bond_seed_index() if not existing else None

        to_translate = []
        result = {}
        unchanged = seeded = changed = 0

        for entry in scraped:
            title = entry["title"]
            h = self._source_hash(entry)
            old = existing.get(title)
            if old is not None and old.get("_src_hash") == h:
                result[title] = old
                unchanged += 1
                continue

            self._apply_bond_deterministic(entry, release_map)
            self._default_bond_cn_fields(entry)
            seed = self._match_bond_seed(seed_index, entry) if seed_index else None
            if seed is not None:
                entry = self._merge_bond_seed(entry, seed)
                entry["_src_hash"] = h
                result[title] = entry
                seeded += 1
            else:
                entry["_src_hash"] = h
                to_translate.append(entry)
                result[title] = entry
                changed += 1

        print(f"[WikiScraper] bonds: {unchanged} unchanged, {seeded} seeded, "
              f"{changed} to translate", file=sys.stderr)

        if to_translate:
            await self._translate_bond_details(to_translate)

        # Stamp each entry with its content hash for card-render dedup.
        for entry in result.values():
            entry["_card_hash"] = self._entry_card_hash(entry)

        # Write the text cache FIRST — lookups only need the JSON.
        cache = {
            "scraped_at": time.time(),
            "scraped_at_iso": datetime.now(BEIJING).isoformat(),
            "source_url": WIKI_API,
            "data": result,
        }
        os.makedirs(self._cache_dir, exist_ok=True)
        with open(self._bond_detail_cache, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

        # Fire-and-forget icon download + card rendering, serialized so bond
        # cards are never drawn before their icon lands (idempotent, non-fatal).
        try:
            sem = asyncio.Semaphore(4)
            tasks = [self._download_bond_icon(e, sem) for e in result.values()]
            asyncio.create_task(
                self._background_download_then_render(
                    tasks, "bond icon", list(result.values()), "bond"
                )
            )
        except Exception as e:
            print(f"[WikiScraper] bond icon download skipped: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)

        return result

    def _load_bond_cache(self) -> dict:
        """Load existing {title: entry} from bond_details.json."""
        if not os.path.exists(self._bond_detail_cache):
            return {}
        try:
            with open(self._bond_detail_cache, "r", encoding="utf-8") as f:
                cache = json.load(f)
            return cache.get("data", {})
        except Exception:
            return {}

    def _openrubi_bond_seed_index(self) -> dict | None:
        """Index openrubi bonds_info.json for seeding (by id + by alias)."""
        if not os.path.exists(OPENRUBI_BONDS_INFO):
            return None
        try:
            with open(OPENRUBI_BONDS_INFO, "r", encoding="utf-8") as f:
                bonds = json.load(f)
        except Exception:
            return None
        by_id: dict = {}
        by_alias: dict = {}
        for b in bonds:
            bid = str(b.get("id", "")).strip().upper()
            if bid:
                by_id[bid] = b
            for key in [b.get("name"), b.get("e7_name"), *b.get("other_names", [])]:
                if key:
                    by_alias.setdefault(str(key).strip().lower(), b)
        return {"by_id": by_id, "by_alias": by_alias}

    @staticmethod
    def _match_bond_seed(seed_index: dict | None, entry: dict) -> dict | None:
        """Match a scraped bond to an openrubi seed by id, then by title."""
        if not seed_index:
            return None
        eid = str(entry.get("id", "")).strip().upper()
        if eid and eid in seed_index["by_id"]:
            return seed_index["by_id"][eid]
        title = entry.get("title", "").strip().lower()
        if title and title in seed_index["by_alias"]:
            return seed_index["by_alias"][title]
        return None

    @staticmethod
    def _apply_bond_deterministic(entry: dict, release_map: dict | None = None):
        """Fill class CN + deterministic sell/XP + release date (no LLM)."""
        entry["class_cn"] = CLASS_CN.get(entry.get("class_en", ""), entry.get("class_en", ""))
        stars = str(entry.get("stars", ""))
        entry["sell_gold"] = BOND_SELL_GOLD.get(stars, "")
        entry["sell_fragment"] = BOND_SELL_FRAGMENT.get(stars, "")
        entry["xp_value"] = BOND_XP_VALUE.get(stars, "")
        if release_map:
            entry["release"] = WikiScraper._normalize_release_date(
                release_map.get(entry.get("title", ""), "")
            )

    @staticmethod
    def _default_bond_cn_fields(entry: dict):
        """Fall back Chinese display fields to their English source."""
        entry.setdefault("name_cn", entry.get("title", ""))
        entry.setdefault("desc", entry.get("desc_en", ""))
        entry.setdefault("effect", entry.get("effect_en", ""))
        entry.setdefault("notes", entry.get("notes_en", ""))
        entry.setdefault("obtain", entry.get("obtain_en", ""))
        entry.setdefault("member", entry.get("member_en", ""))

    @staticmethod
    def _merge_bond_seed(entry: dict, seed: dict) -> dict:
        """Overwrite the fields openrubi translated, leaving the rest as-is."""
        if seed.get("name"):
            entry["name_cn"] = seed["name"]
        if seed.get("Description"):
            entry["desc"] = seed["Description"]
        if seed.get("Skill"):
            entry["effect"] = seed["Skill"]
        if seed.get("Notes"):
            entry["notes"] = seed["Notes"]
        if seed.get("Related"):
            entry["member"] = seed["Related"]
        if seed.get("Released"):
            entry["release"] = WikiScraper._normalize_release_date(str(seed["Released"]))
        return entry

    def _openrubi_bond_name_lookup(self) -> dict:
        """Build lowercase alias → Chinese bond name map from openrubi."""
        lookup: dict = {}
        bond_dic = os.path.join(OPENRUBI_CHAR_DIR, "bonds_search_dic.json")
        if os.path.exists(bond_dic):
            try:
                with open(bond_dic, "r", encoding="utf-8") as f:
                    dic = json.load(f)
                for alias, name in dic.items():
                    low = str(alias).strip().lower()
                    if low and name:
                        lookup.setdefault(low, name)
            except Exception:
                pass
        if os.path.exists(OPENRUBI_BONDS_INFO):
            try:
                with open(OPENRUBI_BONDS_INFO, "r", encoding="utf-8") as f:
                    bonds = json.load(f)
                for b in bonds:
                    cn = b.get("name")
                    if not cn:
                        continue
                    for key in [b.get("e7_name"), *b.get("other_names", [])]:
                        if key:
                            lookup.setdefault(str(key).strip().lower(), cn)
            except Exception:
                pass
        return lookup

    async def _translate_bond_details(self, bonds: list[dict]):
        """Translate English bond entries to Chinese (name + free-text)."""
        name_lookup = self._openrubi_bond_name_lookup()
        char_lookup = self._openrubi_name_lookup()
        for b in bonds:
            cn = name_lookup.get(b["title"].strip().lower())
            if cn:
                b["name_cn"] = cn
            m = b.get("member_en", "").strip()
            if m:
                b["member"] = char_lookup.get(m.lower(), m)

        if self.llm_client:
            await self._llm_translate_bond_details(bonds)

    async def _llm_translate_bond_details(self, bonds: list[dict]):
        """Batch-translate desc/effect/notes/obtain (+ unmapped name) via LLM."""
        if not self.llm_client:
            return
        glossary = (
            "术语参考：ATK=攻击力，DEF=防御力，HP=生命值，Shield=护盾，"
            "Immune=免疫，Provoke=嘲讽，Stun=眩晕，Bleed=流血，Burn=灼烧，"
            "Poison=中毒，Revive=复活，Barrier=屏障，Recovery=恢复。"
        )
        for i in range(0, len(bonds), 8):
            batch = bonds[i : i + 8]
            payload = []
            for b in batch:
                payload.append({
                    "title": b["title"],
                    "desc": b.get("desc_en", ""),
                    "effect": b.get("effect_en", ""),
                    "notes": b.get("notes_en", ""),
                    "obtain": b.get("obtain_en", ""),
                })
            prompt = (
                "你是《Ark Re:Code》游戏本地化翻译，将以下羁绊(Bond)的英文文本翻译为简体中文。\n"
                "保持游戏术语风格，方括号内的数值区间（如 [50-100%]）原样保留。\n"
                + glossary
                + "\n输入 JSON（英文）：\n"
                + json.dumps(payload, ensure_ascii=False, indent=1)
                + "\n\n返回 ONLY JSON（无 markdown 代码块、无解释），结构相同但字段翻译为中文：\n"
                '{"<title>": {"name_cn": "...", "desc": "...", "effect": "...", "notes": "...", "obtain": "..."}}'
            )
            try:
                result = await self.llm_client.chat_completion(prompt, timeout_set=60.0)
                translated = self._parse_translation_json(result)
            except Exception:
                translated = {}
            for b in batch:
                t = translated.get(b["title"])
                if not isinstance(t, dict):
                    continue
                if t.get("name_cn") and b.get("name_cn") == b["title"]:
                    b["name_cn"] = t["name_cn"]
                if t.get("desc"):
                    b["desc"] = t["desc"]
                if t.get("effect"):
                    b["effect"] = t["effect"]
                if t.get("notes"):
                    b["notes"] = t["notes"]
                if t.get("obtain"):
                    b["obtain"] = t["obtain"]

    async def _download_bond_icon(self, entry: dict, semaphore: asyncio.Semaphore):
        """Download the bond head icon (Icon_Head_S_{id}.png). Idempotent."""
        cid = str(entry.get("id", "")).strip()
        if not cid:
            return
        dest = os.path.join(BOND_ICON_DIR, f"{cid}.png")
        if os.path.exists(dest):
            return
        async with semaphore:
            url = await self._resolve_image_url(f"Icon_Head_S_{cid}.png")
            if url:
                await self._download_image(url, dest)

    # ── Battle Mechanics (buffs/debuffs/mechanics/damage/AI) ───────

    async def refresh_battle_mechanics(self) -> dict:
        """Scrape the Battle_Mechanics page, cache it, download status icons.

        Mirrors refresh_characters()/refresh_bonds(): fetch wikitext → parse →
        write the text cache first → fire-and-forget icon download. The buffs/
        debuffs entries carry the wiki ``[[File:...]]`` icon filename; only
        those filenames present in ``STATUS_ICON_CN`` are downloaded (the rest
        are broken uploads on the wiki). Returns the parsed dict (empty on
        failure).
        """
        wikitext = await self._fetch_page_wikitext("Battle_Mechanics")
        if not wikitext:
            print("[WikiScraper] Battle_Mechanics wikitext empty", file=sys.stderr)
            return {}

        data = self._parse_battle_mechanics_wikitext(wikitext)
        buffs = data.get("buffs", [])
        debuffs = data.get("debuffs", [])
        print(f"[WikiScraper] Parsed {len(buffs)} buffs, {len(debuffs)} debuffs "
              f"from Battle_Mechanics", file=sys.stderr)
        if not buffs and not debuffs:
            print("[WikiScraper] WARNING: no buffs/debuffs parsed, "
                  "discarding empty result", file=sys.stderr)
            return {}

        cache = {
            "scraped_at": time.time(),
            "scraped_at_iso": datetime.now(BEIJING).isoformat(),
            "source_url": WIKI_API,
            "data": data,
        }
        os.makedirs(self._cache_dir, exist_ok=True)
        with open(self._battle_mechanics_cache, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

        # Fire-and-forget status-icon download (idempotent, non-fatal).
        try:
            sem = asyncio.Semaphore(4)
            tasks = [self._download_status_icons(buffs + debuffs, sem)]
            asyncio.create_task(self._background_downloads(tasks, "status icon"))
        except Exception as e:
            print(f"[WikiScraper] status icon download skipped: "
                  f"{type(e).__name__}: {e}", file=sys.stderr)

        return data

    async def _download_status_icons(
        self, entries: list[dict], semaphore: asyncio.Semaphore
    ):
        """Download the 64x64 wiki status icons (those in STATUS_ICON_CN).

        Idempotent: skips files already on disk and skips any icon filename
        not in ``STATUS_ICON_CN`` (broken wiki uploads are deliberately absent
        from that map).
        """
        seen: set[str] = set()
        tasks = []
        for entry in entries:
            icon = (entry.get("icon") or "").strip()
            if not icon or icon not in STATUS_ICON_CN or icon in seen:
                continue
            seen.add(icon)
            dest = os.path.join(STATUS_ICON_DIR, icon)
            if os.path.exists(dest):
                continue
            tasks.append(self._download_status_icon_one(icon, dest, semaphore))
        if tasks:
            await asyncio.gather(*tasks)

    async def _download_status_icon_one(
        self, filename: str, dest: str, semaphore: asyncio.Semaphore
    ):
        """Download a single status icon (original 64x64, no thumbnail)."""
        async with semaphore:
            url = await self._resolve_image_url(filename)
            if url:
                await self._download_image(url, dest)

    # ── Battle Mechanics parsing ──────────────────────────────────

    @staticmethod
    def _parse_battle_mechanics_wikitext(wikitext: str) -> dict:
        """Parse Battle_Mechanics wikitext into a structured dict.

        Walks the page in document order, tracking ``== headings ==`` so tables
        and prose can be bucketed. Icon-bearing tables (Name/Icon/Effect) are
        classified as buffs vs debuffs by the icon filename prefix; iconless
        tables are bucketed by heading; prose between headings is stored in
        ``sections`` (e.g. Mechanics / Damage Formula).

        Effect text is stored in English (groundwork); the authoritative
        Chinese labels live in ``STATUS_ICON_CN`` and are applied later.
        """
        result: dict = {
            "buffs": [],
            "debuffs": [],
            "other_effects": [],
            "trigger_effects": [],
            "ai": [],
            "useful_categories": [],
            "misc_tables": [],
            "sections": {},
        }

        lines = wikitext.splitlines()
        n = len(lines)
        current_heading = ""
        prose_buffer: list[str] = []

        def flush_prose():
            nonlocal prose_buffer
            if current_heading and prose_buffer:
                text = WikiScraper._strip_wiki_markup("\n".join(prose_buffer))
                if text:
                    result["sections"][current_heading] = text
            prose_buffer = []

        i = 0
        while i < n:
            stripped = lines[i].strip()
            # Heading
            if re.match(r"^==+\s*.*?\s*==+\s*$", stripped):
                flush_prose()
                current_heading = re.sub(r"^=+\s*|\s*=+$", "", stripped).strip()
                i += 1
                continue
            # Table start
            if stripped.startswith("{|"):
                flush_prose()
                rows: list[list[str]] = []
                j = i + 1
                while j < n and not lines[j].strip().startswith("|}"):
                    cell_line = lines[j].strip()
                    if not cell_line or cell_line.startswith("|-") or cell_line.startswith("|+"):
                        j += 1
                        continue
                    if cell_line.startswith("!"):
                        rows.append(WikiScraper._split_table_cells(cell_line[1:]))
                    elif cell_line.startswith("|"):
                        rows.append(WikiScraper._split_table_cells(cell_line[1:]))
                    elif rows:
                        # Multi-line continuation of the previous cell.
                        rows[-1][-1] = rows[-1][-1] + "\n" + cell_line
                    j += 1
                WikiScraper._classify_battle_table(result, current_heading, rows)
                i = j + 1
                continue
            # Prose
            if stripped:
                prose_buffer.append(stripped)
            i += 1

        flush_prose()
        return result

    @staticmethod
    def _classify_battle_table(result: dict, heading: str, rows: list[list[str]]):
        """Classify one Battle_Mechanics table and append to *result*."""
        if not rows:
            return
        header = [c.strip() for c in rows[0]]
        data_rows = rows[1:] if len(rows) > 1 else []
        icon_idx = next(
            (k for k, h in enumerate(header) if h.lower() == "icon"), None
        )

        # Icon-bearing table → buffs or debuffs.
        if icon_idx is not None:
            buff_n = debuff_n = 0
            for row in data_rows:
                icon = WikiScraper._extract_icon(row[icon_idx]) if icon_idx < len(row) else ""
                kind = WikiScraper._classify_status_icon(icon)
                if kind == "buff":
                    buff_n += 1
                elif kind == "debuff":
                    debuff_n += 1
            key = "buffs" if buff_n >= debuff_n else "debuffs"
            for row in data_rows:
                name = WikiScraper._strip_wiki_markup(row[0]) if row else ""
                icon = WikiScraper._extract_icon(row[icon_idx]) if icon_idx < len(row) else ""
                effect = WikiScraper._strip_wiki_markup(row[2]) if len(row) > 2 else ""
                if not (name or icon or effect):
                    continue
                result[key].append({"name": name, "icon": icon, "effect": effect})
            return

        # Iconless table → bucket by heading.
        bucket = WikiScraper._bucket_battle_heading(heading)
        for row in data_rows:
            cells = [WikiScraper._strip_wiki_markup(c) for c in row]
            if not any(cells):
                continue
            result[bucket].append({"cells": cells})

    @staticmethod
    def _split_table_cells(row: str) -> list[str]:
        """Split a MediaWiki table row into cells.

        Splits on ``||`` / ``!!`` only when outside ``[[...]]`` / ``{{...}}``
        (so ``|`` inside a link label or template param doesn't end a cell).
        """
        cells: list[str] = []
        buf: list[str] = []
        i = 0
        n = len(row)
        while i < n:
            two = row[i : i + 2]
            if two == "[[":
                buf.append(two)
                i += 2
                continue
            if two == "]]":
                buf.append(two)
                i += 2
                continue
            if two == "{{":
                buf.append(two)
                i += 2
                continue
            if two == "}}":
                buf.append(two)
                i += 2
                continue
            if two in ("||", "!!"):
                cells.append("".join(buf).strip())
                buf = []
                i += 2
                continue
            buf.append(row[i])
            i += 1
        cells.append("".join(buf).strip())
        return cells

    @staticmethod
    def _extract_icon(cell: str) -> str:
        """Extract a ``File:...`` filename from an icon cell, else ``""``."""
        m = re.search(r"\[\[(?:File|Image):([^|\]]+)", cell or "")
        return m.group(1).strip() if m else ""

    @staticmethod
    def _classify_status_icon(filename: str) -> str | None:
        """Return ``"buff"`` / ``"debuff"`` for a status icon filename, else None."""
        if not filename:
            return None
        if filename.startswith("Buff_") or filename in ("H118S2.png", "Immortal_Resident.png"):
            return "buff"
        if filename.startswith("Debuff_") or filename == "H168S2_1.png":
            return "debuff"
        return None

    @staticmethod
    def _bucket_battle_heading(heading: str) -> str:
        """Map a section heading to a non-icon table bucket key."""
        h = (heading or "").lower()
        if "other" in h:
            return "other_effects"
        if "trigger" in h:
            return "trigger_effects"
        if "ai" in h or "artificial" in h:
            return "ai"
        if "categor" in h:
            return "useful_categories"
        return "misc_tables"

    @staticmethod
    def _strip_wiki_markup(text: str) -> str:
        """Remove wiki/HTML markup → plain text, keeping English.

        Like ``_clean_markup`` but without the ``_STATUS_TERM_CN`` translation,
        so Battle_Mechanics effect text is stored in English (groundwork) and
        translated later.
        """
        if not text:
            return ""
        text = re.sub(
            r"\{\{\s*Status Tooltip\s*(?:\|([^}|]*))?(?:\|([^}]*))?\s*\}\}",
            lambda m: (m.group(2) or m.group(1) or "").strip(),
            text,
        )
        text = re.sub(r"\{\{\s*([^{}|]+)\s*\}\}", r"\1", text)
        text = re.sub(r"\[\[(?:File|Image):[^\]]*\]\]", "", text)
        text = re.sub(r"\[\[(?:[^|\]]*\|)*([^\]]+?)\]\]", r"\1", text)
        text = text.replace("'''", "").replace("''", "")
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        lines = []
        for ln in text.splitlines():
            ln = re.sub(r"[ \t]+", " ", ln).strip()
            if ln:
                lines.append(ln)
        return "\n".join(lines)

    # ── Classify ──────────────────────────────────────────────────

    def _classify_members(self, members: list[dict]) -> dict:
        """Classify Member pages into character pools.

        Returns a dict with pool definitions (description, star, type, items).
        """
        pools = {
            "three_color_five_star": self._make_pool("三色五星角色", 5, "character"),
            "special_three_color_five_star": self._make_pool("限定三色五星角色", 5, "character"),
            "special_five_star": self._make_pool("光暗五星角色", 5, "character"),
            "three_color_four_star": self._make_pool("三色四星角色", 4, "character"),
            "three_color_three_star": self._make_pool("三色三星角色", 3, "character"),
            "special_four_star": self._make_pool("光暗四星角色", 4, "character"),
            "special_three_star": self._make_pool("光暗三星角色", 3, "character"),
        }

        for m in members:
            element = m["element"]
            stars = m["stars"]
            bond = m.get("bond", "")
            obtain = m.get("obtain", "")

            item = {"name": m["name"]}
            if bond:
                item["bond"] = bond

            if element in LIGHT_DARK_ELEMENTS:
                if stars == 5:
                    pools["special_five_star"]["items"].append(item)
                elif stars == 4:
                    pools["special_four_star"]["items"].append(item)
                elif stars == 3:
                    pools["special_three_star"]["items"].append(item)
            elif element in THREE_COLOR_ELEMENTS:
                if stars == 5:
                    if "Normal Recruit" in obtain:
                        pools["three_color_five_star"]["items"].append(item)
                    else:
                        pools["special_three_color_five_star"]["items"].append(item)
                elif stars == 4:
                    pools["three_color_four_star"]["items"].append(item)
                elif stars == 3:
                    pools["three_color_three_star"]["items"].append(item)

        # Sort items within each pool for deterministic output
        for pool in pools.values():
            pool["items"].sort(key=lambda x: x["name"])

        return pools

    def _classify_bonds(self, bonds: list[dict], pools: dict):
        """Classify Bond pages and derived 5★ bonds into bond pools.

        Modifies pools dict in place, adding bonds_* entries.
        """
        # 3★ / 4★ bonds from wiki Bond pages
        bonds_three = []
        bonds_four = []
        for b in bonds:
            if b["stars"] == 3:
                bonds_three.append({"name": b["name"]})
            elif b["stars"] == 4:
                bonds_four.append({"name": b["name"]})

        bonds_three.sort(key=lambda x: x["name"])
        bonds_four.sort(key=lambda x: x["name"])

        pools["bonds_three_star"] = {
            "description": "三星羁绊",
            "star": 3,
            "type": "bond",
            "items": bonds_three,
        }
        pools["bonds_four_star"] = {
            "description": "四星羁绊",
            "star": 4,
            "type": "bond",
            "items": bonds_four,
        }

        # 5★ bonds derived from three-color five-star characters
        bonds_all = []
        bonds_tricolor = []
        for pool_key in ("three_color_five_star", "special_three_color_five_star"):
            for item in pools[pool_key]["items"]:
                if "bond" in item:
                    bonds_all.append({"name": item["bond"]})

        for item in pools["three_color_five_star"]["items"]:
            if "bond" in item:
                bonds_tricolor.append({"name": item["bond"]})

        bonds_all.sort(key=lambda x: x["name"])
        bonds_tricolor.sort(key=lambda x: x["name"])

        pools["bonds_five_star_all"] = {
            "description": "五星羁绊（全部）",
            "star": 5,
            "type": "bond",
            "items": bonds_all,
        }
        pools["bonds_five_star_tricolor"] = {
            "description": "五星羁绊（三色）",
            "star": 5,
            "type": "bond",
            "items": bonds_tricolor,
        }

    @staticmethod
    def _make_pool(description: str, star: int, pool_type: str) -> dict:
        return {
            "description": description,
            "star": star,
            "type": pool_type,
            "items": [],
        }

    # ── Translation ───────────────────────────────────────────────

    async def _translate_names(self, pools: dict):
        """Translate all English names in pools to Chinese (hybrid approach).

        1. Load cached English→Chinese mapping
        2. Collect all unmapped names from pools
        3. If LLM client is available, batch-translate unmapped names
        4. Save updated mapping
        5. Apply translations to all pool items
        """
        # Collect all unique names from pools
        all_names = set()
        for pool in pools.values():
            for item in pool.get("items", []):
                all_names.add(item["name"])
                if "bond" in item:
                    all_names.add(item["bond"])

        # Load existing translation map
        name_map = self._load_translation_map()

        # Find unmapped names
        unmapped = [n for n in all_names if n not in name_map]

        if unmapped and self.llm_client:
            new_translations = await self._llm_translate(unmapped)
            name_map.update(new_translations)
            self._save_translation_map(name_map)

        # Apply translations
        self._apply_translations(pools, name_map)

    def _load_translation_map(self) -> dict:
        """Load English→Chinese name mapping from cache file."""
        if not os.path.exists(self._translation_cache):
            return {}
        try:
            with open(self._translation_cache, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_translation_map(self, name_map: dict):
        """Save English→Chinese name mapping to cache file."""
        os.makedirs(self._cache_dir, exist_ok=True)
        try:
            with open(self._translation_cache, "w", encoding="utf-8") as f:
                json.dump(name_map, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    async def _llm_translate(self, names: list[str]) -> dict:
        """Batch-translate English names to Chinese via LLM.

        Returns a dict mapping English→Chinese.
        """
        if not names:
            return {}

        names_list = "\n".join(names)
        prompt = (
            "Translate the following Ark Re:Code game character names and bond names "
            "into Simplified Chinese. These are from a gacha game, so use commonly "
            "accepted Chinese translations where they exist. "
            "Return ONLY a JSON object mapping each English name to its Chinese "
            "translation. No explanation, no markdown formatting.\n\n"
            f"{names_list}\n\n"
            'Output format: {{"English Name": "中文名", ...}}'
        )

        try:
            result = await self.llm_client.chat_completion(
                prompt, timeout_set=30.0
            )
            if result:
                # Try to extract JSON from the response
                return self._parse_translation_response(result, names)
        except Exception:
            pass

        return {}

    @staticmethod
    def _parse_translation_response(response: str, names: list[str]) -> dict:
        """Parse LLM translation response into a dict, with fallback heuristics."""
        import re as _re
        # Try to extract a JSON object
        json_match = _re.search(r"\{[^{}]*\}", response, _re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(0))
            except json.JSONDecodeError:
                pass

        # Fallback: try to parse line-by-line "English: Chinese" pairs
        result = {}
        for line in response.strip().split("\n"):
            line = line.strip().strip(",").strip('"').strip("'")
            for sep in (": ", "：", " → ", " - "):
                if sep in line:
                    en, cn = line.split(sep, 1)
                    en = en.strip().strip('"').strip("'")
                    cn = cn.strip().strip('"').strip("'")
                    if en and cn and en in names:
                        result[en] = cn
                    break

        return result

    def _apply_translations(self, pools: dict, name_map: dict):
        """Replace English names with Chinese names in all pool items."""
        for pool in pools.values():
            for item in pool.get("items", []):
                en_name = item["name"]
                if en_name in name_map:
                    item["name"] = name_map[en_name]
                if "bond" in item and item["bond"] in name_map:
                    item["bond"] = name_map[item["bond"]]

    # ── Merge ─────────────────────────────────────────────────────

    def _load_static_fallback(self) -> dict:
        """Load complete gacha data from static config (fallback when scraping fails)."""
        try:
            with open(self._static_config, "r", encoding="utf-8") as f:
                static = json.load(f)
            return {"pools": static["pools"], "banners": static.get("banners", {})}
        except Exception:
            return {"pools": {}, "banners": {}}

    def _merge_with_static(self, pools: dict) -> dict:
        """Merge scraped pools with static banner config."""
        banners = {}
        try:
            with open(self._static_config, "r", encoding="utf-8") as f:
                static = json.load(f)
            banners = static.get("banners", {})
        except Exception:
            pass

        return {"pools": pools, "banners": banners}
