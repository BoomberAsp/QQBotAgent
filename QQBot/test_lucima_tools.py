import os
import unittest
from unittest.mock import AsyncMock, patch

from agent.inbound import InboundMessage
from agent.lucima_tools import lucima_group_turn, lucima_private_turn, lucima_tool_names, register_lucima_tools
from agent.tool_registry import ToolRegistry


class LucimaProposalToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        register_lucima_tools(self.registry)
        self.enterContext(patch.dict(os.environ, {"LUCIMA_INTERNAL_TOKEN": "test-only"}))

    async def test_manual_reads_requested_topic_without_creating_a_proposal(self):
        view = InboundMessage(user_id="user", self_id="bot", message_id="request", platform="qq_official")
        sender = AsyncMock()
        request = AsyncMock()
        with patch("agent.lucima_tools.request_lucima", request):
            with lucima_private_turn(view, sender) as turn:
                self.assertIn("lucima_manual", lucima_tool_names())
                overview = await self.registry.execute("lucima_manual", {})
                self.assertIn("Lucima QQ 使用手册", overview)
                self.assertFalse(turn.proposed)
            view.is_group, view.group_id = True, "room"
            with lucima_group_turn(view, sender, direct=True, valid=lambda: True) as turn:
                self.assertIn("lucima_manual", lucima_tool_names())
                topic = await self.registry.execute("lucima_manual", {"topic": "托管"})
                self.assertIn("/lucima 托管 动力转换 20:00", topic)
                self.assertFalse(turn.proposed)
            request.assert_not_awaited()
        sender.send_keyboard.assert_not_awaited()
        sender.send_text.assert_not_awaited()

    async def test_operation_only_posts_proposal_and_token_stays_in_private_confirmation(self):
        view = InboundMessage(user_id="user", self_id="bot", message_id="request", platform="qq_official")
        sender = AsyncMock()
        sender.supports_keyboard = True
        token = "Z" * 16
        request = AsyncMock(return_value={"approval": token, "message": "查询回执。"})
        with patch("agent.lucima_tools.request_lucima", request), lucima_private_turn(view, sender):
            self.assertEqual(lucima_tool_names(), set(self.registry.list_tools()))
            result = await self.registry.execute("lucima_query", {"operation": "receipt"})
            self.assertIn("待确认", result)
            self.assertNotIn(token, result)
            request.assert_awaited_once_with(("qq_official", "bot", "user", "request"), "receipt", "", propose=True)
            self.assertIn(token, sender.send_keyboard.await_args.args[0].rows[0][0].data)
            await self.registry.execute("lucima_rewards", {"operation": "signin"})
            self.assertEqual(request.await_count, 1)
        self.assertEqual(lucima_tool_names(), set())

    async def test_unavailable_star_source_quote_is_reported_without_a_confirmation_card(self):
        view = InboundMessage(user_id="user", self_id="bot", message_id="request", platform="qq_official")
        sender = AsyncMock()
        unavailable = "星源商店暂无可核实的刷新报价。"
        request = AsyncMock(return_value={"status": "quote_unavailable", "message": unavailable})
        with patch("agent.lucima_tools.request_lucima", request), lucima_private_turn(view, sender):
            result = await self.registry.execute("lucima_long", {"operation": "refresh_star_source", "arg": "1"})
        self.assertEqual(result, unavailable)
        sender.send_keyboard.assert_not_awaited()
        sender.send_text.assert_not_awaited()

    async def test_group_and_agent_confirmation_attempts_never_reach_bridge(self):
        sender = AsyncMock()
        sender.supports_keyboard = False
        view = InboundMessage(user_id="user", self_id="bot", message_id="request", platform="onebot", is_group=True)
        with patch("agent.lucima_tools.request_lucima", new_callable=AsyncMock) as request:
            with lucima_private_turn(view, sender):
                self.assertEqual(lucima_tool_names(), set())
                await self.registry.execute("lucima_rewards", {"operation": "mail"})
            view.is_group = False
            with lucima_private_turn(view, sender):
                await self.registry.execute("lucima_account", {"operation": "approve", "arg": "X" * 16})
            request.assert_not_awaited()
