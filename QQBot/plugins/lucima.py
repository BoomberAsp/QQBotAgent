"""Lucima commands terminate before the agent turn and its memory pipeline."""
from __future__ import annotations

import asyncio
import base64
import io
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from nonebot import on_message
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import (
    Bot as OneBot, GroupMessageEvent as OneBotGroup, MessageSegment as OneBotSegment,
    PrivateMessageEvent,
)
from nonebot.adapters.qq import Bot as OfficialBot, MessageSegment as OfficialSegment
from nonebot.adapters.qq.event import C2CMessageCreateEvent, GroupAtMessageCreateEvent, GroupMessageCreateEvent

from agent.group_commands import without_mention_prefix
from agent.outbound import OutKeyboard
from lib.button_spec import Btn
from lucima_bridge.client import Actor, approval_card, enabled, request_lucima
from lucima_bridge.long_tasks import (
    ACTIVITY_SWEEP_QUERY, BATTLE_IDS, ILLUSION_SWEEP_ORDER, LONG_QUEUE_TTL, SCHEDULE_TASKS, STAGE_QUERY,
    SWEEP_IDS, SWEEP_ORDER, SWEEP_TASKS, TASK_TIMEOUT,
)
from lucima_bridge.manual import is_manual_command, manual_qq_reply
from lucima_bridge.menu import HOME_LINKS, NAV_NAMES, PAGES
from lucima_bridge.receipt import render_panel, render_receipt
from lucima_bridge.service import LOGIN_NOTICE_LIMIT, LOGIN_TIMEOUT, TICKET_TTL


load_dotenv(Path(__file__).resolve().parents[1] / ".env")
PREFIX = re.compile(r"^/(?:璐茜玛|璐西玛|露西玛|lucima)(?:\s+|$)", re.IGNORECASE)
COMMANDS = {
    "登录": "login", "大厅": "lobby", "确认": "confirm", "状态": "status", "取消": "cancel", "解绑": "unbind",
    "签到": "signin", "领邮件": "mail", "领基地": "base", "兑换": "redeem", "图鉴": "encyclopedia",
    "领派遣": "dispatch", "领成就": "achievement", "团签到": "guild", "团周奖": "guild_weekly", "免费招募": "recruit",
    "筛装备": "equipment", "执行": "approve", "放弃": "discard", "回执": "receipt",
    "活动推进": "activity", "虚构深渊": "abyss", "深渊挑战": "abyss", "批量购买": "buy_batch", "长任务查询": "long_info",
    "托管": "schedule_set", "托管状态": "schedule_status", "停止托管": "schedule_stop",
    "刷新秘密商店": "refresh_secret", "秘密商店刷新": "refresh_secret",
    "刷新星源商店": "refresh_star_source", "星源商店刷新": "refresh_star_source",
    "菜单": "navigate", "添加账号": "add_account", "账号别名": "rename_account", "主题": "theme", "界面": "view_card",
    "分页": "view_page",
    "任务历史": "history", "清空日志": "clear_logs", "刷新信息": "account_view", "查看资源": "account_view",
    "展开账号信息": "expand_profile",
    "确认命中": "confirm_shop_hit", "购买命中": "buy_shop_hit",
    "查看团员": "account_view", "查看物品": "account_view", "查看队伍": "account_view",
    "查看团概况": "account_view", "查看团周任务": "account_view",
    "助战好友": "friend_support", "每日体力": "send_meal", "动力转换": "reactor",
    "成长药剂": "potion", "升星水晶": "starforce",
    "查看团店": "account_view", "后台计时": "timer",
    "技能模块": "tesseract", "查看幻境": "account_view", "幻境扫荡": "sweep_illusion", "购买团店": "member_purchase",
    "讨伐扫荡": "sweep_hunt", "元素扫荡": "sweep_elf", "元素探索扫荡": "sweep_elf", "活动扫荡": "sweep_activity",
    "查看竞技场": "account_view", "查看投票": "account_view", "查看装备活动": "account_view",
    "查看援助": "account_view", "查看团长店": "account_view", "查看星源商店": "account_view",
    "保存装备方案": "filter_save", "复制装备方案": "filter_copy", "导出装备方案": "filter_export",
    "预览装备方案": "filter_preview", "删除装备方案": "filter_drop",
}
VIEW_NAMES = {"刷新信息": "profile", "查看资源": "resources", "查看团员": "heroes", "查看物品": "items",
              "查看队伍": "teams", "查看团概况": "guild", "查看团周任务": "weekly", "查看团店": "guild_shop",
              "查看幻境": "abyss_sweep", "幻境扫荡": "abyss_sweep", "查看竞技场": "arena", "查看投票": "vote", "查看装备活动": "equip_event",
              "查看援助": "guild_aid", "查看团长店": "guild_captain", "查看星源商店": "star_source"}
