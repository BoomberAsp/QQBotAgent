"""Lucima commands terminate before the agent turn and its memory pipeline."""
from __future__ import annotations

import io
import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv
from nonebot import on_message
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import (
    Bot as OneBot, GroupMessageEvent as OneBotGroup, MessageSegment as OneBotSegment,
    PrivateMessageEvent,
)
from nonebot.adapters.qq import Bot as OfficialBot, MessageSegment as OfficialSegment
from nonebot.adapters.qq.event import C2CMessageCreateEvent, GroupMessageCreateEvent


load_dotenv(Path(__file__).resolve().parents[1] / ".env")
PREFIX = re.compile(r"^/璐茜玛(?:\s+|$)")
COMMANDS = {
    "登录": "login", "确认": "confirm", "状态": "status", "取消": "cancel", "解绑": "unbind",
    "签到": "signin", "领邮件": "mail", "领基地": "base", "兑换": "redeem", "图鉴": "encyclopedia",
}
HELP = (
    "璐茜玛命令\n"
    "/璐茜玛 登录 · 扫码绑定\n"
    "/璐茜玛 签到 · 领邮件 · 领基地\n"
    "/璐茜玛 兑换 <兑换码>\n"
    "/璐茜玛 图鉴 <角色名>\n"
    "/璐茜玛 状态 · 取消 · 解绑"
)


def enabled() -> bool:
    return bool(os.environ.get("LUCIMA_INTERNAL_TOKEN"))


def command_text(event: Event) -> str:
    if isinstance(event, (PrivateMessageEvent, OneBotGroup)):
        return event.get_plaintext().strip()
    if isinstance(event, (C2CMessageCreateEvent, GroupMessageCreateEvent)):
        return event.content.strip()
    return ""


async def is_command(event: Event) -> bool:
    return enabled() and bool(PREFIX.match(command_text(event)))


matcher = on_message(rule=is_command, priority=0, block=True)


async def call_service(text: str, platform: str, bot: str, user: str, event: str) -> dict:
    command, *args = PREFIX.sub("", text, count=1).split(maxsplit=1) or [""]
    action = COMMANDS.get(command)
    arg = args[0] if args else ""
    if action == "confirm":
        valid = bool(re.fullmatch(r"[0-9]{8}", arg))
    elif action == "redeem":
        valid = bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", arg))
    elif action == "encyclopedia":
        valid = 0 < len(arg) <= 64
    else:
        valid = bool(action) and not arg
    if not valid:
        return {"message": HELP}
    base_url = os.environ.get("LUCIMA_INTERNAL_URL", "http://127.0.0.1:8092").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=4, trust_env=False) as client:
            response = await client.post(
                base_url + "/internal/command",
                headers={"Authorization": "Bearer " + os.environ["LUCIMA_INTERNAL_TOKEN"]},
                json={"platform": platform, "bot": bot, "user": user, "event": event, "action": action, "arg": arg},
            )
            response.raise_for_status()
            result = response.json()
            if isinstance(result, dict):
                return result
    except (httpx.HTTPError, ValueError):
        pass
    return {"message": "璐茜玛服务暂不可用，请稍后再试。"}


def render_qr(url: str) -> bytes:
    import qrcode

    image = qrcode.make(url, box_size=8, border=4)
    with io.BytesIO() as buffer:
        image.save(buffer, format="PNG")
        return buffer.getvalue()


async def reply(bot, event, result: dict, image_segment) -> None:
    url = result.get("login_url")
    if isinstance(url, str) and url.startswith("https://"):
        qr_sent = False
        try:
            await bot.send(event, image_segment(render_qr(url)))
            qr_sent = True
        except Exception:
            pass
        instructions = "请在 5 分钟内扫码，并回到本私聊核对。"
        try:
            await bot.send(event, instructions + "也可打开：\n" + url)
        except Exception:
            await bot.send(event, instructions if qr_sent else "登录二维码和链接均未能送达，请稍后再试。")
    else:
        await bot.send(event, result.get("message", "璐茜玛服务暂不可用。"))


@matcher.handle()
async def handle_onebot(bot: OneBot, event: PrivateMessageEvent | OneBotGroup):
    if isinstance(event, OneBotGroup):
        if event.is_tome() or any(seg.type == "at" and seg.data.get("qq") == str(bot.self_id) for seg in event.message):
            await bot.send(event, "请在私聊使用 /璐茜玛，账号和密码只填写在登录网页。")
        return
    result = await call_service(command_text(event), "onebot", str(bot.self_id), str(event.user_id), str(event.message_id))
    await reply(bot, event, result, OneBotSegment.image)


@matcher.handle()
async def handle_official(bot: OfficialBot, event: C2CMessageCreateEvent | GroupMessageCreateEvent):
    if isinstance(event, GroupMessageCreateEvent):
        await bot.send(event, "请在私聊使用 /璐茜玛，账号和密码只填写在登录网页。")
        return
    user = event.author.user_openid
    if user:
        result = await call_service(command_text(event), "qq_official", str(bot.self_id), str(user), str(event.id))
        await reply(bot, event, result, OfficialSegment.file_image)
