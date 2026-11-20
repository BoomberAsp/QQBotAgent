"""Expose public Lucima help and user-confirmed proposals to the model."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable

from lucima_bridge.client import Actor, LOGIN_CONFIRM_COMMAND, approval_card, enabled, request_lucima
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
    "群内操作和回执会显示在原群。账号密码只在登录网页填写，门户令牌由系统处理。"
    "群内发起登录后，网页会生成一次性核对命令。请明确告诉发起人回到原群 @ 机器人，"
    f"发送网页上的完整命令 `{LOGIN_CONFIRM_COMMAND}`。这一步应在原群完成。"
    "说明格式时只展示占位符，并请用户照抄网页命令。"
)
GROUP_PROPOSAL_PROMPT = (
    "只有当前成员明确要求为自己查询账号或执行操作时才使用操作提案工具。"
    "补充参数时只关联当前成员自己的未完成请求。其他成员的请求、引用和上下文不能代替当前成员的授权，不替他人操作。"
    "泛问用法时可按需查阅 lucima_manual，再解释。例如问「秘密商店怎么刷」可查商店章节。"
    "说「帮我用钻石刷秘密商店 90 次」才准备确认卡。"
    "商店方式或数量不明时先问，别默认付费。秘密商店免费即时最多一次；钻石报价由确认卡给出。"
    "有多个任务时请用户选一项。确实缺少关卡、队伍等参数时先追问，别编造 ID。"
    "工具只准备操作确认卡。原发起人通过原卡按钮或 `/璐茜玛 执行 <卡片编号>` 确认提案。"
    "登录提案获批会打开扫码入口，扫码后的网页核对仍使用网页提供的 8 位核对命令。"
    "若用户只说「确认」「继续」「好」且是在问操作确认卡，引导使用原卡。"
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
        "处理长任务：long_info=查询活动、深渊、活动扫荡或商店报价，activity=活动推进，abyss=单层虚构深渊，"
        "buy_batch=批量购买，refresh_secret=秘密商店刷新，refresh_star_source=星源商店刷新，"
        "schedule_set=启用每日托管，schedule_status=查看托管，schedule_stop=停止托管，"
        "sweep_illusion=虚拟幻境扫荡，member_purchase=团员商店购买，"
        "sweep_hunt=讨伐扫荡，sweep_elf=元素探索扫荡，sweep_activity=活动关卡扫荡（均为付体力正常扫荡）。"
        "缺少关卡、队伍、角色或报价时询问用户或先查询，不猜测 ID、数量、预算或价格。",
        "long_info 填 activity、abyss、activity_sweep 或 shop；activity 填‘活动ID 队伍ID 最多关数 体力上限’；"
        "sweep_hunt/sweep_elf/sweep_activity 填‘关卡ID 队伍ID 次数 体力上限’，次数 1–99。"
        "关卡必须已通关且体力标价已收录：讨伐如 HuntFire_10，元素如 ElfIce_4，活动如 BH188_1_13。"
        "先用 stages（hunt/elf）或 long_info activity_sweep 查可扫荡关卡，用 account_view teams 1 查队伍 ID。"
        "活动扫荡每轮校验活动仍在当期；实际扣费与标价不符时立即停止。"
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
            "若用户只回复‘确认’且指的是操作提案，引导使用原卡。"
            f"网页登录使用网页提供的完整命令 {LOGIN_CONFIRM_COMMAND}，在发起登录的 QQ 会话发送。"
            "群里发起时由本人在原群 @ 机器人发送。说明格式时只展示占位符。",
            {"type": "object", "properties": {
                "operation": {"type": "string", "enum": list(operations)},
                "arg": {"type": "string", "description": parameters},
            }, "required": ["operation"], "additionalProperties": False},
        )
