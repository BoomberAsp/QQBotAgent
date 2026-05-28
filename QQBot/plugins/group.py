from datetime import timedelta
from typing import Union

import pandas as pd
import numpy as np
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
# from utils import group_command_rule

# DEPRECATED: on_command and on_message handlers disabled. All commands
# are now routed through agent_router.py via the agent's tool system.
# Utility functions (parse_speed_data, compute_speed_results) remain
# for use by tools/legacy_tools.py.
#
# hello = on_command("hello",priority=5, aliases={"介绍", "Hello", "你是谁"}, rule=to_me())
# group_info = on_command("群信息", aliases={"groupinfo", "/ginfo"}, priority=5, rule=to_me())
# speed_test = on_command(
#     "测速",
#     aliases={"测个速", "compute speed", "cesu"},
#     priority=5,
#     state={"expire_time": timedelta(minutes=5)},
#     rule=to_me()
# )

help_msg = (
            "我是基于NoneBot的机器人Roxy~"
            "🤖 机器人使用指南：\n"
            "1. 输入 测速 进行速度计算\n"
            "2. 输入 hello 打招呼\n"
            "3. 输入 单抽 进行一次招募\n"
            "4. 输入 十连抽 进行十次招募\n"
            "5. 输入 乱速 速度1 速度2 计算乱速概率\n"
            "6. 输入 ds 你的文本 与deepseek大模型对话\n"
            "7. 输入 聊天 你的文本 与deepseek大模型进行有历史记录的对话\n"
            "（开发中）输入 来点美图 进入pixiv搜索模式\n"
            "（开发中）输入 来点铯土 随机发送一张\n"
            "（开发中）输入 查询团战信息 查询团战信息\n"
        )

# group_msg = on_message(priority=100, block=False, rule=to_me())

# 测速格式说明（全局常量）
SPEED_TEST_FORMAT = (
    "请按照以下格式输入战斗数据：\n"
    "----------------------------\n"
    "我方\n"
    "角色名1 初始行动值 结束行动值 速度\n"
    "角色名2 初始行动值 结束行动值 速度\n"
    "（至少一名我方角色需提供速度）\n"
    "敌方\n"
    "敌方名1 初始行动值 结束行动值\n"
    "敌方名2 初始行动值 结束行动值\n"
    "----------------------------\n"
    "示例：\n"
    "我方\n"
    "兔子 0 100 220\n"
    "盖儿 3 56 0\n"
    "火飞 5 88 189\n"
    "敌方\n"
    "金人司阍 0 88\n"
    "丰饶灵兽 0 77\n"
    "⚠️ 注意：速度填0表示未知"
)


# 辅助函数 - 解析测速数据
def parse_speed_data(text: str):
    """
    解析用户输入的测速数据
    返回格式: (allies, enemies) 或错误信息
    """
    lines = [line.strip() for line in text.split('\n') if line.strip()]

    if len(lines) < 4:
        return None, "❌ 输入数据不完整，请检查格式"

    allies = []
    enemies = []
    current_section = None

    for line in lines:
        # 检测部分切换
        if line in ["我方", "我方角色", "我方成员"]:
            current_section = "ally"
            continue
        if line in ["敌方", "敌方角色", "敌人"]:
            current_section = "enemy"
            continue

        # 解析数据行
        parts = line.split()

        if current_section == "ally":
            if len(parts) < 4:
                return None, f"❌ 我方数据格式错误: {line}"

            try:
                ally_data = {
                    'name': parts[0],
                    'init': float(parts[1]),
                    'current': float(parts[2]),
                    'speed': int(parts[3]) if parts[3] != '0' else None
                }
                allies.append(ally_data)
            except ValueError:
                return None, f"❌ 数值格式错误: {line}"

        elif current_section == "enemy":
            if len(parts) < 3:
                return None, f"❌ 敌方数据格式错误: {line}"

            try:
                enemy_data = {
                    'name': parts[0],
                    'init': float(parts[1]),
                    'current': float(parts[2])
                }
                enemies.append(enemy_data)
            except ValueError:
                return None, f"❌ 数值格式错误: {line}"

    # 验证数据
    if not allies:
        return None, "❌ 未输入我方角色数据"
    if not enemies:
        return None, "❌ 未输入敌方角色数据"

    # 检查至少一个我方角色有速度
    if all(ally['speed'] is None for ally in allies):
        return None, "❌ 至少需要一名我方角色提供速度值"

    return (allies, enemies), None


