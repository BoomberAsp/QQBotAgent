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
                self.assertEqual(total_ms, 8_000)
                with Image.open(gif_path) as im:
                    self.assertEqual(im.n_frames, 6)
                    self.assertEqual(im.size, (1280, 720))
                first_mtime = os.path.getmtime(gif_path)
                gif_path2, _ = ensure_gif(
                    frames, star_level=5, is_single=False,
                    mode="full", width=1280, interval=0.75)
                self.assertEqual(gif_path, gif_path2)
                self.assertEqual(os.path.getmtime(gif_path), first_mtime)

    def test_frame_timing(self):
        from lib.gacha_animation import LAST_FRAME_MS, durations_for

        full = durations_for(0.75, "full")
        self.assertEqual(full, (1000, 1000, 1000, 1000, 1000, 3000))
        self.assertEqual(full[-1], LAST_FRAME_MS)
        key = durations_for(0.75, "key")
        self.assertEqual(key, (1000, 1000, 3000))

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
                self.assertEqual(total_ms, 5_000)


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

        with patch("plugins.pullingMonitor.draw_cards",
                   return_value=[("测试角色", 5, "角色")]), \
             patch("tools.wiki_scraper.WikiScraper.is_stale",
                   return_value=False):
            text = await legacy_tools.gacha_pull("常规招募", 1, None)
        self.assertIn("抽卡结果", text)
        self.assertEqual(legacy_tools.last_gacha_star(), 5)


    async def test_red_light_dark_in_all_eligible_banners(self):
        from plugins import pullingMonitor as monitor
        from tools import legacy_tools
        from tools.wiki_scraper import WikiScraper

        static = WikiScraper()._load_static_fallback()
        with patch.object(monitor, "_load_gacha_data", return_value=static), \
             patch.object(WikiScraper, "is_stale", return_value=False), \
             patch("tools.name_resolver.get_resolver") as resolver, \
             patch.object(monitor.random, "choices", side_effect=lambda rows, *, k, **_: [
                 next(row for row in rows if row[0] == "蝶子")] * k):
            resolver.return_value.resolve_character.return_value = "蝶子"
            for banner, up in (("常规招募", None), ("神秘招募", "蝶子"), ("银河招募", None)):
                for count in (1, 10):
                    with self.subTest(banner=banner, count=count):
                        text = await legacy_tools.gacha_pull(banner, count, up)
                        self.assertIn("★★★★★★ 红色 角色【蝶子】", text)
                        self.assertEqual(legacy_tools.last_gacha_star(), 6)
                        if count == 10:
                            self.assertIn("6★×10", text)
            self.assertIn("[Gacha Error]", await legacy_tools.gacha_pull("几率up招募", 1, "蝶子"))
            self.assertIn("[Gacha Error]", await legacy_tools.gacha_pull("神秘招募", 10))

    def test_reload_old_names_and_use_bond_rarity(self):
        import json

        from plugins import pullingMonitor as monitor
        from tools import wiki_scraper

        def pool(star, kind, *items):
            return {"star": star, "type": kind, "items": list(items)}

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            scraper = wiki_scraper.WikiScraper()
            scraper._cache_file = str(path / "gacha.json")
            scraper._translation_cache = str(path / "name_mapping.json")
            scraper._character_detail_cache = str(path / "characters.json")
            scraper._bond_detail_cache = str(path / "bonds.json")
            (path / "name_mapping.json").write_text(
                json.dumps({"Tiffany": "蒂芙尼", "Art of Temptation": "诱惑的艺术"}),
                encoding="utf-8")
            roles = {"H610": {"id": "H610", "names": {"en": "Tiffany", "zh_hans": "蒂芬妮"}}}
            artifacts = {
                "A0389": {"id": "A0389", "star": 5, "names": {
                    "en": "Art of Temptation", "zh_hans": "欲擒和故纵"}},
                "A0036": {"id": "A0036", "star": 4, "names": {
                    "en": "Passionate Treatment", "zh_hans": "激情的治疗"}},
            }
            with patch.object(wiki_scraper, "WikiScraper", return_value=scraper), \
                 patch.object(wiki_scraper, "load_roles", return_value=roles), \
                 patch.object(wiki_scraper, "load_artifacts", return_value=artifacts), \
                 patch.object(monitor, "_gacha_data", None), \
                 patch.object(monitor, "_gacha_stamp", None):
                before = monitor._load_gacha_data()
                (path / "gacha.json").write_text(json.dumps({"data": {"pools": {
                    "special_three_color_five_star": pool(
                        5, "character", {"name": "蒂芙尼", "bond": "诱惑的艺术"},
                        {"name": "茉伊拉", "bond": "激情的治疗"}),
                    "bonds_four_star": pool(4, "bond", {"name": "激情的治疗"}),
                }}}), encoding="utf-8")
                after = monitor._load_gacha_data()
                self.assertIsNot(after, before)
                self.assertIs(after, monitor._load_gacha_data())
                self.assertEqual(after["pools"]["bonds_five_star_all"]["items"],
                                 [{"id": "A0389", "name": "欲擒和故纵", "title": "Art of Temptation"}])
                self.assertEqual(after["pools"]["bonds_four_star"]["items"][0]["name"], "激情的治疗")
                with patch.object(monitor.random, "choices", side_effect=lambda rows, **_: [
                    next(row for row in rows if row[0] == "欲擒和故纵")]) as pick:
                    result = monitor.drawing_cards("几率up招募", "蒂芙尼")
                rows = pick.call_args.args[0]
                self.assertEqual(result, ("欲擒和故纵", 5, "羁绊"))
                self.assertEqual(pick.call_args.kwargs["weights"][rows.index(result)], 1.05)

    async def test_refresh_writes_resolved_names_and_preserves_cache_on_partial_fetch(self):
        import json

        from tools import wiki_scraper

        role_source = "{{Member\n|ID=H610\n|Stars=5\n|Element=Nature\n|Bond=Passionate Treatment\n}}"
        bond_source = "{{Bond\n|Stars=4\n|Image=Icon_Head_S_A0036.png\n}}"
        roles = {"H610": {"names": {"en": "Tiffany", "zh_hans": "蒂芬妮"}}}
        artifacts = {"A0036": {"star": 4, "names": {
            "en": "Passionate Treatment", "zh_hans": "激情的治疗"}}}

        with tempfile.TemporaryDirectory() as tmp:
            scraper = wiki_scraper.WikiScraper()
            scraper._cache_dir = tmp
            cache_file = Path(tmp) / "gacha.json"
            scraper._cache_file = str(cache_file)
            scraper._translation_cache = str(Path(tmp) / "translations.json")
            scraper._character_detail_cache = str(Path(tmp) / "characters.json")
            scraper._bond_detail_cache = str(Path(tmp) / "bonds.json")
            with patch.object(scraper, "_fetch_template_pages", new_callable=AsyncMock) as fetch, \
                 patch.object(wiki_scraper, "load_roles", return_value=roles), \
                 patch.object(wiki_scraper, "load_artifacts", return_value=artifacts):
                fetch.side_effect = [[("Tiffany", role_source)], [("Passionate Treatment", bond_source)]]
                saved = await scraper.refresh()
                data = json.loads(cache_file.read_text(encoding="utf-8"))["data"]
                self.assertEqual(saved, data)
                pools = data["pools"]
                self.assertEqual(pools["special_three_color_five_star"]["items"][0]["name"], "蒂芬妮")
                self.assertEqual(pools["bonds_four_star"]["items"][0]["id"], "A0036")
                self.assertEqual(pools["bonds_five_star_all"]["items"], [])
                fetch.side_effect = [[("Tiffany", role_source)], []]
                self.assertEqual(await scraper.refresh(), data)
                self.assertEqual(list(Path(tmp).iterdir()), [cache_file])

    def test_errors_do_not_count_as_blue(self):
        from plugins.pullingMonitor import format_result

        text, highest = format_result([("未找到可抽取角色", 0, "错误")] * 10, 10)
        self.assertIn("3★×0", str(text))
        self.assertEqual(highest, 0)


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
