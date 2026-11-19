"""Lucima transport shared by explicit commands and proposal tools."""
from __future__ import annotations

import os

import httpx

from agent.outbound import OutKeyboard
from lib.button_spec import Btn


Actor = tuple[str, str, str, str] | tuple[str, str, str, str, str]


def enabled() -> bool:
    return bool(os.environ.get("LUCIMA_INTERNAL_TOKEN"))


async def request_lucima(actor: Actor, action: str, arg: str, *, propose=False) -> dict:
    platform, bot, user, event, *group = actor
    payload = {"platform": platform, "bot": bot, "user": user, "event": event, "action": action, "arg": arg}
    if group:
        payload["group"] = group[0]
    base_url = os.environ.get("LUCIMA_INTERNAL_URL", "http://127.0.0.1:8092").rstrip("/")
    operation = "propose" if propose else "command"
    prefix = "group-" if group else ""
    route = f"/internal/{prefix}{operation}"
    try:
        timeout = httpx.Timeout(25, connect=4) if action == "login_notice" and not propose else 4
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(
                base_url + route,
                headers={"Authorization": "Bearer " + os.environ["LUCIMA_INTERNAL_TOKEN"]},
                json=payload,
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


def approval_card(message: str, ticket: str, *, group: bool = False) -> OutKeyboard:
    confirm = f"/璐茜玛 执行 {ticket}"
    discard = f"/璐茜玛 放弃 {ticket}"
    notice = ("仅发起人可在原群确认，请在 5 分钟内点按钮或 @ 机器人发送以下命令。"
              if group else "请在 5 分钟内确认。")
    body = f"{message}\n{notice}\n{confirm}\n{discard}"
    return OutKeyboard(body, [[Btn("确认执行", confirm), Btn("取消", discard, style=0)]], label="lucima-confirm")