SCHEDULE_ALIASES = {label: task for task, label in SCHEDULE_TASKS.items()}
SHORTCUTS = {"捐赠金币": ("donate", "gold"), "捐赠证明": ("donate", "courage"),
             "讨伐关卡": ("stages", "hunt"), "元素关卡": ("stages", "elf")}
HELP = (
    "璐茜玛命令\n"
    "别名 /璐西玛、/露西玛、/lucima\n"
    "/lucima 使用手册 · 获取完整版网页入口\n"
    "/璐茜玛 登录 · 扫码绑定 · /璐茜玛 大厅\n"
    "/璐茜玛 签到 · 领邮件 · 领基地\n"
    "/璐茜玛 兑换 <兑换码>\n"
    "/璐茜玛 图鉴 <角色名>\n"
    "/璐茜玛 状态 · 取消 · 回执 · 解绑\n"
    "/璐茜玛 进阶 查看派遣、成就、佣兵团和装备功能\n"
    "/璐茜玛 长任务 查看活动、深渊、批购与每日托管"
)
MORE = (
    "/璐茜玛 领派遣 · 领成就 · 团签到 · 团周奖\n"
    "/璐茜玛 捐赠金币 · 捐赠证明 · 免费招募\n"
    "/璐茜玛 讨伐关卡 · 元素关卡\n"
    "/璐茜玛 筛装备 [套装名]，按副属性分数查看前几件"
)
BATTLE_INPUTS = {
    "activity": ("活动推进", "<队伍ID> <最多关数> <体力上限>", "查队伍", "查看队伍 1"),
    "abyss": ("虚构深渊", "<上半角色列表> <下半角色列表>", "查团员", "查看团员 1"),
}
SWEEP_COMMANDS = {"hunt": "讨伐扫荡", "elf": "元素扫荡", "activity": "活动扫荡"}
SWEEP_ELEMENTS = {"Fire": "火", "Earth": "木", "Ice": "水", "Dark": "暗", "Light": "光"}
SWEEP_FIELDS = "<队伍ID> <次数> <体力上限>"


def sweep_label(kind: str, value: str) -> str:
    if kind == "activity":
        _, chapter, stage = value.split("_")
        return f"{chapter}-{stage}"
    element, floor = value.removeprefix("Hunt").removeprefix("Elf").split("_")
    return f"{SWEEP_ELEMENTS[element]}{floor}"
