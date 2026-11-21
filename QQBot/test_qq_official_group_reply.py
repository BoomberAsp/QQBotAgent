"""Wire regressions for official QQ group mentions. No QQ connection required."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from nonebot.adapters.qq import Bot
from nonebot.adapters.qq.exception import ActionFailed, NetworkError
from nonebot.drivers import Response

from agent.outbound import OutImage

_MEMBER = "A0B1C2D3E4F500000000000000000001"


class Transport:
    # Run the SDK serialization with the actual OpenAPI calls mocked.
    send_to_group = Bot.send_to_group
    _prepare_message = staticmethod(Bot._prepare_message)
    _extract_send_message = staticmethod(Bot._extract_send_message)
    _extract_qq_media = staticmethod(Bot._extract_qq_media)

    def __init__(self):
        self.post_group_messages = AsyncMock()
        self.post_group_files = AsyncMock(return_value=SimpleNamespace(file_info="uploaded-image"))


class GroupReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        env = {
            key: str(self.root / key.lower())
            for key in ("USER_DATA_ROOT", "CHARACTERS_DIR", "BONDS_DIR", "MAILBOX_DIR", "MEMORY_DIR", "ASSETS_DIR")
        }
        env["QQ_OFFICIAL_MARKDOWN"] = "on"
        environment = patch.dict(os.environ, env)
        environment.start()
        self.addCleanup(environment.stop)

        from adapters import qq_official

        breaker = patch.object(qq_official, "_md_breaker", qq_official._MdBreaker())
        breaker.start()
        self.addCleanup(breaker.stop)
        self.transport = Transport()
        event = SimpleNamespace(group_openid="group", id="incoming", _reply_seq=0)
        self.sender = qq_official.QQOfficialSender(self.transport, event, True)

    async def test_first_segment_uses_markdown_mention_and_next_uses_plain_text(self):
        self.assertEqual(await self.sender.send_group_reply_text("团长早。", mention_user=_MEMBER), "sent")
        self.assertEqual(await self.sender.send_group_reply_text("今天也来啦。"), "sent")

        first, second = [call.kwargs for call in self.transport.post_group_messages.await_args_list]
        self.assertEqual([first["msg_type"], second["msg_type"]], [2, 0])
        self.assertEqual(first["markdown"].content, f'<qqbot-at-user id="{_MEMBER}" /> 团长早。')
        self.assertIsNone(first["content"])
        self.assertEqual(second["content"], "今天也来啦。")
        self.assertEqual([first["msg_id"], second["msg_id"]], ["incoming", "incoming"])
        self.assertEqual([first["msg_seq"], second["msg_seq"]], [1, 2])
        self.assertNotIn("message_reference", second)

    async def test_rejected_markdown_falls_back_to_body_only(self):
        wire = self.transport.post_group_messages
        wire.side_effect = [ActionFailed(Response(400, content=b'{"code":40034011}')), None]

        self.assertEqual(await self.sender.send_group_reply_text("团长早。", mention_user=_MEMBER), "sent")
        first, second = [call.kwargs for call in wire.await_args_list]
        self.assertEqual([first["msg_type"], second["msg_type"]], [2, 0])
        self.assertEqual(second["content"], "团长早。")
        self.assertNotIn("markdown", second)
        self.assertEqual(second["msg_seq"], 2)
        self.assertEqual(self.sender.group_reply_slots, 4)

    async def test_uncertain_markdown_delivery_is_not_sent_again(self):
        wire = self.transport.post_group_messages
        wire.side_effect = NetworkError("connection interrupted")

        self.assertEqual(await self.sender.send_group_reply_text("团长早。", mention_user=_MEMBER), "uncertain")
        self.assertEqual(wire.await_count, 1)

    async def test_disabled_markdown_drops_raw_mention_markup(self):
        with patch.dict(os.environ, {"QQ_OFFICIAL_MARKDOWN": "off"}):
            text = f'<@{_MEMBER}> <qqbot-at-user id="{_MEMBER}" /> 团长早。'
            self.assertEqual(await self.sender.send_group_reply_text(text, mention_user=_MEMBER), "sent")
        payload = self.transport.post_group_messages.await_args.kwargs
        self.assertEqual(payload["msg_type"], 0)
        self.assertEqual(payload["content"], "团长早。")

    async def test_image_stays_native_with_no_visible_id(self):
        image = self.root / "sticker.png"
        image.write_bytes(b"test-image")
        self.assertEqual(await self.sender.send_group_reply_image(OutImage(path=str(image)), mention_user=_MEMBER), "sent")
        self.transport.post_group_files.assert_awaited_once()
        self.transport.post_group_messages.assert_awaited_once()
        payload = self.transport.post_group_messages.await_args.kwargs
        self.assertEqual(payload["msg_type"], 7)
        self.assertIsNone(payload["content"])
        self.assertNotIn("markdown", payload)
