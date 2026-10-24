#!/usr/bin/env python3
"""
Story Knowledge Base test suite.

Covers the offline-buildable core: text tokenisation helpers, the SQLite/FTS5
index builder, bilingual retrieval, ranking bonuses and the two rendering
paths (L4 injection block / story_search tool text).

Everything here runs without network access and without NoneBot: a synthetic
corpus is indexed into a temp database in ``setUpClass``, so the tests exercise
the exact same code path the production DB uses.

Usage::

    cd QQBot
    python -m pytest test_story_kb.py -v
    python test_story_kb.py
"""

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.story_kb import (  # noqa: E402
    SCHEMA_VERSION,
    StoryKB,
    cjk_bigrams,
    en_query_terms,
    has_cjk,
    persona_char_id,
    zh_query_bigrams,
)


def _load_builder():
    """Import scripts/build_story_kb.py as a module (it is not a package)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "scripts", "build_story_kb.py")
    spec = importlib.util.spec_from_file_location("build_story_kb_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # dataclasses need __module__ resolvable
    spec.loader.exec_module(mod)
    return mod


bsk = _load_builder()


# ── Synthetic corpus ───────────────────────────────────────────────
#
# Two units: a main-story page (Chinese gist via the enrichment file) and an
# event page. Deliberately small, but it exercises scene parsing structures,
# chunk splitting, both FTS indexes and the persona bonus.

def _unit_main(page_title: str, chapter: int, title_en: str):
    lines = [
        bsk.Line("", f"Chapter {chapter}: {title_en}", "", "title"),
        bsk.Line("Apathy", "You don't think I'd be coming just to see your "
                           "unconscious ass, do you?", "H016", "say"),
        bsk.Line("团长", "I see you haven't changed much in ten years.", "", "player"),
        bsk.Line("露比", "Whaaaat? He only just woke up...", "H076", "say"),
        bsk.Line("旁白", "Apathy's cheeks blush ever so slightly.", "", "narration"),
        bsk.Line("Apathy", "A storm is coming. Let's walk and talk.", "H016", "say"),
    ]
    scene = bsk.Scene(title="Stage 1-1: Intro", lines=lines)
    scene.compute_hash()
    meta = {
        "work": "main", "episode": "Episode 1", "chapter_no": chapter,
        "chapter_title_en": title_en, "event_name": "", "member": "",
    }
    return bsk.Unit(page_title=page_title, url="https://example.invalid/x",
                    meta=meta, scenes=[scene], work_rank=1)


def _unit_event(page_title: str, event_name: str):
    lines = [
        bsk.Line("", f"Event Story: {event_name}", "", "title"),
        bsk.Line("", "Mercenary Union Commander's Room", "", "scene"),
        bsk.Line("Saku", "Reporting in, Commander.", "H613", "say"),
        bsk.Line("Senica", "Watch out!", "H106", "say"),
        bsk.Line("旁白", "Saku tripped over her untied shoelace.", "", "narration"),
    ] * 3
    scene = bsk.Scene(title="Scene 1: Part 1", lines=lines)
    scene.compute_hash()
    meta = {
        "work": "event", "episode": "", "chapter_no": None,
        "chapter_title_en": "", "event_name": event_name, "member": "Saku",
    }
    return bsk.Unit(page_title=page_title, url="https://example.invalid/y",
                    meta=meta, scenes=[scene], work_rank=2)


ZH_FIXTURE = {
    "Main Story/Episode 1/Chapter 1/Stage 1-5": {
        "version": bsk.ZH_PROMPT_VERSION,
        "unit_summary": "团长苏醒后与阿帕西重逢，露比在旁插科打诨，暴雨将至。",
        "unit_keywords": ["苏醒", "重逢", "暴雨", "露比"],
        "chapter_title_zh": "共鸣觉醒",
        "scenes": [{
            "title_zh": "Stage 1-1: 开场",
            "gist": "露比在雨夜守着刚苏醒的团长，阿帕西催他们上路。",
            "keywords": ["雨夜", "苏醒", "露比"],
        }],
    },
    "Event Story/A Budding Heart": {
        "version": bsk.ZH_PROMPT_VERSION,
        "unit_summary": "咲在佣兵团报告任务，赛妮卡接住了要摔倒的她。",
        "unit_keywords": ["咲", "佣兵团", "报告"],
        "chapter_title_zh": "萌芽的心意",
        "scenes": [{
            "title_zh": "Scene 1: Part 1",
            "gist": "咲踩到松开的鞋带摔倒，赛妮卡及时扶住她。",
            "keywords": ["鞋带", "摔倒", "赛妮卡"],
        }],
    },
}


class TokenisationTests(unittest.TestCase):
    def test_has_cjk(self):
        self.assertTrue(has_cjk("露比"))
        self.assertTrue(has_cjk("Ark 方舟"))
        self.assertFalse(has_cjk("Ark-α only"))

    def test_cjk_bigrams(self):
        self.assertEqual(cjk_bigrams("露比"), ["露比"])
        self.assertEqual(cjk_bigrams("雨夜谈心"), ["雨夜", "夜谈", "谈心"])
        # non-CJK separates runs
        self.assertEqual(cjk_bigrams("露比Ark伊娥丝"), ["露比", "伊娥", "娥丝"])

    def test_zh_query_drops_function_word_bigrams(self):
        grams = zh_query_bigrams("露比的了是什么")
        self.assertIn("露比", grams)
        self.assertNotIn("的了", grams)
        self.assertNotIn("是什", grams)
        self.assertNotIn("什么", grams)

    def test_zh_query_dedupes(self):
        grams = zh_query_bigrams("露比露比")
        self.assertEqual(grams.count("露比"), 1)

    def test_en_query_terms(self):
        terms = en_query_terms("What did Rubi say about the ark war?")
        self.assertIn("rubi", terms)
        self.assertIn("ark", terms)
        self.assertIn("say", terms)
        for stop in ("what", "did", "about", "the"):
            self.assertNotIn(stop, terms)


class PersonaMappingTests(unittest.TestCase):
    def test_rubi_maps_to_character_id(self):
        self.assertEqual(persona_char_id("rubi", "露比 (Rubi)"), "H076")
        self.assertEqual(persona_char_id("rubi"), "H076")

    def test_unknown_persona_has_no_mapping(self):
        self.assertEqual(persona_char_id("assistant", "助手 Roxy"), "")
        self.assertEqual(persona_char_id("", ""), "")


class BuilderAndRetrievalTests(unittest.TestCase):
    """Index a synthetic corpus once, then query it in both languages."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="storykb_test_")
        cls.db_path = os.path.join(cls.tmpdir, "story.db")
        units = [
            _unit_main("Main Story/Episode 1/Chapter 1/Stage 1-5", 1,
                       "Resonant Awakening"),
            _unit_event("Event Story/A Budding Heart", "A Budding Heart"),
        ]
        cls.stats = bsk.build_index(units, ZH_FIXTURE, cls.db_path,
                                    source_note="fixture")
        cls.kb = StoryKB(cls.db_path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    # ── index shape ──────────────────────────────────────────────

    def test_index_stats(self):
        self.assertEqual(self.stats["units"], 2)
        self.assertEqual(self.stats["scenes"], 2)
        self.assertGreaterEqual(self.stats["chunks"], 2)
        self.assertEqual(self.stats["units_with_zh"], 2)

    def test_available_and_stats(self):
        self.assertTrue(self.kb.available)
        st = self.kb.stats()
        self.assertEqual(st["units"], 2)
        self.assertEqual(st["zh_units"], 2)
        self.assertEqual(st["source"], "fixture")

    def test_translated_chapter_titles_are_stored(self):
        hits = self.kb.search("共鸣觉醒", limit=1)
        self.assertTrue(hits)
        self.assertEqual(hits[0].chapter_title_zh, "共鸣觉醒")
        self.assertEqual(hits[0].chapter_title_en, "Resonant Awakening")

    def test_event_chinese_name_falls_back_to_llm_title(self):
        hits = self.kb.search("萌芽的心意", limit=1)
        self.assertTrue(hits)
        self.assertEqual(hits[0].event_name_zh, "萌芽的心意")
        self.assertIn("活动剧情", hits[0].label())

    # ── retrieval ────────────────────────────────────────────────

    def test_chinese_query_hits_zh_gist(self):
        hits = self.kb.search("露比 雨夜", limit=2)
        self.assertTrue(hits, "Chinese query should hit the Chinese index")
        self.assertIn("雨夜", hits[0].zh_gist)

    def test_english_query_hits_dialogue(self):
        hits = self.kb.search("unconscious ass ten years", limit=2)
        self.assertTrue(hits)
        self.assertIn("ten years", hits[0].text_en)

    def test_unrelated_query_returns_nothing_or_low_score(self):
        hits = self.kb.search("quantum chromodynamics lattice gauge", limit=2)
        for hit in hits:
            self.assertLess(hit.score, 0.02)

    def test_empty_query_returns_nothing(self):
        self.assertEqual(self.kb.search(""), [])
        self.assertEqual(self.kb.search("   "), [])

    def test_persona_bonus_prefers_scenes_with_the_character(self):
        without = self.kb.search("reporting in commander", limit=2)
        with_bonus = self.kb.search("reporting in commander", limit=2,
                                    persona_char_id="H613")
        self.assertTrue(without and with_bonus)
        self.assertGreaterEqual(with_bonus[0].score, without[0].score)
        self.assertIn("本角色", with_bonus[0].label("H613"))

    def test_limit_is_respected(self):
        self.assertEqual(len(self.kb.search("commander", limit=1)), 1)

    def test_scene_diversity_prefers_one_chunk_per_scene(self):
        hits = self.kb.search("saku shoelace commander senica", limit=4)
        scene_ids = [h.scene_id for h in hits]
        self.assertEqual(len(scene_ids), len(set(scene_ids)))

    # ── rendering ────────────────────────────────────────────────

    def test_injection_block_has_preamble_and_gist(self):
        block = self.kb.format_injection("露比 雨夜", persona_char_id="H076",
                                         persona_name="露比")
        self.assertTrue(block.startswith("## 剧情记忆"))
        self.assertIn("梗概：", block)
        self.assertIn("片段：", block)
        self.assertIn("露比", block)

    def test_injection_block_respects_budget(self):
        block = self.kb.format_injection("commander", max_chars=500,
                                         dialogue_chars=200)
        self.assertLessEqual(len(block), 1400)

    def test_injection_block_empty_when_nothing_matches(self):
        self.assertEqual(
            self.kb.format_injection("zzzz qqqq xxxx vvvv", min_score=0.5), "")

    # ── auto-injection gate ──────────────────────────────────────

    def test_entity_terms_come_from_the_index(self):
        terms = self.kb.entity_terms()
        self.assertIn("露比", terms)
        self.assertIn("共鸣觉醒", terms)          # chapter title
        self.assertIn("萌芽的心意", terms)          # event name
        self.assertNotIn("的", terms)              # too short

    def test_entity_hit_recognises_names_and_titles(self):
        self.assertTrue(self.kb.entity_hit("你还记得露比吗"))
        self.assertTrue(self.kb.entity_hit("共鸣觉醒讲了什么"))
        self.assertFalse(self.kb.entity_hit("今天天气怎么样"))
        self.assertFalse(self.kb.entity_hit(""))

    def test_gate_blocks_unrelated_chitchat(self):
        """Auto injection must not fire on messages with no story entity."""
        for q in ("今天天气怎么样", "帮我写个正则", "推荐一部电影"):
            self.assertEqual(
                self.kb.format_injection(q, persona_char_id="H076"), "",
                f"{q!r} should not inject",
            )

    def test_gate_allows_entity_queries(self):
        self.assertTrue(
            self.kb.format_injection("露比在雨夜说了什么",
                                     persona_char_id="H076", min_score=0.0165))


    def test_tool_result_has_source_and_dialogue(self):
        out = self.kb.format_tool_result("unconscious ass", limit=1)
        self.assertIn("[story_search]", out)
        self.assertIn("对白：", out)
        self.assertIn("来源：", out)

    def test_tool_result_empty_path(self):
        out = self.kb.format_tool_result("zzzz qqqq xxxx")
        self.assertIn("没有找到", out)


class MissingDatabaseTests(unittest.TestCase):
    def test_missing_db_is_unavailable_and_silent(self):
        kb = StoryKB(os.path.join(tempfile.gettempdir(), "no_such_story.db"))
        self.assertFalse(kb.available)
        self.assertEqual(kb.search("露比"), [])
        self.assertEqual(kb.format_injection("露比"), "")
        self.assertEqual(kb.stats(), {"available": False})

    def test_wrong_schema_version_is_rejected(self):
        import sqlite3

        tmpdir = tempfile.mkdtemp(prefix="storykb_badschema_")
        try:
            path = os.path.join(tmpdir, "story.db")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO meta VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION + 1),))
            conn.commit()
            conn.close()
            self.assertFalse(StoryKB(path).available)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class ProductionDatabaseSmokeTest(unittest.TestCase):
    """Sanity-check the real DB when it has been built (skipped otherwise)."""

    def test_production_db_queries(self):
        kb = StoryKB()
        if not kb.available:
            self.skipTest("data/story_kb/story.db not built yet")
        hits = kb.search("第一次方舟战争", limit=3, persona_char_id="H076")
        self.assertTrue(hits, "expected at least one hit in the production DB")
        self.assertGreater(kb.stats()["chunks"], 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