LONG_HELP = (
    "选择战斗：/璐茜玛 活动推进 或 /璐茜玛 虚构深渊\n"
    "付体力扫荡：/璐茜玛 讨伐扫荡 · 元素扫荡 · 活动扫荡（先选关）\n"
    "/璐茜玛 长任务查询 活动扫荡 或 商店\n"
    "/璐茜玛 刷新秘密商店 · 刷新星源商店\n"
    "/璐茜玛 活动推进 <活动ID> <队伍ID> <最多关数> <体力上限>\n"
    "/璐茜玛 讨伐扫荡 <关卡ID> <队伍ID> <次数> <体力上限>，元素/活动扫荡同格式\n"
    "/璐茜玛 虚构深渊 <关卡ID> <上半角色ID列表> <下半角色ID列表>，队内用逗号分隔，空位填 -\n"
    "/璐茜玛 批量购买 <商品ID:数量:货币ID:单价>，多档用空格隔开\n"
    "/璐茜玛 托管 <签到|领邮件|领基地> <北京时间HH:MM>\n"
    "/璐茜玛 状态 · 取消 · 回执 [编号]"
)
GROUP_HELP = "请先 @ 机器人再发命令。群内发起的任务结果会显示在本群，账号密码只在登录网页填写。"
MENUS = {
    "main": [[Btn("扫码登录", "/璐茜玛 登录"), Btn("任务状态", "/璐茜玛 状态"), Btn("查看回执", "/璐茜玛 回执")],
             [Btn("签到", "/璐茜玛 签到"), Btn("领邮件", "/璐茜玛 领邮件"), Btn("领基地", "/璐茜玛 领基地")],
             [Btn("打开大厅", "/璐茜玛 大厅"), Btn("进阶任务", "/璐茜玛 进阶")]],
    "login": [[Btn("扫码登录", "/璐茜玛 登录")]],
    "lobby": [[Btn(label, "/璐茜玛 " + command) for label, command in HOME_LINKS[index:index + 3]]
              for index in range(0, len(HOME_LINKS), 3)],
    "more": [[Btn("领派遣", "/璐茜玛 领派遣"), Btn("领成就", "/璐茜玛 领成就"), Btn("团签到", "/璐茜玛 团签到")],
             [Btn("捐赠金币", "/璐茜玛 捐赠金币"), Btn("捐赠证明", "/璐茜玛 捐赠证明"), Btn("免费招募", "/璐茜玛 免费招募")],
             [Btn("讨伐关卡", "/璐茜玛 讨伐关卡"), Btn("元素关卡", "/璐茜玛 元素关卡"), Btn("筛装备", "/璐茜玛 筛装备")]],
    "long": [[Btn("查活动", "/璐茜玛 长任务查询 活动"), Btn("查深渊", "/璐茜玛 长任务查询 深渊"),
              Btn("查商店", "/璐茜玛 长任务查询 商店")],
             [Btn("讨伐扫荡", "/璐茜玛 讨伐扫荡"), Btn("元素扫荡", "/璐茜玛 元素扫荡"),
              Btn("活动扫荡", "/璐茜玛 活动扫荡")],
             [Btn("托管状态", "/璐茜玛 托管状态"), Btn("停止托管", "/璐茜玛 停止托管")],
             [Btn("任务状态", "/璐茜玛 状态"), Btn("取消任务", "/璐茜玛 取消"), Btn("查看回执", "/璐茜玛 回执")]],
}
WATCHERS: set[asyncio.Task] = set()
LOGIN_WATCHERS: dict[str, asyncio.Task] = {}


def command_text(event: Event) -> str:
    if isinstance(event, (OneBotGroup, GroupMessageCreateEvent)):
        return without_mention_prefix(event.get_plaintext()).strip()
    if isinstance(event, (PrivateMessageEvent, C2CMessageCreateEvent)):
        return event.get_plaintext().strip()
    return ""


async def is_command(event: Event) -> bool:
    text = command_text(event)
    if is_manual_command(text):
        # Groups use the shared command registry, including the @ check.
        return not isinstance(event, (OneBotGroup, GroupMessageCreateEvent))
    return enabled() and bool(PREFIX.match(text))


matcher = on_message(rule=is_command, priority=0, block=True)


