# import openai
# import os
# from nonebot import get_driver
# from datetime import timedelta
# from typing import Union
#
# import pandas as pd
# import numpy as np
# from nonebot.internal.matcher import Matcher
# from nonebot.plugin import on_message, on_command, on_notice
# from nonebot.adapters.onebot.v11 import (
#     GroupMessageEvent,
#     Message,
#     MessageSegment,
#     GroupIncreaseNoticeEvent,
#     MessageEvent
# )
# from nonebot.typing import T_State
# from nonebot.params import CommandArg
# from nonebot.rule import to_me
#
# # 从环境变量读取配置
# config = get_driver().config
#
#
# async def call_deepseek(prompt: str, model: str = "deepseek-chat") -> str:
#     """调用DeepSeek API"""
#
#     # 配置OpenAI客户端（兼容DeepSeek）
#     client = openai.AsyncOpenAI(
#         api_key=config.deepseek_api_key,
#         base_url=config.deepseek_api_base
#     )
#
#     try:
#         response = await client.chat.completions.create(
#             model=model,
#             messages=[{"role": "user", "content": prompt}],
#             stream=False,
#             max_tokens=2048  # 控制生成长度
#         )
#         return response.choices[0].message.content
#
#     except Exception as e:
#         return f"DeepSeek API调用失败：{str(e)}"
#
#
# chat = on_command("聊天")
#
# @chat.handle()
# async def handle_chat(event:MessageEvent):
#     pass