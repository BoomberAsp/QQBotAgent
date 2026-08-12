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
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode


# ── Constants ────────────────────────────────────────────────────

WIKI_API = "https://arkrecodewiki.miraheze.org/w/api.php"
STATIC_CONFIG = os.path.join(
    os.path.dirname(__file__), "..", "config", "gacha_data.json"
)
CACHE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data", "wiki_cache"
)
CACHE_FILE = os.path.join(CACHE_DIR, "gacha.json")
TRANSLATION_CACHE = os.path.join(CACHE_DIR, "name_mapping.json")

BATCH_SIZE = 50  # MediaWiki pages per revisions query
BEIJING = timezone(timedelta(hours=8))

# Element → pool prefix mapping
LIGHT_DARK_ELEMENTS = {"Light", "Dark"}
THREE_COLOR_ELEMENTS = {"Flame", "Water", "Nature"}


# ── WikiScraper ──────────────────────────────────────────────────

class WikiScraper:
    """Scrapes Ark Re:Code Wiki for gacha pool and bond data."""

    def __init__(self, llm_client=None):
        self._cache_dir = os.path.abspath(CACHE_DIR)
        self._cache_file = os.path.abspath(CACHE_FILE)
        self._translation_cache = os.path.abspath(TRANSLATION_CACHE)
        self._static_config = os.path.abspath(STATIC_CONFIG)
        self.llm_client = llm_client  # For name translation (can be set later)

    def set_client(self, client):
        """Set or update the LLM client (for lazy initialization)."""
        self.llm_client = client

    # ── Public API ────────────────────────────────────────────────

    def get_cached_data(self) -> dict | None:
        """Synchronously load cached gacha data if not stale.

        Returns None if cache doesn't exist, is stale, or contains empty data
        (caller should fall back to static JSON). Does NOT make network requests.
        """
        if not os.path.exists(self._cache_file):
            return None

        try:
            with open(self._cache_file, "r", encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:
            return None

        scraped_at = cache.get("scraped_at", 0)
        if self._is_stale(scraped_at):
            return None

        data = cache.get("data", {})
        pools = data.get("pools", {})
        total_items = sum(len(p.get("items", [])) for p in pools.values())
        if total_items == 0:
            print("[WikiScraper] Cached data is empty, treating as stale", file=sys.stderr)
            return None

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
    async def _curl_json(url: str, params: dict) -> dict | None:
        """Make a GET request via curl and return parsed JSON.

        Uses subprocess.run in a thread executor for reliability.
        """
        query = urlencode(params)
        full_url = f"{url}?{query}"
        loop = asyncio.get_running_loop()

        def _run():
            try:
                result = subprocess.run(
                    ["curl", "-sS", "--max-time", "30",
                     "-H", "User-Agent: QQBotAgent/1.0 (Wiki Scraper)",
                     full_url],
                    capture_output=True, timeout=35,
                )
                if result.returncode != 0:
                    print(f"[WikiScraper] curl exit {result.returncode}: "
                          f"{result.stderr.decode('utf-8', errors='replace')[:200]}",
                          file=sys.stderr)
                    return None
                return json.loads(result.stdout.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as e:
                print(f"[WikiScraper] JSON decode error: {e}", file=sys.stderr)
                return None
            except FileNotFoundError:
                print("[WikiScraper] curl not found!", file=sys.stderr)
                return None
            except subprocess.TimeoutExpired:
                print("[WikiScraper] curl timed out", file=sys.stderr)
                return None
            except Exception as e:
                print(f"[WikiScraper] curl error: {type(e).__name__}: {e}", file=sys.stderr)
                return None

        return await loop.run_in_executor(None, _run)

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
            data = await self._curl_json(WIKI_API, {
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
            data = await self._curl_json(WIKI_API, params)
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

        return {
            "name": title,
            "stars": stars,
            "element": element.strip(),
            "bond": bond.strip() if bond else "",
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

    def _extract_param(self, block: str, param_name: str) -> str | None:
        """Extract a template parameter value.

        Matches |ParamName= value (until next | or end of block).
        Handles multi-line values and nested templates/links.
        """
        pattern = r"\|" + re.escape(param_name) + r"\s*=\s*"
        match = re.search(pattern, block)
        if not match:
            return None

        start = match.end()
        # Track nested braces and brackets to find the real end of value
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

        value = block[start:i].strip()
        # Remove HTML tags and wiki markup from the value
        value = re.sub(r"<[^>]+>", "", value)
        value = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+?)\]\]", r"\1", value)
        return value or None

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
