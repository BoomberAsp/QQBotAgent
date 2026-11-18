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

    def command(self, action, user="owner", arg="", event="1", platform="onebot"):
        return self.client.post("/internal/command", headers={"Authorization": "Bearer " + TOKEN}, json={
            "platform": platform, "bot": "bot", "user": user, "event": event, "action": action, "arg": arg,
        }).json()

    def post(self, path, body, origin=ORIGIN):
        return self.client.post(path, headers={"Origin": origin}, json=body)

    def open_ticket(self, user="owner"):
        ticket = self.command("login", user=user)["login_url"].split("#")[1]
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


class QueueTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertIn("已提交", bridge.submit("0", "mail", "", "event"))
        consumer = asyncio.create_task(bridge.consume())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertIn("已经提交", bridge.submit("0", "mail", "", "event"))
            self.assertIn("已有", bridge.submit("0", "mail", "", "another"))
            for index in range(1, 6):
                self.assertIn("已提交", bridge.submit(str(index), "mail", "", "event"))
            self.assertIn("队列已满", bridge.submit("6", "mail", "", "event"))
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
