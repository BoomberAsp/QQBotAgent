"""Offline regressions for the Lucima authorization and process boundaries."""
import asyncio
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from lucima_bridge.long_tasks import LOCAL, TASK_TIMEOUT
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

    def command(self, action, user="owner", arg="", event="1", platform="onebot", *, proposal=False, group=None):
        path = "/internal/propose" if proposal else "/internal/command"
        body = {
            "platform": platform, "bot": "bot", "user": user, "event": event, "action": action, "arg": arg,
        }
        if group is not None:
            body["group"] = group
            if not proposal:
                path = "/internal/group-command"
        return self.client.post(path, headers={"Authorization": "Bearer " + TOKEN}, json=body).json()

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

    def test_shared_group_qr_only_authorizes_the_owners_selected_browser(self):
        scope = {"group": "room", "platform": "qq_official"}
        issue = self.command("login", event="original", **scope)
        ticket = {"ticket": issue["login_url"].split("#")[1]}
        first = self.post("/api/claim", ticket).json()
        unselected = self.client.cookies.get("__Host-lucima")
        self.client.cookies.clear()
        selected = self.post("/api/claim", ticket).json()
        chosen = self.client.cookies.get("__Host-lucima")
        self.assertTrue(selected["group"])
        self.assertNotEqual(first["pin"], selected["pin"])
        for extra in ({"user": "other"}, {"group": "other-room"}, {"group": None}, {"platform": "onebot"}):
            result = self.command("confirm", arg=selected["pin"], **(scope | extra))
            self.assertIn("无效", result["message"])
        self.assertIn("已确认", self.command("confirm", arg=selected["pin"], **scope)["message"])
        self.assertEqual(self.post("/api/claim", ticket).status_code, 410)
        for session_id, status in ((unselected, 403), (chosen, 200)):
            self.client.cookies.clear()
            self.client.cookies.set("__Host-lucima", session_id)
            result = self.post("/api/login", {"account": "stub", "password": "stub", "remember": True})
            self.assertEqual(result.status_code, status)
        self.worker.assert_awaited_once()
        self.assertIn("发起登录的 QQ 群", result.json()["message"])
        self.assertEqual(self.command("login_notice", arg=issue["login_notice"], event="original", **scope)["menu"], "lobby")
        self.assertEqual(self.command("lobby", **scope)["menu"], "lobby")
        self.assertEqual(self.command("lobby", **(scope | {"user": "other"}))["menu"], "login")
        self.assertEqual(self.command("lobby", **(scope | {"group": None}))["menu"], "login")

    def test_group_confirmation_and_receipt_stay_in_owner_and_group_scope(self):
        scope = {"group": "room"}
        issue = self.command("login", **scope)
        claim = self.post("/api/claim", {"ticket": issue["login_url"].split("#")[1]}).json()
        self.command("confirm", arg=claim["pin"], **scope)
        self.assertEqual(self.post("/api/login", {"account": "stub", "password": "stub"}).status_code, 200)
        proposal = self.command("donate", arg="gold", event="proposed", **scope)
        ticket = proposal["approval"]
        wrong_scopes = ({"group": "room", "user": "other"}, {"group": "other-room"}, {})
        for other in wrong_scopes:
            self.assertNotIn("job", self.command("approve", arg=ticket, event="confirm", **other))
        self.assertNotIn("job", self.command("approve", arg=ticket, event="proposed", **scope))
        self.worker.assert_awaited_once()
        job = self.command("approve", arg=ticket, event="confirm", **scope)["job"]
        self.assertNotIn("job", self.command("approve", arg=ticket, event="repeat", **scope))
        for _ in range(50):
            answer = self.command("receipt", arg=job, **scope)
            if "receipt" in answer:
                break
            time.sleep(0.01)
        self.assertEqual(answer["receipt"]["id"], job)
        for other in wrong_scopes:
            self.assertNotIn("receipt", self.command("receipt", arg=job, **other))
        self.assertNotIn("approval", self.command("mail", proposal=True, **scope))

    def test_group_route_rejects_missing_scope_and_private_route_rejects_group_scope(self):
        body = {"platform": "onebot", "bot": "bot", "user": "owner", "event": "event", "action": "login"}
        headers = {"Authorization": "Bearer " + TOKEN}
        self.assertEqual(self.client.post("/internal/group-command", json=body).status_code, 403)
        self.assertEqual(self.client.post("/internal/group-command", headers=headers, json=body).status_code, 400)
        self.assertEqual(self.client.post("/internal/command", headers=headers, json={**body, "group": "room"}).status_code, 400)

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

    def test_remembered_login_autofills_only_after_qq_confirmation_and_preserves_expiry(self):
        account = "remembered@example.invalid"
        self.command("confirm", arg=self.open_ticket())
        self.assertEqual(self.post("/api/login", {"account": account, "password": "dummy-pass", "remember": True}).status_code, 200)
        self.assertNotIn(account.encode(), self.db_path.read_bytes())
        self.assertNotIn(b"dummy-pass", self.db_path.read_bytes())
        self.client.portal.call(app.state.bridge.attempts.clear)
        issue = self.command("login", event="resume-request")
        pin = self.claim_ticket(issue["login_url"])
        self.assertNotIn("saved_login", self.client.get("/api/session").json())
        self.assertEqual(self.post("/api/login", {"use_saved": True}).status_code, 403)
        self.command("confirm", arg=pin)
        candidate = self.client.get("/api/session").json()["saved_login"]
        self.assertEqual(candidate["account"], account)
        self.assertNotIn("fake-refresh", str(candidate))
        self.worker.return_value = {"status": "ok", "auth": {**AUTH, "refresh_token": "rotated-fake-refresh"}}
        self.assertEqual(self.post("/api/login", {"use_saved": True}).status_code, 200)
        args = self.worker.await_args.args[0]
        self.assertEqual(args, {"action": "resume", "auth": AUTH})
        notice = self.command("login_notice", arg=issue["login_notice"], event="resume-request")
        self.assertIn("已恢复记住的登录", notice["message"])
        with sqlite3.connect(self.db_path) as db:
            self.assertEqual(db.execute("SELECT expires FROM binding").fetchone()[0], candidate["expires"])

    def test_legacy_remembered_login_has_candidate_and_falls_back_without_new_qr(self):
        def put_legacy_record():
            vault = app.state.bridge.vault
            owner = vault.index('["onebot","bot","owner"]')
            self.assertTrue(vault.bind(owner, AUTH, True))
            game, _, _ = vault.record(owner)
            with vault.db:
                vault.db.execute("UPDATE binding SET credential = ? WHERE owner = ?", (vault.seal(owner, game, AUTH), owner))

        self.client.portal.call(put_legacy_record)
        issue = self.command("login", event="legacy")
        self.command("confirm", arg=self.claim_ticket(issue["login_url"]))
        self.assertEqual(self.client.get("/api/session").json()["saved_login"]["account"], "")
        self.worker.return_value = {"status": "login_failed"}
        self.assertEqual(self.post("/api/login", {"use_saved": True}).status_code, 409)
        self.assertNotIn("saved_login", self.client.get("/api/session").json())
        self.worker.return_value = {"status": "ok", "auth": AUTH}
        self.assertEqual(self.post("/api/login", {"account": "dummy-account", "password": "dummy-pass"}).status_code, 200)
        self.assertEqual(self.command("login_notice", arg=issue["login_notice"], event="legacy")["menu"], "lobby")

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

    def test_long_commands_and_agent_require_owner_confirmation(self):
        self.command("confirm", arg=self.open_ticket())
        self.assertEqual(self.post("/api/login", {"account": "stub", "password": "stub"}).status_code, 200)
        self.assertNotIn("approval", self.command("schedule_set", arg="signin 08:00"))
        self.assertNotIn("approval", self.command("buy_batch", arg="FriendShip3:11:24:100"))
        for proposal in (False, True):
            with self.subTest(proposal=proposal):
                self.worker.reset_mock()
                plan = self.command("buy_batch", arg="FriendShip3:2:24:100", event="request", proposal=proposal)
                self.assertIn("24 × 200", plan["message"])
                self.worker.assert_not_called()
                ticket = plan["approval"]
                self.assertNotIn("job", self.command("approve", arg=ticket, event="request"))
                self.assertNotIn("job", self.command("approve", user="other", arg=ticket, event="accept"))
                event = f"accept-{proposal}"
                job = self.command("approve", arg=ticket, event=event)
                self.assertTrue(job["long_job"])
                self.assertNotIn("job", self.command("approve", arg=ticket, event="replay"))
                for _ in range(50):
                    result = self.command("receipt", arg=job["job"])
                    if "receipt" in result:
                        break
                    time.sleep(0.01)
                self.assertEqual(result["receipt"]["task"], "批量购买")
                self.assertNotIn("receipt", self.command("receipt", user="other", arg=job["job"]))
                payload, timeout = self.worker.await_args.args
                self.assertEqual((payload["task"], payload["code"], timeout), ("buy_batch", "FriendShip3:2:24:100", TASK_TIMEOUT))

    def test_schedule_changes_require_confirmation_and_remembered_login(self):
        self.command("confirm", arg=self.open_ticket())
        self.post("/api/login", {"account": "stub", "password": "stub", "remember": True})
        ticket = self.command("schedule_set", arg="mail 08:00", event="schedule")["approval"]
        self.assertIn("没有有效", self.command("schedule_status")["message"])
        self.assertIn("已启用", self.command("approve", arg=ticket, event="enable")["message"])
        self.assertIn("08:00", self.command("schedule_status")["message"])
        self.assertIn("没有有效", self.command("schedule_status", user="other")["message"])
        ticket = self.command("schedule_stop", event="stop")["approval"]
        self.assertIn("08:00", self.command("schedule_status")["message"])
        self.assertIn("已停止", self.command("approve", arg=ticket, event="disable")["message"])
        self.assertIn("没有有效", self.command("schedule_status")["message"])


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_is_deduplicated_across_restart_and_never_replays_started_jobs(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        vault = Vault(directory / "state.sqlite3", Fernet.generate_key().decode())
        self.addCleanup(vault.db.close)
        vault.bind("alice", AUTH, True)
        vault.bind("bob", {**AUTH, "user_id": "bob"}, True)
        worker = AsyncMock()
        bridge = Bridge(vault, worker, ORIGIN)
        created = int(datetime(2025, 1, 1, 22, tzinfo=LOCAL).timestamp())
        due = int(datetime(2025, 1, 2, 0, 10, tzinfo=LOCAL).timestamp())
        bridge.long_tasks.replace_schedule("alice", vault.record("alice")[0], "mail", "23:45", "rev", created)
        bridge.enqueue_due(due)
        bridge.enqueue_due(due)
        self.assertEqual(len(bridge.queue), 1)
        job = bridge.queue.popleft()
        self.assertEqual((job.task, job.schedule), ("mail", "rev"))
        bridge.long_tasks.progress(job.id, "最后的模拟进度", int(time.time()))
        queued = bridge.submit("bob", "activity", "BH001 0 1 30", "queue")["job"]
        restarted = Bridge(vault, worker, ORIGIN)
        restarted.enqueue_due(due)
        self.assertFalse(restarted.queue)
        self.assertEqual(restarted.receipt("alice", job.id)["receipt"]["status"], "unconfirmed")
        self.assertEqual(restarted.receipt("alice", job.id)["receipt"]["details"], ["最后的模拟进度"])
        self.assertNotIn("receipt", restarted.receipt("bob", job.id))
        self.assertEqual(restarted.receipt("bob", queued)["receipt"]["status"], "cancelled")
        worker.assert_not_called()

    async def test_revoked_schedule_cannot_fall_back_to_a_live_session(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        vault = Vault(directory / "state.sqlite3", Fernet.generate_key().decode())
        self.addCleanup(vault.db.close)
        vault.bind("alice", AUTH, True)
        worker = AsyncMock()
        bridge = Bridge(vault, worker, ORIGIN)
        bridge.sessions["alice"] = (time.monotonic() + 900, AUTH)
        created = int(datetime(2025, 1, 1, 7, tzinfo=LOCAL).timestamp())
        bridge.long_tasks.replace_schedule("alice", vault.record("alice")[0], "mail", "08:00", "rev", created)
        bridge.enqueue_due(created + 3600)
        job = bridge.queue.popleft()
        vault.forget("alice")
        await bridge.run_job(job)
        self.assertEqual(bridge.receipt("alice", job.id)["receipt"]["status"], "cancelled")
        self.assertIn(job.id, bridge.schedule_status("alice"))
        worker.assert_not_called()

    async def test_cancel_only_targets_owners_active_long_job_and_releases_slot(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        vault = Vault(directory / "state.sqlite3", Fernet.generate_key().decode())
        self.addCleanup(vault.db.close)
        vault.bind("alice", AUTH, True)
        entered = asyncio.Event()

        async def worker(payload, timeout, *, cancel, progress):
            progress("模拟进度 1/3")
            entered.set()
            await cancel.wait()
            return {"status": "cancelled", "details": ["模拟已完成第一关"]}

        bridge = Bridge(vault, worker, ORIGIN)
        job = bridge.submit("alice", "activity", "BH001 0 3 100", "queue")["job"]
        consumer = asyncio.create_task(bridge.consume())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertIn("模拟进度 1/3", bridge.status("alice"))
            self.assertNotIn("模拟进度", bridge.status("bob"))
            self.assertNotIn("请求停止", bridge.cancel("bob", job_id=job))
            plan = bridge.prepare("alice", "cancel", "", preview="停止活动", event="request")
            self.assertIn("请求停止", bridge.approve("alice", plan["approval"], "accept", None)["message"])
            for _ in range(50):
                if bridge.active is None:
                    break
                await asyncio.sleep(0.01)
            self.assertIsNone(bridge.active)
            self.assertEqual(bridge.receipt("alice", job)["receipt"]["details"], ["模拟已完成第一关"])
        finally:
            consumer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await consumer

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

    async def test_streaming_worker_reports_progress_then_stops_and_reaps(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (directory / "backend").mkdir()
        (directory / "backend/bot_worker.py").write_text(
            "import json, os, signal, sys, threading\nfrom pathlib import Path\n"
            "stop = threading.Event()\nsignal.signal(signal.SIGTERM, lambda *_: stop.set())\n"
            "Path('worker-state').write_text(str(os.getpid()) + '\\n' + os.environ['ARK_DATA_DIR'])\n"
            "assert json.loads(sys.stdin.readline())['stream']\n"
            "print(json.dumps({'progress':'one stage'}), flush=True)\n"
            "assert stop.wait(10)\nprint(json.dumps({'status':'cancelled'}), flush=True)\n", encoding="utf-8",
        )
        cancel = asyncio.Event()
        messages = []

        def progress(message):
            messages.append(message)
            cancel.set()

        result = await Worker(directory, sys.executable)({"action": "test"}, 5, cancel=cancel, progress=progress)
        self.assertEqual(result, {"status": "cancelled"})
        self.assertEqual(messages, ["one stage"])
        pid, work = (directory / "worker-state").read_text().splitlines()
        self.assertFalse(Path("/proc", pid).exists())
        self.assertFalse(Path(work).exists())
