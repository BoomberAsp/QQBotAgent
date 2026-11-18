import asyncio
import ast
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0,str(Path(__file__).parent))
from lib.plane_policy import persona_address,strip_worker_identity,task_presentation


@pytest.mark.parametrize('text',['黑塔？','大黑塔','你是黑塔吗？','黑塔在吗','herta?','你是谁？','你好'])
def test_address_stays_social(text):
    assert persona_address(text,('herta','大黑塔 (The Herta)','大黑塔'))


@pytest.mark.parametrize('text',['查一下黑塔的剧情','黑塔，查一下今天的天气','帮我执行代码','翻译文件','黑塔的语音有哪些'])
def test_task_does_not_match_address(text):
    assert not persona_address(text,('herta','大黑塔 (The Herta)'))


def router_with(reply=None,error=False):
    from lib.model_router import ModelRouter
    router=ModelRouter.__new__(ModelRouter)
    router._task_routing={}
    client=type('Client',(),{})()
    client.chat_completion=AsyncMock(side_effect=RuntimeError('network') if error else None,return_value=reply)
    router.get_client=lambda purpose:client
    return router,client


def test_reported_input_bypasses_llm():
    router,client=router_with(error=True)
    result=asyncio.run(router.classify('黑塔？',persona_key='herta',persona_name='大黑塔 (The Herta)'))
    assert result=={'plane':'social','complexity':'simple'}
    client.chat_completion.assert_not_called()


@pytest.mark.parametrize('bad',[True,False])
def test_ambiguous_failure_not_forced_to_roxy(bad):
    router,_=router_with('unexpected',error=bad)
    assert asyncio.run(router.classify('不明确的话',persona_key='herta'))=={'plane':'social','complexity':'complex'}


def test_triage_receives_selected_identity_and_task_still_routes():
    router,client=router_with('plane=task complexity=complex')
    result=asyncio.run(router.classify('查一下黑塔剧情',persona_key='herta',persona_name='大黑塔'))
    assert result['plane']=='task'
    assert 'herta' in client.chat_completion.call_args.kwargs['message']
    assert '当前会话' in client.chat_completion.call_args.kwargs['message']


def test_worker_identity_guard_preserves_result_facts():
    text='黑塔？你想聊什么？\n\n我这边是 Roxy，不是黑塔本塔~ 想查设定请告诉我。数值是 25.5，来源 https://example.com。'
    fixed=strip_worker_identity(text)
    assert '我是 Roxy' not in fixed and '不是黑塔' not in fixed and '我这边是 Roxy' not in fixed
    assert '数值是 25.5，来源 https://example.com。' in fixed
    assert '想查设定请告诉我。' in fixed


@pytest.mark.parametrize('text',['> 我是 Roxy，不是黑塔。','“我是 Roxy，不是黑塔。”','```text\n我是 Roxy，不是黑塔。\n```'])
def test_identity_quotes_and_code_are_not_rewritten(text):
    assert strip_worker_identity(text)==text


def test_verbatim_request_preserved():
    text='我是 Roxy，不是黑塔。'
    assert strip_worker_identity(text,'逐字输出“我是 Roxy，不是黑塔。”')==text


def test_final_revoice_cannot_passthrough_worker_self_introduction():
    from agent.delegation import revoice_task_result
    client=type('Client',(),{'chat_completion':AsyncMock(return_value='哼，结果在这里。')})()
    result=asyncio.run(revoice_task_result(client=client,persona_content='你是大黑塔',short_name='大黑塔',
                        user_request='黑塔？',response='我这边是 Roxy，不是黑塔本塔~ 查询结果是 25。'))
    assert result.startswith('哼，结果在这里。')
    assert '不是黑塔' not in result and '我这边是 Roxy' not in result
    assert '查询结果是 25。' in result


@pytest.mark.parametrize('pairs',[1,14])
def test_special_session_patches_visible_answer_and_survives_restart(tmp_path,pairs):
    from agent.special_session import SpecialSessionManager
    manager=SpecialSessionManager(str(tmp_path))
    session=manager.create('u1','test',metadata={'persona_key':'herta'})
    manager.switch_to('u1',session.name)
    for _ in range(pairs):
        manager.add_message('u1','user','q')
        manager.add_message('u1','assistant','RAW')
    before=manager.get_active('u1')
    assert manager.replace_last_assistant('u1','RAW','VISIBLE',expected_session_id=session.session_id)
    restarted=SpecialSessionManager(str(tmp_path))
    after=restarted.get_active('u1')
    assert after.context[-1]['content']=='VISIBLE'
    assert after.total_messages==before.total_messages
    assert after.metadata['persona_key']=='herta'
    restarted.add_message('u1','user','next')
    assert restarted.get_active('u1').context[-2]['content']=='VISIBLE'


def test_special_patch_rejects_cleared_folded_or_other_session(tmp_path):
    from agent.special_session import SpecialSessionManager
    manager=SpecialSessionManager(str(tmp_path))
    one=manager.create('u1','one',metadata={'persona_key':'herta'})
    manager.switch_to('u1','one')
    manager.add_message('u1','assistant','FOLDED')
    assert not manager.replace_last_assistant('u1','RAW','VISIBLE',one.session_id)
    assert not manager.replace_last_assistant('u1','FOLDED','VISIBLE','other-id')
    manager.clear_context('u1')
    assert not manager.replace_last_assistant('u1','RAW','VISIBLE',one.session_id)
    assert manager.get_active('u1').context==[]


def test_actual_old_delegation_and_router_regressions():
    from test_agent import TestDelegation,TestModelRouterClassify
    TestDelegation().run()
    TestModelRouterClassify().run()


def test_task_prompt_is_installed_after_persona_and_origins_reset():
    assert '不向用户自我介绍为 Roxy' in task_presentation('herta')
    assert task_presentation('')==''
    file=Path(__file__).parent/'agent/agent.py'
    text=file.read_text(encoding='utf8')
    assert text.index('presentation = task_presentation(')>text.index('persona_content = pm.load(persona_name)')
    source=(Path(__file__).parent/'agent/turn.py').read_text(encoding='utf8')
    assert source.count('persona_key=persona_key, persona_name=pm.get_display_name(persona_key)')==2
    assert 'expected_session_id=active_special.session_id' in source


def test_reverse_identity_leak_removed_without_changing_numbers():
    text='我不是黑塔，我是 Roxy。结果为 25.5。'
    assert strip_worker_identity(text)=='结果为 25.5。'
    assert task_presentation('assistant')==''


def test_delegated_public_identity_context_restored_on_error():
    from agent.context import _current_task_persona,_current_personality,_delegate_depth
    from agent.delegation import run_delegated_task
    class Agent:
        async def run(self,*args,**kwargs):
            assert _current_task_persona.get()=='herta'
            assert _current_personality.get()=='assistant'
            raise RuntimeError('test failure')
    class Model:
        flash_client=reasoning_client=None
        async def classify_complexity(self,task):return 'simple'
    class Permissions:
        def get_role(self,user):return type('Role',(),{'value':'user'})()
        def get_allowed_tools(self,role):return set()
    p=_current_personality.set('herta');t=_current_task_persona.set('');d=_delegate_depth.set(0)
    try:
        result=asyncio.run(run_delegated_task('test',agent=Agent(),model_router=Model(),perm_manager=Permissions(),user_id='u1'))
        assert result.startswith('[Error]')
        assert _current_task_persona.get()=='' and _current_personality.get()=='herta'
    finally:
        _current_personality.reset(p);_current_task_persona.reset(t);_delegate_depth.reset(d)
