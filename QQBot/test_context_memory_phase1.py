import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from agent.profile import ProfileManager
from agent.memory import TieredMemory
from agent.request_budget import fit_request, ContextBudgetError, request_cost
from test_agent import TestSessionCompression


class PhaseOne(unittest.TestCase):
    def test_compression_failure_preserves_old_and_concurrent_append(self):
        helper = TestSessionCompression()
        with tempfile.TemporaryDirectory() as d:
            mgr = helper._mgr(d)
            helper._fill(mgr, "u", "chat", 32)
            old = mgr.get_by_name("u", "chat").context
            original = mgr._atomic_context
            def fail(path, value):
                if path.endswith("committed_context.json"):
                    raise OSError("disk full")
                return original(path, value)
            with patch.object(mgr, "_atomic_context", side_effect=fail):
                with self.assertRaises(OSError):
                    mgr.apply_compression("u", "chat", "summary")
            self.assertEqual(old, mgr.get_by_name("u", "chat").context)
            mgr.add_message("u", "user", "new")
            with self.assertRaises(ValueError):
                mgr.apply_compression("u", "chat", "summary", expected_context=old)
            self.assertEqual("new", mgr.get_by_name("u", "chat").context[-1]["content"])

    def test_budget_removes_whole_exchange_and_counts_tools(self):
        msgs = [{"role":"system", "content":"rules"}, {"role":"user", "content":"old"},
                {"role":"assistant", "tool_calls":[{"id":"a", "function":{"name":"x", "arguments":"{}"}}]},
                {"role":"tool", "tool_call_id":"a", "content":"中"*6000},
                {"role":"user", "content":"now"}]
        fitted = fit_request(msgs, [], 5000, 100)
        self.assertEqual(["system", "user"], [m["role"] for m in fitted])
        self.assertEqual(5, len(msgs))
        with self.assertRaises(ContextBudgetError):
            fit_request(fitted, [{"description":"中"*6000}], 5000, 100)
        with self.assertRaises(ContextBudgetError):
            fit_request([{"role":"tool", "tool_call_id":"missing", "content":"x"}], [])

    def test_replay_and_conflicting_edit(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                class Client:
                    async def chat_completion(self, *args, **kwargs):
                        return json.dumps({"memory_candidates":[{"content":"用户喜欢登山", "judge":"new"}]})
                mgr = ProfileManager(d, Client())
                memory = TieredMemory(os.path.join(d, "mem"))
                mgr.set_memory(memory)
                turns = [("我喜欢登山", "好", "", "event-1")]
                self.assertTrue(await mgr.extract_batch("u", turns))
                restored = ProfileManager(d, Client())
                restored.set_memory(TieredMemory(os.path.join(d, "mem")))
                self.assertTrue(await restored.extract_batch("u", turns))
                self.assertEqual(1, restored._memory._get_state("u").short[0].count)
                restored.observe_turn("u", "我喜欢登山", "好", turn_id="event-1")
                self.assertEqual(0, len(restored._recent["u"]))
                class EditingClient:
                    async def chat_completion(self, *args, **kwargs):
                        profile = restored.get("u")
                        profile.preferences["风格"] = "新选择"
                        restored.save(profile)
                        return json.dumps({"updated_preferences":{"风格":"旧选择"}})
                restored.client = EditingClient()
                self.assertTrue(await restored.extract_batch("u", [("old", "", "", "event-2")]))
                self.assertEqual("新选择", restored.get("u").preferences["风格"])
                class MustNotRun:
                    async def chat_completion(self, *args, **kwargs):
                        raise AssertionError("superseded evidence was replayed")
                resumed = ProfileManager(d, MustNotRun())
                resumed.set_memory(TieredMemory(os.path.join(d, "mem")))
                self.assertTrue(await resumed.extract_batch("u", [("old", "", "", "event-2")]))

        asyncio.run(run())

    def test_failed_parse_keeps_batch_and_recovers(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                class BadClient:
                    async def chat_completion(self, *args, **kwargs):
                        return "not json"
                mgr = ProfileManager(d, BadClient())
                for i in range(2):
                    mgr.observe_turn("u", str(i), "", turn_id=str(i))
                turns = list(mgr._recent["u"])
                mgr._batch_sizes["u"] = 2
                mgr._save_buffer("u")
                # No acknowledgement on invalid extraction.
                self.assertIsNone(await mgr.extract_batch("u", turns))
                restored = ProfileManager(d, BadClient())
                restored._restore_buffer("u")
                self.assertEqual(turns, list(restored._recent["u"]))
                self.assertEqual(2, restored._batch_sizes["u"])
                # Appending a new event must not change the persisted short batch.
                restored.observe_turn("u", "new", "", turn_id="new")
                self.assertEqual(2, restored._batch_sizes["u"])
                self.assertEqual(3, len(restored._recent["u"]))
        asyncio.run(run())

    def test_inflight_buffer_survives_restart_without_overlap(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                gate = asyncio.Event()
                class Client:
                    async def chat_completion(self, *args, **kwargs):
                        await gate.wait()
                        return "{}"
                mgr = ProfileManager(d, Client())
                for i in range(15):
                    mgr.observe_turn("u", str(i), "reply", turn_id=str(i))
                await asyncio.sleep(0)
                with open(mgr._buffer_path("u")) as f:
                    self.assertEqual(15, len(json.load(f)["turns"]))
                gate.set()
                await asyncio.sleep(.05)
                self.assertEqual(0, len(mgr._recent["u"]))
        asyncio.run(run())

if __name__ == "__main__":
    unittest.main()
