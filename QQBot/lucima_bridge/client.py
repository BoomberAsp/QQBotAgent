"""Private Lucima transport shared by explicit commands and proposal-only tools."""
from __future__ import annotations

import os

import httpx

from agent.outbound import OutKeyboard
from lib.button_spec import Btn


def enabled() -> bool:
    return bool(os.environ.get("LUCIMA_INTERNAL_TOKEN"))


async def request_lucima(actor: tuple[str, str, str, str], action: str, arg: str, *, propose=False) -> dict:
    platform, bot, user, event = actor
    base_url = os.environ.get("LUCIMA_INTERNAL_URL", "http://127.0.0.1:8092").rstrip("/")
    route = "/internal/propose" if propose else "/internal/command"
    try:
        timeout = httpx.Timeout(25, connect=4) if action == "login_notice" and not propose else 4
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(
                base_url + route,
                headers={"Authorization": "Bearer " + os.environ["LUCIMA_INTERNAL_TOKEN"]},
                json={"platform": platform, "bot": bot, "user": user, "event": event, "action": action, "arg": arg},
            )
            if response.status_code == 400:
                return {"message": "Lucima 操作或参数无效。"}
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict):
                return result
    except (httpx.HTTPError, KeyError, ValueError):
        pass
    return {"message": "璐茜玛服务暂不可用，请稍后再试。"}


def approval_card(message: str, ticket: str) -> OutKeyboard:
    confirm = f"/璐茜玛 执行 {ticket}"
    discard = f"/璐茜玛 放弃 {ticket}"
    body = f"{message}\n请在 5 分钟内确认。\n{confirm}\n{discard}"
    return OutKeyboard(body, [[Btn("确认执行", confirm), Btn("取消", discard, style=0)]], label="lucima-confirm")
