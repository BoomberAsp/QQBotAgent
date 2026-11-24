"""Regression coverage for the shared Wiki directory and its public surfaces."""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "QQBot")]

from lib.wiki_catalog import WikiCatalog, record_wiki_refresh
from tools.character_list import list_characters


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class WikiCatalogueTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.cache = root / "wiki"
        self.game = root / "game"
        self.config = root / "config"
        self.alias_files = {k: self.config / (k + "_aliases.json") for k in ("char", "bond")}
        self.catalog = WikiCatalog(self.cache, self.game, alias_files=self.alias_files)
        self.catalog.character_dir = self.config
        self.roles = {
            "H001": {"names": {"en": "Alpha", "zh_hans": "甲"}, "element": "Ice", "job": "Defender"},
            "H002": {"names": {"en": "Beta", "zh_hans": "乙"}, "element": "Earth", "job": "Warrior"},
            "H003": {"names": {"en": "Future", "zh_hans": "丙"}, "element": "Light", "job": "Defender"},
            "H004": {"names": {"en": "Future Two", "zh_hans": "丁"}, "element": "Dark", "job": "Defender"},
            "H005": {"names": {"en": "Unknown", "zh_hans": "戊"}, "element": "Dark", "job": "Defender"},
            "H999": {"names": {"en": "H999", "zh_hans": "H999"}},
        }
        write_json(self.game / "roles.json", {"roles": self.roles})
        self.wiki_rows = {
            "Alpha": {"id": "H001", "name_cn": "人工甲", "_manual_fields": ["name_cn"],
                      "release": "March, 19, 2025"},
            "Alpha duplicate": {"id": "H001", "name_cn": "甲二"},
            "Beta": {"id": "H002", "release": "2026-01-01"},
            "Future": {"id": "H003", "release": "?"},
            "Future Two": {"id": "H004", "release": "2099-01-01"},
            "Legacy": {"id": "H006", "name_cn": "旧角色", "class_en": "Medic",
                       "element_en": "Flame", "is_released": True},
        }
        write_json(self.cache / "character_details.json", {"data": self.wiki_rows})
        write_json(self.config / "wiki_release_overrides.json", {"H003": {"status": "unreleased"}})
        write_json(self.alias_files["char"], {"H001": "甲", "甲的昵称": "甲", "H002": "乙"})

    def test_filters_and_equal_unreleased_priority(self):
        result = self.catalog.query(sort="release_desc")
        items = result["items"]
        self.assertEqual([r["id"] for r in items], ["H003", "H004", "H002", "H001", "H005", "H006"])
        self.assertEqual([r["release_status"] for r in items], [
            "unreleased", "unreleased", "released", "released", "unknown", "released",
        ])
        self.assertEqual(items[3]["name_cn"], "人工甲")
        self.assertEqual(items[3]["release_date"], "2025-03-19")
        asc = self.catalog.query(sort="release_asc", limit=3)
        self.assertEqual([r["id"] for r in asc["items"]], ["H003", "H004", "H001"])
        self.assertEqual(asc["next_offset"], 3)
        self.assertEqual(self.catalog.query(element=["Fire", "水"], job=["重装"], q="昵称")["matched"], 1)
        self.assertEqual(self.catalog.query(release_status="unknown", sort="release_desc")["matched"], 1)

    def test_bond_uses_related_elements_and_its_own_job(self):
        write_json(self.game / "artifacts.json", {"artifacts": {
            "A0162": {"id": "A0162", "names": {"en": "Trade", "zh_hans": "交易"}, "job": "Other"},
            "A9999": {"names": {"en": "A9999", "zh_hans": "A9999"}},
        }})
        write_json(self.cache / "bond_details.json", {"data": {
            "Trade": {"id": "A0165", "member_en": "Alpha", "class_en": "Caster"},
        }})
        write_json(self.config / "related_dic.json", {"乙": "交易"})
        write_json(self.alias_files["bond"], {"A0165": "交易", "交易别名": "交易"})
        item, = self.catalog.query("bond", element=["木"], job=["全职业"], q="交易别名")["items"]
        self.assertEqual((item["id"], item["job"], set(item["elements"])), ("A0162", "全职业", {"木", "水"}))
        self.assertEqual(item["sources"], ["wiki", "gamedata"])
        alias, = self.catalog.query("bond", view="aliases", element=["水"], job=["全职业"])["items"]
        self.assertEqual(alias["canonical"], "交易")

    def test_dates_survive_import_refresh_and_alias_edits(self):
        initial = self.catalog.query(observe=True)
        self.assertTrue(all(row["added_at"] is None for row in initial["items"]))
        self.roles["H007"] = {"names": {"zh_hans": "新角色"}, "element": "Fire"}
        write_json(self.game / "roles.json", {"roles": self.roles})
        new_role = self.catalog.query(observe=True)["items"][0]
        self.assertEqual(new_role["id"], "H007")
        self.assertIsNotNone(new_role["added_at"])
        self.roles["H007"]["names"]["zh_hans"] = "改名角色"
        write_json(self.game / "roles.json", {"roles": self.roles})
        self.assertEqual(self.catalog.query(observe=True)["items"][0]["added_at"], new_role["added_at"])

        old = self.catalog.alias_groups("char")
        new = {**old, "甲": [*old["甲"], "新别名"]}
        self.catalog.record_alias_save("char", old, new)
        row, = self.catalog.query(view="aliases", q="新别名", groups=new, observe=True)["items"]
        self.assertIsNone(row["added_at"])
        self.assertIsNone(row["alias_added_at"]["甲的昵称"])
        self.assertIsNotNone(row["alias_added_at"]["新别名"])

    def test_wiki_refresh_records_new_entries_without_opening_panel(self):
        after = {**self.wiki_rows, "Recently added": {"id": "H008", "name_cn": "刚入库"}}
        with patch("lib.wiki_catalog.WikiCatalog", return_value=self.catalog):
            record_wiki_refresh(self.cache, "char", self.wiki_rows, after)
        write_json(self.cache / "character_details.json", {"data": after})
        top = self.catalog.query()["items"][0]
        self.assertEqual(top["id"], "H008")
        self.assertIsNotNone(top["added_at"])

    def test_tool_uses_shared_filters_and_does_not_write_metadata(self):
        with patch("tools.character_list.WikiCatalog", return_value=self.catalog):
            answer = json.loads(asyncio.run(list_characters(
                element=["光", "暗"], job=["重装"], release_status="unreleased", limit=1,
            )))
            self.assertTrue(asyncio.run(list_characters(element=["不存在"])).startswith("[Error]"))
        self.assertEqual(answer["matched"], 2)
        self.assertEqual(answer["next_offset"], 1)
        self.assertEqual(answer["items"][0]["id"], "H003")
        self.assertFalse((self.cache / "catalog_metadata.sqlite3").exists())

    def test_http_filters_keep_full_alias_snapshot_on_save(self):
        from fastapi.testclient import TestClient
        from webui import auth, data_reader
        from webui.main import app

        with patch.object(auth, "require_setup", return_value=False), \
             patch.object(auth, "is_authenticated", return_value=True), \
             patch.object(data_reader, "_wiki_catalog", return_value=self.catalog), \
             patch.dict(data_reader._ALIAS_FILES, self.alias_files), \
             patch("tools.name_resolver.build_index"):
            client = TestClient(app)
            response = client.get("/api/wiki/catalog", params=[
                ("view", "aliases"), ("element", "水"), ("element", "火"), ("job", "重装"),
            ])
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual([row["canonical"] for row in data["items"]], ["甲"])
            self.assertEqual(set(data["alias_snapshot"]), {"甲", "乙"})
            data["alias_snapshot"]["甲"].append("网页新别名")
            saved = client.put("/api/wiki/aliases", json={"kind": "char", "aliases": data["alias_snapshot"]})
            self.assertEqual(saved.status_code, 200)
            aliases = self.catalog.alias_groups("char")
            self.assertEqual(aliases["乙"], ["H002"])
            self.assertIn("网页新别名", aliases["甲"])
            self.assertEqual(client.get("/api/wiki/catalog?job=无效").status_code, 400)

    def test_production_override_contains_all_confirmed_ids(self):
        overrides_path = ROOT / "QQBot/config/characters/wiki_release_overrides.json"
        overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
        self.assertEqual({rid for rid, row in overrides.items() if row["status"] == "unreleased"},
                         {"H154", "H155", "H157", "H158", "H159", "H161", "H164", "H607"})


if __name__ == "__main__":
    unittest.main()
