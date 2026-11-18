"""Lucima commands terminate before the agent turn and its memory pipeline."""
from __future__ import annotations

import asyncio
import io
import re
from pathlib import Path

from dotenv import load_dotenv
from nonebot import on_message
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import (
    Bot as OneBot, GroupMessageEvent as OneBotGroup, MessageSegment as OneBotSegment,
    PrivateMessageEvent,
)
from nonebot.adapters.qq import Bot as OfficialBot, MessageSegment as OfficialSegment
from nonebot.adapters.qq.event import C2CMessageCreateEvent, GroupMessageCreateEvent

from agent.outbound import OutKeyboard
from lib.button_spec import Btn
from lucima_bridge.client import approval_card, enabled, request_lucima
from lucima_bridge.receipt import render_receipt


load_dotenv(Path(__file__).resolve().parents[1] / ".env")
PREFIX = re.compile(r"^/(?:璐茜玛|璐西玛|露西玛|lucima)(?:\s+|$)", re.IGNORECASE)
COMMANDS = {
    "登录": "login", "确认": "confirm", "状态": "status", "取消": "cancel", "解绑": "unbind",
    "签到": "signin", "领邮件": "mail", "领基地": "base", "兑换": "redeem", "图鉴": "encyclopedia",
    "领派遣": "dispatch", "领成就": "achievement", "团签到": "guild", "团周奖": "guild_weekly", "免费招募": "recruit",
    "筛装备": "equipment", "执行": "approve", "放弃": "discard", "回执": "receipt",
}
SHORTCUTS = {"捐赠金币": ("donate", "gold"), "捐赠证明": ("donate", "courage"),
             "讨伐关卡": ("stages", "hunt"), "元素关卡": ("stages", "elf")}
HELP = (
    "璐茜玛命令\n"
    "别名 /璐西玛、/露西玛、/lucima\n"
    "/璐茜玛 登录 · 扫码绑定\n"
    "/璐茜玛 签到 · 领邮件 · 领基地\n"
    "/璐茜玛 兑换 <兑换码>\n"
    "/璐茜玛 图鉴 <角色名>\n"
    "/璐茜玛 状态 · 取消 · 回执 · 解绑\n"
    "/璐茜玛 进阶 查看派遣、成就、佣兵团和装备功能"
)
MORE = (
    "/璐茜玛 领派遣 · 领成就 · 团签到 · 团周奖\n"
    "/璐茜玛 捐赠金币 · 捐赠证明 · 免费招募\n"
    "/璐茜玛 讨伐关卡 · 元素关卡\n"
    "/璐茜玛 筛装备 [套装名]，按副属性分数查看前几件"
)
MENUS = {
    "main": [[Btn("扫码登录", "/璐茜玛 登录"), Btn("任务状态", "/璐茜玛 状态"), Btn("查看回执", "/璐茜玛 回执")],
             [Btn("签到", "/璐茜玛 签到"), Btn("领邮件", "/璐茜玛 领邮件"), Btn("领基地", "/璐茜玛 领基地")],
             [Btn("进阶任务", "/璐茜玛 进阶")]],
    "more": [[Btn("领派遣", "/璐茜玛 领派遣"), Btn("领成就", "/璐茜玛 领成就"), Btn("团签到", "/璐茜玛 团签到")],
             [Btn("捐赠金币", "/璐茜玛 捐赠金币"), Btn("捐赠证明", "/璐茜玛 捐赠证明"), Btn("免费招募", "/璐茜玛 免费招募")],
             [Btn("讨伐关卡", "/璐茜玛 讨伐关卡"), Btn("元素关卡", "/璐茜玛 元素关卡"), Btn("筛装备", "/璐茜玛 筛装备")]],
}
WATCHERS: set[asyncio.Task] = set()


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
    if command == "进阶" and not arg:
        return {"message": MORE, "menu": "more"}
    if command in SHORTCUTS and not arg:
        action, arg = SHORTCUTS[command]
        valid = True
    elif action == "confirm":
        valid = bool(re.fullmatch(r"[0-9]{8}", arg))
    elif action == "redeem":
        valid = bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", arg))
    elif action == "encyclopedia":
        valid = 0 < len(arg) <= 64
    elif action in {"approve", "discard"}:
        valid = bool(re.fullmatch(r"[A-Za-z0-9_-]{16}", arg))
    elif action == "receipt":
        valid = not arg or bool(re.fullmatch(r"[A-Za-z0-9_-]{16}", arg))
    elif action == "equipment":
        valid = len(arg) <= 24
    else:
        valid = bool(action) and not arg
    if not valid:
        return {"message": HELP, "menu": "main"}
    return await request_lucima((platform, bot, user, event), action, arg)


