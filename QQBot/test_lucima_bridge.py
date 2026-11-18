"""Offline regressions for the Lucima authorization and process boundaries."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from lucima_bridge.service import Bridge, Vault, Worker
from lucima_bridge.web import app


ORIGIN = "https://lucima.example.invalid"
TOKEN = "test_" + "x" * 40
AUTH = {"user_id": "mock-game-user", "access_token": "fake-access", "refresh_token": "fake-refresh", "device_id": "device"}


class WebAuthorizationTests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        root = directory / "lucima"
        (root / "backend").mkdir(parents=True)
        (root / "backend/bot_worker.py").touch()
        (root / "assets/encyclopedia").mkdir(parents=True)
        (root / "assets/encyclopedia/index.json").write_text('{"characters":[]}', encoding="utf-8")
        self.db_path = directory / "state/binding.sqlite3"
        self.enterContext(patch.dict(os.environ, {
            "LUCIMA_ROOT": str(root), "LUCIMA_BRIDGE_DATA": str(self.db_path.parent),
            "LUCIMA_INTERNAL_TOKEN": TOKEN, "LUCIMA_PUBLIC_ORIGIN": ORIGIN,
            "LUCIMA_VAULT_KEY": Fernet.generate_key().decode(),
        }))
        self.worker = AsyncMock(return_value={"status": "ok", "auth": AUTH})
        self.enterContext(patch("lucima_bridge.web.Worker", return_value=self.worker))
        self.client = self.enterContext(TestClient(app, base_url=ORIGIN))

    def command(self, action, user="owner", arg="", event="1", platform="onebot", *, proposal=False):
        path = "/internal/propose" if proposal else "/internal/command"
        return self.client.post(path, headers={"Authorization": "Bearer " + TOKEN}, json={
            "platform": platform, "bot": "bot", "user": user, "event": event, "action": action, "arg": arg,
        }).json()

    def post(self, path, body, origin=ORIGIN):
        return self.client.post(path, headers={"Origin": origin}, json=body)

    def open_ticket(self, user="owner"):
        return self.claim_ticket(self.command("login", user=user)["login_url"])

    def claim_ticket(self, url):
        ticket = url.split("#")[1]
        claim = self.post("/api/claim", {"ticket": ticket})
        self.assertEqual(claim.status_code, 200)
        self.assertIn("HttpOnly", claim.headers["set-cookie"])
        self.assertEqual(self.post("/api/claim", {"ticket": ticket}).status_code, 410)
        return claim.json()["pin"]

    def test_qr_requires_same_qq_identity_origin_and_one_use(self):
        self.assertEqual(self.client.post("/internal/command", json={}).status_code, 403)
        pin = self.open_ticket()
        self.assertIn("无效", self.command("confirm", user="other", arg=pin)["message"])
        self.assertIn("无效", self.command("confirm", platform="qq_official", arg=pin)["message"])
        body = {"account": "dummy-account", "password": "dummy-password"}
        self.assertEqual(self.post("/api/login", body).status_code, 403)
        self.assertIn("已确认", self.command("confirm", arg=pin)["message"])
        self.assertEqual(self.post("/api/login", body, "https://other.invalid").status_code, 403)
        self.assertEqual(self.post("/api/login", body).status_code, 200)
        self.assertEqual(self.post("/api/login", body).status_code, 403)
        self.worker.assert_awaited_once()
        self.assertIn("已授权", self.command("status")["message"])
        self.assertIn("无有效授权", self.command("status", user="other")["message"])
        self.assertIn("无有效授权", self.command("status", platform="qq_official")["message"])
        for secret in ("dummy-account", "dummy-password", "fake-access", "fake-refresh", AUTH["user_id"]):
            self.assertNotIn(secret.encode(), self.db_path.read_bytes())

    def test_login_success_delivers_one_lobby_to_original_event(self):
        self.assertEqual(self.command("lobby")["menu"], "login")
        issue = self.command("login", event="original")
        notice = issue["login_notice"]
        self.assertNotEqual(notice, issue["login_url"].split("#")[1])
        self.assertEqual(self.command("login_notice", user="other", arg=notice, event="original"), {})
        self.assertEqual(self.command("login_notice", platform="qq_official", arg=notice, event="original"), {})
        self.assertEqual(self.command("login_notice", arg=notice, event="different"), {})
        with patch("lucima_bridge.service.LOGIN_NOTICE_WAIT", 0.01):
            self.assertEqual(self.command("login_notice", arg=notice, event="original"), {"pending": True})
        self.command("confirm", arg=self.claim_ticket(issue["login_url"]))
        result = self.post("/api/login", {"account": "dummy-account", "password": "dummy-password"})
        self.assertEqual(result.status_code, 200)
        self.assertNotIn(notice, result.text)
        lobby = self.command("login_notice", arg=notice, event="original")
        self.assertEqual(lobby["menu"], "lobby")
        self.assertIn("登录成功", lobby["message"])
        self.assertIn("15 分钟", lobby["message"])
        self.assertNotIn(AUTH["user_id"], str(lobby))
        self.assertEqual(self.command("login_notice", arg=notice, event="original"), {})
        self.assertEqual(self.command("lobby")["menu"], "lobby")

    def test_failed_login_does_not_notify_success(self):
        issue = self.command("login", event="original")
        self.command("confirm", arg=self.claim_ticket(issue["login_url"]))
        self.worker.return_value = {"status": "login_failed"}
        failed = self.post("/api/login", {"account": "dummy-account", "password": "dummy-password"})
        self.assertEqual(failed.status_code, 400)
        self.assertEqual(self.command("login_notice", arg=issue["login_notice"], event="original"), {})
        self.assertEqual(self.command("lobby")["menu"], "login")

    def test_remembered_authorization_is_encrypted_and_cannot_be_taken_over(self):
        self.command("confirm", arg=self.open_ticket())
        body = {"account": "dummy-account", "password": "dummy-password", "remember": True}
        self.assertEqual(self.post("/api/login", body).status_code, 200)
        self.command("confirm", user="other", arg=self.open_ticket("other"))
        self.assertEqual(self.post("/api/login", body).status_code, 409)
        self.assertIn("无有效授权", self.command("status", user="other")["message"])
        self.assertNotIn(b"fake-refresh", self.db_path.read_bytes())
        self.assertNotIn(b"dummy-password", self.db_path.read_bytes())
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "binding.sqlite3"
            copy.write_bytes(self.db_path.read_bytes())
            vault = Vault(copy, os.environ["LUCIMA_VAULT_KEY"])
            try:
                owner = vault.index('["onebot","bot","owner"]')
                self.assertEqual(vault.load(owner), AUTH)
            finally:
                vault.db.close()
        self.assertIn("清除", self.command("unbind")["message"])
        self.assertIn("无有效授权", self.command("status")["message"])

    def test_spending_approval_bound_to_owner_and_consumed_once(self):
        self.command("confirm", arg=self.open_ticket())
        body = {"account": "test-account", "password": "test-password"}
        self.assertEqual(self.post("/api/login", body).status_code, 200)
        superseded = self.command("recruit")["approval"]
        ticket = self.command("donate", arg="gold")["approval"]
        self.assertIn("已失效", self.command("discard", arg=superseded)["message"])
        self.assertIn("已失效", self.command("discard", user="other", arg=ticket)["message"])
        self.assertNotIn("job", self.command("approve", user="other", arg=ticket))
        self.assertNotIn("job", self.command("approve", platform="qq_official", arg=ticket))
        task = self.command("approve", arg=ticket, event="submit")["job"]
        self.assertNotIn("job", self.command("approve", arg=ticket, event="replay"))
        for _ in range(40):
            result = self.command("receipt", arg=task)
            if "receipt" in result:
                break
        self.assertEqual(result["receipt"]["task"], "佣兵团捐赠")
        self.assertNotIn("receipt", self.command("receipt", user="other", arg=task))
        self.assertNotIn("auth", result["receipt"])
        payload = self.worker.await_args.args[0]
        self.assertEqual((payload["task"], payload["code"]), ("donate", "gold"))

    def test_natural_language_action_waits_for_a_distinct_private_event(self):
        self.command("confirm", arg=self.open_ticket())
        self.assertEqual(self.post("/api/login", {"account": "stub", "password": "stub"}).status_code, 200)
        proposed = self.command("mail", proposal=True, event="natural-request")
        ticket = proposed["approval"]
        self.worker.assert_awaited_once()  # only the login, no game task
        self.assertNotIn("job", self.command("approve", arg=ticket, event="natural-request"))
        self.assertNotIn("job", self.command("approve", user="other", arg=ticket, event="click"))
        self.assertNotIn("job", self.command("approve", platform="qq_official", arg=ticket, event="click"))
        self.assertNotIn("job", self.command("approve", proposal=True, arg=ticket, event="click"))
        self.assertIn("job", self.command("approve", arg=ticket, event="click"))
        self.assertNotIn("job", self.command("approve", arg=ticket, event="replay"))

    def test_queries_and_login_links_are_created_only_after_approval(self):
        proposed = self.command("login", proposal=True, event="natural-request")
        self.assertNotIn("login_url", proposed)
        approved = self.command("approve", arg=proposed["approval"], event="click")
        self.assertIn("login_url", approved)
        with patch("lucima_bridge.service.Bridge.status", return_value="TEST_AUTHORIZED_RESULT") as status:
            proposed = self.command("status", proposal=True, event="query-request")
            status.assert_not_called()
            self.assertNotIn("TEST_AUTHORIZED_RESULT", str(proposed))
            approved = self.command("approve", arg=proposed["approval"], event="query-click")
            self.assertEqual(approved["message"], "TEST_AUTHORIZED_RESULT")
            status.assert_called_once()

    def test_expired_natural_language_proposal_is_rejected(self):
        proposed = self.command("login", proposal=True, event="expiry-request")

        def expire():
            for plan in app.state.bridge.plans.values():
                plan.expires = 0

        self.client.portal.call(expire)
        refused = self.command("approve", arg=proposed["approval"], event="later-click")
        self.assertNotIn("login_url", refused)


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_login_notifications_wake_and_only_one_collects(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        vault = Vault(directory / "state.sqlite3", Fernet.generate_key().decode())
        self.addCleanup(vault.db.close)
        worker = AsyncMock(return_value={"status": "ok", "auth": AUTH})
        bridge = Bridge(vault, worker, ORIGIN)
        issue = bridge.new_ticket("owner", "event")
        browser, pin = bridge.claim(issue["login_url"].split("#")[1])
        bridge.confirm("owner", pin)
        waiters = [asyncio.create_task(bridge.wait_login_notice("owner", issue["login_notice"], "event"))
                   for _ in range(2)]
        await asyncio.sleep(0)
        self.assertTrue(all(not waiter.done() for waiter in waiters))
        self.assertEqual((await bridge.login(browser, "dummy-user", "dummy-pass", True))[0], 200)
        results = await asyncio.wait_for(asyncio.gather(*waiters), 2)
        lobbies = [result for result in results if result.get("menu") == "lobby"]
        self.assertEqual(len(lobbies), 1)
        self.assertIn("最长 30 天", lobbies[0]["message"])

    async def test_queue_limits_and_reaps_before_next_job(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        vault = Vault(directory / "state.sqlite3", Fernet.generate_key().decode())
        self.addCleanup(vault.db.close)
        entered, release = asyncio.Event(), asyncio.Event()
        ran = []

        async def worker(payload, timeout):
            ran.append(payload["auth"]["user_id"])
            entered.set()
            await release.wait()
            return {"status": "ok"}

        bridge = Bridge(vault, worker, ORIGIN)
        for index in range(7):
            vault.bind(str(index), {**AUTH, "user_id": str(index)}, True)
        self.assertIn("已提交", bridge.submit("0", "mail", "", "event")["message"])
        consumer = asyncio.create_task(bridge.consume())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertIn("已经提交", bridge.submit("0", "mail", "", "event")["message"])
            self.assertIn("已有", bridge.submit("0", "mail", "", "another")["message"])
            for index in range(1, 6):
                self.assertIn("已提交", bridge.submit(str(index), "mail", "", "event")["message"])
            self.assertIn("队列已满", bridge.submit("6", "mail", "", "event")["message"])
            self.assertEqual(ran, ["0"])
            self.assertIn("已取消", bridge.cancel("2"))
            release.set()
            for _ in range(100):
                if not bridge.queue and bridge.active is None:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(ran, ["0", "1", "3", "4", "5"])
        finally:
            consumer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await consumer

    async def test_real_subprocess_timeout_reaps_worker_and_removes_temp_directory(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (directory / "backend").mkdir()
        (directory / "backend/bot_worker.py").write_text(
            "import os, time\nfrom pathlib import Path\n"
            "Path('worker-state').write_text(str(os.getpid()) + '\\n' + os.environ['ARK_DATA_DIR'])\n"
            "time.sleep(60)\n", encoding="utf-8",
        )
        result = await Worker(directory, sys.executable)({"action": "test"}, 1)
        self.assertEqual(result, {"status": "timeout"})
        pid, work = (directory / "worker-state").read_text().splitlines()
        self.assertFalse(Path("/proc", pid).exists())
        self.assertFalse(Path(work).exists())
