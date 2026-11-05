import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from agent.cognitive_state import CognitiveState, scope_key, current_scope, pack_blocks, tool_summary
from agent.profile import ProfileManager

class CognitiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=CognitiveState(self.tmp.name)
        self.scope=scope_key("onebot:bot","","rubi")
    def tearDown(self):
        self.tmp.cleanup()
    def seed(self,text="用户住在杭州",batch="b",event="e",scope=None):
        scope=scope or self.scope
        self.store.ingest("u",scope,self.store.generation("u",scope),batch,[{"content":text,"predicate":"current_city","evidence_turn_ids":[event]}],[event])
        return self.store.list("u",scope)[0]
    def test_scope_permissions_and_no_fallback(self):
        self.seed()
        for owner,scope in [("other",self.scope),("u",scope_key("onebot:bot","group","rubi")),("u",scope_key("onebot:bot","","roxy")),("u",scope_key("official:bot","","rubi"))]:
            self.assertEqual([],self.store.list(owner,scope))
    def test_versioned_correction_blocks_late_results(self):
        row=self.seed()
        generation=self.store.generation("u",self.scope)
        self.store.correct("u",self.scope,row["id"],"用户住在南京")
        self.assertEqual("superseded",self.store.ingest("u",self.scope,generation,"late",[{"content":"用户住在杭州"}],["late"]))
        result=self.store.list("u",self.scope)[0]
        self.assertEqual(2,result["version"])
        self.assertEqual("用户住在南京",result["content"])
        self.assertEqual([],self.store.list("u",self.scope,"杭州"))
    def test_forget_erases_versions_and_derived_data(self):
        row=self.seed()
        self.store.correct("u",self.scope,row["id"],"用户住在南京")
        generation=self.store.generation("u",self.scope)
        self.store.archive("u",self.scope,"summary","用户住在南京")
        self.store.task("u",self.scope,{"goal":"去南京"})
        self.store.forget("u",self.scope,row["id"])
        self.assertEqual([],self.store.list("u",self.scope))
        self.assertEqual([],self.store.recall("u",self.scope,"南京"))
        self.assertEqual({},self.store.task("u",self.scope))
        self.assertEqual("",self.store.archive("u",self.scope,"summary","旧结果",generation))
        with self.store.db() as db:
            self.assertEqual(0,db.execute("SELECT count(*) FROM history").fetchone()[0])
    def test_same_event_different_batch_does_not_reinforce(self):
        row=self.seed()
        self.seed(batch="different")
        self.assertEqual(1,self.store.list("u",self.scope)[0]["supports"])
    def test_relevance_beats_frequency_and_budget_skips_long_block(self):
        self.seed("用户喜欢登山",scope=self.scope)
        self.store.ingest("u",self.scope,0,"travel",[{"content":"用户计划去南京旅行"}],["travel"])
        self.assertEqual("用户计划去南京旅行",self.store.list("u",self.scope,"南京旅行")[0]["content"])
        self.assertEqual("short",pack_blocks(["x"*100,"short"],10))
    def test_raw_view_separate_from_summary_and_task_persists(self):
        raw=json.dumps({"user":"不要发布","assistant":"尚未完成"},ensure_ascii=False)
        self.store.archive("u",self.scope,"conversation",raw)
        self.store.archive("u",self.scope,"summary","用户要求不要发布，任务未完成")
        self.assertEqual("不要发布",self.store.view("u",self.scope)[0]["content"])
        self.store.task("u",self.scope,{"goal":"检查文档","constraints":["不要发布"],"pending":["校对"]})
        reopened=CognitiveState(self.tmp.name)
        self.assertEqual(["不要发布"],reopened.task("u",self.scope)["constraints"])
    def test_tool_summary_keeps_failure_beyond_first_line(self):
        self.assertIn("错误",tool_summary("结果记录\n普通内容\n错误：未能保存"))
        self.assertIn("disk full",tool_summary(json.dumps({"status":"failed","error":"disk full","noise":"x"*1000})))
    def test_scoped_extraction_has_evidence_and_survives_restart(self):
        async def run():
            class Client:
                async def chat_completion(self,prompt,**kwargs):
                    assert "来源消息ID" in prompt
                    return json.dumps({"memory_candidates":[{"content":"用户喜欢登山","evidence_turn_ids":["ev"]}]})
            manager=ProfileManager(self.tmp.name,Client())
            token=current_scope.set(self.scope)
            try:
                manager.observe_turn("u","喜欢登山","好的",turn_id="ev")
                turns=list(manager._recent["u"])
                await manager.extract_batch("u",turns)
                restored=ProfileManager(self.tmp.name,Client())
                self.assertEqual(1,len(restored.cognition.list("u",self.scope)))
                restored.observe_turn("u","喜欢登山","好的",turn_id="ev")
                self.assertEqual(1,len(restored._recent["u"])) # pending persisted before ack; no additional copy
            finally:
                current_scope.reset(token)
        asyncio.run(run())

    def test_clear_retains_facts_but_removes_working_history(self):
        self.seed()
        self.store.archive("u",self.scope,"conversation",json.dumps({"user":"旧历史","assistant":"旧回答"}))
        self.store.task("u",self.scope,{"goal":"旧任务"})
        self.store.clear_context("u",self.scope)
        self.assertEqual([],self.store.view("u",self.scope))
        self.assertEqual({},self.store.task("u",self.scope))
        self.assertEqual(1,len(self.store.list("u",self.scope)))

    def test_history_scope_and_repeated_messages(self):
        row=self.seed()
        self.store.correct("u",self.scope,row["id"],"用户住在南京")
        self.assertEqual(1,len(self.store.versions("u",self.scope,row["id"])))
        self.assertEqual([],self.store.versions("other",self.scope,row["id"]))
        pair=json.dumps({"user":"你好","assistant":"你好"})
        for event in ("one","two","two"):
            self.store.archive("u",self.scope,"conversation",pair,source_id=event)
        self.assertEqual(4,len(self.store.view("u",self.scope)))

    def test_pause_and_late_task_are_fenced(self):
        self.seed()
        generation=self.store.generation("u",self.scope)
        self.store.pause("u",self.scope,True)
        self.assertEqual("",self.store.archive("u",self.scope,"summary","旧摘要"))
        with self.assertRaises(ValueError):
            self.store.task("u",self.scope,{"goal":"旧任务"},expected_generation=generation)
        self.store.pause("u",self.scope,False)
        self.assertEqual("superseded",self.store.ingest("u",self.scope,generation,"late",[{"content":"旧结果"}],["late"]))

    def test_pending_scope_deletion_preserves_other_scope(self):
        manager=ProfileManager(os.path.join(self.tmp.name,"pending"),object())
        token=current_scope.set(self.scope)
        try:
            manager.observe_turn("u","私聊数据","回复",turn_id="private")
            group=scope_key("onebot:bot","group","rubi")
            current_scope.set(group)
            manager.observe_turn("u","群聊数据","回复",turn_id="group")
            manager.discard_scope_pending("u",self.scope)
            self.assertEqual(1,len(manager._recent["u"]))
            self.assertEqual("group",manager._recent["u"][0][3])
            self.assertNotIn("私聊数据",Path(manager._buffer_path("u")).read_text())
        finally:
            current_scope.reset(token)

    def test_agent_run_scope_and_clear_integration(self):
        from unittest.mock import patch
        from agent.agent import Agent
        from agent.session import SessionManager
        from agent.tool_registry import ToolRegistry
        from agent.context import _current_evidence_id
        from test_agent import MockDeepSeekClient
        async def run():
            client=MockDeepSeekClient()
            profiles=ProfileManager(os.path.join(self.tmp.name,"profiles"))
            agent=Agent(client,ToolRegistry(),os.path.join(self.tmp.name,"configs"),
                session_manager=SessionManager(persistence_dir=os.path.join(self.tmp.name,"sessions")),profile_manager=profiles)
            scopes=[]
            token=_current_evidence_id.set(json.dumps(["onebot","bot","","u","m1"]))
            scope_token=current_scope.set("")
            try:
                with patch("agent.character_registry.get_character_registry") as registry:
                    registry.return_value.resolve_effective_personality.return_value="rubi"
                    registry.return_value.load.return_value=""
                    registry.return_value.get_short_name.return_value="rubi"
                    await agent.run("私聊秘密是蓝鲸",user_id="u")
                    private=scope_key("onebot:bot","","rubi")
                    self.assertEqual(2,len(profiles.cognition.view("u",private)))
                    _current_evidence_id.set(json.dumps(["onebot","bot","group","u","m2"]))
                    await agent.run("你好",user_id="u")
                    group=scope_key("onebot:bot","group","rubi")
                    self.assertNotEqual(private,group)
                    self.assertNotIn("蓝鲸",json.dumps(client.chat_calls[-1],ensure_ascii=False))
                    current_scope.set(group)
                    agent.clear_user_session("u")
                    self.assertEqual([],profiles.cognition.view("u",group))
                    self.assertEqual(2,len(profiles.cognition.view("u",private)))
                    await agent.run("隔离请求",user_id="u",persist=False)
                    self.assertEqual(group,current_scope.get())
                    self.assertNotIn("蓝鲸",json.dumps(client.chat_calls[-1],ensure_ascii=False))
            finally:
                _current_evidence_id.reset(token)
                current_scope.reset(scope_token)
        asyncio.run(run())

if __name__ == "__main__":
    unittest.main()
