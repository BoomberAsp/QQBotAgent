# DEPRECATED: This plugin has been superseded by agent_router.py.
# All commands (clear, chat) are now handled by the Agent's tool system.
# Keeping this file for reference but all handlers are disabled.

# from nonebot import on_command, on_message
# from nonebot.rule import to_me
# from nonebot.adapters.onebot.v11 import MessageEvent, GROUP
# from nonebot.params import CommandArg
# from collections import defaultdict
# from ..plugins import deepseek_chat
# import asyncio
#
# from ..lib.deepseek_client import deepseek_client
#
# # 思考时长限制
# Maximum_timeout = 180.0
#
# # 提醒间隔
# interval = 21
#
# # 存储对话上下文
# conversation_context = defaultdict(list)
# MAX_CONTEXT_LENGTH = 10  # 最大上下文长度
#
# # 清除上下文的命令
# clear_ctx = on_command("clear", aliases={"清除上下文", "新对话"}, priority=5, rule=to_me())
#
#
# @clear_ctx.handle()
# async def handle_clear(event: MessageEvent):
#     user_id = event.get_user_id()
#     conversation_context[user_id].clear()
#     await clear_ctx.finish("已清除对话上下文，开始新对话")
#
#
# # 带上下文的聊天
# context_chat = on_command("chat",aliases={"聊天"},rule=to_me(), priority=10)
#
#
# @context_chat.handle()
# async def handle_context_chat(event: MessageEvent):
#     ...
