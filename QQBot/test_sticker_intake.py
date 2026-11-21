import asyncio
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from agent import media_ledger, media_tools, sticker_group, sticker_intake, sticker_subscriptions
from agent.context import _current_user_workspace
from agent.group_commands import dispatch
from agent.group_resources import registry
from agent.group_state import GroupStore, scope_for
from agent.inbound import Attachment, InboundMessage


def png(color, size=(16, 16)):
    data = io.BytesIO()
    Image.new("RGB", size, color).save(data, "PNG")
    return data.getvalue()


class StickerIntakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.store = GroupStore(root / "groups")
        self.registry_path = root / "registry.json"
        self.registry_path.write_text(json.dumps({
            "media": [], "page_hosts": ["pages.example"], "image_hosts": ["images.example"],
        }))
        env = patch.dict("os.environ", {
            "MEDIA_DATA_ROOT": str(root / "ledger"), "PERSONA_RESOURCE_REGISTRY": str(self.registry_path),
        })
        env.start()
        self.addCleanup(env.stop)
        self.sender = SimpleNamespace(send_group_text=AsyncMock(return_value="sent"))
        self.view = InboundMessage("user", is_group=True, group_id="group", self_id="bot",
                                   platform="onebot", message_id="upload", mentioned=True)
        self.scope = scope_for(self.view)
        self.owner = sticker_intake.owner_key(self.scope, self.view.user_id)
        self.store.configure(self.scope, persona="rubi")

    def group_candidate(self, image):
        return sticker_intake.make_candidate(
            image, persona="rubi", scope=self.scope, origin="group", owner=self.owner,
            description="拍拍安慰", author="投稿者", license="本群复用",
        )

    async def test_approval_is_group_scoped_and_uploader_can_withdraw(self):
        ident, new = sticker_intake.submit(self.group_candidate(png("red")))
        self.assertTrue(new)
        self.assertEqual(sticker_intake.pending()[0]["uses"], ["comfort"])
        self.assertTrue(sticker_intake.preview_bytes(ident).startswith(b"\x89PNG"))
        duplicate = sticker_intake.submit(self.group_candidate(png((254, 0, 0), size=(24, 24))))
        self.assertEqual(duplicate, (ident, False))
        self.assertEqual(media_tools.group_sticker_candidates("rubi", self.scope), [])
        details = [{"id": ident, "uses": "安慰,庆祝"}]
        self.assertEqual(sticker_intake.review(details, approve=True)[0]["status"], "published")
        self.assertEqual(sticker_intake.review(details, approve=True)[0]["status"], "published")
        assets, base = registry()
        self.assertEqual(len(assets["media"]), 1)
        record = assets["media"][0]
        published_path = base / record["path"]
        self.assertTrue(media_tools.published("rubi", self.scope, ident, published_path))
        self.assertFalse(media_tools.published("rubi", "other", ident, published_path))
        self.assertFalse(sticker_intake.withdraw(ident, self.scope, "different-owner"))
        self.assertTrue(sticker_intake.withdraw(ident, self.scope, self.owner))
        self.assertEqual(media_tools.group_sticker_candidates("rubi", self.scope), [])

    async def test_group_quote_requires_verified_author_and_uses_intake_workspace(self):
        async def resolve(_):
            path = Path(_current_user_workspace.get()) / "sticker.png"
            path.write_bytes(png("blue"))
            return [Attachment("image", str(path), quoted=True)]

        resolver = AsyncMock(side_effect=resolve)
        self.view.resolver = SimpleNamespace(resolve=resolver)
        self.view.reply_message_id = "original"
        self.view.reply_author_id = "someone-else"
        self.view.text = "/收表情 安慰"
        self.assertTrue(await dispatch(self.view, self.sender, "regular", self.store))
        resolver.assert_not_awaited()
        self.assertIn("自己发的", self.sender.send_group_text.await_args.args[0])
        self.view.reply_author_id = "user"
        self.assertTrue(await dispatch(self.view, self.sender, "regular", self.store))
        item, = sticker_intake.pending()
        self.assertEqual((item["scope"], item["uses"]), (self.scope, ["comfort"]))
        self.assertIn(item["id"], self.sender.send_group_text.await_args.args[0])
        self.assertFalse(Path(resolver.await_args.args[0].attachments[0].path).exists())
        self.view.text = "/我的表情"
        self.assertTrue(await dispatch(self.view, self.sender, "regular", self.store))
        self.assertIn(item["id"] + " · 待审核", self.sender.send_group_text.await_args.args[0])

    async def test_automatic_group_collection_requires_both_group_and_uploader_consent(self):
        async def resolve(_):
            path = Path(_current_user_workspace.get()) / "sticker.png"
            path.write_bytes(png("green"))
            return [Attachment("image", str(path))]

        resolver = AsyncMock(side_effect=resolve)
        self.view.resolver = SimpleNamespace(resolve=resolver)
        self.view.is_sticker = True
        self.view.text = "终于通关！"
        state = self.store.group(self.scope)
        sticker_intake.consent(self.scope, enabled=True)
        sticker_group.maybe_collect(self.view, self.store, state)
        resolver.assert_not_awaited()
        sticker_intake.consent(self.scope, self.owner, True)
        self.view.is_sticker = False
        sticker_group.maybe_collect(self.view, self.store, state)
        resolver.assert_not_awaited()
        self.view.is_sticker = True
        sticker_group.maybe_collect(self.view, self.store, state)
        await asyncio.gather(*list(sticker_group._jobs))
        resolver.assert_awaited_once()
        self.assertEqual(sticker_intake.pending()[0]["uses"], ["celebrate"])
        self.assertEqual(sticker_intake.pending()[0]["description"], "")
        sticker_group.maybe_collect(self.view, self.store, state)
        await asyncio.gather(*list(sticker_group._jobs))
        self.assertEqual(len(sticker_intake.pending()), 1)
        self.assertEqual(sticker_intake.pending()[0]["repeats"], 1)

    async def test_web_subscription_syncs_only_allowed_images_and_requires_provenance(self):
        with self.assertRaises(ValueError):
            sticker_subscriptions.save_source({"url": "https://outside.example/pack", "persona": "rubi"})
        source = sticker_subscriptions.save_source({"url": "https://pages.example/pack", "persona": "rubi"})
        page = (
            '<img src="https://outside.example/image.png">'
            '<img src="https://images.example/celebrate.png" alt="通关庆祝">'
        ).encode()
        approved = "https://images.example/celebrate.png"
        downloaded = AsyncMock(return_value=(png("yellow"), "image/png", approved))
        with patch.object(sticker_subscriptions, "read_source_page", AsyncMock(
            return_value=(page, "text/html", source["url"])
        )), patch.object(sticker_subscriptions, "download", downloaded):
            result = await sticker_subscriptions.sync_source(source["id"])
            self.assertEqual(result["imported"], 1)
            self.assertFalse((await sticker_subscriptions.sync_source(source["id"]))["ok"])
        downloaded.assert_awaited_once_with(approved, {"images.example"}, sticker_intake.CANDIDATE_BYTES)
        item, = sticker_intake.pending()
        self.assertEqual((item["scope"], item["uses"]), ("", ["celebrate"]))
        self.assertEqual(sticker_intake.review([{"id": item["id"]}], approve=True)[0]["status"], "missing_rights")
        self.assertEqual(sticker_intake.review([{"id": item["id"], "author": "作者",
                                               "license": "可公开复用"}], approve=True)[0]["status"], "published")
        self.assertEqual(registry()[0]["media"][0]["visibility"], "public")

    async def test_use_ranking_and_recent_rotation(self):
        milestone = sticker_intake.submit(self.group_candidate(png("red")))[0]
        unlucky = sticker_intake.submit(self.group_candidate(png("blue")))[0]
        sticker_intake.review([{"id": milestone, "uses": "庆祝"},
                              {"id": unlucky, "uses": "吐槽运气"}], approve=True)
        self.assertEqual(media_tools.group_sticker_candidates("rubi", self.scope, "十连又歪了")[0]["id"], unlucky)
        ticket = media_ledger.reserve(self.scope, "member", "rubi", "event-one", unlucky)
        self.assertTrue(ticket)
        media_ledger.outcome(ticket, "sent")
        self.assertFalse(media_ledger.reserve(self.scope, "member", "rubi", "event-two", unlucky))
        with media_ledger.db() as conn:
            conn.execute("UPDATE tickets SET created=? WHERE id=?", (time.time() - 1900, ticket))
        assets, _ = media_tools.sticker_candidates("rubi", self.scope, use="吐槽运气")
        self.assertEqual([item["id"] for item in assets], [unlucky])
        self.assertTrue(media_ledger.reserve(self.scope, "member", "rubi", "event-three", unlucky))
