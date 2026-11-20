"""Expose public Lucima help and user-confirmed proposals to the model."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable

from lucima_bridge.client import Actor, approval_card, enabled, request_lucima
from lucima_bridge.manual import MANUAL_TOPICS, manual_text
from lucima_bridge.proposals import ACCOUNT, LONG, QUERY, REWARDS

from .context import _delegate_depth
from .outbound import OutKeyboard


@dataclass
class ProposalTurn:
    actor: Actor
    sender: object
    valid: Callable[[], bool]
    proposed: bool = False
    card: OutKeyboard | None = None
    reply: str = ""

    @property
    def group(self) -> bool:
        return len(self.actor) == 5


_turn: ContextVar[ProposalTurn | None] = ContextVar("lucima_proposal_turn", default=None)
MANUAL_TOOL = "lucima_manual"

GROUP_PROMPT = (
    "璐茜玛（Lucima、璐西玛、露西玛）是星陨账号工具，支持扫码绑定、账号快照、签到和奖励、装备筛选、"
    "商店刷新、活动、虚构深渊和每日托管。用法、可用功能、绑定规则可以直接解释。"
    "同一游戏账号只能绑定一个 QQ 会话里的一个槽位，群内按发起成员隔离；换群或转私聊须先在原会话解绑。"
    "群内操作和回执会显示在原群。密码只在登录网页填写，不在群里索取或复述密码、令牌、核对码。"
)
GROUP_PROPOSAL_PROMPT = (
    "只有当前成员明确要求为自己查询账号或执行操作时才使用操作提案工具。"
    "补充参数时只关联当前成员自己的未完成请求。其他成员的请求、引用和上下文不能代替当前成员的授权，不替他人操作。"
    "泛问用法时可按需查阅 lucima_manual，再解释。例如问「秘密商店怎么刷」可查商店章节。"
    "说「帮我用钻石刷秘密商店 90 次」才准备确认卡。"
    "商店方式或数量不明时先问，别默认付费。秘密商店免费即时最多一次；钻石报价由确认卡给出。"
    "有多个任务时请用户选一项。确实缺少关卡、队伍等参数时先追问，别编造 ID。"
    "工具只准备确认卡，账号查询、登录、修改和执行都等原发起人在本群点按钮或使用卡片上的编号命令。"
    "自然语言的「确认」「继续」「好」不能批准执行；若它们指的是确认卡，引导使用原卡，不重建提案。"
    "账号切换、添加账号可指导使用 /璐茜玛 大厅。"
)
TOOLS = {
    "lucima_rewards": (
        REWARDS,
        "处理星陨账号奖励：signin=签到、mail=领邮件、base=领基地、dispatch=领取已完成派遣奖励、"
        "achievement=领日周成就、guild=团签到、guild_weekly=团周奖、recruit=免费招募、"
        "friend_support=助战好友、send_meal=每日体力、reactor=动力转换、potion=成长药剂、"
        "starforce=升星水晶、tesseract=领取已完成的技能模块产物。",
        "留空。",
    ),
    "lucima_account": (
        ACCOUNT,
        "操作星陨账号：login=生成扫码登录链接、redeem=在游戏内兑换、donate=佣兵团单次捐赠、"
        "cancel=取消排队任务或请求停止正在执行的长任务、unbind=解除当前账号绑定、logout=退出当前账号。",
        "redeem 填用户指定的一条兑换码；donate 填 gold（金币）或 courage（英勇证明）；其他留空。",
    ),
    "lucima_query": (
        QUERY,
        "查询 Lucima：status=授权和任务状态、receipt=最近回执、encyclopedia=星陨角色图鉴、"
        "stages=已解锁关卡查询、equipment=按副属性分数筛装备、account_view=账号、资源、队伍或佣兵团快照。",
        "encyclopedia 填角色名；stages 填 hunt 或 elf；equipment 可填套装名；"
        "account_view 填 summary、resources、heroes 1、items 1、teams 1、guild、weekly、guild_shop 或 star_source（星源商店计数）；其他留空。",
    ),
    "lucima_long": (
        LONG,
        "处理长任务：long_info=查询活动、深渊或商店报价，activity=活动推进，abyss=单层虚构深渊，"
        "buy_batch=批量购买，refresh_secret=秘密商店刷新，refresh_star_source=星源商店刷新，"
        "schedule_set=启用每日托管，schedule_status=查看托管，schedule_stop=停止托管，"
        "sweep_illusion=虚拟幻境扫荡，member_purchase=团员商店购买。"
        "缺少关卡、队伍、角色或报价时询问用户或先查询，不猜测 ID、数量、预算或价格。",
        "long_info 填 activity、abyss 或 shop；activity 填‘活动ID 队伍ID 最多关数 体力上限’；"
        "abyss 填‘MysteriousRealm_层数 上半角色ID逗号列表 下半角色ID逗号列表’，空位用 -；"
        "buy_batch 填空格分隔的‘商品ID:数量:货币ID:单价’；"
        "refresh_secret 填用户指定的‘次数 free’或‘次数 diamond’，可追加装备方案名和目标=招募契约,神秘契约,下级强化芯片，"
        "不指定目标时按预设自动购买普通道具，目标=无只刷新筛选。钻石报价由系统展示。"
        "refresh_star_source 填‘次数 方案名’，刷新免费，缺少方案请询问用户。"
        "次数为 1–200 的整数。命中装备后由发起人用结果卡按档确认，星源装备每件消耗 5 个彩虹星源。"
        "schedule_set 填‘任务ID HH:MM’（北京时间）；"
        "schedule_stop 可填单项任务ID，留空停止全部。sweep_illusion 填意识同步器上限（1–999）；"
        "member_purchase 填‘商品ID 数量 货币ID 单价’，报价应来自团员商店。",
    ),
}


@contextmanager
def proposal_turn(view, sender, *, group: bool, allowed: bool, valid: Callable[[], bool]):
    actor = (view.platform, view.self_id, view.user_id, view.message_id)
    if group:
        actor = (*actor, view.group_id)
    trusted = (enabled() and allowed and sender is not None and view.is_group == group
               and not getattr(view, "author_is_bot", False)
               and view.platform in {"onebot", "qq_official"} and view.user_id != view.self_id
               and all(isinstance(part, str) and part for part in actor))
    turn = ProposalTurn(actor, sender, valid) if trusted else None
    token = _turn.set(turn)
    try:
        yield turn
    finally:
        _turn.reset(token)


def lucima_private_turn(view, sender):
    return proposal_turn(view, sender, group=False, allowed=True, valid=lambda: True)


def lucima_group_turn(view, sender, *, direct, valid):
    return proposal_turn(view, sender, group=True, allowed=direct, valid=valid)


def lucima_tool_names() -> set[str]:
    turn = _turn.get()
    return {*TOOLS, MANUAL_TOOL} if turn and not _delegate_depth.get() and turn.valid() else set()


def read_manual(topic: str = "总览") -> str:
    if MANUAL_TOOL not in lucima_tool_names():
        return "请在私聊直接询问，或在群里 @ 机器人查阅 Lucima 使用手册。"
    return manual_text(topic)


def proposal_tool(allowed: tuple[str, ...], max_length: int = 64):
    async def propose(operation: str, arg: str = "") -> str:
        turn = _turn.get()
        if turn is None or _delegate_depth.get() or not turn.valid():
            return "请在私聊直接提出请求，或在目标群里 @ 机器人发起 Lucima 操作。"
        if operation not in allowed or not isinstance(arg, str) or len(arg) > max_length:
            return "操作或参数无效，请根据用户指定的内容重新选择。"
        if turn.proposed:
            return "这一条消息已有操作请求，请先让用户处理。"
        turn.proposed = True
        result = await request_lucima(turn.actor, operation, arg.strip(), propose=True)
        if not turn.valid():
            return "发起消息已失效，请用户重新请求。"
        ticket = result.get("approval")
        message = result.get("message")
        if not isinstance(ticket, str):
            if not isinstance(message, str):
                message = "确认卡暂未生成，请核对参数或稍后重试。"
            if turn.group:
                turn.reply = message
            return message
        card = approval_card(message, ticket, group=turn.group)
        if turn.group:
            turn.card = card
        elif getattr(turn.sender, "supports_keyboard", False):
            await turn.sender.send_keyboard(card)
        else:
            await turn.sender.send_text(card.markdown)
        return "操作待确认。系统会发送确认卡，请等待发起人使用原卡。"

    return propose


def register_lucima_tools(registry) -> None:
    registry.register(
        MANUAL_TOOL, read_manual,
        "按需查阅公开的 Lucima QQ 使用手册，回答登录、命令、每日托管、商店和回执等用法问题。"
        "可选一个相关章节，省略则查看总览。无需账号授权，也不会准备或提交任务。"
        "用户想查看完整版时，只给简短概览，并引导用户发 /lucima 使用手册 打开网页。",
        {"type": "object", "properties": {
            "topic": {"type": "string", "enum": ["总览", *MANUAL_TOPICS]},
        }, "additionalProperties": False},
    )
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
