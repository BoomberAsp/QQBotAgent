"""Offline tests for official gacha pool keyboards + GIF animation."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

_TEST_ROOT = tempfile.mkdtemp(prefix="qqbot-gacha-test-")
os.environ["USER_DATA_ROOT"] = _TEST_ROOT
os.environ.setdefault("GROUP_DATA_ROOT", os.path.join(_TEST_ROOT, "groups"))


class GachaUiTests(unittest.TestCase):
    def test_pool_rows_validate(self):
        from lib.button_spec import validate
        from tools.legacy_tools import gacha_pool_rows

        for ten in (False, True):
            for as_command in (False, True):
                rows = gacha_pool_rows(ten=ten, as_command=as_command)
                self.assertEqual(len(rows), 2)
                self.assertEqual(sum(len(row) for row in rows), 4)
                self.assertEqual(validate(rows), [])
                self.assertEqual(rows[0][0].action, "cmd")
                self.assertIn("常规招募", rows[0][0].data)
        self.assertEqual(
            gacha_pool_rows(ten=False, as_command=False)[0][0].data,
            "单抽 常规招募")
        self.assertEqual(
            gacha_pool_rows(ten=True, as_command=True)[0][0].data,
            "/十连抽 常规招募")


class GachaGifTests(unittest.TestCase):
    def _frames(self, directory: Path) -> list[str]:
        colors = [(30, 30, 30), (80, 20, 20), (20, 80, 20),
                  (20, 20, 80), (80, 80, 20), (60, 20, 60)]
        paths = []
        for index, color in enumerate(colors):
            path = directory / f"frame_{index}.png"
            Image.new("RGB", (64, 36), color).save(path)
            paths.append(str(path))
        return paths

    def test_full_gif_and_cache(self):
        from lib.gacha_animation import ensure_gif

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            frames = self._frames(tmp_path)
            cache = tmp_path / "cache"
            with patch.dict(os.environ, {"QQ_GACHA_GIF_DIR": str(cache)}):
                gif_path, total_ms = ensure_gif(
                    frames, star_level=5, is_single=False,
                    mode="full", width=1280, interval=0.75)
                self.assertTrue(os.path.isfile(gif_path))
                self.assertLess(os.path.getsize(gif_path), 5_000_000)
                self.assertGreater(total_ms, 3_000)
                with Image.open(gif_path) as im:
                    self.assertEqual(im.n_frames, 6)
                    self.assertEqual(im.size, (1280, 720))
                first_mtime = os.path.getmtime(gif_path)
                gif_path2, _ = ensure_gif(
                    frames, star_level=5, is_single=False,
                    mode="full", width=1280, interval=0.75)
                self.assertEqual(gif_path, gif_path2)
                self.assertEqual(os.path.getmtime(gif_path), first_mtime)

    def test_key_mode_three_frames(self):
        from lib.gacha_animation import ensure_gif

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            frames = self._frames(tmp_path)
            cache = tmp_path / "cache"
            with patch.dict(os.environ, {"QQ_GACHA_GIF_DIR": str(cache)}):
                gif_path, total_ms = ensure_gif(
                    frames[2:4] + [frames[-1]], star_level=5, is_single=True,
                    mode="key", width=1280, interval=0.75)
                with Image.open(gif_path) as im:
                    self.assertEqual(im.n_frames, 3)
                self.assertGreater(total_ms, 2_000)


class GachaCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import agent.turn as turn_mod
        self.turn = turn_mod

    async def test_private_official_empty_command_returns_keyboard(self):
        class Sender:
            platform = "qq_official"
            supports_keyboard = True
            supports_markdown = True
            max_messages_per_turn = 4

            def __init__(self):
                self.keyboards = []

            async def send_keyboard(self, msg):
                self.keyboards.append(msg)

            async def send_text(self, text):
                pass

            async def send_markdown(self, text):
                pass

            async def send_image(self, image):
                pass

        sender = Sender()
        token = self.turn._turn_sender.set(sender)
        try:
            handled = await self.turn._handle_gacha_pool_prompt(
                "/抽卡", view=SimpleNamespace(platform="qq_official"))
            self.assertTrue(handled)
            self.assertEqual(len(sender.keyboards), 1)
            data = [btn.data for row in sender.keyboards[0].rows for btn in row]
            self.assertEqual(data[0], "单抽 常规招募")

            handled = await self.turn._handle_gacha_pool_prompt(
                "/抽卡 常规招募", view=SimpleNamespace(platform="qq_official"))
            self.assertFalse(handled)

            handled = await self.turn._handle_gacha_pool_prompt(
                "/抽卡", view=SimpleNamespace(platform="onebot"))
            self.assertFalse(handled)
        finally:
            self.turn._turn_sender.reset(token)

    async def test_group_empty_command_returns_keyboard(self):
        from agent.group_games import _pending, dispatch_game
        from agent.group_state import GroupStore
        from agent.inbound import InboundMessage

        class Sender:
            def __init__(self):
                self.keyboards = []

            async def send_group_keyboard(self, text, rows, plain=None):
                self.keyboards.append((text, rows))

            async def send_text(self, text):
                pass

        def make_view(text):
            return InboundMessage(
                user_id="u1", group_id="g1", is_group=True, self_id="bot",
                platform="qq_official", message_id="m1", mentioned=True,
                text=text)

        with tempfile.TemporaryDirectory() as tmp:
            store = GroupStore(tmp)
            sender = Sender()
            try:
                await dispatch_game(make_view("/抽卡"), sender, store, "/抽卡", "")
                self.assertEqual(len(sender.keyboards), 1)
                rows = sender.keyboards[0][1]
                data = [btn.data for row in rows for btn in row]
                self.assertEqual(data[0], "/抽卡 常规招募")
                self.assertEqual(len(data), 4)
            finally:
                _pending.clear()


class GachaAnimationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from agent import context as ctx
        from tools import legacy_tools
        self.ctx = ctx
        self.tools = legacy_tools
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.frames = []
        for index in range(6):
            path = os.path.join(self.tmp.name, f"frame_{index}.png")
            Image.new("RGB", (32, 18), (index * 30, 0, 0)).save(path)
            self.frames.append(path)
        self.gif_path = os.path.join(self.tmp.name, "anim.gif")
        Image.new("RGB", (32, 18), (1, 2, 3)).save(self.gif_path)

    def _patch_frames(self):
        import plugins.pullingMonitor as pm
        names = [
            "ten_pull_start", "single_pull_start", "pull_1",
            "blue_middle", "blue_end", "purple_end", "golden_end", "red_end",
            "blue_or_purple_spaceship_close", "gold_spaceship_close",
            "red_spaceship_close", "blue_spaceship_open",
            "purple_spaceship_open", "gold_spaceship_open",
            "red_spaceship_open",
        ]
        stack = []
        for index, name in enumerate(names):
            path = self.frames[index % len(self.frames)]
            stack.append(patch.object(
                pm, name, SimpleNamespace(data={"file": f"file://{path}"})))
        for item in stack:
            item.start()
            self.addCleanup(item.stop)

    async def test_one_gif_on_onebot_path(self):
        self._patch_frames()

        class Sender:
            platform = "onebot"
            supports_keyboard = False
            supports_markdown = False
            max_messages_per_turn = 100

            def __init__(self):
                self.images = []

            async def send_image(self, image):
                self.images.append(image)

        sender = Sender()
        s_tok = self.ctx._current_sender.set(sender)
        g_tok = self.ctx._current_group_id.set("")
        try:
            with patch("lib.gacha_animation.ensure_gif",
                       return_value=(self.gif_path, 4600)), \
                 patch.object(self.tools.asyncio, "sleep", new=AsyncMock()):
                result = await self.tools.play_gacha_animation(5, False)
            self.assertIn("GIF", result)
            self.assertEqual(len(sender.images), 1)
            self.assertTrue(sender.images[0].path.endswith(".gif"))
        finally:
            self.ctx._current_sender.reset(s_tok)
            self.ctx._current_group_id.reset(g_tok)

    async def test_official_uses_confirmed_media(self):
        self._patch_frames()

        class Raw:
            def __init__(self):
                self.images = []

            async def send_media_confirmed(self, image):
                self.images.append(image)
                return "sent"

        class Sender:
            platform = "qq_official"
            supports_keyboard = True
            supports_markdown = True
            max_messages_per_turn = 4

            async def send_image(self, image):
                raise AssertionError("official path must use send_media_confirmed")

        raw = Raw()
        sender = Sender()
        s_tok = self.ctx._current_sender.set(sender)
        t_tok = self.ctx._turn_sender.set(raw)
        g_tok = self.ctx._current_group_id.set("")
        try:
            with patch("lib.gacha_animation.ensure_gif",
                       return_value=(self.gif_path, 4600)), \
                 patch.object(self.tools.asyncio, "sleep", new=AsyncMock()):
                result = await self.tools.play_gacha_animation(5, False)
            self.assertIn("GIF", result)
            self.assertEqual(len(raw.images), 1)
            self.assertTrue(raw.images[0].path.endswith(".gif"))
        finally:
            self.ctx._current_sender.reset(s_tok)
            self.ctx._turn_sender.reset(t_tok)
            self.ctx._current_group_id.reset(g_tok)


class GachaStarTests(unittest.IsolatedAsyncioTestCase):
    async def test_gacha_pull_sets_last_star(self):
        from tools import legacy_tools

        with patch("plugins.pullingMonitor.drawing_cards",
                   return_value=("测试角色", 5, "角色")), \
             patch("plugins.pullingMonitor.format_result",
                   return_value=("抽卡结果", 5)), \
             patch("tools.wiki_scraper.WikiScraper.is_stale",
                   return_value=False):
            text = await legacy_tools.gacha_pull("常规招募", 1, None)
        self.assertIn("抽卡结果", text)
        self.assertEqual(legacy_tools.last_gacha_star(), 5)


class GroupGachaAnimationTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_pull_triggers_gif(self):
        from agent import context as ctx
        from agent.group_games import _pending, dispatch_game
        from agent.group_state import GroupStore
        from agent.inbound import InboundMessage
        from tools import legacy_tools

        class Sender:
            def __init__(self):
                self.texts = []

            async def send_group_text(self, text):
                self.texts.append(text)

        def make_view(text):
            return InboundMessage(
                user_id="u1", group_id="g1", is_group=True, self_id="bot",
                platform="qq_official", message_id="m1", mentioned=True,
                text=text)

        async def fake_pull(*args, **kwargs):
            ctx._last_gacha_star.set(5)
            return "十连抽结果: 测试"

        with tempfile.TemporaryDirectory() as tmp:
            store = GroupStore(tmp)
            sender = Sender()
            try:
                with patch.object(
                        legacy_tools, "gacha_pull",
                        new=AsyncMock(side_effect=fake_pull)), \
                     patch.object(
                        legacy_tools, "play_gacha_animation_for_sender",
                        new=AsyncMock(return_value="[Gacha] ok")) as anim:
                    await dispatch_game(
                        make_view("/十连抽 银河招募"), sender, store,
                        "/十连抽", "银河招募")
                anim.assert_awaited_once()
                args = anim.await_args.args
                self.assertIs(args[0], sender)
                self.assertEqual(args[1], 5)
                self.assertFalse(anim.await_args.kwargs.get("is_single", False))
                self.assertTrue(sender.texts)
            finally:
                _pending.clear()

    async def test_group_image_off_skips_gif(self):
        from agent import context as ctx
        from agent.group_games import _pending, dispatch_game
        from agent.group_state import GroupStore, scope_for
        from agent.inbound import InboundMessage
        from tools import legacy_tools

        class Sender:
            def __init__(self):
                self.texts = []

            async def send_group_text(self, text):
                self.texts.append(text)

        view = InboundMessage(
            user_id="u1", group_id="g1", is_group=True, self_id="bot",
            platform="qq_official", message_id="m1", mentioned=True,
            text="/十连抽 银河招募")

        async def fake_pull(*args, **kwargs):
            ctx._last_gacha_star.set(5)
            return "十连抽结果: 测试"

        with tempfile.TemporaryDirectory() as tmp:
            store = GroupStore(tmp)
            store.configure(scope_for(view),
                            features={"gacha": True, "image": False,
                                      "voice": True})
            sender = Sender()
            try:
                with patch.object(
                        legacy_tools, "gacha_pull",
                        new=AsyncMock(side_effect=fake_pull)), \
                     patch.object(
                        legacy_tools, "play_gacha_animation_for_sender",
                        new=AsyncMock(return_value="[Gacha] ok")) as anim:
                    await dispatch_game(
                        view, sender, store, "/十连抽", "银河招募")
                anim.assert_not_awaited()
                self.assertTrue(sender.texts)
            finally:
                _pending.clear()

    async def test_explicit_sender_uses_confirmed_media(self):
        from tools import legacy_tools

        class Sender:
            def __init__(self):
                self.images = []

            async def send_media_confirmed(self, image):
                self.images.append(image)
                return "sent"

        with tempfile.TemporaryDirectory() as tmp:
            gif_path = os.path.join(tmp, "anim.gif")
            Image.new("RGB", (8, 8), (1, 2, 3)).save(gif_path)
            sender = Sender()
            with patch("lib.gacha_animation.ensure_gif",
                       return_value=(gif_path, 4600)), \
                 patch.object(legacy_tools.asyncio, "sleep", new=AsyncMock()):
                result = await legacy_tools.play_gacha_animation_for_sender(
                    sender, 5, False)
            self.assertIn("GIF", result)
            self.assertEqual(len(sender.images), 1)
            self.assertTrue(sender.images[0].path.endswith(".gif"))


if __name__ == "__main__":
    unittest.main()
