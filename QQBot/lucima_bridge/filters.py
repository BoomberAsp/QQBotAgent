"""Per-account storage for bounded desktop-format equipment filters."""
from __future__ import annotations

import json
import math
import re


NAME = re.compile(r"[\w-]{1,12}\Z")
CODE = re.compile(r"[A-Za-z0-9_]{1,32}\Z")
SLOTS = {"weapon", "helmet", "armor", "necklace", "ring", "boots"}
FLAGS = {"scoreEnabled", "forceScoreEnabled"}
SCORES = {"minScore", "forceScore", "mixRequired"}
ALLOWED = {"sets", "main", "subs"} | FLAGS | SCORES


def parse_scheme(raw: str):
    if not raw or len(raw) > 2200:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"name", "slots"}:
        return None
    name, slots = payload["name"], payload["slots"]
    if not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(slots, dict) or not slots.keys() <= SLOTS:
        return None
    for rules in slots.values():
        if not isinstance(rules, list) or len(rules) > 4:
            return None
        for rule in rules:
            if not isinstance(rule, dict) or not rule.keys() <= ALLOWED:
                return None
            sets, main, subs = rule.get("sets"), rule.get("main", ""), rule.get("subs", [])
            if not isinstance(sets, list) or not 1 <= len(sets) <= 12 or not all(
                    isinstance(label, str) and CODE.fullmatch(label) for label in sets):
                return None
            if not isinstance(main, str) or main and not CODE.fullmatch(main):
                return None
            if not isinstance(subs, list) or len(subs) > 8:
                return None
            for sub in subs:
                if not isinstance(sub, dict) or set(sub) != {"stat", "mode", "value"}:
                    return None
                stat, mode, value = sub["stat"], sub["mode"], sub["value"]
                if (not isinstance(stat, str) or not CODE.fullmatch(stat) or mode not in ("fixed", "mixed")
                        or type(value) not in (float, int) or not -50000 <= value <= 50000 or not math.isfinite(value)):
                    return None
            if any(type(rule[key]) is not bool for key in FLAGS & rule.keys()):
                return None
            if any(type(rule[key]) is not int or not 0 <= rule[key] <= 50000 for key in SCORES & rule.keys()):
                return None
    if not any(slots.values()):
        return None
    return name, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class Filters:
    def __init__(self, db):
        self.db = db
        db.execute("""CREATE TABLE IF NOT EXISTS lucima_equipment_filter (
            owner TEXT NOT NULL, name TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY (owner, name)
        )""")

    def names(self, owner):
        return [name for (name,) in self.db.execute(
            "SELECT name FROM lucima_equipment_filter WHERE owner = ? ORDER BY name", (owner,))]

    def get(self, owner, name):
        row = self.db.execute("SELECT payload FROM lucima_equipment_filter WHERE owner = ? AND name = ?",
                              (owner, name)).fetchone()
        return row[0] if row else None

    def save(self, owner, name, payload):
        existing = self.names(owner)
        if len(existing) >= 8 and name not in existing:
            return False
        with self.db:
            self.db.execute("""INSERT INTO lucima_equipment_filter VALUES (?, ?, ?)
                ON CONFLICT (owner, name) DO UPDATE SET payload = excluded.payload""", (owner, name, payload))
        return True

    def drop(self, owner, name=None):
        with self.db:
            if name is None:
                self.db.execute("DELETE FROM lucima_equipment_filter WHERE owner = ?", (owner,))
            else:
                self.db.execute("DELETE FROM lucima_equipment_filter WHERE owner = ? AND name = ?", (owner, name))
