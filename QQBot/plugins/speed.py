import math
from datetime import timedelta
from typing import Union

from nonebot.internal.matcher import Matcher
from nonebot.plugin import on_message, on_command, on_notice
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageSegment,
    GroupIncreaseNoticeEvent,
    MessageEvent
)
from nonebot.typing import T_State
from nonebot.params import CommandArg
from nonebot.rule import to_me

speed_compare = on_command("乱速",
                           aliases={"luansu"},
                           rule=to_me(),
                           priority=5,
                           state={"expire_time": timedelta(minutes=2)})

SPEED_FORMAT = ("请安下示格式输入要比较的两个速度：（整数）"
                "速度1 速度2\n"
                "比如：\n"
                "190 200\n"
                "(输入‘取消’取消)")


def parse_speed_data(input: str):
    inputs = input.split(" ")
    if inputs[0].isnumeric() and inputs[1].isnumeric():
        speed_1 = int(inputs[0])
        speed_2 = int(inputs[1])
        if speed_1 < speed_2: return speed_1, speed_2, None
        else: return speed_2, speed_1, None
    elif (not inputs[0].isnumeric()) and inputs[1].isnumeric():
        return None, None, inputs[0]
    elif (not inputs[0].isnumeric()) and (not inputs[1].isnumeric()):
        return None, None, inputs[0]+"、"+inputs[1]
    else:
        return None, None, inputs[1]


def compute_prob(smaller: int, bigger: int):
    rate = float(bigger)/float(smaller)
    if rate > (20/19):
        return "0"
    else:
        prob = (1/2) * (20 - 19 * rate) * (20 - 19 * rate) / rate
        return f"{prob * 100}%"


@speed_compare.handle()
async def handle_first_receive(
        event: MessageEvent,
        matcher: Matcher,
        state: T_State,
        args: Message = CommandArg()
):
    # 存储用户信息用于后续验证
    if isinstance(event, GroupMessageEvent):
        state["user_id"] = event.user_id
        state["group_id"] = event.group_id
    else:
        state["user_id"] = event.user_id

    user_input = args.extract_plain_text().strip()

    if user_input:
        # 尝试解析输入
        user_input = user_input.removeprefix("luansu").strip()
        user_input = user_input.removeprefix("乱速").strip()
        speed_smaller, speed_bigger, error = parse_speed_data(user_input)
        if error:
            await matcher.reject(f"无法解析【{error}】！\n{SPEED_FORMAT}")
        else:
            prob = compute_prob(speed_smaller, speed_bigger)
            matcher.finish(f"对于速度{speed_smaller}与{speed_bigger}，乱速概率：{prob}")
    else:
        # 无输入时发送帮助信息
        await matcher.send(SPEED_FORMAT)
        await matcher.pause("请按照上述格式输入数据：(输入‘取消’取消)")


@speed_compare.handle()
async def handle_secondary_input(
        event: Union[MessageEvent, GroupMessageEvent],
        matcher: Matcher,
        state: T_State
):
    # 验证用户身份
    if isinstance(event, GroupMessageEvent):
        if state["user_id"] != event.user_id or state["group_id"] != event.group_id:
            return
    else:
        if state["user_id"] != event.user_id:
            return

    # 获取用户输入
    user_input = event.get_plaintext().strip()

    # 处理取消命令
    if user_input in ["取消", "退出", "cancel", "exit", "0"]:
        await matcher.finish("已取消操作")

    if user_input:
        # 尝试解析输入
        user_input = user_input.removeprefix("luansu").strip()
        user_input = user_input.removeprefix("乱速").strip()
        speed_smaller, speed_bigger, error = parse_speed_data(user_input)
        if error:
            await matcher.reject(f"无法解析【{error}】！\n{SPEED_FORMAT}")
        else:
            prob = compute_prob(speed_smaller, speed_bigger)
            await matcher.finish(f"对于速度{speed_smaller}与{speed_bigger}，乱速概率：{prob}")
    else:
        # 无输入时发送帮助信息
        await matcher.send(SPEED_FORMAT)
        await matcher.pause("请按照上述格式输入数据：(输入‘取消’取消)")