# 辅助函数 - 计算测速结果
def compute_speed_results(allies, enemies):
    """执行测速计算逻辑"""
    results = []

    # 预处理我方数据
    valid_allies = [a for a in allies if a['current'] > a['init']]

    if not valid_allies:
        return None, "⚠️ 我方角色行动值未增加，请检查输入"

    # 对每个敌方角色计算速度
    for enemy in enemies:
        enemy_diff = enemy['current'] - enemy['init']
        if enemy_diff <= 0:
            continue  # 跳过行动值未增加的敌方

        speeds = []
        for ally in valid_allies:
            ally_diff = ally['current'] - ally['init']
            if ally_diff <= 0:
                continue

            if ally['speed']:  # 如果提供了速度
                speed = (enemy_diff / ally_diff) * ally['speed']
                speeds.append(speed)

        if speeds: # 至少提供了一个我方速度
            avg_speed = sum(speeds) / len(speeds)
            max_speed = max(speeds)
            results.append({
                '敌方名称': enemy['name'],
                '估算速度(平均)': round(avg_speed, 2),
                '最大速度': round(max_speed, 2),
                '参考角色数': len(speeds)
            })
            if abs(avg_speed - max_speed) > 5:
                return results, None, True

    return results, None, False


# 辅助函数 - 计算并发送结果
async def compute_and_send_results(matcher: Matcher, state: T_State):
    allies = state["allies"]
    enemies = state["enemies"]
    results, error, warning = compute_speed_results(allies, enemies)
    if error:
        await matcher.finish(error)

    if not results:
        await matcher.finish("⚠️ 未计算出有效结果，请检查敌方角色行动值是否增加")

    # 格式化结果
    result_msg = "测速结果：\n"
    for res in results:
        result_msg += (
            f"敌方: {res['敌方名称']}\n"
            f"  ▪ 平均速度: {res['估算速度(平均)']}\n"
            f"  ▪ 最大速度: {res['最大速度']}\n"
            f"  ▪ 参考角色: {res['参考角色数']}名\n"
        )
    # 添加原始数据确认
    result_msg += "\n我方角色数据确认：\n"
    for ally in allies:
        speed_display = ally['speed'] if ally['speed'] is not None else "未知"
        result_msg += f"{ally['name']}: 初始={ally['init']}% → 结束={ally['current']}% | 速度={speed_display}\n"

    # 添加警告信息
    if warning:
        result_msg += "某一敌方测算的平均速度与最大速度相差过大，请检查数据是否输入错误！"

    await matcher.finish(result_msg)


# =============== 命令处理函数 (DEPRECATED) ===============
#
# @hello.handle()
# async def handle_hello(event: MessageEvent): ...
#
# @group_info.handle()
# async def handle_group_info(event: GroupMessageEvent): ...
#
# @speed_test.handle()
# async def handle_first_receive(...): ...
#
# @speed_test.handle()
# async def handle_secondary_input(...): ...




# # 状态超时处理
# @speed_test.state_expire()
# async def state_expired(state: T_State):
#     """状态超时回调"""
#     user_id = state.get("user_id")
#     group_id = state.get("group_id")
#
#     msg = "⏰ 测速操作已超时，请重新开始"
#     if group_id:
#         # 在群聊中@用户
#         await speed_test.finish(MessageSegment.at(user_id) + " " + msg)
#     else:
#         # 私聊直接发送
#         await speed_test.finish(msg)


# =============== 消息处理函数 (DEPRECATED) ==============
#
# @group_msg.handle()
# async def handle_group_msg(event: GroupMessageEvent):
#     [DEPRECATED] Group message handler — superseded by agent_router.py.