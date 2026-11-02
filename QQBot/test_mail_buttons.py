"""Mailbox navigation regressions; no QQ connection or LLM required."""
import os
import tempfile
import time
import types
import unittest
from datetime import datetime
from unittest.mock import patch

from test_agent import _import_turn_for_tests
from agent.mailbox import MailboxManager
from lib.button_spec import validate


class Sender:
    platform = "qq_official"
    supports_markdown = True
    supports_keyboard = True

    def __init__(self):
        self.out = []

    async def send_text(self, text):
        self.out.append(text)

    async def send_markdown(self, text):
        self.out.append(text)

    async def send_keyboard(self, message):
        self.out.append(message)


class TestMailButtons(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.turn = _import_turn_for_tests()
        self.tmp = tempfile.TemporaryDirectory()
        self.box = MailboxManager(self.tmp.name)
        self.sender = Sender()
        self.uid = "mail_buttons_user"
        self.view = types.SimpleNamespace(platform="qq_official", is_group=False)
        self.patches = [patch.object(self.turn, "_mailbox_manager", self.box),
                        patch.object(self.turn, "default_sender", self.sender),
                        patch.object(self.turn, "_MAIL_PAGES", {})]
        for p in self.patches:
            p.start()
        self.token = self.turn._turn_sender.set(None)
        self.seed(12)

    def tearDown(self):
        self.turn._turn_sender.reset(self.token)
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def seed(self, count):
        self.letters = [dict(id=f"L-20261102-{i:04x}", name="同名角色", persona="p",
                             text=f"全文{i} *保留正文*", preview=f"预览{i}",
                             created_at=time.time() - i, read=False)
                        for i in range(count)]
        box = self.box.load_user(self.uid)
        box["letters"] = self.letters
        self.box.save_user(self.uid, box)

    async def command(self, text):
        self.sender.out.clear()
        self.assertTrue(await self.turn._handle_mail_command(text, self.uid, self.view))
        return self.sender.out[-1]

    def buttons(self, message):
        self.assertEqual(validate(message.rows), [])
        return [b for row in message.rows for b in row]

    async def test_open_read_return_and_explicit_page_buttons(self):
        page = await self.command("/邮件")
        buttons = self.buttons(page)
        opens = [b for b in buttons if b.id.startswith("mail_open_")]
        self.assertEqual(len(opens), 5)
        self.assertEqual(len({b.label for b in opens}), 5)
        self.assertTrue(all(b.action == "cmd" and b.enter for b in buttons))
        self.assertTrue(all(not l["read"] for l in self.box.list_letters(self.uid)))
        next_button = next(b for b in buttons if b.label == "下一页")
        self.assertEqual(next_button.data, "/邮件 p2")
        detail = await self.command(opens[0].data)
        self.assertIn("全文0", detail.markdown)
        self.assertNotIn("理性判断", detail.markdown)
        self.assertTrue(self.box.get_letter(self.uid, self.letters[0]["id"])["read"])
        back = self.buttons(detail)[0]
        self.assertEqual(back.data, "/邮件")
        await self.command(back.data)
        page2 = await self.command(next_button.data)
        self.assertIn("第 2/3 页", page2.markdown)
        page3 = await self.command(next(b.data for b in self.buttons(page2)
                                        if b.label == "下一页"))
        self.assertIn("第 3/3 页", page3.markdown)
        self.assertFalse(any(b.label == "下一页" for b in self.buttons(page3)))
        self.assertIn("第 2/3 页", (await self.command("/邮件 上一页")).markdown)
        self.assertIn("第 3/3 页", (await self.command("/邮件 下一页")).markdown)

    async def test_date_pagination_and_plain_degradation(self):
        mmdd = datetime.fromtimestamp(self.letters[0]["created_at"]).strftime("%m-%d")
        page = await self.command(f"/邮件 {mmdd}")
        next_cmd = next(b.data for b in self.buttons(page) if b.label == "下一页")
        self.assertEqual(next_cmd, f"/邮件 {mmdd} p2")
        self.assertIn("第 2/3 页", (await self.command(next_cmd)).markdown)
        self.sender.supports_keyboard = False
        text = await self.command("/邮件 p2")
        self.assertIsInstance(text, str)
        self.assertIn("/邮件 p3", text)
        self.assertIn(self.letters[5]["id"], text)
        self.assertNotIn("理性判断", text)

    async def test_channel_privacy_expired_missing_withdrawn_and_empty(self):
        for view in [types.SimpleNamespace(platform="onebot", is_group=False),
                     types.SimpleNamespace(platform="qq_official", is_group=True)]:
            self.sender.out.clear()
            await self.turn._handle_mail_command("/邮件", self.uid, view)
            self.assertIn("仅官方单聊", self.sender.out[-1])
        self.letters[0]["created_at"] = time.time() - 29 * 86400
        box = self.box.load_user(self.uid)
        box["letters"] = self.letters
        self.box.save_user(self.uid, box)
        expired = await self.command(f"/邮件 {self.letters[0]['id']}")
        self.assertIn("没有找到", expired.markdown)
        self.assertFalse(self.box.get_letter(self.uid, self.letters[0]["id"])["read"])
        self.assertEqual(self.buttons(expired)[0].data, "/邮件")
        self.sender.out.clear()
        await self.turn._handle_mail_command(f"/邮件 {self.letters[1]['id']}", "other", self.view)
        self.assertIn("没有找到", self.sender.out[-1].markdown)
        box = self.box.load_user(self.uid)
        box["digests"] = [dict(letter_ids=["L-withdrawn"])]
        box["letters"] = []
        self.box.save_user(self.uid, box)
        self.assertIn("撤回", (await self.command("/邮件 L-withdrawn")).markdown)
        empty = await self.command("/邮件")
        self.assertIn("没有角色来信", empty.markdown)
        self.assertEqual(self.buttons(empty)[0].data, "/邮件")

    async def test_keyboard_wire_protocol(self):
        from adapters.qq_official_buttons import to_message_keyboard
        page = await self.command("/邮件")
        wire = to_message_keyboard(page.rows, scope="c2c", openid=self.uid)
        for row in wire.content.rows:
            for button in row.buttons:
                self.assertEqual(button.action.type, 2)
                self.assertTrue(button.action.enter)
                self.assertTrue(button.action.data.startswith("/邮件"))
                self.assertIsNone(button.action.anchor)

    async def test_envelope_truncation_and_preview_caps(self):
        import nonebot
        try:
            nonebot.get_driver()
        except ValueError:
            nonebot.init()
        import plugins.qq_official as po
        letters = [dict(l, name="超长角色名称测试", preview="预" * 21)
                   for l in self.letters]
        cfg = dict(mail_full_when_single=False, mail_max_chars=220,
                   mail_max_previews=100)
        md, plain, rows = po._build_envelope(letters, cfg)
        self.assertIn("另有", md)
        self.assertEqual(validate(rows), [])
        visible_previews = sum(line.startswith("- **") for line in md.splitlines())
        flat = [b for row in rows for b in row]
        self.assertEqual(len(flat), min(10, visible_previews))
        self.assertTrue(all(b.data == f"/邮件 {l['id']}"
                            for b, l in zip(flat, letters)))
        with patch.dict(os.environ, QQ_OFFICIAL_BUTTONS_MAX_PER_ROW="1",
                        QQ_OFFICIAL_BUTTONS_MAX_ROWS="1"):
            md, plain, rows = po._build_envelope(letters, dict(cfg, mail_max_chars=2000))
            self.assertEqual(validate(rows), [])
            self.assertEqual(sum(map(len, rows)), 1)

    async def test_hot_read_layout_caps(self):
        for cols in (1, 2, 3, 5):
            for rows in (1, 2, 5):
                with patch.dict(os.environ, QQ_OFFICIAL_BUTTONS_MAX_PER_ROW=str(cols),
                                QQ_OFFICIAL_BUTTONS_MAX_ROWS=str(rows)):
                    page = await self.command("/邮件 p2")
                    self.buttons(page)
                    self.assertIn("/邮件 p3", page.markdown)


if __name__ == "__main__":
    unittest.main()