async def call_service(text: str, platform: str, bot: str, user: str, event: str, group: str | None = None) -> dict:
    if is_manual_command(text):
        return {"message": manual_qq_reply()}
    command, *args = PREFIX.sub("", text, count=1).split(maxsplit=1) or [""]
    action = COMMANDS.get(command)
    arg = args[0] if args else ""
    if command == "进阶" and not arg:
        return {"message": MORE, "menu": "more"}
    if command == "长任务" and not arg:
        return {"message": LONG_HELP, "menu": "long"}
    if action == "sweep_illusion" and not arg:
        action = "account_view"
    if action in BATTLE_INPUTS:
        if not arg:
            action, arg = "long_info", action
        elif BATTLE_IDS[action].fullmatch(arg):
            label, fields, lookup, lookup_command = BATTLE_INPUTS[action]
            return {"message": f"请补齐参数后发送。\n/lucima {label} {arg} {fields}",
                    "buttons": [[lookup, lookup_command]]}
    if action in SWEEP_TASKS:
        kind = SWEEP_TASKS[action]
        if not arg:
            action, arg = ("long_info", "activity_sweep") if kind == "activity" else ("stages", kind)
        elif SWEEP_IDS[kind].fullmatch(arg):
            return {"message": f"请补齐参数后发送。\n/lucima {command} {arg} {SWEEP_FIELDS}",
                    "buttons": [["查队伍", "查看队伍 1"]]}
    if not command:
        action, valid = "lobby", True
    elif command == "切换账号" and arg:
        action, valid = "switch_account", bool(re.fullmatch(r"[1-5]", arg))
    elif command in {"退出当前账号", "退出账号"} and arg == "确认":
        action, arg, valid = "logout", "", True
    elif command in NAV_NAMES and not arg and command != "大厅":
        action, arg, valid = "navigate", NAV_NAMES[command], True
    elif action == "navigate":
        valid = arg in PAGES
    elif action == "view_page":
        valid = 0 < len(arg) <= 45
    elif action == "account_view":
        view = VIEW_NAMES[command]
        valid = (view in {"heroes", "items", "teams"} and (not arg or len(arg) <= 36)) or not arg
        arg = f"{view} {arg or '1'}" if view in {"heroes", "items", "teams"} else view
    elif action in {"refresh_secret", "refresh_star_source"}:
        valid = len(arg) <= 96
    elif action in {"theme", "rename_account"}:
        valid = len(arg) <= 24
    elif action == "history":
        arg = arg or "1"
        valid = bool(re.fullmatch(r"[1-9][0-9]{0,2}", arg))
    elif action and action.startswith("filter_"):
        valid = 0 < len(arg) <= 2200
    elif command in SHORTCUTS:
        action, kind = SHORTCUTS[command]
        if action == "stages":
            arg = f"{kind} {arg}" if arg else kind
            valid = STAGE_QUERY.fullmatch(arg) is not None
        else:
            valid = not arg
            arg = kind
    elif action == "confirm":
        valid = bool(re.fullmatch(r"[0-9]{8}", arg))
    elif action == "redeem":
        valid = bool(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", arg))
    elif action == "encyclopedia":
        valid = 0 < len(arg) <= 64
    elif action in {"approve", "discard", "confirm_shop_hit", "expand_profile"}:
        valid = bool(re.fullmatch(r"[A-Za-z0-9_-]{16}", arg))
    elif action == "buy_shop_hit":
        valid = bool(re.fullmatch(r"[A-Za-z0-9_-]{16} [1-6]", arg))
    elif action == "receipt":
        valid = not arg or bool(re.fullmatch(r"[A-Za-z0-9_-]{16}", arg))
    elif action == "equipment":
        valid = len(arg) <= 24
    elif action == "sweep_illusion":
        arg = " ".join(arg.split())
        valid = ILLUSION_SWEEP_ORDER.fullmatch(arg) is not None
    elif action in {"activity", "abyss", "buy_batch", "member_purchase"}:
        valid = 0 < len(arg) <= 255
    elif action in SWEEP_TASKS:
        match = SWEEP_ORDER.fullmatch(arg)
        valid = match is not None and SWEEP_IDS[SWEEP_TASKS[action]].fullmatch(match.group(1)) is not None
    elif action == "stages":
        valid = STAGE_QUERY.fullmatch(arg) is not None
    elif action == "long_info":
        parts = arg.split(maxsplit=1)
        if parts:
            key = {"活动": "activity", "深渊": "abyss", "活动扫荡": "activity_sweep", "商店": "shop"}.get(parts[0], parts[0])
            arg = " ".join([key, *parts[1:]])
        valid = arg in {"activity", "abyss", "shop"} or ACTIVITY_SWEEP_QUERY.fullmatch(arg) is not None
    elif action == "schedule_set":
        task, when = (arg.split(maxsplit=1) + ["", ""])[:2]
        task = SCHEDULE_ALIASES.get(task, task)
        arg = f"{task} {when.strip()}"
        valid = 0 < len(arg) <= 24
    elif action == "schedule_stop":
        arg = SCHEDULE_ALIASES.get(arg, arg)
        valid = not arg or arg in SCHEDULE_TASKS
    else:
        valid = bool(action) and not arg
    if not valid:
        return {"message": f"{HELP}\n\n{GROUP_HELP}" if group else HELP, "menu": "main"}
    actor = (platform, bot, user, event)
    return await request_lucima((*actor, group) if group else actor, action, arg)


def render_qr(url: str) -> bytes:
    import qrcode

    image = qrcode.make(url, box_size=8, border=4)
    with io.BytesIO() as buffer:
        image.save(buffer, format="PNG")
        return buffer.getvalue()


def result_buttons(result: dict) -> list:
    buttons = result.get("buttons")
    if not isinstance(buttons, list):
        return MENUS.get(result.get("menu"), [])
    eligible = [(item[0], item[1]) for item in buttons[:15]
                if isinstance(item, list) and len(item) == 2 and all(isinstance(part, str) for part in item)]
    return [[Btn(label, "/璐茜玛 " + command) for label, command in eligible[index:index + 3]]
            for index in range(0, len(eligible), 3)]


async def send_receipt(bot, event, receipt: dict, image_segment, actor: Actor) -> None:
    detail = "\n".join([receipt["message"], *receipt["details"]])
    if receipt.get("kind") == "profile":
        delivered = False
        if encoded := receipt.get("profile_png"):
            try:
                await bot.send(event, image_segment(base64.b64decode(encoded, validate=True)))
                delivered = True
            except Exception:
                pass
        if receipt.get("expand_profile"):
            await send_menu(bot, event, actor, "账号信息已更新。" if delivered else detail,
                            [[Btn("展开详情", f"/璐茜玛 展开账号信息 {receipt['id']}")]], include_commands=True)
        elif not delivered:
            await bot.send(event, detail)
        return
    if receipt.get("status") == "shop_hit" and receipt.get("shop_confirm_until", 0) > time.time():
        job = receipt["id"]
        choices = receipt.get("shop_choices", [])
        controls = [Btn(f"买第{slot}档", f"/璐茜玛 购买命中 {job} {slot}") for slot, _ in choices]
        controls.append(Btn("结束本轮", f"/璐茜玛 确认命中 {job}", style=0))
        prices = "\n".join(f"第 {slot} 档 · {quote}" for slot, quote in choices)
        hint = "请在 5 分钟内由发起人选择购买或结束。"
        if len(actor) == 5:
            hint += "使用文字命令时请 @ 机器人。"
        body = "\n".join(filter(None, (detail, prices, hint)))
        rows = [controls[index:index + 3] for index in range(0, len(controls), 3)]
        await send_menu(bot, event, actor, body, rows, include_commands=True)
    elif quote := receipt.get("illusion_quote"):
        controls = [Btn("准备扫荡", f"/璐茜玛 幻境扫荡 {quote['count']} {quote['floor']}"),
                    Btn("刷新状态", "/璐茜玛 幻境扫荡")]
        await send_menu(bot, event, actor, detail, [controls], include_commands=True)
    elif choices := receipt.get("sweep_choices"):
        kind, ids = choices
        command = SWEEP_COMMANDS[kind]
        controls = [Btn(sweep_label(kind, value), f"/璐茜玛 {command} {value} ", enter=False) for value in ids]
        controls.append(Btn("查队伍", "/璐茜玛 查看队伍 1"))
        rows = [controls[index:index + 3] for index in range(0, len(controls), 3)]
        guide = f"选择已通关关卡后补齐参数再发送。\n/lucima {command} <关卡ID> {SWEEP_FIELDS}"
        await send_menu(bot, event, actor, detail + "\n" + guide, rows, include_commands=True)
    elif choices := receipt.get("battle_choices"):
        kind, ids = choices
        command, fields, lookup, lookup_command = BATTLE_INPUTS[kind]
        controls = [Btn(value if kind == "activity" else f"第 {value.rsplit('_', 1)[-1]} 层",
                        f"/璐茜玛 {command} {value} ", enter=False) for value in ids]
        controls.append(Btn(lookup, f"/璐茜玛 {lookup_command}"))
        rows = [controls[index:index + 3] for index in range(0, len(controls), 3)]
        guide = f"选择后补齐参数再发送。\n/lucima {command} <{'活动ID' if kind == 'activity' else '关卡ID'}> {fields}"
        if kind == "abyss":
            guide += "\n每半场 1–4 个角色 ID，用逗号分隔，空位填 -。"
        await send_menu(bot, event, actor, detail + "\n" + guide, rows, include_commands=True)
    elif view := receipt.get("view"):
        position, kind, page, pages, query = view
        query = " " + query if query else ""
        # Long search text must fit the keyboard's command size.
        controls = [Btn(label, f"/璐茜玛 分页 {position} {kind} {number}{query}") for label, number in
                    (("上一页", page - 1), ("下一页", page + 1)) if 1 <= number <= pages]
        controls.append(Btn("基本信息", "/璐茜玛 基本信息"))
        await send_menu(bot, event, actor, detail, [controls], include_commands=True)
    else:
        await bot.send(event, detail)
    await send_receipt_image(bot, event, receipt, image_segment)


async def send_receipt_image(bot, event, receipt: dict, image_segment) -> None:
    try:
        data = await asyncio.to_thread(render_receipt, receipt)
        await bot.send(event, image_segment(data))
    except Exception:
        # The result above remains readable if a QQ client rejects media.
        pass


async def watch_receipt(bot, event, actor: Actor, job: str, image_segment, *, long_job=False) -> None:
    horizon = LONG_QUEUE_TTL + TASK_TIMEOUT + 30 if long_job else 420
    deadline = asyncio.get_running_loop().time() + horizon
    interval = 3
    try:
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(interval)
            if long_job:
                interval = 10
            result = await call_service(f"/璐茜玛 回执 {job}", *actor)
            if receipt := result.get("receipt"):
                await send_receipt(bot, event, receipt, image_segment, actor)
                return True
            if not result.get("pending") and "暂不可用" not in result.get("message", ""):
                return
    except Exception:
        # A later /回执 in the same conversation can retrieve the result.
        return


async def send_menu(bot, event, actor: Actor, message: str, rows: list, *, include_commands=False) -> None:
    def fallback():
        return message + "\n\n" + "\n".join(btn.data for row in rows for btn in row)

    if rows and actor[0] == "qq_official":
        from adapters.qq_official import QQOfficialSender

        sender = QQOfficialSender(bot, event, is_group=len(actor) == 5)
        if include_commands and not sender.supports_keyboard:
            await sender.send_text(fallback())
        else:
            await sender.send_keyboard(OutKeyboard(message, rows, label="lucima"))
        await sender.flush()
    else:
        await bot.send(event, fallback() if rows and include_commands else message)


async def watch_login(bot, event, actor: Actor, notice: str, image_segment) -> None:
    deadline = asyncio.get_running_loop().time() + TICKET_TTL + LOGIN_TIMEOUT + 15
    try:
        while asyncio.get_running_loop().time() < deadline:
            result = await request_lucima(actor, "login_notice", notice)
            if result.get("menu") == "lobby":
                receipt = result.get("receipt")
                message = result["message"]
                if receipt:
                    message += f"\n登录回执 /璐茜玛 回执 {receipt['id']}"
                job = result.get("profile_job")
                if not job:
                    message += "\n暂未取得游戏快照，可用 /lucima 刷新信息。"
                try:
                    await send_menu(bot, event, actor, message, result_buttons(result), include_commands=True)
                finally:
                    if isinstance(job, str) and not await watch_receipt(bot, event, actor, job, image_segment):
                        await bot.send(event, "账号快照暂未送达，可用 /lucima 刷新信息 重试。")
                return
            if result.get("pending"):
                continue
            if "暂不可用" not in result.get("message", ""):
                return
            await asyncio.sleep(2)
    except Exception:
        # /大厅 reopens the authorized menu when the original reply cannot be delivered.
        return


async def reply(bot, event, result: dict, image_segment, actor: Actor) -> None:
    url = result.get("login_url")
    if isinstance(url, str) and url.startswith("https://"):
        qr_sent = False
        try:
            await bot.send(event, image_segment(render_qr(url)))
            qr_sent = True
        except Exception:
            pass
        instructions = "请在 5 分钟内扫码，并回到本私聊核对。登录后在这里查看大厅。"
        if len(actor) == 5:
            instructions = ("请发起人在 5 分钟内扫码，回到本群 @ 机器人发送自己网页显示的核对命令。"
                            "账号密码只在网页填写，登录结果会显示在本群。")
        delivered = qr_sent
        try:
            await bot.send(event, instructions + "也可打开：\n" + url)
            delivered = True
        except Exception:
            await bot.send(event, instructions if qr_sent else "登录二维码和链接均未能送达，请稍后再试。")
        finally:
            notice = result.get("login_notice")
            if (delivered and isinstance(notice, str) and notice not in LOGIN_WATCHERS
                    and len(LOGIN_WATCHERS) < LOGIN_NOTICE_LIMIT):
                watcher = asyncio.create_task(watch_login(bot, event, actor, notice, image_segment))
                LOGIN_WATCHERS[notice] = watcher
                watcher.add_done_callback(lambda _: LOGIN_WATCHERS.pop(notice, None))
        return
    if receipt := result.get("receipt"):
        await send_receipt(bot, event, receipt, image_segment, actor)
        return
    message = result.get("message", "璐茜玛服务暂不可用。")
    rows = result_buttons(result)
    if ticket := result.get("approval"):
        card = approval_card(message, ticket, group=len(actor) == 5)
        message, rows = card.markdown, card.rows
    if job := result.get("job"):
        rows = [[Btn("任务状态", "/璐茜玛 状态"), Btn("查看回执", f"/璐茜玛 回执 {job}")]]
    await send_menu(bot, event, actor, message, rows, include_commands="buttons" in result)
    if result.get("picture"):
        try:
            data = await asyncio.to_thread(render_panel, result)
            await bot.send(event, image_segment(data))
        except Exception:
            pass
    if job and len(WATCHERS) < 12:
        watcher = asyncio.create_task(watch_receipt(bot, event, actor, job, image_segment,
                                                   long_job=result.get("long_job") is True))
        WATCHERS.add(watcher)
        watcher.add_done_callback(WATCHERS.discard)


@matcher.handle()
async def handle_onebot(bot: OneBot, event: PrivateMessageEvent | OneBotGroup):
    if str(event.user_id) == str(bot.self_id):
        return
    actor = ("onebot", str(bot.self_id), str(event.user_id), str(event.message_id))
    if isinstance(event, OneBotGroup):
        at_bot = any(seg.type == "at" and seg.data.get("qq") == str(bot.self_id) for seg in event.message)
        if not event.is_tome() and not at_bot:
            return
        actor = (*actor, str(event.group_id))
    result = await call_service(command_text(event), *actor)
    await reply(bot, event, result, OneBotSegment.image, actor)


@matcher.handle()
async def handle_official(bot: OfficialBot, event: C2CMessageCreateEvent | GroupMessageCreateEvent):
    user = event.get_user_id()
    if user and str(user) != str(bot.self_id) and not getattr(event.author, "bot", False):
        actor = ("qq_official", str(bot.self_id), str(user), str(event.id))
        if isinstance(event, GroupMessageCreateEvent):
            if not event.group_openid or not (event.is_tome() or isinstance(event, GroupAtMessageCreateEvent)):
                return
            actor = (*actor, event.group_openid)
        result = await call_service(command_text(event), *actor)
        await reply(bot, event, result, OfficialSegment.file_image, actor)
