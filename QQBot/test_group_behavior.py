"""Offline group isolation and delivery regressions: python -m unittest test_group_behavior -v."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

from agent.inbound import InboundMessage
from agent.group_state import GroupStore, scope_for, identity_for
from agent import group_runtime as runtime


def view(user='a', group='g', message='m', **kw):
    return InboundMessage(user_id=user, group_id=group, is_group=bool(group),
                          self_id='bot', platform='qq_official', message_id=message,
                          mentioned=True, text='你好', **kw)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = GroupStore(self.tmp.name)

    def test_generation_and_group_isolation(self):
        s = self.store
        a,b = scope_for(view()),scope_for(view(group='other'))
        gen = s.group(a)['generation']
        self.assertTrue(s.commit(a,gen,'one','A','hello','world'))
        self.assertEqual(s.history(b),[])
        s.configure(a,persona='roxy',receive_all=True)
        self.assertEqual(len(s.history(a)),2)
        self.assertFalse(s.commit(a,gen,'old','A','stale','result'))
        self.assertTrue(s.group(a)['config']['receive_all'])
        self.assertEqual(s.group(a)['config']['persona'],'roxy')

    def bind(self):
        s=self.store
        g,p=s.identity(view()),s.identity(view(user='dm',group=''))
        scope=scope_for(view())
        code=s.begin_binding(g['identity'],scope)
        nonce=s.claim_binding(code,p['identity'])
        with self.assertRaises(ValueError):
            s.confirm_binding(g['identity'],'wrong',code,nonce)
        s.confirm_binding(g['identity'],scope,code,nonce)
        return s.resolve(g['identity']),p,scope

    def test_binding_visibility_unlink_and_tombstones(self):
        s=self.store
        g,p,scope=self.bind()
        self.assertEqual(g['bond_user'],'dm')
        s.group(scope)
        s.add_fact(g['identity'],scope,'a','我喜欢Python','source_group',g['revision'],0)
        s.add_fact(p['identity'],'private','b','私有事实','private',p['revision'])
        self.assertEqual(len(s.facts(p['identity'],'private')),2)
        self.assertEqual(len(s.facts(g['identity'],'group',scope)),1)
        self.assertEqual(s.facts(g['identity'],'group','other'),[])
        stranger=s.identity(view(user='stranger',group=''))
        self.assertEqual(s.facts(stranger['identity'],'private'),[])
        fid=s.facts(g['identity'],'group',scope)[0]['id']
        self.assertTrue(s.change_fact(p['identity'],fid,public=True))
        self.assertEqual(len(s.facts(g['identity'],'group','other')),1)
        self.assertTrue(s.change_fact(p['identity'],fid,delete=True))
        self.assertFalse(s.add_fact(g['identity'],scope,'later','旧任务','source_group',g['revision']))
        s.unlink(g['identity'])
        self.assertNotEqual(s.resolve(g['identity'])['bond_user'],'dm')
        self.assertEqual(s.facts(g['identity'],'private'),[])

    def test_clear_blocks_old_fact_writer_and_duplicate_event(self):
        s=self.store; v=view(); scope=scope_for(v); i=s.identity(v)
        s.group(scope)
        self.assertTrue(s.claim(scope,'event'))
        self.assertFalse(s.claim(scope,'event'))
        self.assertTrue(s.claim(scope_for(view(group='other')),'event'))
        s.reset(scope,clear=True)
        self.assertFalse(s.add_fact(i['identity'],scope,'e','我喜欢猫','source_group',i['revision'],0))

    def test_quota_is_durable(self):
        s=self.store; scope=scope_for(view()); config=s.group(scope)['config']
        self.assertTrue(s.reserve_auto(scope,config,100000))
        self.assertFalse(GroupStore(self.tmp.name).reserve_auto(scope,config,100001))
        self.assertTrue(s.reserve_auto(scope,config,100181))

    def test_onebot_identity_shared_without_nickname_matching(self):
        a,b=view(),view(group='')
        a.platform=b.platform='onebot'
        self.assertEqual(self.store.identity(a),self.store.identity(b))
        a.platform=b.platform='qq_official'
        self.assertNotEqual(self.store.identity(a)['canonical'],self.store.identity(b)['canonical'])

    def test_bond_group_does_not_refresh_private_contact(self):
        from agent.bond import BondManager
        b=BondManager(base_dir=str(Path(self.tmp.name)/'bonds'),config_path=str(Path(self.tmp.name)/'missing'))
        b.settle_turn('a','rubi',event_id='dm')
        last=b.get_state('a','rubi')['stats']['last_seen']
        result=b.settle_turn('a','rubi',event_id='g',channel='group')
        self.assertEqual(last,b.get_state('a','rubi')['stats']['last_seen'])
        self.assertTrue(b.settle_turn('a','rubi',event_id='g',channel='group')['duplicate'])
        self.assertEqual(b.get_state('a','rubi')['exp'],result['exp'])


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import agent
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store=GroupStore(self.tmp.name)
        self.sender=NS(send_group_text=AsyncMock(return_value='sent'))
        self.calls=[]
        async def run(*args,**kwargs):
            self.calls.append(runtime.current_group_run.get())
            return '公开回答'
        self.fake=NS(default_sender=self.sender,agent=NS(run=run),
            _perm_manager=NS(get_role=lambda u:NS(value='regular'),get_allowed_tools=lambda r:{'get_time','read_file'}),
            get_group_features=lambda:NS(refresh=lambda:None,get_disabled_tools=lambda g:set()),
            _bond_manager=NS(get_level=lambda *a:2,settle_turn=MagicMock()),
            _model_router=NS(flash_client=NS(chat_completion=AsyncMock(return_value='{}'))),
            set_usage_context=lambda *a:None)
        pm=NS(is_builtin=lambda p:True,load=lambda p:'群人格',get_mode=lambda p:'split')
        for p in (patch.object(runtime,'_store',self.store),
                  patch.object(agent,'turn',self.fake,create=True),
                  patch.dict('sys.modules',{'agent.turn':self.fake}),
                  patch('agent.character_registry.get_character_registry',return_value=pm)):
            p.start(); self.addCleanup(p.stop)
        for name in ('_locks','_buffers','_follow','_jobs','_confirmations'):
            getattr(runtime,name).clear()

    async def test_members_share_only_group_history_and_dedup(self):
        await runtime.run_group_turn(view(),self.sender)
        await runtime.run_group_turn(view(user='b',message='n'),self.sender)
        self.assertEqual(len(self.calls[1].history),2)
        self.assertNotIn('read_file',self.calls[1].tools)
        await runtime.run_group_turn(view(user='b',message='n'),self.sender)
        self.assertEqual(len(self.calls),2)
        await runtime.run_group_turn(view(group='other',message='x'),self.sender)
        self.assertEqual(self.calls[-1].history,[])
        self.assertEqual(self.fake._bond_manager.settle_turn.call_count,3)

    async def test_failed_delivery_no_history_or_bond(self):
        self.sender.send_group_text.return_value='uncertain'
        await runtime.run_group_turn(view(),self.sender)
        self.assertEqual(self.store.history(scope_for(view())),[])
        self.fake._bond_manager.settle_turn.assert_not_called()

    async def test_reset_during_model_discards_answer(self):
        entered=asyncio.Event(); release=asyncio.Event()
        async def slow(*a,**k):
            entered.set(); await release.wait(); return '旧结果'
        self.fake.agent.run=slow
        task=asyncio.create_task(runtime.run_group_turn(view(),self.sender))
        await entered.wait()
        self.store.reset(scope_for(view()))
        release.set(); await task
        self.sender.send_group_text.assert_not_awaited()
        self.assertEqual(self.store.history(scope_for(view())),[])

    async def test_cancel_requires_original_actor_or_group_admin(self):
        entered=asyncio.Event()
        async def slow(*a,**k):
            entered.set(); await asyncio.Future()
        self.fake.agent.run=slow
        task=asyncio.create_task(runtime.run_group_turn(view(),self.sender))
        await entered.wait()
        other=view(user='b',message='cancel-b'); other.text='/取消'
        await runtime.run_group_turn(other,self.sender)
        self.assertFalse(task.done())
        owner=view(message='cancel-a'); owner.text='/取消'
        await runtime.run_group_turn(owner,self.sender)
        await task
        self.assertEqual(self.store.history(scope_for(owner)),[])

    async def test_personal_memory_reaches_only_verified_private_user(self):
        from agent.context import _current_evidence_id, _current_group_id
        g,p=self.store.identity(view()),self.store.identity(view(user='dm',group=''))
        scope=scope_for(view()); self.store.group(scope)
        code=self.store.begin_binding(g['identity'],scope)
        nonce=self.store.claim_binding(code,p['identity'])
        self.store.confirm_binding(g['identity'],scope,code,nonce)
        g=self.store.resolve(g['identity'])
        self.store.add_fact(g['identity'],scope,'fact','我喜欢Python','source_group',g['revision'],0)
        token=_current_evidence_id.set(json.dumps(['qq_official','bot','','dm','m']))
        token2=_current_group_id.set('')
        try:
            self.assertIn('Python',runtime.private_memory_block('dm'))
            self.assertEqual(runtime.private_memory_block('other'),'')
        finally:
            _current_evidence_id.reset(token); _current_group_id.reset(token2)

    async def test_private_commands_and_management_permissions(self):
        v=view(); v.text='/邮件'
        await runtime.run_group_turn(v,self.sender)
        self.assertEqual(self.calls,[])
        v=view(message='n'); v.text='/群人格 rubi'
        await runtime.run_group_turn(v,self.sender)
        self.assertEqual(self.store.group(scope_for(v))['config']['persona'],'assistant')
        v=view(message='o',actor_role='admin'); v.text='/群人格 rubi'
        await runtime.run_group_turn(v,self.sender)
        self.assertEqual(self.store.group(scope_for(v))['config']['persona'],'assistant')
        self.fake._perm_manager.get_role=lambda u:NS(value='admin')
        v=view(message='p');v.text='/群人格 rubi'
        await runtime.run_group_turn(v,self.sender)
        self.assertEqual(self.store.group(scope_for(v))['config']['persona'],'rubi')

    async def test_natural_agent_decides_without_judge_or_keywords(self):
        v=view();v.mentioned=False;v.text='哈哈今天这个梗真有意思'
        self.store.configure(scope_for(v),receive_all=True,mode='natural')
        await runtime.run_group_turn(v,self.sender)
        self.assertEqual(len(self.calls),1)
        self.assertTrue(self.calls[0].autonomous)
        self.fake._model_router.flash_client.chat_completion.assert_not_awaited()
        self.sender.send_group_text.assert_awaited_once()

    async def test_silence_does_not_send_commit_or_consume_quota(self):
        v=view();v.mentioned=False;v.text='普通聊天'
        scope=scope_for(v);self.store.configure(scope,receive_all=True,mode='natural')
        async def silent(*a,**k):return '[[GROUP_SILENT]]'
        self.fake.agent.run=silent
        await runtime.run_group_turn(v,self.sender)
        self.sender.send_group_text.assert_not_awaited()
        self.assertEqual(self.store.history(scope),[])
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM quotas').fetchone()[0],0)
        self.fake._bond_manager.settle_turn.assert_not_called()

    async def test_autonomous_prompt_offers_reply_and_silence(self):
        run=runtime.GroupRun('assistant','人格',[],'',set(),lambda:True,autonomous=True)
        messages=run.messages(None,'普通消息')
        self.assertIn('[[GROUP_SILENT]]',str(messages))
        self.assertIn('不要求消息是疑问句',str(messages))

    async def test_external_reset_clears_runtime_buffer(self):
        v=view();scope=scope_for(v)
        self.store.configure(scope,receive_all=True)
        await runtime.run_group_turn(v,self.sender)
        self.assertTrue(runtime._buffers[scope])
        self.store.reset(scope)
        v=view(message='after');v.text='新消息'
        await runtime.run_group_turn(v,self.sender)
        self.assertEqual(len(runtime._buffers[scope]),1)
        self.assertNotIn('你好',self.calls[-1].context)

    async def test_overheard_picture_then_question_uses_group_workspace_and_pixels(self):
        from PIL import Image
        from agent.context import _current_user_workspace
        from agent.inbound import Attachment
        v=view();v.text='';v.mentioned=False;v.attachment_kinds=('image',)
        self.store.configure(scope_for(v),receive_all=True)
        async def resolve(event):
            workspace=Path(_current_user_workspace.get())
            self.assertTrue(workspace.is_relative_to(self.store.root/'workspaces'))
            path=workspace/'uploads'/'sample.png';path.parent.mkdir(exist_ok=True)
            Image.new('RGB',(32,32),'green').save(path)
            return [Attachment(kind='image',path=str(path))]
        resolver=AsyncMock(side_effect=resolve)
        v.resolver=NS(resolve=resolver)
        await runtime.run_group_turn(v,self.sender)
        resolver.assert_not_awaited()
        asked=view(message='next');asked.text='刚才那张图是什么颜色？'
        self.fake._model_router.reasoning_client=NS(supports_vision=True)
        async def inspect(text,user,**kw):
            self.assertIs(kw['client'],self.fake._model_router.reasoning_client)
            parts=runtime.current_group_run.get().messages(None,text,kw['images'])[-1]['content']
            self.assertEqual(parts[1]['type'],'image_url')
            self.assertTrue(parts[1]['image_url']['url'].startswith('data:image/'))
            return '绿色'
        self.fake.agent.run=inspect
        await runtime.run_group_turn(asked,self.sender)
        resolver.assert_awaited_once()
        self.assertEqual(self.store.history(scope_for(v))[-1]['content'],'绿色')

    async def test_image_without_vision_is_never_an_empty_message(self):
        v=view();v.text='';v.attachment_kinds=('image',)
        await runtime.run_group_turn(v,self.sender)
        self.assertIn('[图片×1]',self.store.history(scope_for(v))[0]['content'])

    async def test_sticker_first_delivery_outcomes(self):
        async def choose(*args,**kwargs):
            self.assertEqual(runtime.current_group_run.get().sticker_choices,[{'id':'happy'}])
            return '[[STICKER:happy]]\n好耶'
        async def enqueue(sticker_id):
            runtime.current_group_run.get().media.append(('/test.png',sticker_id))
        self.fake.agent.run=choose
        for outcome in ('sent','failed','uncertain'):
            v=view(group=outcome)
            self.store.configure(scope_for(v),persona='rubi')
            self.sender.send_group_image=AsyncMock(return_value=outcome)
            self.sender.send_group_text.reset_mock()
            with patch('agent.media_tools.group_sticker_candidates',return_value=[{'id':'happy'}]),patch('agent.media_tools.send_sticker',side_effect=enqueue),patch('agent.media_tools.published',return_value=True):
                await runtime.run_group_turn(v,self.sender)
            self.sender.send_group_image.assert_awaited_once()
            if outcome=='failed':self.sender.send_group_text.assert_awaited_once_with('好耶')
            else:self.sender.send_group_text.assert_not_awaited()
            history=self.store.history(scope_for(v))
            if outcome=='sent':self.assertNotIn('好耶',history[-1]['content'])
            if outcome=='uncertain':self.assertEqual(history,[])

    async def test_mention_wrapped_commands_stop_before_agent(self):
        scope=scope_for(view())
        self.store.configure(scope,receive_all=True,mode='natural')
        for i,text in enumerate(('<@opaque-id> /群状态','<@!123> /未知命令','\ufeff<@123>\u200b／群人格 rubi','[CQ:at,qq=123] /群重置','@机器人 /群帮助')):
            v=view(message=str(i));v.text=text
            await runtime.run_group_turn(v,self.sender)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.store.history(scope),[])
        self.assertEqual(self.store.group(scope)['config']['persona'],'assistant')
        self.assertEqual(self.sender.send_group_text.await_count,5)
        v=view(message='other');v.text='<@another> /群状态';v.mentioned=False
        await runtime.run_group_turn(v,self.sender)
        self.assertEqual(self.sender.send_group_text.await_count,5)
        self.assertEqual(self.calls,[])
        v=view(message='chat');v.text='<@123> 今天讨论 /群状态 的用法'
        await runtime.run_group_turn(v,self.sender)
        self.assertEqual(len(self.calls),1)

    async def test_public_group_commands_use_system_handlers(self):
        from agent.context import _turn_sender,_current_group_id
        from agent.public_commands import PUBLIC_COMMANDS
        for command in PUBLIC_COMMANDS:
            for spelling in (command.name,*command.aliases):
                with self.subTest(command=spelling):
                    async def handle(text,user,**opts):
                        self.assertIs(_turn_sender.get(),self.sender)
                        self.assertEqual(_current_group_id.get(),'g')
                        self.assertEqual(text,command.name+' 3')
                        if command.renders_image:self.assertFalse(opts['allow_images'])
                        return True
                    mock=AsyncMock(side_effect=handle)
                    setattr(self.fake,command.handler,mock)
                    v=view(message=spelling);v.text='<@123> '+spelling+' 3'
                    self.store.configure(scope_for(v),features={'image':False})
                    await runtime.run_group_turn(v,self.sender)
                    mock.assert_awaited_once()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.store.history(scope_for(v)),[])
        for spelling in ('/反馈','/邮件','/清除形象','/刷新角色数据'):
            v=view(message=spelling);v.text=spelling
            await runtime.run_group_turn(v,self.sender)
            self.assertIn('私聊',self.sender.send_group_text.await_args.args[0])
        self.assertEqual(self.calls,[])

    async def test_group_game_owner_followup_and_direct_calculation(self):
        from agent.group_games import _pending
        self.addCleanup(_pending.clear)
        async def say(user,text,mid,direct=True):
            v=view(user=user,message=mid);v.text=text;v.mentioned=direct
            await runtime.run_group_turn(v,self.sender)
        self.store.configure(scope_for(view()),receive_all=True)
        await say('a','/luansu','a1')
        await say('b','190 200','b1',False)
        self.assertEqual(self.sender.send_group_text.await_count,1)
        await say('a','<@bot> 190 200','a2')
        self.assertIn('乱速概率',self.sender.send_group_text.await_args.args[0])
        await say('a','/测速\n我方\n兔子 0 100 220\n敌方\n目标 0 88','a3')
        self.assertIn('193.6',self.sender.send_group_text.await_args.args[0])
        with patch('tools.legacy_tools.gacha_pull',new_callable=AsyncMock,return_value='招募结果') as pull:
            await say('a','/十连抽','a4')
            await say('a','几率UP','a5',False)
            await say('a','UP 夏妮','a6',False)
            pull.assert_awaited_once_with('几率up招募',10,'夏妮')
        await say('a','/测速','a7')
        await say('a','/取消','a8',False)
        self.assertFalse(_pending)
        self.assertFalse(self.calls)

    async def test_unmentioned_default_no_agent(self):
        v=view(); v.mentioned=False
        await runtime.run_group_turn(v,self.sender)
        self.assertEqual(self.calls,[])
        self.sender.send_group_text.assert_not_awaited()

    async def test_extract_failures_and_attribution(self):
        v=view(); v.text='B说A喜欢猫'; ident=self.store.identity(v)
        self.store.group(scope_for(v))
        client=NS(chat_completion=AsyncMock(side_effect=RuntimeError('backend unavailable')))
        await runtime.extract_self_fact(self.store,v,ident,0,client)
        client.chat_completion.assert_not_awaited()
        v.text='我喜欢猫'
        await runtime.extract_self_fact(self.store,v,ident,0,client)
        self.assertEqual(self.store.facts(ident['identity'],'private'),[])


class PromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_agent_never_reads_private_stores(self):
        from agent.agent import Agent
        from agent.tool_registry import ToolRegistry
        from test_agent import MockDeepSeekClient
        with tempfile.TemporaryDirectory() as tmp:
            client=MockDeepSeekClient()
            sessions=MagicMock()
            sessions.get_or_create.side_effect=AssertionError('private session read')
            special=MagicMock()
            special.get_active.side_effect=AssertionError('private special read')
            special.get_context_window.return_value=128000
            a=Agent(client,ToolRegistry(),tmp,session_manager=sessions,special_session_manager=special)
            run=runtime.GroupRun('assistant','群人格',[],json.dumps({'visible':'公开事实'},ensure_ascii=False),set(),lambda:True)
            tok=runtime.current_group_run.set(run)
            try:
                await a.run('你好','private-user',session_type='special')
            finally:
                runtime.current_group_run.reset(tok)
            sessions.get_or_create.assert_not_called()
            special.get_active.assert_not_called()
            self.assertTrue(client.chat_calls)
            _,messages,tools=client.chat_calls[0]
            self.assertEqual(tools,[])
            self.assertIn('公开事实',str(messages))

    async def test_tool_calls_cannot_escalate_group_permissions(self):
        from agent.agent import Agent
        from agent.session import Session
        from agent.tool_registry import ToolRegistry
        from test_agent import MockDeepSeekClient
        with tempfile.TemporaryDirectory() as tmp:
            registry=ToolRegistry()
            registry.execute=AsyncMock(return_value='ok')
            a=Agent(MockDeepSeekClient(),registry,tmp)
            run=runtime.GroupRun('assistant','群人格',[],'',{'get_time'},lambda:True)
            tok=runtime.current_group_run.set(run)
            try:
                calls=[dict(id='x',function=dict(name='read_file',arguments='{}'))]
                result=await a._execute_tool_calls(calls,Session(user_id='group'),'group',{'read_file','get_time'})
                registry.execute.assert_not_awaited()
                self.assertIn('Permission',str(result))
                run.valid=lambda:False
                calls[0]['function']['name']='get_time'
                await a._execute_tool_calls(calls,Session(user_id='group'),'group',{'get_time'})
                registry.execute.assert_not_awaited()
            finally:
                runtime.current_group_run.reset(tok)


if __name__=='__main__':
    unittest.main()
