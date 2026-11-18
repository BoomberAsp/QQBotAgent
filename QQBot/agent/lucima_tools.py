"""Expose Lucima proposals to the model without exposing execution or confirmation."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from lucima_bridge.client import approval_card, enabled, request_lucima


@dataclass
class PrivateTurn:
    actor: tuple[str, str, str, str]
    sender: object
    proposed: bool = False


_private_turn: ContextVar[PrivateTurn | None] = ContextVar("lucima_private_turn", default=None)

TOOLS = {
    "lucima_rewards": (
        ("signin", "mail", "base", "dispatch", "achievement", "guild", "guild_weekly", "recruit"),
        "处理星陨账号奖励：signin=签到、mail=领邮件、base=领基地、dispatch=领取已完成派遣奖励、"
        "achievement=领日周成就、guild=团签到、guild_weekly=团周奖、recruit=免费招募。",
        "留空。",
    ),
    "lucima_account": (
        ("login", "redeem", "donate", "cancel", "unbind"),
        "操作星陨账号：login=生成扫码登录链接、redeem=在游戏内兑换、donate=佣兵团单次捐赠、"
        "cancel=取消排队任务或请求停止正在执行的长任务、unbind=解除当前账号绑定。",
        "redeem 填用户指定的一条兑换码；donate 填 gold（金币）或 courage（英勇证明）；其他留空。",
    ),
    "lucima_query": (
        ("status", "receipt", "encyclopedia", "stages", "equipment"),
        "查询 Lucima：status=授权和任务状态、receipt=最近回执、encyclopedia=星陨角色图鉴、"
        "stages=已解锁关卡查询、equipment=按副属性分数筛装备。",
        "encyclopedia 填角色名；stages 填 hunt（讨伐）或 elf（元素探索）；equipment 可填套装名；其他留空。",
    ),
    "lucima_long": (
        ("long_info", "activity", "abyss", "buy_batch", "schedule_set", "schedule_status", "schedule_stop"),
        "处理长任务：long_info=查询活动、深渊或商店报价，activity=活动推进，abyss=单层虚构深渊，"
        "buy_batch=批量购买，schedule_set=启用每日托管，schedule_status=查看托管，schedule_stop=停止托管。"
        "缺少关卡、队伍、角色或报价时询问用户或先查询，不猜测 ID、数量、预算或价格。",
        "long_info 填 activity、abyss 或 shop；activity 填‘活动ID 队伍ID 最多关数 体力上限’；"
        "abyss 填‘MysteriousRealm_层数 上半角色ID逗号列表 下半角色ID逗号列表’，空位用 -；"
        "buy_batch 填空格分隔的‘商品ID:数量:货币ID:单价’；schedule_set 填‘signin|mail|base HH:MM’"
        "（选择一项，北京时间）；其余留空。",
    ),
}


@contextmanager
def lucima_private_turn(view, sender):
    actor = (view.platform, view.self_id, view.user_id, view.message_id)
    trusted = (enabled() and sender is not None and not view.is_group and not getattr(view, "author_is_bot", False)
               and view.platform in {"onebot", "qq_official"} and view.user_id != view.self_id
               and all(isinstance(part, str) and part for part in actor))
    token = _private_turn.set(PrivateTurn(actor, sender) if trusted else None)
    try:
        yield
    finally:
        _private_turn.reset(token)


def lucima_tool_names() -> set[str]:
    return set(TOOLS) if _private_turn.get() is not None else set()


def proposal_tool(allowed: tuple[str, ...], max_length: int = 64):
    async def propose(operation: str, arg: str = "") -> str:
        turn = _private_turn.get()
        if turn is None:
            return "请在和机器人私聊时发起 Lucima 操作。"
        if operation not in allowed or not isinstance(arg, str) or len(arg) > max_length:
            return "操作或参数无效，请根据用户指定的内容重新选择。"
        if turn.proposed:
            return "这一条私聊消息已有待确认操作，请先让用户处理确认。"
        turn.proposed = True
        result = await request_lucima(turn.actor, operation, arg.strip(), propose=True)
        ticket = result.get("approval")
        if not isinstance(ticket, str):
            return "确认卡暂未生成，请核对参数或请用户稍后重试。"
        card = approval_card(result["message"], ticket)
        if getattr(turn.sender, "supports_keyboard", False):
            await turn.sender.send_keyboard(card)
        else:
            await turn.sender.send_text(card.markdown)
        return "已在当前私聊提交待确认操作。请等待用户处理确认卡；操作仍处于待确认状态。"

    return propose


def register_lucima_tools(registry) -> None:
    for name, (operations, context, parameters) in TOOLS.items():
        registry.register(
            name, proposal_tool(operations, 255 if name == "lucima_long" else 64),
            context + "每次只准备一个用户明确要求的操作。系统发确认卡，等用户点击或输入确认。"
            "用户只回复‘确认’时引导使用原卡，不重复提案。账户、密码、网页核对码和确认令牌均由系统界面处理。",
            {"type": "object", "properties": {
                "operation": {"type": "string", "enum": list(operations)},
                "arg": {"type": "string", "description": parameters},
            }, "required": ["operation"], "additionalProperties": False},
        )
