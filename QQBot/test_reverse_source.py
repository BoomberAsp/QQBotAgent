"""Offline case replay for the reverse-image search pipeline."""
import asyncio
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image, ImageDraw

from lib.reverse_search import accounts, consensus, lens, quota, service, source_verification
from lib.reverse_search.engines import Candidate
from lib.reverse_search.source_evidence import merge_sources
from lib.reverse_search.vlm import parse_clues
from tools import image_source_tools as tool


POST = "https://x.com/creator/status/2122151597283422308"


def photo(fmt="PNG", size=96):
    image = Image.new("RGB", (size, size), "#4ba8cb")
    draw = ImageDraw.Draw(image)
    draw.rectangle((size // 10, size // 8, size // 3, size // 2), fill="#386800")
    draw.ellipse((size // 2, size // 2, size * 7 // 8, size * 7 // 8), fill="#eeee55")
    buf = io.BytesIO()
    image.save(buf, format=fmt)
    data = buf.getvalue()
    return data if len(data) >= 512 else data + bytes(512 - len(data))


class SampleReplay(unittest.TestCase):
    def test_facebook_posts_and_low_similarity_artwork(self):
        rows = [{"url": "https://www.facebook.com/admin.neko/photos/1135385805387172/"},
                {"url": "https://www.facebook.com/animeloversII/posts/title/122211562898223321/"}]
        candidates = lens.rows_to_candidates(rows)
        self.assertEqual([candidate.author_url for candidate in candidates], ["", ""])
        self.assertTrue(all(accounts.is_post_page(row["url"]) for row in rows))
        self.assertIsNone(candidates[0].similarity)
        for post in (1, 2, 3):
            candidates.append(Candidate(
                "SauceNAO", f"https://pixiv.net/artworks/{post}", similarity=45.07,
                author_url="https://www.pixiv.net/users/15928518"))
        accepted, rejected = consensus.build_consensus(candidates)
        self.assertEqual(accepted, [])
        self.assertIn("weak_artwork", {item.rejection_reason for item in rejected})
        sources = merge_sources([], candidates)
        self.assertEqual(sum(source.match == "similar_art" for source in sources), 3)

    def test_roses_watermark_stays_visible_with_vlm_and_post_clues(self):
        candidates = lens.rows_to_candidates([
            {"url": "https://x.com/example_a/status/1"},
            {"url": "https://www.instagram.com/reel/First_Reel/"},
            {"url": "https://x.com/example_b/status/2"},
        ])
        visual = parse_clues(json.dumps({
            "image_type": "角色照片", "work": "示例作品",
            "watermarks": ["水印@ChocoQAQ", "@chocoqaq", "一段原文"],
        }))
        accepted, rejected = consensus.build_consensus(candidates)
        result = service.FindResult(accounts=accepted, rejected=rejected,
                                    vlm_text=visual.text, visual=visual,
                                    sources=merge_sources([], candidates))
        self.assertFalse(accepted)
        self.assertEqual(visual.watermarks, ["@ChocoQAQ"])
        self.assertIn("作品识别", result.render())
        self.assertIn("instagram.com/reel/First_Reel", result.render())
        self.assertIn("@ChocoQAQ", result.render())

    def test_duplicate_post_cannot_promote_author(self):
        variants = [POST, POST + "?utm_source=one",
                    POST.replace("x.com", "twitter.com") + "/photo/1"]
        candidates = [Candidate(engine, url) for engine, url in
                      zip(("Lens", "SauceNAO", "Lens"), variants)]
        accepted, dropped = consensus.build_consensus(candidates)
        self.assertFalse(accepted)
        self.assertEqual(dropped[0].evidence_count, 1)
        self.assertEqual(dropped[0].engines, {"Lens", "SauceNAO"})
        self.assertEqual(len(merge_sources([], candidates)), 1)

    def test_verified_work_url_is_still_shown_as_matching_page(self):
        sources = merge_sources([], [Candidate("trace.moe", POST, similarity=98)])
        sources[0].verification = "same_image"
        sources[0].attribution = "Credit example"
        result = service.FindResult(work={"title": "Example", "url": POST}, sources=sources)
        self.assertIn("同图页面", result.render())
        self.assertIn("Credit example", result.render())

    def test_cache_versions_and_partial_expiry(self):
        data = photo()
        state = quota._fresh()
        old_key = sha256(data).hexdigest()
        quota.cache_put(state, old_key, {"legacy": True}, 100)
        key = quota.cache_key(data)
        self.assertIsNone(quota.cache_get(state, key, 101))
        quota.cache_put(state, key, {"partial": True}, 100, ttl=quota.PARTIAL_CACHE_TTL_S)
        self.assertEqual(quota.cache_get(state, key, 101), {"partial": True})
        self.assertIsNone(quota.cache_get(state, key, 101 + quota.PARTIAL_CACHE_TTL_S))


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_search_reports_engine_outcomes_and_caches_partial_results(self):
        candidate = Candidate("SauceNAO", POST, similarity=91)
        with tempfile.TemporaryDirectory() as directory:
            store = quota.QuotaStore(Path(directory, "quota.json"))
            with patch.object(service, "_store", store), \
                 patch.object(lens, "lens_enabled", return_value=False), \
                 patch.object(service, "run_engines", AsyncMock(return_value=(
                     [candidate], {"IQDB": "引擎超时"}))):
                first = await service.run(photo(), user_id="one")
                replay = await service.run(photo(), user_id="two")
        self.assertEqual(first.engine_status["SauceNAO"], {"status": "found", "candidates": 1})
        self.assertEqual(first.engine_status["IQDB"]["status"], "error")
        self.assertEqual(first.engine_status["trace.moe"]["status"], "empty")
        self.assertEqual(first.engine_status["Lens"]["status"], "disabled")
        self.assertTrue(replay.cached)
        self.assertEqual(replay.engine_status, first.engine_status)


class LensReplay(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_cards_advance_and_exact_tab_reuses_upload(self):
        default = "https://www.google.com/search?udm=26&vsrid=same-upload"
        exact = "https://www.google.com/search?udm=48&vsrid=same-upload&gsessionid=fixture"
        wrappers = [f"https://www.google.com/goto?url={i}" for i in range(5)]
        pages = {
            default: {"url": default, "exactUrl": exact, "results": [],
                      "cards": [{"wrapper": wrapper, "title": "@ChocoQAQ" if index == 3 else "X 示例"}
                                for index, wrapper in enumerate(wrappers)]},
            exact: {"url": exact, "results": [{"url": "https://example.com/exact-post"}], "cards": []},
        }
        destinations = dict(zip(wrappers, (
            POST, POST + "?utm_source=copy", POST.replace("x.com", "twitter.com"),
            "https://x.com/second/status/100", "https://x.com/third/status/200")))

        class Browser:
            location = default
            navigate = AsyncMock()
            start = AsyncMock()
            send = AsyncMock(return_value={"success": True})
            wait_for = AsyncMock(return_value=True)
            evaluate_object = AsyncMock(return_value="file-picker")

            async def evaluate(self, code, timeout=0):
                if code == lens.EXTRACT_JS:
                    return pages[self.location]
                if code == lens.PREPARE_JS:
                    return "ok"
                if code == lens.SUBMIT_JS:
                    self.location = default
                    return "submitted"
                return ""

            async def url(self):
                return self.location

        browser = Browser()

        async def go(url, **kwargs):
            browser.location = url
            return url

        browser.navigate.side_effect = go
        resolve = AsyncMock(side_effect=lambda client, url, timeout: destinations[url])
        with patch.object(lens, "resolve_goto_http", resolve), patch.object(lens, "user_cookies", return_value=[]):
            candidates, _ = await lens._search_once(
                browser, Path("/tmp/test.png"), "FakeUA", time.monotonic() + 30,
                watermarks=lambda: ["@ChocoQAQ"])
        self.assertEqual(resolve.await_count, 5)
        self.assertEqual(resolve.await_args_list[0].args[1], wrappers[3])
        self.assertEqual(browser.navigate.await_args.args[0], exact)
        self.assertEqual(candidates[0].extra["match"], "exact")
        self.assertEqual(candidates[0].extra["exact_status"], "success")
        self.assertEqual(len(candidates), 4)
        inputs = [call for call in browser.send.await_args_list if call.args[0] == "DOM.setFileInputFiles"]
        self.assertEqual(len(inputs), 1)

    async def test_exact_page_failure_preserves_default_results(self):
        url = "https://www.google.com/search?udm=26&vsrid=fixture"
        page = {"results": [{"url": POST}], "cards": []}
        browser = unittest.mock.MagicMock()
        browser.start = AsyncMock()
        browser.send = AsyncMock(return_value={"success": True})
        browser.navigate = AsyncMock(return_value=url)
        browser.wait_for = AsyncMock(return_value=True)
        browser.evaluate_object = AsyncMock(return_value="input")
        browser.url = AsyncMock(return_value=url)
        browser.evaluate = AsyncMock(side_effect=lambda expr, timeout: (
            "ok" if expr == lens.PREPARE_JS else page if expr == lens.EXTRACT_JS else ""))
        with patch.object(lens, "_fetch_exact_page", AsyncMock(side_effect=lens.BrowserError("offline"))):
            candidates, _ = await lens._search_once(browser, Path("/tmp/test.png"), "FakeUA", time.monotonic() + 30)
        self.assertEqual([candidate.page_url for candidate in candidates], [POST])
        self.assertEqual(candidates[0].extra["exact_status"], "unavailable")


class VerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_redirect_resolves_again_and_public_request_pins_ip(self):
        requests = []

        async def respond(request):
            requests.append((request.url.host, request.headers["Host"], request.extensions["sni_hostname"]))
            return httpx.Response(302, headers={"Location": "https://redirect.test/path"})

        client = httpx.AsyncClient

        async def dns(host, port, **kwargs):
            return [(None, None, None, None, ("127.0.0.1" if host == "redirect.test"
                                              else "93.184.216.34", port))]

        with patch.object(asyncio.get_running_loop(), "getaddrinfo", side_effect=dns), \
             patch.object(source_verification.httpx, "AsyncClient",
                          side_effect=lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs)):
            with self.assertRaises(source_verification.PublicPageUnavailable):
                await source_verification._public_get(
                    "https://public.test/post", limit=500, content="text/html")
        self.assertEqual(requests, [("93.184.216.34", "public.test", "public.test")])

    async def test_metadata_and_reencoded_image_prove_a_matching_page(self):
        html = b'<meta property="og:image" content="/photo.jpg"><meta name="author" content="Example credit">'
        get = AsyncMock(side_effect=[(html, "https://public.test/post"), (photo("JPEG"), "")])
        with patch.object(source_verification, "_public_get", get):
            result = await source_verification.verify_source("https://public.test/post", photo())
        self.assertEqual(result, source_verification.Verification("same_image", "Example credit"))
        blank = io.BytesIO()
        Image.new("RGB", (96, 96), "blue").save(blank, "PNG")
        self.assertFalse(source_verification._same_image(photo(), blank.getvalue()))


class AgentRefinementTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_turn_reuses_first_search_and_dedups_refinements(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory, "input.png")
            image.write_bytes(photo())
            result = service.FindResult(
                sources=merge_sources([], [Candidate("Lens", POST, similarity=None)]),
                visual=parse_clues('{"watermarks":["@ChocoQAQ"]}'))
            first = AsyncMock(return_value=result)
            extra = AsyncMock(return_value=[Candidate(
                "SearXNG", "https://x.com/ChocoQAQ/status/123",
                similarity=None, extra={"match": "watermark"})])
            verify = AsyncMock(return_value=source_verification.Verification("same_image", "署名"))
            with patch.object(service, "run", first), patch.dict("os.environ", {"FIND_SOURCE_ENABLED": "on"}), \
                 patch.object(source_verification, "lookup_watermark", extra), \
                 patch.object(source_verification, "verify_source", verify):
                token = tool.current.set(tool.ImageSearchTurn("tester", directory, str(image), authorized=True))
                try:
                    initial = json.loads(await tool.find_image_source())
                    self.assertEqual(initial, json.loads(await tool.find_image_source()))
                    first.assert_awaited_once_with(photo(), image_path=str(image), user_id="tester")
                    self.assertIn("[Error]", await tool.find_image_source("verify_source", "foreign-id"))
                    watermark = initial["watermarks"][0]["id"]
                    more = json.loads(await tool.find_image_source("lookup_watermark", watermark))
                    await tool.find_image_source("lookup_watermark", watermark)
                    extra.assert_awaited_once_with("@ChocoQAQ")
                    page_id = next(row["id"] for row in more["sources"] if "ChocoQAQ" in row["url"])
                    checked = json.loads(await tool.find_image_source("verify_source", page_id))
                    self.assertIn("同图页面", checked["summary"])
                    self.assertEqual(checked["remaining_refinements"], 0)
                    self.assertEqual(checked["available_actions"], {})
                    await tool.find_image_source("verify_source", initial["sources"][0]["id"])
                    verify.assert_awaited_once()
                finally:
                    tool.current.reset(token)
            self.assertIn("[Error]", await tool.find_image_source("verify_source", page_id))


if __name__ == "__main__":
    unittest.main()
