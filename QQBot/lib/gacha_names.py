"""Resolve recruitment names and identities from maintained game data."""
from collections import defaultdict
import re

from lib.game_names import reconcile


def name_key(name):
    return str(name or "").strip().replace("’", "'").casefold()


def _unique(values):
    return next(iter(values)) if len(values) == 1 else None


class GachaNames:
    def __init__(self, game, details, roster, aliases, old_translations, *, redirects=None):
        self.entries = {}
        self.titles = defaultdict(set)
        self.aliases = defaultdict(set)
        self.redirects = redirects or {}
        self.old_titles = defaultdict(set)
        self.old_translations = old_translations

        for title, name in old_translations.items():
            if isinstance(name, str):
                self.old_titles[name_key(name)].add(title)

        game = {str(rid).strip().upper(): row for rid, row in game.items()}
        by_id = {str(row["id"]).strip().upper(): row for row in roster if row.get("id")}
        detail_by_id = {}
        for title, row in details.items():
            rid = self._id(row.get("id"))
            if rid:
                self.titles[name_key(title)].add(rid)
                detail_by_id[rid] = row

        for rid in game.keys() | by_id.keys() | detail_by_id.keys():
            if self._id(rid) != rid:
                continue
            record = game.get(rid, {})
            local = by_id.get(rid, {})
            detail = detail_by_id.get(rid, {})
            raw_names = record.get("names") or {}
            title = raw_names.get("en") or detail.get("title") or ""
            entry = reconcile({"id": rid, "title": title, "name_cn": detail.get("name_cn"),
                               "_manual_fields": detail.get("_manual_fields", [])}, game)
            maintained = aliases.get(rid) or local.get("name") or aliases.get(title)
            if entry["name_source"] != "manual" and maintained and re.search(r"[\u3400-\u9fff]", maintained):
                entry["name_cn"] = maintained
            self.entries[rid] = (entry["name_cn"] or maintained or rid,
                                 record.get("star") or local.get("Stars") or detail.get("stars"), title)
            for original in (title, detail.get("title")):
                if original:
                    self.titles[name_key(original)].add(rid)
            for alias in (rid, entry["name_cn"], maintained, local.get("name"), local.get("e7_name"),
                          detail.get("name_cn"), *raw_names.values(), *(local.get("other_names") or ()),
                          *(detail.get("name_aliases") or ())):
                if alias:
                    self.aliases[name_key(alias)].add(rid)

        for alias, canonical in aliases.items():
            rid = self._id(alias)
            if rid not in self.entries:
                rid = _unique(self.aliases.get(name_key(canonical), ()))
            if rid:
                self.aliases[name_key(alias)].add(rid)

    def _id(self, rid):
        rid = str(rid or "").strip().upper()
        return self.redirects.get(rid, rid)

    def resolve(self, value, rid="", title="", *, star=None):
        key = name_key(value)
        title_key = name_key(title or value)
        direct = self.titles.get(title_key) or (self.aliases.get(title_key) if title else None)
        legacy = set().union(*(
            self.titles.get(name_key(source)) or self.aliases.get(name_key(source), ())
            for source in self.old_titles.get(key, ())
        ))
        alias = self.aliases.get(key, set())
        identity = self._id(rid)
        if not identity:
            candidates = direct or legacy or alias
            if star and len(candidates) > 1:
                candidates = {candidate for candidate in candidates if self.entries.get(candidate, ("", None))[1] == star}
            identity = _unique(candidates)
        entry = self.entries.get(identity)
        if entry is None:
            return identity, self.old_translations.get(value, value) if title == value else value, None, title
        name, rarity, original = entry
        return identity, name, rarity, title or original


def build_five_star_bonds(pools):
    """Derive five-star bond pools using the bond's own rarity where known."""
    tricolor = {}
    all_bonds = {}
    for key in ("three_color_five_star", "special_three_color_five_star"):
        for item in pools.get(key, {}).get("items", ()):
            if not item.get("bond") or item.get("bond_star", 5) != 5:
                continue
            rid = item.get("bond_id")
            identity = rid or item["bond"]
            bond = {"name": item["bond"]}
            if rid:
                bond["id"] = rid
            if item.get("bond_title"):
                bond["title"] = item["bond_title"]
            if key == "three_color_five_star":
                tricolor.setdefault(identity, bond)
            all_bonds.setdefault(identity, bond)

    for key, entries, label in (("bonds_five_star_all", all_bonds, "全部"),
                                ("bonds_five_star_tricolor", tricolor, "三色")):
        pools[key] = {"description": f"五星羁绊（{label}）", "star": 5, "type": "bond",
                      "items": sorted(entries.values(), key=lambda item: item["name"])}
