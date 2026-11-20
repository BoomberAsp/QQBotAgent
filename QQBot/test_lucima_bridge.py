"""Offline regressions for the Lucima authorization and process boundaries."""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
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
            path = "/internal/group-propose" if proposal else "/internal/group-command"
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

    def test_public_manual_and_qq_summary_need_no_game_authorization(self):
        page = self.client.get("/manual")
        self.assertEqual(page.status_code, 200)
        self.assertIn("text/html", page.headers["content-type"])
        self.assertNotIn("set-cookie", page.headers)
        self.assertIn("Lucima QQ 使用手册", page.text)
        self.assertIn("/lucima 托管 动力转换 20:00", page.text)
        self.assertIn("&lt;队伍ID&gt;", page.text)
        self.assertEqual(self.client.get("/manual.css").status_code, 200)
        private = self.command("manual")
        group = self.command("manual", group="room", platform="qq_official")
        self.assertEqual(private, group)
        self.assertTrue(private["message"].endswith(ORIGIN + "/manual"))
        self.assertNotIn("20:00", private["message"])
        self.assertEqual(self.client.post("/internal/command", json={"action": "manual"}).status_code, 403)
        self.worker.assert_not_awaited()

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
        event = "ROBOT1.0_" + "A" * 128
        issue = self.command("login", event=event, **scope)
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
        self.assertEqual(self.command("login_notice", arg=issue["login_notice"], event=event, **scope)["menu"], "lobby")
        self.assertEqual(self.command("lobby", **scope)["menu"], "lobby")
        self.assertEqual(self.command("lobby", **(scope | {"user": "other"}))["menu"], "login")
        self.assertEqual(self.command("lobby", **(scope | {"group": None}))["menu"], "login")

    def test_group_proposal_confirmation_and_receipt_stay_in_owner_and_group_scope(self):
        scope = {"group": "room"}
        issue = self.command("login", **scope)
        claim = self.post("/api/claim", {"ticket": issue["login_url"].split("#")[1]}).json()
        self.command("confirm", arg=claim["pin"], **scope)
        self.assertEqual(self.post("/api/login", {"account": "stub", "password": "stub"}).status_code, 200)
        proposal = self.command("donate", arg="gold", event="proposed", proposal=True, **scope)
        ticket = proposal["approval"]
        wrong_scopes = ({"group": "room", "user": "other"}, {"group": "other-room"},
                        {"group": "room", "platform": "qq_official"}, {})
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
        self.assertNotIn("approval", self.command("mail", proposal=True, group="other-room"))

    def test_group_natural_login_only_issues_qr_after_user_confirmation(self):
        scope = {"group": "room", "platform": "qq_official"}
        plan = self.command("login", event="request", proposal=True, **scope)
        self.assertIn("原群", plan["message"])
        self.assertNotIn("login_url", plan)
        for action in ("approve", "confirm", "confirm_shop_hit", "login_notice", "discard", "clear_logs"):
            rejected = self.command(action, arg=plan["approval"], event="agent", proposal=True, **scope)
            self.assertNotIn("approval", rejected)
            self.assertNotIn("login_url", rejected)
        self.assertNotIn("login_url", self.command("approve", arg=plan["approval"], event="request", **scope))
        issue = self.command("approve", arg=plan["approval"], event="confirmed", **scope)
        self.assertIn("login_url", issue)
        self.assertIn("login_notice", issue)
        self.worker.assert_not_awaited()

    def test_shop_reference_is_public_and_does_not_forward_auth(self):
        self.worker.return_value = {"status": "ok", "details": ["静态商品参考价"]}
        scope = {"group": "shop-room"}
        job = self.command("long_info", arg="shop", **scope)["job"]
        for _ in range(50):
            result = self.command("receipt", arg=job, **scope)
            if "receipt" in result:
                break
            time.sleep(0.01)
        self.assertEqual(result["receipt"]["details"], ["静态商品参考价"])
        self.worker.assert_awaited_once_with({"action": "run", "task": "long_info", "code": "shop"}, 100)

    def test_login_profile_card_obeys_visibility_and_original_group_scope(self):
        from lucima_bridge.account_card import render_account_card

        for group in (None, "profile-room"):
            with self.subTest(group=group):
                scope = {"platform": "qq_official", "group": group}
                auth = {**AUTH, "user_id": f"profile-{group}"}
                snapshot = {
                    "name": "示例团长", "level": 70, "sampled_at": 1_780_000_000,
                    "uid": "123456789", "currencies": {"diamond": 31415},
                    "heroes": [{"name": "样例团员", "level": 60}],
                    "heroes_total": 12, "items_total": 25,
                    "stamina": {"current": 38, "max": 296}, "flag": {"current": 0, "max": 5},
                }
                self.worker.return_value = {"status": "ok", "auth": auth, "accountProfile": snapshot}
                issue = self.command("login", event="profile-login", **scope)
                claim = self.post("/api/claim", {"ticket": issue["login_url"].split("#")[1]})
                self.assertEqual(claim.status_code, 200)
                self.command("confirm", arg=claim.json()["pin"], event="profile-check", **scope)
                self.assertEqual(self.post("/api/login", {"account": "stub", "password": "stub",
                                                          "remember": True}).status_code, 200)
                notice = self.command("login_notice", arg=issue["login_notice"], event="profile-login", **scope)
                self.assertEqual(notice["menu"], "lobby")
                self.assertIn("profile_job", notice)
                self.assertFalse(self.command("login_notice", arg=issue["login_notice"], event="profile-login", **scope))

                def get_card(job):
                    for _ in range(50):
                        response = self.command("receipt", arg=job, **scope)
                        if "receipt" in response:
                            return response["receipt"]
                        time.sleep(0.01)
                    self.fail("账号卡回执未完成")

                with patch("lucima_bridge.web.render_account_card", wraps=render_account_card) as renderer:
                    card = get_card(notice["profile_job"])
                    self.assertTrue(card["profile_png"].startswith("iVBORw"))
                    self.assertNotIn("account_profile", card)
                    self.assertNotIn("profile_binding", card)
                    self.assertEqual(card["expand_profile"], group is not None)
                    data = renderer.call_args.args[0]
                    self.assertEqual(data.get("uid"), None if group else snapshot["uid"])
                    self.assertEqual(data.get("currencies", {}).get("diamond"), None if group else 31415)
                    self.worker.assert_awaited_with(
                        {"action": "run", "task": "account_view", "code": "profile" if group else "profile_full",
                         "auth": auth}, 100,
                    )

                    if group:
                        receipt_id = card["id"]
                        self.assertNotIn("approval", self.command("expand_profile", arg=receipt_id, user="other", **scope))
                        self.assertNotIn("job", self.command("account_view", arg="profile_full", **scope))
                        proposal = self.command("expand_profile", arg=receipt_id, event="profile-expand", **scope)
                        self.assertIn("群成员都可以看到", proposal["message"])
                        self.assertNotIn("job", self.command("approve", arg=proposal["approval"], user="other",
                                                            event="profile-other", **scope))
                        job = self.command("approve", arg=proposal["approval"], event="profile-confirm", **scope)["job"]
                        self.assertNotIn("job", self.command("approve", arg=proposal["approval"],
                                                            event="profile-replay", **scope))
                        expanded = get_card(job)
                        self.assertFalse(expanded["expand_profile"])
                        self.assertEqual(renderer.call_args.args[0]["uid"], snapshot["uid"])
                    else:
                        saved = self.command("login", event="profile-resume", **scope)
                        claim = self.post("/api/claim", {"ticket": saved["login_url"].split("#")[1]})
                        self.command("confirm", arg=claim.json()["pin"], event="resume-check", **scope)
                        with patch.dict(self.client.app.state.bridge.attempts, clear=True):
                            self.assertEqual(self.post("/api/login", {"use_saved": True}).status_code, 200)
                        returned = self.command(
                            "login_notice", arg=saved["login_notice"], event="profile-resume", **scope,
                        )
                        self.assertIn("已恢复", returned["message"])
                        self.assertTrue(get_card(returned["profile_job"])["profile_png"])

                with patch("lucima_bridge.web.render_account_card", side_effect=OSError):
                    fallback = get_card(notice["profile_job"])
                self.assertNotIn("profile_png", fallback)
                self.assertTrue(any("体力 38/296" in item for item in fallback["details"]))

                self.worker.return_value = {"status": "game_login_unavailable", "auth": auth}
                job = self.command("account_view", arg="profile", event="profile-refresh", **scope)["job"]
                failed = get_card(job)
                self.assertNotIn("profile_png", failed)
                self.assertIn("刷新信息", failed["details"][0])
                self.assertEqual(self.command("lobby", **scope)["menu"], "lobby")

    def test_battle_choices_and_confirmations_in_qq_group_and_private(self):
        cases = (
            ("activity", "BH12", "BH12 0 1 30", {"group": "battle-room", "platform": "qq_official"}),
            ("abyss", "MysteriousRealm_10", "MysteriousRealm_10 H001 H002", {"platform": "qq_official"}),
        )
        for kind, choice, code, scope in cases:
            with self.subTest(kind=kind):
                auth = {**AUTH, "user_id": f"mock-{kind}"}
                self.worker.return_value = {"status": "ok", "auth": auth}
                scope = {**scope, "user": kind}
                issue = self.command("login", event="issue", **scope)
                claim = self.post("/api/claim", {"ticket": issue["login_url"].split("#")[1]})
                self.assertEqual(claim.status_code, 200)
                pin = claim.json()["pin"]
                self.command("confirm", arg=pin, event="confirm-login", **scope)
                login = self.post("/api/login", {"account": "stub", "password": "stub"})
                self.assertEqual(login.status_code, 200)

                self.worker.return_value = {"status": "ok", "auth": auth, "battleOptions": [choice, "bad/id", choice]}
                job = self.command("long_info", arg=kind, event="query", **scope)["job"]
                for _ in range(50):
                    receipt = self.command("receipt", arg=job, event="query-receipt", **scope).get("receipt")
                    if receipt:
                        break
                    time.sleep(0.01)
                self.assertEqual(receipt["battle_choices"], [kind, [choice]])

                before = self.worker.await_count
                proposal = self.command(kind, arg=code, event="propose", **scope)
                token = proposal["approval"]
                self.assertIn("体力上限", proposal["message"])
                self.assertNotIn("job", self.command("approve", arg=token, event="propose", **scope))
                wrong_scope = {**scope, "group": None} if "group" in scope else {**scope, "group": "battle-room"}
                self.assertNotIn("job", self.command("approve", arg=token, event="outside", **wrong_scope))
                self.assertEqual(self.worker.await_count, before)

                self.worker.return_value = {"status": "ok", "auth": auth, "details": ["游戏结算已确认"]}
                job = self.command("approve", arg=token, event="confirm-battle", **scope)["job"]
                self.assertNotIn("job", self.command("approve", arg=token, event="repeat", **scope))
                for _ in range(50):
                    receipt = self.command("receipt", arg=job, event="battle-receipt", **scope).get("receipt")
                    if receipt:
                        break
                    time.sleep(0.01)
                self.assertEqual(receipt["details"], ["游戏结算已确认"])
                self.assertEqual(self.worker.await_count, before + 1)
                self.assertEqual(self.worker.await_args.args[0]["code"], code)

    def test_illusion_sweep_quote_requires_separate_confirmation_in_each_qq_context(self):
        for group in (None, "illusion-room"):
            with self.subTest(group=group):
                scope = {"platform": "qq_official", "group": group}
                auth = {**AUTH, "user_id": f"illusion-{group}"}
                self.worker.return_value = {"status": "ok", "auth": auth}
                issue = self.command("login", event="illusion-login", **scope)
                claim = self.post("/api/claim", {"ticket": issue["login_url"].split("#")[1]})
                self.assertEqual(claim.status_code, 200)
                self.command("confirm", arg=claim.json()["pin"], event="illusion-login-confirm", **scope)
                response = self.post("/api/login", {"account": "stub", "password": "stub"})
                self.assertEqual(response.status_code, 200)

                self.worker.return_value = {"status": "ok", "auth": auth,
                                            "illusionQuote": {"count": 3, "floor": 80}}
                job = self.command("account_view", arg="abyss_sweep", event="illusion-query", **scope)["job"]
                for _ in range(50):
                    receipt = self.command("receipt", arg=job, event="illusion-quote", **scope).get("receipt")
                    if receipt:
                        break
                    time.sleep(0.01)
                self.assertEqual(receipt["illusion_quote"], {"count": 3, "floor": 80})
                before = self.worker.await_count

                prepared = self.command("sweep_illusion", arg="3 80", event="illusion-prepare", **scope)
                self.assertIn("第 80 层", prepared["message"])
                self.assertIn("全部 3 个意识同步器", prepared["message"])
                self.assertIn("体力消耗 0", prepared["message"])
                token = prepared["approval"]
                self.assertNotIn("job", self.command("approve", arg=token, event="illusion-prepare", **scope))
                self.assertNotIn("job", self.command("approve", arg=token, event="illusion-other", user="other", **scope))
                self.assertEqual(self.worker.await_count, before)

                self.worker.return_value = {"status": "ok", "auth": auth, "details": ["扫荡扣费已确认"]}
                job = self.command("approve", arg=token, event="illusion-confirm", **scope)["job"]
                self.assertNotIn("job", self.command("approve", arg=token, event="illusion-replay", **scope))
                for _ in range(50):
                    receipt = self.command("receipt", arg=job, event="illusion-result", **scope).get("receipt")
                    if receipt:
                        break
                    time.sleep(0.01)
                self.assertEqual(receipt["details"], ["扫荡扣费已确认"])
                self.assertNotIn("illusion_quote", receipt)
                self.worker.assert_awaited_with({"action": "run", "task": "sweep_illusion",
                                                 "code": "3 80", "auth": auth}, 100)
                self.assertEqual(self.worker.await_count, before + 1)



    def test_shop_refreshes_require_scoped_confirmation(self):
        scope = {"group": "shop-room"}
        issue = self.command("login", **scope)
        pin = self.post("/api/claim", {"ticket": issue["login_url"].split("#")[1]}).json()["pin"]
        self.command("confirm", arg=pin, **scope)
        self.assertEqual(self.post("/api/login", {"account": "stub", "password": "stub"}).status_code, 200)
        action = "refresh_secret"
        self.worker.reset_mock()
        plan = self.command(action, event="prepare", **scope)
        ticket = plan["approval"]
        self.assertIn("1 次", plan["message"])
        self.assertIn("自动按货架金币价购买", plan["message"])
        self.worker.assert_not_called()
        self.assertNotIn("job", self.command("approve", arg=ticket, event="accept", group="other-room"))
        job = self.command("approve", arg=ticket, event=f"accept-{action}", **scope)["job"]
        for _ in range(50):
            result = self.command("receipt", arg=job, **scope)
            if "receipt" in result:
                break
            time.sleep(0.01)
        self.assertEqual(result["receipt"]["status"], "ok")
        self.worker.assert_awaited_once()
        payload, timeout = self.worker.await_args.args
        self.assertEqual((payload["task"], timeout), (action, 100))
        self.assertEqual(json.loads(payload["code"]), {"order": "1 free", "scheme": None,
                                                       "targets": ["recruit", "mystery", "chip"]})

    def test_full_navigation_and_theme_are_available_without_a_game_login(self):
        group = {"group": "layout"}
        home = self.command("lobby", **group)
        self.assertEqual([row[0] for row in home["buttons"][:9]], [
            "基本信息", "自动任务", "主动任务", "佣兵团", "工具箱", "运行日志", "退出账号", "切换明暗", "切换账号",
        ])
        for label, command in home["buttons"][:9]:
            view = self.command("navigate", arg=command.split()[1], **group)
            self.assertIn(["大厅", "菜单 home"], view["buttons"])
            self.assertLessEqual(len(view["buttons"]), 15)
        changed = self.command("theme", arg="亮色", **group)
        self.assertEqual(changed["theme"], "light")
        self.assertTrue(changed["picture"])
        self.assertEqual(self.command("lobby")["theme"], "default")
        self.worker.assert_not_called()

    def test_multiple_slots_keep_old_jobs_and_invalidates_pending_confirmations(self):
        scope = {"group": "room"}
        first = self.command("login", **scope)
        pin = self.post("/api/claim", {"ticket": first["login_url"].split("#")[1]}).json()["pin"]
        self.command("confirm", arg=pin, **scope)
        body = {"account": "fixture", "password": "fixture", "remember": True}
        self.assertEqual(self.post("/api/login", body).status_code, 200)
        self.command("navigate", arg="info", **scope)
        first_receipt = self.command("receipt", **scope)["receipt"]["id"]
        plan = self.command("mail", event="draft", **scope)["approval"]
        second = self.command("add_account", **scope, event="adding")
        pin2 = self.post("/api/claim", {"ticket": second["login_url"].split("#")[1]}).json()["pin"]
        self.assertIn("无效", self.command("confirm", arg=pin2, group="elsewhere")["message"])
        self.command("confirm", arg=pin2, **scope)
        self.worker.return_value = {"status": "ok", "auth": {**AUTH, "user_id": "second"}}
        self.assertEqual(self.post("/api/login", body).status_code, 200)
        self.assertIn("账号 2", self.command("navigate", arg="accounts", **scope)["message"])
        self.assertNotEqual(self.command("receipt", **scope)["receipt"]["id"], first_receipt)
        self.assertIn("receipt", self.command("receipt", arg=first_receipt, **scope))
        self.assertNotIn("receipt", self.command("receipt", arg=first_receipt, group="elsewhere"))
        self.assertNotIn("job", self.command("approve", arg=plan, event="accept", **scope))
        self.assertIn("基本信息", self.command("switch_account", arg="1", **scope)["message"])
        self.assertNotIn("job", self.command("approve", arg=plan, event="accept", **scope))
        logout = self.command("logout", event="logout", **scope)["approval"]
        self.assertIn("已退出 账号 1", self.command("approve", arg=logout, event="accept-logout", **scope)["message"])
        self.assertIn("账号 2", self.command("lobby", **scope)["message"])

    def test_integer_shop_order_keeps_free_cap_and_stops_without_verified_quotes(self):
        self.command("confirm", arg=self.open_ticket())
        self.assertEqual(self.post("/api/login", {"account": "fixture", "password": "fixture"}).status_code, 200)
        self.worker.reset_mock()
        choose = self.command("refresh_secret", arg="２００")
        self.assertIn(["选择免费", "刷新秘密商店 200 免费"], choose["buttons"])
        plan = self.command("refresh_secret", arg="２００ 免费", event="plan")
        self.assertIn("申请 200 次", plan["message"])
        self.assertIn("扣款上限 0", plan["message"])
        self.assertNotIn("approval", self.command("refresh_star_source", arg="200"))
        self.assertIn("报价", self.command("refresh_star_source", arg="200")["message"])
        self.assertNotIn("approval", self.command("refresh_secret", arg="5 钻石"))
        self.assertIn("整数", self.command("refresh_secret", arg="1.5")["message"])
        self.assertIn("1–200", self.command("refresh_secret", arg="201")["message"])
        self.worker.assert_not_called()
        job = self.command("approve", arg=plan["approval"], event="approve")["job"]
        for _ in range(50):
            result = self.command("receipt", arg=job)
            if "receipt" in result:
                break
            time.sleep(0.01)
        self.assertEqual(result["receipt"]["status"], "ok")
        self.worker.assert_awaited_once()
        payload = self.worker.await_args.args[0]
        self.assertEqual(json.loads(payload["code"]),
                         {"order": "200 free", "scheme": None, "targets": ["recruit", "mystery", "chip"]})

    def test_paid_secret_shop_freezes_90_rounds_and_270_diamond_budget(self):
        scope = {"group": "shop-room"}
        ticket = self.command("login", **scope)["login_url"].split("#")[1]
        pin = self.post("/api/claim", {"ticket": ticket}).json()["pin"]
        self.command("confirm", arg=pin, **scope)
        self.assertEqual(self.post("/api/login", {"account": "stub", "password": "stub"}).status_code, 200)
        self.worker.reset_mock()
        quote = {"gameVersion": "5.0.0.116271", "catalog": "catalog_116310", "currency": "2", "unitPrice": 3}
        self.client.portal.call(setattr, app.state.bridge, "secret_shop_quote", quote)
        self.assertNotIn("approval", self.command("refresh_secret", arg="201 diamond", proposal=True, **scope))
        plan = self.command("refresh_secret", arg="90 diamond", event="prepare", proposal=True, **scope)
        self.assertIn("每次 3 颗钻石", plan["message"])
        self.assertIn("270 颗钻石", plan["message"])
        self.worker.assert_not_awaited()
        self.assertNotIn("job", self.command("approve", arg=plan["approval"], event="other", group="other"))
        job = self.command("approve", arg=plan["approval"], event="approve", **scope)
        self.assertTrue(job["long_job"])
        for _ in range(50):
            receipt = self.command("receipt", arg=job["job"], **scope)
            if "receipt" in receipt:
                break
            time.sleep(0.01)
        self.assertEqual(receipt["receipt"]["status"], "ok")
        payload, timeout = self.worker.await_args.args
        self.assertEqual(timeout, TASK_TIMEOUT)
        self.assertEqual(payload["task"], "refresh_secret_paid")
        self.assertEqual(json.loads(payload["code"]), {"count": 90, "quote": quote, "budget": 270, "scheme": None,
                                                       "targets": ["recruit", "mystery", "chip"]})

    def test_star_source_single_and_batch_stop_before_queue_in_group_and_agent(self):
        for scope in ({}, {"group": "shop-room"}):
            for arg in ("", "1", "200"):
                result = self.command("refresh_star_source", arg=arg, **scope)
                self.assertEqual(result["status"], "quote_unavailable")
                self.assertNotIn("approval", result)
                self.assertNotIn("job", result)
                self.assertIn("查看星源商店", result["message"])
            proposal = self.command("refresh_star_source", arg="1", proposal=True, **scope)
            self.assertEqual(proposal["status"], "quote_unavailable")
            self.assertNotIn("approval", proposal)
        self.worker.assert_not_awaited()

    def test_star_source_status_reaches_only_the_read_only_query(self):
        self.command("confirm", arg=self.open_ticket())
        self.assertEqual(self.post("/api/login", {"account": "fixture", "password": "fixture"}).status_code, 200)
        self.worker.reset_mock()
        self.worker.return_value = {"status": "ok", "details": ["星源商店刷新计数 2。"]}
        result = self.command("account_view", arg="star_source", event="query")
        self.assertNotIn("approval", result)
        for _ in range(50):
            receipt = self.command("receipt", arg=result["job"])
            if "receipt" in receipt:
                break
            time.sleep(0.01)
        self.assertEqual(receipt["receipt"]["details"], ["星源商店刷新计数 2。"])
        self.worker.assert_awaited_once_with({"action": "run", "task": "account_view",
                                            "code": "star_source", "auth": AUTH}, 100)

    def test_star_source_plan_and_equip_selection_stay_in_original_scope(self):
        scope = {"group": "shop-room"}
        ticket = self.command("login", **scope)["login_url"].split("#")[1]
        pin = self.post("/api/claim", {"ticket": ticket}).json()["pin"]
        self.command("confirm", arg=pin, **scope)
        self.assertEqual(self.post("/api/login", {"account": "stub", "password": "stub"}).status_code, 200)
        policy = {"gameVersion": "5.0.0.116271", "catalog": "catalog_116310",
                  "refresh": {"currency": "", "amount": 0}, "equipment": {"currency": "109", "amount": 5}}
        self.client.portal.call(setattr, app.state.bridge, "star_source_quote", policy)
        scheme = {"name": "速度", "slots": {"ring": [{"sets": ["Speed"]}]}}
        self.command("filter_save", arg=json.dumps(scheme), **scope)
        self.worker.reset_mock()
        signature = "a" * 64
        self.worker.return_value = {
            "status": "shop_hit", "details": ["第 2 档 · 速度戒指"],
            "shopOptions": [{"slot": 2, "key": signature, "price": policy["equipment"]}],
        }
        self.assertNotIn("approval", self.command("refresh_star_source", arg="3", **scope))
        plan = self.command("refresh_star_source", arg="3 速度", event="request", proposal=True, **scope)
        self.assertIn("免费刷新 3 次", plan["message"])
        self.assertIn("5 个彩虹星源", plan["message"])
        self.worker.assert_not_awaited()
        job = self.command("approve", arg=plan["approval"], event="refresh", **scope)["job"]
        for _ in range(50):
            result = self.command("receipt", arg=job, **scope)
            if "receipt" in result:
                break
            time.sleep(0.01)
        self.assertEqual(result["receipt"]["shop_choices"], [[2, "5 彩虹星源"]])
        self.assertNotIn(signature, str(result))
        self.worker.assert_awaited_once()
        self.assertEqual(json.loads(self.worker.await_args.args[0]["code"])["scheme"], scheme)
        for other in ({"group": "elsewhere"}, {"user": "other", **scope}, {}):
            self.assertNotIn("job", self.command("buy_shop_hit", arg=f"{job} 2", event="click", **other))
        self.assertNotIn("job", self.command("buy_shop_hit", arg=f"{job} 2", proposal=True, **scope))
        self.assertNotIn("job", self.command("buy_shop_hit", arg=f"{job} 1", event="unmatched", **scope))
        self.worker.return_value = {"status": "ok", "details": ["已确认扣除 5 个彩虹星源"]}
        purchase = self.command("buy_shop_hit", arg=f"{job} 2", event="bought", **scope)["job"]
        self.assertNotIn("job", self.command("buy_shop_hit", arg=f"{job} 2", event="replay", **scope))
        for _ in range(50):
            result = self.command("receipt", arg=purchase, **scope)
            if "receipt" in result:
                break
            time.sleep(0.01)
        self.assertIn("彩虹星源", result["receipt"]["details"][0])
        self.assertEqual(self.worker.await_count, 2)
        payload = self.worker.await_args.args[0]
        self.assertEqual(payload["task"], "buy_shop_equipment")
        self.assertEqual(json.loads(payload["code"])["key"], signature)

    def test_equipment_hit_pauses_same_shop_until_owner_confirms(self):
        scope = {"group": "shop-room"}
        ticket = self.command("login", **scope)["login_url"].split("#")[1]
        pin = self.post("/api/claim", {"ticket": ticket}).json()["pin"]
        self.command("confirm", arg=pin, **scope)
        self.assertEqual(self.post("/api/login", {"account": "stub", "password": "stub", "remember": True}).status_code, 200)
        self.worker.reset_mock()
        self.worker.return_value = {"status": "shop_hit", "details": ["第 2 档 · 速度戒指"]}
        plan = self.command("refresh_secret", arg="1 免费", event="prepare", **scope)["approval"]
        job = self.command("approve", arg=plan, event="submit", **scope)["job"]
        for _ in range(50):
            result = self.command("receipt", arg=job, **scope)
            if "receipt" in result:
                break
            time.sleep(0.01)
        receipt = result["receipt"]
        self.assertEqual(receipt["status"], "shop_hit")
        self.assertGreater(receipt["shop_confirm_until"], time.time())
        self.assertNotIn("receipt", self.command("confirm_shop_hit", arg=job, user="other", **scope))
        self.assertNotIn("receipt", self.command("confirm_shop_hit", arg=job, group="elsewhere"))
        self.assertNotIn("receipt", self.command("confirm_shop_hit", arg=job))
        again = self.command("refresh_secret", arg="1 免费", event="prepare2", **scope)["approval"]
        blocked = self.command("approve", arg=again, event="submit2", **scope)
        self.assertNotIn("job", blocked)
        self.assertEqual(blocked["receipt"]["id"], job)
        self.assertEqual(self.command("confirm_shop_hit", arg=job, event="ending", **scope)["receipt"]["status"], "ok")
        self.assertNotIn("receipt", self.command("confirm_shop_hit", arg=job, event="ending-again", **scope))
        self.assertIn("命中已确认", self.command("receipt", arg=job, **scope)["message"])
        self.worker.assert_awaited_once()

    def test_equipment_scheme_is_scoped_and_frozen_into_the_shop_confirmation(self):
        self.command("confirm", arg=self.open_ticket())
        self.assertEqual(self.post("/api/login", {"account": "fixture", "password": "fixture"}).status_code, 200)
        speed = '{"name":"筛选","slots":{"ring":[{"sets":["Speed"]}]}}'
        attack = '{"name":"筛选","slots":{"ring":[{"sets":["Attack"]}]}}'
        self.assertIn("保存", self.command("filter_save", arg=speed)["message"])
        self.assertEqual(self.command("filter_export", arg="筛选")["message"], speed)
        self.assertNotIn("Speed", self.command("filter_export", arg="筛选", user="other")["message"])
        order = self.command("refresh_secret", arg="1 免费 筛选", event="prepare")
        self.assertIn("筛选", order["message"])
        self.command("filter_save", arg=attack, event="mutate")
        job = self.command("approve", arg=order["approval"], event="execute")["job"]
        for _ in range(50):
            if "receipt" in self.command("receipt", arg=job):
                break
            time.sleep(0.01)
        self.assertIn("Speed", self.worker.await_args.args[0]["code"])
        self.assertNotIn("Attack", self.worker.await_args.args[0]["code"])

    def test_group_route_rejects_missing_scope_and_private_route_rejects_group_scope(self):
        body = {"platform": "onebot", "bot": "bot", "user": "owner", "event": "event", "action": "login"}
        headers = {"Authorization": "Bearer " + TOKEN}
        self.assertEqual(self.client.post("/internal/group-command", json=body).status_code, 403)
        self.assertEqual(self.client.post("/internal/group-command", headers=headers, json=body).status_code, 400)
        oversized = {**body, "group": "room", "event": "E" * 513}
        self.assertEqual(self.client.post("/internal/group-command", headers=headers, json=oversized).status_code, 400)
        self.assertEqual(self.client.post("/internal/group-propose", json=body).status_code, 403)
        self.assertEqual(self.client.post("/internal/group-propose", headers=headers, json=body).status_code, 400)
        group_body = {**body, "group": "room"}
        for path in ("/internal/command", "/internal/propose"):
            self.assertEqual(self.client.post(path, headers=headers, json=group_body).status_code, 400)
        accepted = self.client.post("/internal/group-propose", headers=headers, json=group_body)
        self.assertEqual(accepted.status_code, 200)
        self.assertIn("approval", accepted.json())

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
        receipt = lobby["receipt"]
        self.assertEqual(receipt["kind"], "login")
        self.assertEqual(self.command("receipt", arg=receipt["id"])["receipt"], receipt)
        self.assertNotIn("receipt", self.command("receipt", arg=receipt["id"], user="other"))
        self.assertNotIn("dummy-account", str(receipt))
        from lucima_bridge.receipt import render_receipt
        self.assertTrue(render_receipt(receipt).startswith(b"\x89PNG\r\n\x1a\n"))
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

    def test_temporary_saved_login_failure_preserves_confirmed_ticket_for_retry(self):
        self.command("confirm", arg=self.open_ticket())
        self.assertEqual(self.post("/api/login", {
            "account": "remembered", "password": "dummy", "remember": True,
        }).status_code, 200)
        self.client.portal.call(app.state.bridge.attempts.clear)
        self.command("confirm", arg=self.open_ticket())
        self.worker.return_value = {"status": "auth_unavailable"}
        failed = self.post("/api/login", {"use_saved": True})
        self.assertEqual(failed.status_code, 503)
        self.assertIn("授权仍保留", failed.json()["message"])
        self.assertEqual(self.client.get("/api/session").json()["saved_login"]["account"], "remembered")
        self.assertEqual(self.post("/api/login", {"use_saved": True}).status_code, 429)
        self.client.portal.call(app.state.bridge.attempts.clear)
        self.worker.return_value = {"status": "ok", "auth": AUTH}
        self.assertEqual(self.post("/api/login", {"use_saved": True}).status_code, 200)

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
        unbind = self.command("unbind", event="unbind")["approval"]
        self.assertIn("清除", self.command("approve", arg=unbind, event="confirm-unbind")["message"])
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
    async def test_only_confirmed_auth_expiry_clears_credentials_and_schedules(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        vault = Vault(directory / "state.sqlite3", Fernet.generate_key().decode())
        self.addCleanup(vault.db.close)
        vault.bind("alice", AUTH, True)
        worker = AsyncMock(return_value={"status": "auth_unavailable", "auth": AUTH})
        bridge = Bridge(vault, worker, ORIGIN)
        self.assertIn("已启用", bridge.set_schedule("alice", "base 20:00"))
        plan = bridge.long_tasks.schedule("alice", "base")
        candidate = vault.candidate("alice")
        first = bridge.submit("alice", "reactor", "", "first")["job"]
        await bridge.run_job(bridge.queue.popleft())
        receipt = bridge.receipt("alice", first)["receipt"]
        self.assertEqual(receipt["status"], "auth_unavailable")
        self.assertIn("托管计划已保留", receipt["message"])
        self.assertEqual(vault.candidate("alice"), candidate)
        self.assertEqual(bridge.long_tasks.schedule("alice", "base"), plan)

        worker.return_value = {"status": "auth_expired"}
        second = bridge.submit("alice", "reactor", "", "second")["job"]
        await bridge.run_job(bridge.queue.popleft())
        self.assertEqual(bridge.receipt("alice", second)["receipt"]["status"], "auth_expired")
        self.assertIsNotNone(vault.record("alice"))
        self.assertIsNone(vault.candidate("alice"))
        self.assertIsNone(bridge.long_tasks.schedule("alice", "base"))

    async def test_equipment_hit_survives_restart_and_expires_without_replaying(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        vault = Vault(directory / "state.sqlite3", Fernet.generate_key().decode())
        self.addCleanup(vault.db.close)
        vault.bind("alice", AUTH, True)
        worker = AsyncMock(return_value={"status": "shop_hit", "details": ["匹配装备"]})
        bridge = Bridge(vault, worker, ORIGIN)
        job_id = bridge.submit("alice", "refresh_secret", "1 free", "once")["job"]
        await bridge.run_job(bridge.queue.popleft())
        restarted = Bridge(vault, worker, ORIGIN)
        receipt = restarted.receipt("alice", job_id)["receipt"]
        self.assertEqual(receipt["status"], "shop_hit")
        with patch("lucima_bridge.service.time.time", return_value=receipt["shop_confirm_until"] + 1):
            self.assertNotIn("receipt", restarted.confirm_shop_hit("alice", job_id))
            self.assertIn("超时", restarted.receipt("alice", job_id)["message"])
            self.assertIn("job", restarted.submit("alice", "refresh_secret", "1 free", "next"))
        worker.assert_awaited_once()

    async def test_legacy_schedule_migrates_and_retains_every_independent_rule(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        vault = Vault(directory / "state.sqlite3", Fernet.generate_key().decode())
        self.addCleanup(vault.db.close)
        vault.bind("alice", AUTH, True)
        game = vault.record("alice")[0]
        with vault.db:
            vault.db.execute("""CREATE TABLE lucima_schedule (owner TEXT PRIMARY KEY, game TEXT, task TEXT,
                hhmm TEXT, first_day TEXT, last_day TEXT, revision TEXT)""")
            vault.db.execute("INSERT INTO lucima_schedule VALUES (?, ?, ?, ?, ?, ?, ?)",
                             ("alice", game, "mail", "08:00", "2025-01-01", "2025-01-03", "old"))
        bridge = Bridge(vault, AsyncMock(), ORIGIN)
        self.assertEqual(bridge.long_tasks.schedule("alice", "mail")[4], "2025-01-03")
        self.assertIn("已启用", bridge.set_schedule("alice", "base 20:00"))
        self.assertEqual({row[1] for row in bridge.long_tasks.schedules("alice")}, {"mail", "base"})
        bridge.stop_schedule("alice", "mail")
        self.assertEqual(bridge.long_tasks.schedule("alice")[1], "base")
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
