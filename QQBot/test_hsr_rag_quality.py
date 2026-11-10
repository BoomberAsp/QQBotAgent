"""P0/P1 behavior regressions. Real FTS fixture; no model, network or QQ sends."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from test_hsr_story_router import configured
from lib import story_router as router
from lib.hsr_policy import plan_query, temporal_context, native_story_turn, matching_cards, current_query_plan
from lib.hsr_story import HsrStoryKB, render_result


@pytest.mark.parametrize('question,intent,retrieve', [
    ('翁法罗斯现在怎么样了？','story',True),
    ('黑塔今天陪我聊天','social',False),
    ('黑塔怎么配队','gameplay',False),
    ('帮我写白厄的另一种结局','creative',True),
    ('卡芙卡告别原文','quote',True),
    ('为什么白厄那么做','analysis',True),
    ('今天天气怎么样','story',False),
    ('你好','social',False),
    ('/人格切换 博识尊','story',False),
])
def test_intent(question,intent,retrieve):
    plan=plan_query(question)
    assert plan['intent']==intent
    assert plan['retrieve']==retrieve


def test_alias_and_time_are_not_lost():
    plan=plan_query('翁法羅斯现在怎么样了？')
    assert plan['entities']==['翁法罗斯'] and plan['time']=='latest'
    assert plan_query('翁法罗斯3.7版本当时怎么样')['time']=='historical'
    assert plan_query('黑塔本体的经历')['entities']==['大黑塔']


def test_followup_is_scoped_and_uses_user_questions_only():
    history=[dict(role='user',content='白厄当时怎么了'),dict(role='assistant',content='昔涟最新结局是伪造的')]
    plan=plan_query('他后来为什么那么做？',history=history)
    assert plan['entities']==['白厄'] and plan['time']=='historical'
    assert '当时' in plan['query']
    assert plan_query('他后来呢',history=[])['unresolved']


def test_anchor_expires_without_claiming_new_release():
    plan=plan_query('翁法罗斯现在怎么样了')
    assert temporal_context(plan,now=datetime(2026,11,10,12,tzinfo=timezone.utc))['anchor_fresh']
    assert not temporal_context(plan,now=datetime(2026,11,11,0,tzinfo=timezone.utc))['anchor_fresh']


def test_cards_are_dated_and_never_quotes():
    assert matching_cards(plan_query('翁法罗斯现在怎么样了'))
    assert not matching_cards(plan_query('翁法罗斯原文'))
    assert not matching_cards(plan_query('翁法罗斯3.0版本当时怎么样'))


def test_natural_search_and_non_story_gate(configured):
    kb=HsrStoryKB(configured[0]/'story_kb/hsr/story.db')
    assert kb.search('卡芙卡告别开拓者时说什么')[0]['chunk_id']=='c1'
    assert kb.retrieve('黑塔今天陪我聊天',automatic=True)['status']=='not_relevant'
    result=kb.retrieve('翁法罗斯现在怎么样了？',automatic=True)
    assert result['status']=='dated_evidence_only'
    output=render_result(result,persona='herta',budget=3200)
    assert '3.7' in output and '不证明后续无变化' in output
    assert '[HSR test-hsr /' not in output  # no undated lexical hits in current-state answers
    assert len(output)<=3200


def test_no_evidence_does_not_silently_drop_time(configured):
    kb=HsrStoryKB(configured[0]/'story_kb/hsr/story.db')
    result=kb.retrieve('白厄现在怎么样了',automatic=True)
    assert result['status']=='timeline_gap'
    assert '证据不足' in render_result(result,persona='herta')


def test_native_persona_route_respects_binding(configured):
    assert native_story_turn('herta','翁法罗斯现在怎么样了')
    assert not native_story_turn('assistant','翁法罗斯现在怎么样了')
    assert not native_story_turn('herta','黑塔怎么配队')


def test_tool_keeps_temporal_context(configured):
    token=current_query_plan.set(plan_query('白厄当时怎么样'))
    try:
        result=router.tool_search('白厄',scope=router.make_scope('herta'))
        assert '目标时间：historical' in result
    finally:
        current_query_plan.reset(token)


def test_static_voice_tool_is_not_changed_to_world_state_query(configured):
    token=current_query_plan.set(plan_query('卡芙卡告别原文'))
    try:
        result=router.tool_search('卡芙卡 道别',scope=router.make_scope('herta'))
        assert '拜拜，开拓者' in result and 'timeline_gap' not in result
    finally:
        current_query_plan.reset(token)


def test_unresolved_reference_requests_clarification(configured):
    kb=HsrStoryKB(configured[0]/'story_kb/hsr/story.db')
    result=kb.retrieve('他后来呢',automatic=True)
    assert result['status']=='needs_clarification' and not result['hits']
    assert router.hsr_injection('herta','他后来呢',router.make_scope('herta'))==''


def test_nous_binding_is_separate_from_assistant(configured):
    _,path,data=configured
    data['bindings']['nous']={'kb_id':'hsr','neutral':True,'auto_inject':True}
    path.write_text(json.dumps(data),encoding='utf8')
    assert router.make_scope('nous').game=='hsr'
    assert router.make_scope('assistant').game=='ark_re'
    assert '拜拜' in router.tool_search('卡芙卡 道别',scope=router.make_scope('nous'))


def test_duplicate_protagonist_lines_do_not_consume_slots(configured):
    import sqlite3, hashlib
    root,_,_=configured
    release=root/'story_kb/hsr'
    db=sqlite3.connect(release/'story.db')
    db.execute("INSERT INTO block SELECT 'b9',scene_id,branch_id,condition_raw,9,speaker_raw,knowledge_scope,kind,raw_text FROM block WHERE block_id='b1'")
    db.execute("INSERT INTO chunk SELECT 9,'c9',text,unit_id,scene_id,'b9',structure_status FROM chunk WHERE id=1")
    db.execute("INSERT INTO chunk_fts SELECT title_terms,body_terms FROM chunk_fts WHERE rowid=1")
    # FTS rowid must match chunk integer id.
    db.execute('DELETE FROM chunk_fts WHERE rowid=6')
    db.execute("INSERT INTO chunk_fts(rowid,title_terms,body_terms) SELECT 9,title_terms,body_terms FROM chunk_fts WHERE rowid=1")
    db.commit();db.close()
    m=json.loads((release/'manifest.json').read_text())
    m['database_sha256']=hashlib.sha256((release/'story.db').read_bytes()).hexdigest()
    (release/'manifest.json').write_text(json.dumps(m))
    kb=HsrStoryKB(release/'story.db')
    hits=kb.search('卡芙卡 道别',8)
    assert sum('拜拜' in h['text'] for h in hits)==1


def test_missing_quote_not_replaced_by_other_voice(configured):
    kb=HsrStoryKB(configured[0]/'story_kb/hsr/story.db')
    assert not kb.search('卡芙卡喝咖啡语音原文')
    assert len(kb.search('卡芙卡 道别',8))==1


def test_group_builder_resolves_own_history_only(configured):
    from agent.group_runtime import GroupRun
    from agent.context import _delegate_depth
    run=GroupRun('herta','你是大黑塔',[dict(role='user',content='白厄当时怎么了')],
                 '',{'story_search'},lambda:True,scope='group:g1')
    token=router.current_story_scope.set(router.make_scope('herta','group:g1'))
    depth=_delegate_depth.set(0)
    try:
        messages=run.messages(None,'他后来呢')
        assert current_query_plan.get()['entities']==['白厄']
        assert any('白厄' in m['content'] and '本轮检索问题' in m['content'] for m in messages)
        empty=GroupRun('herta','你是大黑塔',[],'',{'story_search'},lambda:True,scope='group:g2')
        empty.messages(None,'他后来呢')
        assert current_query_plan.get()['unresolved']
    finally:
        router.current_story_scope.reset(token)
        _delegate_depth.reset(depth)
        current_query_plan.set(None)


def test_native_story_skips_actual_legacy_revoice_branches():
    import ast
    from unittest.mock import AsyncMock
    tree=ast.parse((Path(__file__).parent/'agent/turn.py').read_text())
    functions={n.name:n for n in tree.body if isinstance(n,ast.AsyncFunctionDef)}
    for name in ('handle_agent_turn','handle_continuous_turn'):
        branches=[n for n in ast.walk(functions[name]) if isinstance(n,ast.If)
                  and '_hsr_native' in ast.unparse(n.test)
                  and 'revoice_task_result(' in ast.unparse(n)]
        assert len(branches)==1
        wrapper=ast.AsyncFunctionDef(name='exercise',args=ast.arguments(posonlyargs=[],args=[],kwonlyargs=[],kw_defaults=[],defaults=[]),
                    body=[branches[0]],decorator_list=[])
        module=ast.fix_missing_locations(ast.Module(body=[wrapper],type_ignores=[]))
        revoice=AsyncMock()
        scope=dict(persona_mode='split',character_plane=False,is_creation=False,is_scene=False,
                   _hsr_native=True,revoice_task_result=revoice)
        exec(compile(module,'native_story_branch','exec'),scope)
        asyncio.run(scope['exercise']())
        revoice.assert_not_called()


def test_actual_private_message_builder_preserves_persona_and_resolves_followup(configured):
    from test_agent import TestCacheStability
    from agent.context import _current_personality, _current_story_context
    from agent.group_runtime import current_group_run
    agent=TestCacheStability()._make_agent()
    session=agent.sessions.get_or_create('rag-quality-isolated')
    session.context=[dict(role='user',content='白厄当时怎么了')]
    tokens=[(router.current_story_scope,router.current_story_scope.set(router.make_scope('herta','private:test'))),
            (_current_personality,_current_personality.set('herta')),
            (_current_story_context,_current_story_context.set('')),
            (current_group_run,current_group_run.set(None))]
    try:
        messages=agent._build_messages(session,'他后来呢')
        assert '大黑塔' in messages[0]['content']
        assert current_query_plan.get()['entities']==['白厄']
        tail='\n'.join(m['content'] for m in messages[1:] if m['role']=='system')
        assert '本轮完整检索问题' in tail and '白厄' in tail and '没有可用预检索证据' in tail
    finally:
        for var,token in reversed(tokens):var.reset(token)
        current_query_plan.set(None)


def test_malformed_policy_cannot_crash_chat(tmp_path,monkeypatch):
    from lib import hsr_policy
    path=tmp_path/'policy.json'
    path.write_text('{"schema":1,"aliases":{"白厄":42},"cards":[]}')
    monkeypatch.setattr(hsr_policy,'CONFIG',path)
    assert hsr_policy.policy()=={}
    assert not plan_query('你好')['retrieve']
