# DEPRECATED: This plugin has been superseded by agent_router.py.
# The "explain" and "translate" commands are now agent tools (legacy_tools.py).

# from nonebot import on_command
# from nonebot.adapters.onebot.v11 import MessageEvent, Message
# from nonebot.params import CommandArg
#
# from ..lib.deepseek_client import deepseek_client
#
# # 代码解释
# explain_code = on_command("explain", aliases={"解释代码", "代码解释"}, priority=5)
#
#
# @explain_code.handle()
# async def handle_explain_code(event: MessageEvent, args: Message = CommandArg()):
#     ...
#
#
# # 翻译功能
# translate = on_command("translate", aliases={"翻译"}, priority=5)
#
#
# @translate.handle()
# async def handle_translate(event: MessageEvent, args: Message = CommandArg()):
#     ...
