"""Shared read model for the Wiki panel and the agent's character list."""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from lib.bond_names import artifact_id_redirects
from lib.game_names import file_stamp, reconcile
from lib.gamedata_source import default_dir
from lib.paths import repo_dir, wiki_cache_dir
from tools.wiki_glossary import load_glossary

DETAIL_FILES = {"char": "character_details.json", "bond": "bond_details.json"}
ALIAS_FILES = {"char": "character_dic.json", "bond": "bonds_search_dic.json"}
ROSTER_FILES = {"char": "members_info.json", "bond": "bonds_info.json"}
RELEASE_STATUSES = ("unreleased", "released", "unknown")
ADDED_SORTS = ("added_desc", "added_asc")
RELEASE_SORTS = ("release_desc", "release_asc")
_GAME_TZ = timezone(timedelta(hours=8))


def _key(value):
    return str(value or "").strip().casefold()


def _entity_key(rid, title):
    return str(rid).strip().upper() if rid else "title:" + _key(title)


@lru_cache(maxsize=48)
def _read(stamp, default_type=dict):
    path = stamp[0]
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(value, default_type):
            return value
    except (OSError, ValueError):
        pass
    return default_type()


def _load(path, default_type=dict):
    return _read(file_stamp(path), default_type)


def _added_times(cache_dir, scope, keys, *, observe=False, before=None):
    """Track new entries from the first observation. Legacy dates stay unknown."""
    keys = set(keys)
    path = Path(cache_dir) / "catalog_metadata.sqlite3"
    if (not keys and before is None) or (not observe and not path.is_file()):
        return {}
    try:
        if observe:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path, timeout=2)
        else:
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            with connection:
                if observe:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("CREATE TABLE IF NOT EXISTS catalog_scope (name TEXT PRIMARY KEY)")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS catalog_added (scope TEXT, key TEXT, added_at TEXT,"
                        " PRIMARY KEY(scope, key))"
                    )
                    new_scope = connection.execute(
                        "INSERT OR IGNORE INTO catalog_scope VALUES (?)", (scope,)
                    ).rowcount
                existing = dict(connection.execute(
                    "SELECT key, added_at FROM catalog_added WHERE scope = ?", (scope,)
                ))
                baseline = set(before or ())
                missing = (keys | baseline) - existing.keys() if observe else set()
                if observe and missing:
                    legacy_keys = keys if new_scope and before is None else baseline
                    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    inserted = {key: None if key in legacy_keys else now for key in missing}
                    connection.executemany(
                        "INSERT INTO catalog_added VALUES (?, ?, ?)",
                        [(scope, key, added) for key, added in inserted.items()],
                    )
                    existing.update(inserted)
                return existing
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        # First-seen metadata is optional when the configured data directory is unavailable.
        return {}


def record_wiki_refresh(cache_dir, kind, previous, current):
    """Capture new cache entries with the same identity used by the directory."""
    catalog = WikiCatalog(cache_dir)
    old = catalog._entries(kind, wiki_entries=previous)
    new = catalog._entries(kind, wiki_entries=current)
    _added_times(cache_dir, kind, new, observe=True, before=old)


def _date(value):
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%B, %d, %Y", "%b %d, %Y", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _release(wiki, roster, game, override, today):
    released_at = next((value for value in map(_date, (
        override.get("date"), wiki.get("release"), roster.get("Released"),
        game.get("release_date") or game.get("release"),
    )) if value), None)
    status = override.get("status") or wiki.get("release_status") or game.get("release_status")
    if status not in RELEASE_STATUSES:
        explicit = wiki.get("is_released", game.get("is_released"))
        if isinstance(explicit, bool):
            status = "released" if explicit else "unreleased"
        elif released_at:
            status = "released" if released_at <= today else "unreleased"
        else:
            status = "unknown"
    return status, released_at


def _label_index(table, variants=None):
    index = {_key(value): label for raw, label in table.items() for value in (raw, label)}
    index.update(variants or {})
    return index


def _facets():
    glossary = load_glossary()
    elements = _label_index(glossary["element"], {
        "fire": "火", "ice": "水", "earth": "木", "darkness": "暗",
    })
    jobs = _label_index(glossary["class"], {
        "any": "全职业", "all": "全职业", "universal": "全职业", "不限": "全职业", "全职业": "全职业",
    })
    return elements, jobs


def _meaningful(value, rid):
    return value and _key(value) != _key(rid) and not _key(value).startswith("t_")


def _name_lookup(rows):
    lookup = {}
    for row in rows:
        for term in row["search_terms"]:
            lookup.setdefault(_key(term), set()).add(row["key"])
    return lookup


def _unambiguous(lookup, terms):
    candidates = set().union(*(lookup.get(_key(term), ()) for term in terms if term))
    return next(iter(candidates)) if len(candidates) == 1 else None


