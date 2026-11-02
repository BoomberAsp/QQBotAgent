import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent import durable
from agent.profile import ProfileManager
from agent.memory import TieredMemory


class P1Reliability(unittest.TestCase):
    def test_partial_commit_recovers_before_replay(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                class Client:
                    calls = 0
                    async def chat_completion(self, *args, **kwargs):
                        self.calls += 1
                        return json.dumps({"new_preferences":{"style":"short"}, "memory_candidates":[
                            {"content":"用户喜欢登山", "judge":"new"}]})
                client = Client()
                mgr = ProfileManager(d, client)
                mgr.set_memory(TieredMemory(os.path.join(d, "memory")))
                turns = [("我喜欢登山", "", "", "platform-event-1")]
                original = durable.atomic_json
                def failure(path, value):
                    if str(path) == mgr._memory._tier_path("u"):
                        raise OSError("simulated crash after profile commit")
                    original(path, value)
                with patch.object(durable, "atomic_json", side_effect=failure):
                    with self.assertRaises(OSError):
                        await mgr.extract_batch("u", turns)
                self.assertTrue(os.path.exists(mgr._journal_path("u")))
                restarted = ProfileManager(d, client)
                restarted.set_memory(TieredMemory(os.path.join(d, "memory")))
                self.assertTrue(await restarted.extract_batch("u", turns))
                self.assertEqual(1, client.calls)
                self.assertEqual(1, restarted._memory._get_state("u").short[0].count)
                self.assertEqual("short", restarted.get("u").preferences["style"])
                self.assertFalse(os.path.exists(mgr._journal_path("u")))
        asyncio.run(run())

    def test_journal_never_overwrites_uncoordinated_new_edit(self):
        with tempfile.TemporaryDirectory() as d:
            target = str(Path(d)/"target.json")
            journal = str(Path(d)/"journal.json")
            durable.atomic_json(target, {"value":"old"})
            durable.atomic_json(journal, {"writes":[{"path":target, "before":durable.revision(target), "value":{"value":"extraction"}}]})
            durable.atomic_json(target, {"value":"user correction"})
            with self.assertRaises(RuntimeError):
                durable.recover(journal)
            self.assertEqual("user correction", json.loads(Path(target).read_text())["value"])

    def test_startup_recovers_without_new_message(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                class Client:
                    async def chat_completion(self, *args, **kwargs):
                        return "{}"
                old = ProfileManager(d, Client())
                old.observe_turn("u", "single pending turn", "", turn_id="id")
                mgr = ProfileManager(d, Client())
                await mgr.start_recovery()
                await asyncio.sleep(.05)
                await mgr.stop_recovery()
                self.assertEqual(0, mgr._pending_n["u"])
                self.assertEqual([], json.loads(Path(mgr._buffer_path("u")).read_text())["turns"])
        asyncio.run(run())

    def test_stale_tier_writer_cannot_overwrite_newer_state(self):
        with tempfile.TemporaryDirectory() as d:
            first = TieredMemory(d)
            first.ingest_candidates("u", [{"content":"用户喜欢登山", "judge":"new"}])
            first.save_user("u")
            stale = TieredMemory(d)
            stale.load_user("u")
            first.ingest_candidates("u", [{"content":"用户喜欢游泳", "judge":"new"}])
            first.save_user("u")
            with self.assertRaises(RuntimeError):
                stale.save_user("u")
            self.assertEqual(2, len(TieredMemory(d).load_user("u").short))

    def test_webui_style_edit_finishes_pending_commit_then_wins(self):
        with tempfile.TemporaryDirectory() as d:
            mgr = ProfileManager(d)
            profile = mgr.get("u")
            profile.preferences["style"] = "old"
            mgr.save(profile)
            path = mgr._path("u")
            desired = profile.to_dict()
            desired["preferences"] = {"style":"extracted"}
            durable.atomic_json(mgr._journal_path("u"), {"writes":[
                {"path":path, "before":durable.revision(path), "value":desired}]})
            profile.preferences["style"] = "user correction"
            mgr.save(profile)
            self.assertFalse(os.path.exists(mgr._journal_path("u")))
            self.assertEqual("user correction", ProfileManager(d).get("u").preferences["style"])

    def test_platform_identity_wiring(self):
        # Contract check plus ContextVar isolation, without importing live adapters.
        from agent.context import _current_evidence_id
        async def run():
            async def one(value):
                token = _current_evidence_id.set(value)
                await asyncio.sleep(0)
                try:
                    return _current_evidence_id.get()
                finally:
                    _current_evidence_id.reset(token)
            self.assertEqual(["onebot:1", "official:1"], await asyncio.gather(one("onebot:1"), one("official:1")))
        asyncio.run(run())

if __name__ == "__main__":
    unittest.main()