def render_qr(url: str) -> bytes:
    import qrcode

    image = qrcode.make(url, box_size=8, border=4)
    with io.BytesIO() as buffer:
        image.save(buffer, format="PNG")
        return buffer.getvalue()


async def send_receipt(bot, event, receipt: dict, image_segment) -> None:
    detail = "\n".join([receipt["message"], *receipt["details"]])
    await bot.send(event, detail)
    try:
        data = await asyncio.to_thread(render_receipt, receipt)
        await bot.send(event, image_segment(data))
    except Exception:
        # The result above remains readable if a QQ client rejects media.
        pass


async def watch_receipt(bot, event, actor: tuple[str, str, str, str], job: str, image_segment) -> None:
    deadline = asyncio.get_running_loop().time() + 420
    try:
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(3)
            result = await call_service(f"/璐茜玛 回执 {job}", *actor)
            if receipt := result.get("receipt"):
                await send_receipt(bot, event, receipt, image_segment)
                return
            if not result.get("pending") and "暂不可用" not in result.get("message", ""):
                return
    except Exception:
        # A later private /回执 command can retrieve the same result.
        return


async def reply(bot, event, result: dict, image_segment, actor: tuple[str, str, str, str]) -> None:
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
        return
    if receipt := result.get("receipt"):
        await send_receipt(bot, event, receipt, image_segment)
        return
    message = result.get("message", "璐茜玛服务暂不可用。")
    rows = MENUS.get(result.get("menu"), [])
    if ticket := result.get("approval"):
        card = approval_card(message, ticket)
        message, rows = card.markdown, card.rows
    if job := result.get("job"):
        rows = [[Btn("任务状态", "/璐茜玛 状态"), Btn("查看回执", "/璐茜玛 回执")]]
    if rows and actor[0] == "qq_official":
        from adapters.qq_official import QQOfficialSender

        sender = QQOfficialSender(bot, event, False)
        await sender.send_keyboard(OutKeyboard(message, rows, label="lucima"))
        await sender.flush()
    else:
        await bot.send(event, message)
    if job and len(WATCHERS) < 12:
        watcher = asyncio.create_task(watch_receipt(bot, event, actor, job, image_segment))
        WATCHERS.add(watcher)
        watcher.add_done_callback(WATCHERS.discard)


@matcher.handle()
async def handle_onebot(bot: OneBot, event: PrivateMessageEvent | OneBotGroup):
    if isinstance(event, OneBotGroup):
        if event.is_tome() or any(seg.type == "at" and seg.data.get("qq") == str(bot.self_id) for seg in event.message):
            await bot.send(event, "请在私聊使用 /璐茜玛，账号和密码只填写在登录网页。")
        return
    if str(event.user_id) == str(bot.self_id):
        return
    actor = ("onebot", str(bot.self_id), str(event.user_id), str(event.message_id))
    result = await call_service(command_text(event), *actor)
    await reply(bot, event, result, OneBotSegment.image, actor)


@matcher.handle()
async def handle_official(bot: OfficialBot, event: C2CMessageCreateEvent | GroupMessageCreateEvent):
    if isinstance(event, GroupMessageCreateEvent):
        await bot.send(event, "请在私聊使用 /璐茜玛，账号和密码只填写在登录网页。")
        return
    user = event.author.user_openid
    if user and str(user) != str(bot.self_id) and not getattr(event.author, "bot", False):
        actor = ("qq_official", str(bot.self_id), str(user), str(event.id))
        result = await call_service(command_text(event), *actor)
        await reply(bot, event, result, OfficialSegment.file_image, actor)
