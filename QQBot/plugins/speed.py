import math
from datetime import timedelta
from typing import Union

from nonebot.internal.matcher import Matcher
# on_message, on_command, on_notice imports removed (handlers disabled)
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

# DEPRECATED: on_command handler disabled. Speed comparison is now routed
# through agent_router.py via the compare_speed_probability tool in tools/legacy_tools.py.
# Utility functions (parse_speed_data, compute_prob) remain.
#
# speed_compare = on_command("乱速",
#                            aliases={"luansu"},
#                            rule=to_me(),
#                            priority=5,
#                            state={"expire_time": timedelta(minutes=2)})

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


# @speed_compare.handle()
# async def handle_first_receive(...):
#     [DEPRECATED] Speed compare handler — superseded by agent compare_speed_probability tool.
#
# @speed_compare.handle()
# async def handle_secondary_input(...):
#     [DEPRECATED] Speed compare secondary input — superseded by agent compare_speed_probability tool.