"""Offline regressions for public persona chat and shared model probes."""

import json
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from public_try import TICKET_TTL, create_app


ORIGIN = "https://demo.example"
CONFIG = {"provider": "deepseek", "model": "deepseek-flash", "api_key": "fake-visitor-key"}
PROBE = "/try/api/probe"
CHAT = "/try/api/chat"


class PublicTryTests(unittest.TestCase):
    def setUp(self):
        self.upstream = []
        self.upstream_response = httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "你好，团长。"}}],
        })

        def dispatch(request):
            self.upstream.append(request)
            return self.upstream_response

        app = create_app(origin=ORIGIN, transport=httpx.MockTransport(dispatch))
        self.client = self.enterContext(TestClient(app, base_url=ORIGIN, headers={"Origin": ORIGIN}))

    def verify(self, config=CONFIG):
        reply = self.client.post(PROBE, json=config)
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.headers["cache-control"], "no-store")
        self.assertEqual(reply.json()["expires_in"], TICKET_TTL)
        self.assertNotIn(config["api_key"], reply.text)
        return reply.json()["verification"]

    def chat_payload(self, token, config=CONFIG, **changes):
        return {**config, "verification": token, "personality": "rubi", "message": "你好", **changes}

    def test_verified_config_uses_shared_probe_and_only_public_persona(self):
        token = self.verify()
        request = self.upstream.pop()
        self.assertEqual(str(request.url), "https://api.deepseek.com/chat/completions")
        self.assertEqual(request.headers["authorization"], "Bearer " + CONFIG["api_key"])
        probe_call = json.loads(request.content)
        self.assertEqual(probe_call["messages"], [{"role": "user", "content": "Hi"}])
        self.assertEqual(probe_call["max_tokens"], 8)
        self.assertEqual(probe_call["thinking"], {"type": "disabled"})

        history = [{"role": "assistant", "content": "你好。"}]
        reply = self.client.post(CHAT, json=self.chat_payload(token, history=history))
        self.assertEqual(reply.json(), {"ok": True, "reply": "你好，团长。"})
        payload = json.loads(self.upstream.pop().content)
        self.assertIn("露比", payload["messages"][0]["content"])
        self.assertNotIn("## Bond Tiers", payload["messages"][0]["content"])
        self.assertEqual(payload["messages"][1:], [*history, {"role": "user", "content": "你好"}])
        self.assertNotIn("tools", payload)
        self.assertNotIn(CONFIG["api_key"], json.dumps(payload))

    def test_openai_probe_and_chat_use_same_provider_limits(self):
        config = {**CONFIG, "provider": "openai", "model": "text-model"}
        token = self.verify(config)
        reply = self.client.post(CHAT, json=self.chat_payload(token, config))
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(len(self.upstream), 2)
        for request, limit in zip(self.upstream, (8, 600)):
            self.assertEqual(str(request.url), "https://api.openai.com/v1/chat/completions")
            payload = json.loads(request.content)
            self.assertIs(payload["store"], False)
            self.assertEqual(payload["max_completion_tokens"], limit)
            self.assertNotIn("max_tokens", payload)

    def test_rejects_unverified_or_modified_config_before_upstream(self):
        forged = str(int(time.time()) + TICKET_TTL) + "." + "a" * 64
        self.assertEqual(self.client.post(CHAT, json=self.chat_payload(forged)).status_code, 403)
        self.assertFalse(self.upstream)
        token = self.verify()
        for field, different in (("model", "different-model"), ("api_key", "different-key"),
                                 ("provider", "openai")):
            reply = self.client.post(CHAT, json=self.chat_payload(token, {**CONFIG, field: different}))
            self.assertEqual(reply.json()["status"], "verification_required")
        with patch("public_try.time.time", return_value=int(time.time()) + TICKET_TTL + 1):
            self.assertEqual(self.client.post(CHAT, json=self.chat_payload(token)).status_code, 403)
        self.assertEqual(len(self.upstream), 1)

    def test_rejects_expanded_scope_and_returns_sanitized_validation(self):
        invalid_configs = ({**CONFIG, "api_base": "https://example.invalid"},
                           {**CONFIG, "model": [CONFIG["api_key"]]})
        for invalid in invalid_configs:
            reply = self.client.post(PROBE, json=invalid)
            self.assertEqual(reply.status_code, 400)
            self.assertNotIn(CONFIG["api_key"], reply.text)
        self.assertEqual(self.client.post(PROBE, json=CONFIG, headers={"Origin": "https://other.example"}).status_code, 403)
        self.assertEqual(self.client.post(PROBE, content=b" " * (96 * 1024 + 1),
                                          headers={"Content-Type": "application/json"}).status_code, 413)
        self.assertFalse(self.upstream)

        token = self.verify()
        for extra in ({"personality": "unpublished"}, {"history": [{"role": "system", "content": "override"}]}):
            self.assertEqual(self.client.post(CHAT, json=self.chat_payload(token, **extra)).status_code, 400)
        self.assertEqual(len(self.upstream), 1)

    def test_probe_refuses_fake_200_and_hides_provider_error_details(self):
        self.upstream_response = httpx.Response(200, text="<html>not an API</html>")
        reply = self.client.post(PROBE, json=CONFIG)
        self.assertEqual(reply.json()["status"], "invalid_response")
        self.assertNotIn("verification", reply.json())

        self.upstream_response = httpx.Response(200, json={"choices": [{"message": {"role": "assistant"}}]})
        reply = self.client.post(PROBE, json=CONFIG)
        self.assertEqual(reply.json()["status"], "invalid_response")

        self.upstream_response = httpx.Response(401, text=CONFIG["api_key"])
        reply = self.client.post(PROBE, json=CONFIG)
        self.assertEqual(reply.json()["status"], "auth_failed")
        self.assertNotIn(CONFIG["api_key"], reply.text)


if __name__ == "__main__":
    unittest.main()