class WikiCatalog:
    def __init__(self, cache_dir=None, game_dir=None, *, detail_files=None, alias_files=None):
        self.cache_dir = Path(cache_dir or wiki_cache_dir())
        self.game_dir = Path(game_dir or default_dir())
        self.character_dir = Path(repo_dir()) / "config" / "characters"
        self.detail_files = detail_files or {k: self.cache_dir / n for k, n in DETAIL_FILES.items()}
        self.alias_files = alias_files or {k: self.character_dir / n for k, n in ALIAS_FILES.items()}
        self.elements, self.jobs = _facets()

    def alias_groups(self, kind):
        groups = {}
        for alias, canonical in _load(self.alias_files[kind]).items():
            if isinstance(canonical, str) and canonical.strip():
                groups.setdefault(canonical, []).append(alias)
        return groups

    def _entries(self, kind, *, wiki_entries=None):
        char = kind == "char"
        group = "roles" if char else "artifacts"
        raw_game = _load(self.game_dir / (group + ".json")).get(group) or {}
        if not isinstance(raw_game, dict):
            raw_game = {}
        raw_ids = {str(rid).upper(): str(rid) for rid, row in raw_game.items() if isinstance(row, dict)}
        game = {rid: raw_game[raw_id] for rid, raw_id in raw_ids.items()}
        redirects = artifact_id_redirects(game) if not char else {}
        raw_wiki = (_load(self.detail_files[kind]).get("data") or {}) if wiki_entries is None else wiki_entries
        if not isinstance(raw_wiki, dict):
            raw_wiki = {}
        wiki = {}
        for title, row in raw_wiki.items():
            if not isinstance(row, dict):
                continue
            rid = str(row.get("id") or "").strip().upper()
            entry = reconcile({**row, "id": redirects.get(rid, rid), "title": title}, game)
            key = _entity_key(entry.get("id"), title)
            if key not in wiki or (entry.get("_manual_fields") and not wiki[key].get("_manual_fields")):
                wiki[key] = entry
        roster = {}
        for row in _load(self.character_dir / ROSTER_FILES[kind], list):
            if isinstance(row, dict) and row.get("id"):
                rid = str(row["id"]).upper()
                roster[redirects.get(rid, rid)] = row

        today = datetime.now(_GAME_TZ).date().isoformat()
        overrides = _load(self.character_dir / "wiki_release_overrides.json") if char else {}
        rows = {}
        for key in game.keys() | wiki.keys() | roster.keys():
            old, official, local = wiki.get(key, {}), game.get(key, {}), roster.get(key, {})
            rid = None if key.startswith("title:") else key
            localized = official.get("names") or {}
            title = old.get("title") or localized.get("en") or rid
            name = next((value for value in (
                old.get("name_cn") if "name_cn" in (old.get("_manual_fields") or ()) else None,
                localized.get("zh_hans"), localized.get("zh_hant"), old.get("name_cn"), local.get("name"), title,
            ) if _meaningful(value, rid)), None)
            if not name:
                continue
            attr = next((self.elements[_key(value)] for value in (
                official.get("element"), old.get("element_en"), old.get("element"), local.get("Attribute"),
            ) if _key(value) in self.elements), None) if char else None
            game_job = official.get("job")
            if not char and _key(game_job) == "other":
                game_job = "全职业"
            job = next((self.jobs[_key(value)] for value in (
                game_job, old.get("class_en"), old.get("class_cn"), local.get("Class"),
            ) if _key(value) in self.jobs), None)
            status, released_at = _release(old, local, official, overrides.get(key) or {}, today)
            rows[key] = {
                "key": key, "id": rid, "gamedata_id": raw_ids.get(key), "title": title, "name_cn": name,
                "elements": [attr] if attr else [], "job": job,
                "stars": official.get("star") or old.get("stars") or local.get("Stars") or 0,
                "release_date": released_at, "release_status": status,
                "source": "wiki" if old else "gamedata" if official else "roster",
                "sources": [value for value, present in (("wiki", old), ("gamedata", official), ("roster", local)) if present],
                "manual": bool(old.get("_manual_fields")),
                "has_cn": any("\u3400" <= c <= "\u9fff" for c in str(name)),
                "search_terms": [str(value) for value in (
                    rid, title, name, old.get("name_cn"), local.get("name"), local.get("e7_name"),
                    *(localized.values()), *(old.get("name_aliases") or ()), *(local.get("other_names") or ()),
                ) if value],
                "related_names": [value for value in (old.get("member_en"), old.get("member"), local.get("Related")) if value],
            }
        for old_id, new_id in redirects.items():
            if new_id in rows:
                rows[new_id]["search_terms"].append(old_id)
        return rows

    def _alias_targets(self, rows, groups):
        by_name = _name_lookup(rows.values())
        for canonical, aliases in groups.items():
            exact_ids = {str(alias).upper() for alias in (canonical, *aliases)
                         if str(alias).upper() in rows}
            if exact_ids:
                key = next(iter(exact_ids)) if len(exact_ids) == 1 else None
            else:
                key = _unambiguous(by_name, (canonical, *aliases))
            yield canonical, aliases, key

    def _with_aliases(self, rows, groups):
        for canonical, aliases, key in self._alias_targets(rows, groups):
            if key is not None:
                rows[key]["search_terms"].extend([canonical, *aliases])
        return rows

    def _join_bond_elements(self, bonds):
        roles = self._with_aliases(self._entries("char"), self.alias_groups("char"))
        role_names = _name_lookup(roles.values())
        bond_names = _name_lookup(bonds.values())
        for owner, bond in _load(self.character_dir / "related_dic.json").items():
            key = _unambiguous(bond_names, [bond])
            if key:
                bonds[key]["related_names"].append(owner)
        for row in bonds.values():
            matches = {_unambiguous(role_names, [name]) for name in row["related_names"]}
            row["elements"] = sorted({element for key in matches if key
                                      for element in roles[key]["elements"]})

    def _alias_rows(self, entries, groups):
        rows = []
        for canonical, aliases, key in self._alias_targets(entries, groups):
            entry = entries.get(key) or {}
            rows.append({
                **entry,
                "key": key or "alias:" + _key(canonical),
                "canonical": canonical, "name_cn": canonical,
                "elements": entry.get("elements", []), "job": entry.get("job"),
                "search_terms": [canonical, *aliases, *entry.get("search_terms", ())],
                "aliases": aliases,
            })
        return rows

    def record_alias_save(self, kind, before, after):
        entries = self._entries(kind)
        old, new = self._alias_rows(entries, before), self._alias_rows(entries, after)
        # An unbound group's aliases let a rename retain its first-seen value.
        self._alias_times(kind, new, observe=True, before=old)

    def _alias_times(self, kind, rows, *, observe=False, before=None):
        group_times = _added_times(self.cache_dir, kind + "_groups", (row["key"] for row in rows),
                                   observe=observe, before=None if before is None else (
                                       row["key"] for row in before))
        def term_keys(groups):
            return (_key(alias) for row in groups for alias in row["aliases"])
        term_times = _added_times(self.cache_dir, kind + "_aliases", term_keys(rows),
                                  observe=observe, before=None if before is None else term_keys(before))
        for row in rows:
            dates = {alias: term_times.get(_key(alias)) for alias in row["aliases"]}
            row["alias_added_at"] = dates
            added = group_times.get(row["key"])
            if row["key"].startswith("alias:") and dates:
                added = min(dates.values()) if all(dates.values()) else None
            row["added_at"] = added
        return rows

    def query(self, kind="char", *, view="entries", q="", element=None, job=None,
              release_status=None, sort="added_desc", offset=0, limit=None, observe=False, groups=None):
        if kind not in DETAIL_FILES or view not in ("entries", "aliases"):
            raise ValueError("无效的目录类型。")
        if sort not in (*ADDED_SORTS, *RELEASE_SORTS):
            raise ValueError("无效的排序方式。")
        if release_status is not None and release_status not in RELEASE_STATUSES:
            raise ValueError("无效的上线状态。")
        elements = _filters(element, self.elements, "属性")
        jobs = _filters(job, self.jobs, "职业")
        groups = self.alias_groups(kind) if groups is None else groups
        entries = self._with_aliases(self._entries(kind), groups)
        if kind == "bond":
            self._join_bond_elements(entries)
        if view == "aliases":
            rows = self._alias_times(kind, self._alias_rows(entries, groups), observe=observe)
        else:
            rows = list(entries.values())
            times = _added_times(self.cache_dir, kind, entries, observe=observe)
            for row in rows:
                row["added_at"] = times.get(row["key"])
        query = _key(q)
        matched = [
            row for row in rows
            if (not query or any(query in _key(term) for term in row["search_terms"]))
            and (not elements or elements.intersection(row["elements"] or ["未知"]))
            and (not jobs or (row["job"] or "未知") in jobs)
            and (not release_status or row.get("release_status") == release_status)
        ]
        matched.sort(key=lambda row: _sort_key(row, sort))
        end = None if limit is None else offset + limit
        items = [{k: v for k, v in row.items() if k not in {"search_terms", "related_names"}}
                 for row in matched[offset:end]]
        jobs_available = [job for job in dict.fromkeys(self.jobs.values()) if kind == "bond" or job != "全职业"]
        return {"items": items, "total": len(rows), "matched": len(matched),
                "next_offset": end if end is not None and end < len(matched) else None,
                "facets": {"element": list(dict.fromkeys(self.elements.values())) + ["未知"],
                           "job": jobs_available + ["未知"]}}


def _filters(values, choices, label):
    if values is None:
        return set()
    if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"{label}须为名称列表。")
    normalized = {(choices.get(_key(value)) or ("未知" if _key(value) in ("unknown", "未知") else None))
                  for value in values}
    if None in normalized:
        raise ValueError(f"存在不支持的{label}。")
    return normalized


def _sort_key(row, sort):
    stable = (row["key"], _key(row["name_cn"]))
    if sort in RELEASE_SORTS:
        if row.get("release_status") == "unreleased":
            return (0, 0, stable)
        if row.get("release_status") == "released" and row.get("release_date"):
            day = date.fromisoformat(row["release_date"]).toordinal()
            return (1, -day if sort == "release_desc" else day, stable)
        return (2, 0, stable)
    added = row.get("added_at")
    if not added:
        return (1, 0, stable)
    time = datetime.fromisoformat(added).timestamp()
    return (0, -time if sort == "added_desc" else time, stable)
