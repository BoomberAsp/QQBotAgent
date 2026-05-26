# DEPRECATED: This plugin has been superseded by agent_router.py.
# The "deepseek" / "ds" commands are now handled by the Agent's tool system.
# Keeping send_thinking_reminder for potential reuse.

import asyncio

# from nonebot import on_command, on_message
# from nonebot.rule import to_me
# from nonebot.adapters.onebot.v11 import MessageEvent, GroupMessageEvent, Message
# from nonebot.params import CommandArg
# from nonebot.typing import T_State
#
# from ..lib.deepseek_client import deepseek_client
#
#
# # 单独命令调用
# deepseek_cmd = on_command("deepseek", aliases={"ds", "思考","DeepSeek","DS"}, priority=5, rule=to_me())
#
#
# @deepseek_cmd.handle()
# async def handle_deepseek_command(event: MessageEvent, args: Message = CommandArg()):
#     ...


async def send_thinking_reminder(cmd_handle, interval=10):
    """定期发送思考中提示的后台任务"""
    try:
        while True:
            await asyncio.sleep(interval)
            await cmd_handle.send("仍在思考中，请稍候...")
    except asyncio.CancelledError:
        pass
