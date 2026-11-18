import os
import unittest
from unittest.mock import AsyncMock, patch

from agent.inbound import InboundMessage
from agent.lucima_tools import lucima_private_turn, lucima_tool_names, register_lucima_tools
from agent.tool_registry import ToolRegistry


class LucimaProposalToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        register_lucima_tools(self.registry)
        self.enterContext(patch.dict(os.environ, {"LUCIMA_INTERNAL_TOKEN": "test-only"}))

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
