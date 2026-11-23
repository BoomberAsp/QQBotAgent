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
from agent.group_chatter import split_chatter
from agent.group_state import GroupStore, scope_for, identity_for
from agent import group_runtime as runtime


def view(user='a', group='g', message='m', **kw):
    return InboundMessage(user_id=user, group_id=group, is_group=bool(group),
                          self_id='bot', platform='qq_official', message_id=message,
                          mentioned=True, text='你好', **kw)


class ChatterTests(unittest.TestCase):
    def test_split_at_sentence_boundaries_and_keep_content(self):
        text = "哈哈！它还知道把柜门关上。看这熟练度，估计不是第一次了。"
        first = "哈哈！它还知道把柜门关上。"
        self.assertEqual(split_chatter(text, 3), [first, text[len(first):]])
        self.assertEqual(split_chatter(text, 1), [text])
        self.assertEqual(split_chatter(text, 3, mention_every_part=True), [text])

    def test_preserve_quotes_links_and_structured_content(self):
        text = "它说「你又偷吃了吗？我都看到了！」笑死我了。看它那副理直气壮的样子。"
        pieces = split_chatter(text, 3)
        self.assertEqual(len(pieces), 2)
        self.assertIn("你又偷吃了吗？我都看到了！", pieces[0])
        for protected in (
            "第一句够长可以切开。来源 https://example.com。",
            "这里是代码块。"+chr(10)+chr(96)+"example"+chr(96),
            "这里是说明。"+chr(10)+"1. 按原顺序使用。",
        ):
            self.assertEqual(split_chatter(protected, 3), [protected])


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

    def test_legacy_history_hides_encoded_mention_ids(self):
        scope = scope_for(view())
        generation = self.store.group(scope)["generation"]
        self.store.commit(scope, generation, "old", "member", "<@old_openid> 来领票", "好")
        for limit in (0, 20):
            history = json.dumps(self.store.history(scope, limit=limit), ensure_ascii=False)
            self.assertIn("[提及了群成员] 来领票", history)
            self.assertNotIn("old_openid", history)

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


class PrivateImageSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_caption_merges_but_keeps_reverse_search_offer(self):
        from agent import image_context
        from agent.inbound import Attachment
        from tools.image_source_tools import prepare

        with tempfile.TemporaryDirectory() as workspace:
            path = Path(workspace) / "photo.png"
            path.touch()
            photo = InboundMessage(user_id="a", self_id="bot", message_id="p", platform="onebot",
                                   attachments=[Attachment("image", str(path))])
            caption = InboundMessage(user_id="a", self_id="bot", message_id="c", platform="onebot",
                                     text="它怎么趴键盘上了")
            waiting = asyncio.create_task(image_context.coalesce_private_images(photo, "private"))
            await asyncio.sleep(0)
            self.assertIsNone(await image_context.coalesce_private_images(caption, "private"))
            grouped = await waiting
            attachments = await grouped.load_attachments()
            self.assertEqual(grouped.text, caption.text)
            self.assertEqual([att.path for att in attachments], [str(path)])
            with patch.dict("os.environ", {"FIND_SOURCE_ENABLED": "on"}):
                offer = prepare(grouped, workspace, attachments, "", text=grouped.text)
            self.assertIn("要帮你查", offer.question)
            self.assertFalse(offer.authorized)

    async def test_private_photo_offer_and_natural_confirmation(self):
        from contextlib import ExitStack
        from agent import survey_flow
        from agent.cognitive_state import current_scope
        from agent.inbound import Attachment
        from lib.reverse_search import service, source_verification
        from lib.reverse_search.source_evidence import SourceEvidence
        from lib.reverse_search.vlm import parse_clues
        from tools import image_source_followup, image_source_tools

        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            dirs = ("USER_DATA_ROOT", "CHARACTERS_DIR", "BONDS_DIR",
                    "MAILBOX_DIR", "MEMORY_DIR", "ASSETS_DIR")
            stack.enter_context(patch.dict("os.environ", {
                key: str(Path(tmp) / key.lower()) for key in dirs
            }))
            from agent import turn

            path = Path(tmp) / "photo.png"
            data = b"\x89PNG\r\n\x1a\n" + bytes(1000)
            path.write_bytes(data)
            pm = NS(resolve_effective_personality=lambda *args: "assistant",
                    get_mode=lambda persona: "unified", get_short_name=lambda persona: "Roxy")
            model = NS(flash_client=NS(), classify_complexity=AsyncMock(return_value="simple"))

            async def run(*args, allowed_tools, **kwargs):
                if "find_image_source" in allowed_tools:
                    self.assertEqual(allowed_tools, {"find_image_source"})
                    await turn._tool_registry.execute("find_image_source", {})
                    return "模型误称找不到来源"
                return "普通回复"

            stack.enter_context(patch.dict("os.environ", {"FIND_SOURCE_ENABLED": "on"}))
            stack.enter_context(patch.object(survey_flow, "try_handle", AsyncMock(return_value=False)))
            model_run = AsyncMock(side_effect=run)
            stack.enter_context(patch.multiple(
                turn,
                _workspace_manager=NS(ensure_dirs=MagicMock(), get_workspace=lambda user: tmp),
                _special_sessions=NS(get_active=lambda user: None),
                _session_manager=NS(message_count=lambda user: 0),
                _handle_cleanup_flow=AsyncMock(return_value=False),
                _quota_warning=lambda user: "",
                _record_session_file=MagicMock(),
                _record_file=MagicMock(),
                _any_vision_client=lambda: False,
                _build_render_context=lambda mode: "",
                get_character_registry=lambda: pm,
                _model_router=model,
                _story_persona_allowed=lambda persona: False,
                _safe_send=AsyncMock(return_value=True),
                _send_response=AsyncMock(),
                agent=NS(run=model_run),
            ))
            profile_url = "https://x.com/ChocoQAQ"
            search = stack.enter_context(patch.object(service, "run", AsyncMock(
                return_value=service.FindResult(
                    vlm_text="查到图片来源。", visual=parse_clues('{"watermarks":["@ChocoQAQ"]}'),
                    sources=[SourceEvidence("https://vgen.co/ChocoQAQ", match="watermark"),
                             SourceEvidence("https://mobile.twstalker.com/ChocoQAQ", match="watermark"),
                             SourceEvidence(profile_url, kind="profile", match="watermark"),
                             SourceEvidence("https://x.com/ChocoQAQ/status/123", kind="post", match="watermark")]))))
            read = stack.enter_context(patch.object(source_verification, "read_public_profile",
                                    AsyncMock(side_effect=asyncio.TimeoutError)))
            lookup = stack.enter_context(patch.object(source_verification, "lookup_watermark", AsyncMock()))
            stack.callback(image_source_followup._recent.clear)
            token = current_scope.set("private-test")
            stack.callback(current_scope.reset, token)
            stack.callback(image_source_tools._pending.clear)

            async def send(text, message, image=False):
                v = view(user="private-user", group="", message=message)
                v.text = text
                if image:
                    v.attachments = [Attachment("image", str(path))]
                await turn.handle_agent_turn(v)

            await send("", "first", image=True)
            self.assertIn("第三方", turn._safe_send.await_args.args[0])
            await send("不用了", "no")
            await send("可以", "unrelated-yes")
            search.assert_not_awaited()
            await send("", "second", image=True)
            await send("可以", "yes")
            search.assert_awaited_once_with(data, image_path=str(path), user_id="private-user")
            self.assertIn("查到图片来源。", turn._send_response.await_args.args[0])
            model_run.reset_mock()
            await send("帮我去抓取一下那个账号", "profile-followup")
            self.assertIn(profile_url, turn._safe_send.await_args.args[0])
            self.assertIn("原图出处待核实", turn._safe_send.await_args.args[0])
            self.assertIn("网页读取超时", turn._safe_send.await_args.args[0])
            model_run.assert_not_awaited()
            read.assert_awaited_once_with(profile_url)
            lookup.assert_not_awaited()
            search.assert_awaited_once()
            await send("帮我查这张图的出处", "explicit", image=True)
            self.assertEqual(search.await_count, 2)


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

    async def test_lucima_direct_turn_delivers_card_without_leaking_token_to_group_history(self):
        from agent.lucima_tools import register_lucima_tools
        from agent.tool_registry import ToolRegistry

        registry = ToolRegistry()
        register_lucima_tools(registry)
        v = view()
        v.text = "帮我用钻石刷秘密商店 90 次"
        ticket = "Z" * 16
        bridge = AsyncMock(return_value={"approval": ticket, "message": "预计最多扣除 270 钻石。"})
        self.sender.send_group_keyboard = AsyncMock(return_value="sent")

        async def model(text, *args, allowed_tools, **kwargs):
            run = runtime.current_group_run.get()
            self.assertIn("lucima_long", allowed_tools)
            system = "\n".join(item["content"] for item in run.messages(None, text) if item["role"] == "system")
            self.assertIn("秘密商店怎么刷", system)
            self.assertIn("回到原群 @ 机器人", system)
            self.assertIn("/璐茜玛 确认 <8 位核对码>", system)
            self.assertIn("/璐茜玛 执行 <卡片编号>", system)
            result = await registry.execute("lucima_long", {"operation": "refresh_secret", "arg": "90 diamond"})
            self.assertIn("待确认", result)
            self.assertNotIn(ticket, result)
            await registry.execute("lucima_query", {"operation": "status"})
            return "模型误称已执行"

        self.fake.agent.run = model
        with patch.dict("os.environ", {"LUCIMA_INTERNAL_TOKEN": "test-only"}):
            with patch("agent.lucima_tools.request_lucima", bridge):
                await runtime.run_group_turn(v, self.sender)
        bridge.assert_awaited_once_with(("qq_official", "bot", "a", "m", "g"),
                                       "refresh_secret", "90 diamond", propose=True)
        self.sender.send_group_text.assert_not_awaited()
        self.sender.send_group_keyboard.assert_awaited_once()
        text, rows, fallback = self.sender.send_group_keyboard.await_args.args
        self.assertIn("270 钻石", text)
        self.assertIn(f"/璐茜玛 执行 {ticket}", rows[0][0].data)
        self.assertIn(ticket, fallback)
        history = json.dumps(self.store.history(scope_for(v)), ensure_ascii=False)
        self.assertNotIn(ticket, history)
        self.assertNotIn("模型误称已执行", history)

    async def test_lucima_help_followup_and_text_confirmation(self):
        from agent.lucima_tools import register_lucima_tools
        from agent.tool_registry import ToolRegistry

        registry = ToolRegistry()
        register_lucima_tools(registry)
        asked = view()
        asked.text = "璐茜玛能做什么"
        follow = view(message="follow")
        follow.mentioned = False
        follow.text = "签到"
        explicit = view(message="reply", reply_to_bot=True)
        explicit.mentioned = False
        explicit.platform = "onebot"
        explicit.text = "帮我领取邮件"
        ticket = "Z" * 16
        bridge = AsyncMock(return_value={"approval": ticket, "message": "领取邮件。"})

        async def model(text, *args, allowed_tools, **kwargs):
            if asked.text in text:
                return "璐茜玛可以签到和领取奖励。"
            if explicit.text in text:
                self.assertIn("lucima_rewards", allowed_tools)
                await registry.execute("lucima_rewards", {"operation": "mail"})
                return ""
            self.assertNotIn("lucima_rewards", allowed_tools)
            await registry.execute("lucima_rewards", {"operation": "signin"})
            return "发起操作请先 @ 机器人。"

        self.fake.agent.run = model
        with patch.dict("os.environ", {"LUCIMA_INTERNAL_TOKEN": "test-only"}):
            with patch("agent.lucima_tools.request_lucima", bridge):
                await runtime.run_group_turn(asked, self.sender)
                self.assertIn("可以签到", self.sender.send_group_text.await_args.args[0])
                await runtime.run_group_turn(follow, self.sender)
                bridge.assert_not_awaited()
                await runtime.run_group_turn(explicit, self.sender)
        bridge.assert_awaited_once_with(("onebot", "bot", "a", "reply", "g"), "mail", "", propose=True)
        self.assertIn(f"/璐茜玛 执行 {ticket}", self.sender.send_group_text.await_args.args[0])
        self.assertIn(f"/璐茜玛 放弃 {ticket}", self.sender.send_group_text.await_args.args[0])

    async def test_group_chatter_mentions_follow_trigger_and_permission(self):
        from agent import media_ledger
        media_root = self.store.root / "test-media"
        env = patch.dict("os.environ", {"MEDIA_DATA_ROOT": str(media_root)})
        env.start()
        self.addCleanup(env.stop)
        media_ledger.set_policy({"auto_global_seconds": 0}, 0)
        body = "这只猫开柜门的动作也太熟练了。它最后还转头看了你好几秒。下次记得把猫粮藏到另一个柜子里。"
        parts = split_chatter(body, 3)
        self.assertEqual(len(parts), 3)
        send_part = AsyncMock(return_value="sent")
        self.sender.send_group_reply_text = send_part
        self.fake.agent.run = AsyncMock(return_value=body)
        for group, active, direct, quote, mentions in (
            ("has-active", True, False, False, [None, None, None]),
            ("no-active", False, False, False, ["a", "a", "a"]),
            ("direct-natural", False, True, False, ["a", None, None]),
            ("quoted-only", False, False, True, [None, None, None]),
        ):
            event = view(group=group, explicit_mention=direct, reply_to_bot=quote)
            event.mentioned = direct
            scope = scope_for(event)
            self.store.configure(scope, mode="natural", receive_all=True, active_enabled=active)
            send_part.reset_mock()
            await runtime.run_group_turn(event, self.sender)
            self.assertEqual([call.args[0] for call in send_part.await_args_list], parts)
            self.assertEqual([call.kwargs["mention_user"] for call in send_part.await_args_list], mentions)
            self.assertEqual(self.store.history(scope)[-1]["content"], body)
        self.sender.send_group_text.assert_not_awaited()

    async def test_other_member_mention_is_context_only_until_bot_is_addressed(self):
        event = view(message="other-at", explicit_mention=False, other_mentions=("华天崎",))
        event.mentioned = False
        event.text = "还没给我送票捏"
        scope = scope_for(event)
        self.store.configure(scope, mode="natural", receive_all=True)
        survey = AsyncMock(return_value=False)
        with patch("agent.survey_flow.try_handle", survey):
            await runtime.run_group_turn(event, self.sender)
            self.assertEqual(self.calls, [])
            self.sender.send_group_text.assert_not_awaited()
            survey.assert_not_awaited()

            named_bot = view(message="direct", explicit_mention=True, other_mentions=("",))
            prompts = []

            async def model(text, *args, **kwargs):
                run = runtime.current_group_run.get()
                prompts.extend(message["content"] for message in run.messages(None, text))
                self.assertFalse(run.autonomous)
                return "公开回答"

            self.fake.agent.run = model
            await runtime.run_group_turn(named_bot, self.sender)

        combined = "\n".join(prompts)
        self.assertIn("[此消息 @ 了其他群成员：\u300c华天崎\u300d] 还没给我送票捏", combined)
        self.assertIn("[此消息 @ 了其他群成员：昵称未知的成员] 你好", prompts[-1])
        self.sender.send_group_text.assert_awaited_once()

    async def test_sticker_submission_question_provides_group_button(self):
        from agent.sticker_help import SUBMIT_TEMPLATE

        self.sender.send_group_keyboard = AsyncMock(return_value="sent")
        original = view(message="upload-question", explicit_mention=True)
        original.text = "怎么上传我自己的表情包？"
        await runtime.run_group_turn(original, self.sender)
        self.assertEqual(self.calls, [])
        _, rows, plain = self.sender.send_group_keyboard.await_args.args
        self.assertEqual(rows[0][0].data, SUBMIT_TEMPLATE)
        self.assertIn("描述：拍拍肩膀", plain)

        other = view(message="other-upload-question", explicit_mention=False, other_mentions=("朋友",))
        other.mentioned = False
        other.text = original.text
        self.store.configure(scope_for(other), mode="natural", receive_all=True)
        await runtime.run_group_turn(other, self.sender)
        self.sender.send_group_keyboard.assert_awaited_once()

    async def test_group_chatter_merges_for_capacity_or_tool_results(self):
        body = "这只猫还知道把柜门关上。看这熟练度，估计不是第一次了。猫粮还是换个地方收起来吧。"
        self.assertEqual(len(split_chatter(body, 3)), 3)
        send_part = AsyncMock(return_value="sent")
        self.sender.send_group_reply_text = send_part
        self.fake.agent.run = AsyncMock(return_value=body)
        for slots in (1, 2):
            self.sender.group_reply_slots = slots
            await runtime.run_group_turn(view(message=f"quota-{slots}"), self.sender)
            send_part.assert_awaited_once_with(body, mention_user="a")
            send_part.reset_mock()
        self.sender.group_reply_slots = 3

        async def tool_run(*args, **kwargs):
            runtime.current_group_run.get().used_tools = True
            return body

        self.fake.agent.run = tool_run
        await runtime.run_group_turn(view(message="tool"), self.sender)
        send_part.assert_awaited_once_with(body, mention_user="a")

    async def test_group_chatter_partial_delivery_only_stores_confirmed_prefix(self):
        body = "这只猫开柜门的动作也太熟练了。它最后还转头看了你好几秒。下次记得把猫粮藏到另一个柜子里。"
        send_part = AsyncMock(side_effect=["sent", "uncertain", "sent"])
        self.sender.send_group_reply_text = send_part
        self.fake.agent.run = AsyncMock(return_value=body)
        event = view(message="split")
        await runtime.run_group_turn(event, self.sender)
        scope = scope_for(event)
        self.assertEqual(send_part.await_count, 2)
        self.assertEqual(self.store.history(scope)[-1]["content"], split_chatter(body, 3)[0])
        with self.store.db() as db:
            deliveries = db.execute("SELECT event,status FROM deliveries WHERE scope=? ORDER BY rowid", (scope,)).fetchall()
        self.assertEqual([tuple(row) for row in deliveries], [("split", "sent"), ("split:text:2", "uncertain")])

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

    async def test_rubi_relationship_routes_to_ark_story_but_skills_stay_gameplay(self):
        from agent.group_resources import story_search

        base = view()
        self.store.configure(scope_for(base), persona="rubi")
        self.fake._perm_manager.get_allowed_tools = lambda role: {"character_detail", "bond_detail"}
        results = []

        async def run(text, *args, allowed_tools, **kw):
            current = runtime.current_group_run.get()
            results.append((current.game_intent, set(allowed_tools), current.context))
            if current.game_intent == "story":
                self.assertEqual(story_search("莱拉"), "Ark剧情证据")
            return "答复"

        self.fake.agent.run = run
        with patch("lib.story_router.tool_search", return_value="Ark剧情证据") as story:
            for i, text in enumerate(("你认识莱拉吗？", "莱拉技能怎么用？",
                                      "你认识莱拉吗？她技能怎么用？")):
                turn = view(message=f"rubi-{i}")
                turn.text = text
                await runtime.run_group_turn(turn, self.sender)
        self.assertEqual([item[0] for item in results], ["story", "gameplay", "mixed"])
        self.assertNotIn("character_detail", results[0][1])
        self.assertNotIn("story_search", results[1][1])
        self.assertTrue({"story_search", "character_detail"} <= results[2][1])
        self.assertIn("Ark剧情证据", results[0][2])
        self.assertNotIn("Ark剧情证据", results[1][2])
        self.assertTrue(all(call.args[2].game == "ark_re" for call in story.call_args_list))

    async def test_group_image_references_preserve_speaker_and_explicit_quote(self):
        from hashlib import sha256
        from PIL import Image
        from agent.inbound import Attachment

        scope = scope_for(view())
        self.store.configure(scope, receive_all=True)
        directory = self.store.root / "workspaces" / sha256(scope.encode()).hexdigest()
        directory.mkdir(parents=True)
        for sender in ("a", "b"):
            path = directory / f"{sender}.png"
            Image.new("RGB", (8, 8)).save(path)
            photo = view(user=sender, message=f"image-{sender}")
            photo.mentioned = False
            photo.text = ""
            photo.attachments = [Attachment("image", str(path))]
            await runtime.run_group_turn(photo, self.sender)

        seen = []

        async def reply(text, user, **kwargs):
            seen.extend(Path(path).stem for path in kwargs["images"])
            return "看到了"

        self.fake.agent.run = reply
        self.fake._model_router.flash_client.supports_vision = True
        for sender, mid, quote in (("a", "own", ""), ("a", "quoted", "image-b"), ("c", "other", "")):
            follow = view(user=sender, message=mid, reply_message_id=quote)
            follow.text = "刚才那张图是什么？"
            await runtime.run_group_turn(follow, self.sender)
        self.assertEqual(seen, ["a", "b"])
        self.assertIn("没对上", self.sender.send_group_text.await_args.args[0])

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

    async def test_bare_lucima_login_pin_is_handled_without_the_agent(self):
        room = scope_for(view())
        self.store.configure(room, receive_all=True, mode="natural")
        pin = "12345678"
        bridge = AsyncMock()
        with patch.dict("os.environ", {"LUCIMA_INTERNAL_TOKEN": "test-only"}), \
                patch("agent.lucima_tools.request_lucima", bridge):
            for i, text in enumerate((f"确认 {pin}", f"<@bot> 确认{pin}", f"／确认 {pin}")):
                v = view(message=f"bare-pin-{i}")
                v.text = text
                await runtime.run_group_turn(v, self.sender)
                answer = self.sender.send_group_text.await_args.args[0]
                self.assertIn("回到原群", answer)
                self.assertIn("/璐茜玛 确认 <8 位核对码>", answer)
                self.assertNotIn(pin, answer)

            v = view(message="bare-pin-by-other")
            v.text = f"<@another> 确认 {pin}"
            v.mentioned = False
            await runtime.run_group_turn(v, self.sender)
            bridge.assert_not_awaited()
        self.assertEqual(self.sender.send_group_text.await_count, 3)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.history(room), [])

    async def test_lucima_manual_is_discoverable_and_answers_mentions(self):
        with patch.dict("os.environ", {"BOT_PUBLIC_SITE_URL": "https://bot.example/", "LUCIMA_INTERNAL_TOKEN": ""}):
            help_view = view(message="manual-help")
            help_view.text = "<@bot> /群帮助"
            await runtime.run_group_turn(help_view, self.sender)
            self.assertIn("/lucima 使用手册", self.sender.send_group_text.await_args.args[0])

            for i, command in enumerate(("/lucima 使用手册", "/LUCIMA  使用手册", "/璐茜玛 使用手册",
                                         "/璐西玛 使用手册", "/露西玛 使用手册")):
                v = view(message=f"manual-{i}")
                v.text = "<@bot> " + command
                await runtime.run_group_turn(v, self.sender)
                self.assertIn("Lucima QQ 使用手册", self.sender.send_group_text.await_args.args[0])
                self.assertIn("https://bot.example/lucima/", self.sender.send_group_text.await_args.args[0])

            v = view(message="manual-no-mention")
            v.text = "<@another> /lucima 使用手册"
            v.mentioned = False
            await runtime.run_group_turn(v, self.sender)

        self.assertEqual(self.sender.send_group_text.await_count, 6)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.history(scope_for(v)), [])

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

    async def test_find_source_group_command_gets_view_and_aliases(self):
        handler=AsyncMock(return_value=True)
        self.fake._handle_find_source_command=handler
        cases=(('/找图','/找图'),('/找源','/找图'),('/找作者','/找图'),
               ('/找图帮助','/找图帮助'),('/找图 https://x/y.png','/找图 https://x/y.png'))
        for spelling,expected in cases:
            with self.subTest(command=spelling):
                v=view(message=spelling);v.text='<@123> '+spelling
                await runtime.run_group_turn(v,self.sender)
                handler.assert_awaited_once_with(expected,'a',view=v)
                handler.reset_mock()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.store.history(scope_for(v)),[])

    async def test_image_source_agent_uses_only_the_uploaders_confirmation(self):
        from hashlib import sha256
        from agent.inbound import Attachment
        from lib.reverse_search import service, source_verification
        from lib.reverse_search.source_evidence import SourceEvidence
        from lib.reverse_search.vlm import parse_clues
        from tools import image_source_followup, image_source_tools

        self.addCleanup(image_source_followup._recent.clear)
        photo = view(message="photo")
        scope = scope_for(photo)
        workspace = self.store.root / "workspaces" / sha256(scope.encode()).hexdigest()
        workspace.mkdir(parents=True)
        path = workspace / "test.png"
        data = b"\x89PNG\r\n\x1a\n" + bytes(1000)
        path.write_bytes(data)
        photo.attachments = [Attachment("image", str(path))]
        photo.attachment_kinds = ("image",)
        photo.text = "<@bot>"
        self.fake._perm_manager.get_allowed_tools = lambda role: {"find_image_source"}

        observed = []

        async def run(*args, allowed_tools, **kwargs):
            can_search = "find_image_source" in allowed_tools
            observed.append(can_search)
            if can_search:
                self.assertEqual(allowed_tools, {"find_image_source"})
                await image_source_tools.find_image_source()
                return "模型误称找不到来源"
            return "看到了。"

        self.fake.agent.run = run
        profile = "https://x.com/ChocoQAQ"
        search = AsyncMock(return_value=service.FindResult(
            vlm_text="找到来源。", visual=parse_clues('{"watermarks":["@ChocoQAQ"]}'),
            sources=[SourceEvidence(profile, kind="profile", match="watermark")]))
        read = AsyncMock(return_value=source_verification.PublicProfile("公开账号", "公开简介示例"))
        with patch.object(service, "run", search), patch.object(source_verification, "read_public_profile", read), \
             patch.dict("os.environ", {"FIND_SOURCE_ENABLED": "on"}):
            await runtime.run_group_turn(photo, self.sender)
            self.assertIn("要帮你查这张图的来源吗", self.sender.send_group_text.await_args.args[0])
            self.assertIn("第三方", self.sender.send_group_text.await_args.args[0])
            for user, group, mid in (("b", "g", "other-member"), ("a", "other", "other-group")):
                reply = view(user=user, group=group, message=mid)
                reply.text = "<@bot> 可以"
                await runtime.run_group_turn(reply, self.sender)
            search.assert_not_awaited()
            yes = view(message="confirm")
            yes.text = "可以"
            await runtime.run_group_turn(yes, self.sender)
            search.assert_awaited_once_with(data, image_path=str(path), user_id="a")
            self.assertIn("找到来源", self.sender.send_group_text.await_args.args[0])
            again = view(message="confirm-again")
            again.text = "可以"
            await runtime.run_group_turn(again, self.sender)
            search.assert_awaited_once()
            for user, group, mid in (("b", "g", "followup-other-user"), ("a", "other", "followup-other-group")):
                other = view(user=user, group=group, message=mid)
                other.text = "帮我查那个账号"
                await runtime.run_group_turn(other, self.sender)
            read.assert_not_awaited()
            followup = view(message="followup-owner")
            followup.text = "帮我查那个账号"
            await runtime.run_group_turn(followup, self.sender)
            read.assert_awaited_once_with(profile)
            self.assertIn("公开简介示例", self.sender.send_group_text.await_args.args[0])
            self.store.reset(scope, clear=True)
            after_reset = view(message="followup-after-reset")
            after_reset.text = "帮我查那个账号"
            await runtime.run_group_turn(after_reset, self.sender)
            read.assert_awaited_once()
        self.assertEqual(observed, [False, False, False, True, False, False, False, False])

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
