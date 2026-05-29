# import nonebot
# from nonebot.adapters.console import Adapter as ConsoleAdapter  # 避免重复命名
# from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
#
# # 初始化 NoneBot
# nonebot.init()
#
# # 注册适配器
# driver = nonebot.get_driver()
# driver.register_adapter(ConsoleAdapter)
# driver.register_adapter(OneBotV11Adapter)
#
# # 在这里加载插件
# nonebot.load_builtin_plugins("echo")  # 内置插件
# # nonebot.load_plugin("thirdparty_plugin")  # 第三方插件
# nonebot.load_plugins("QQBot/plugins")  # 本地插件
#
#
# logger = nonebot.logger
# logger.info(f"已加载插件: {nonebot.get_loaded_plugins()}")
#
# if __name__ == "__main__":
#     nonebot.run()
import os
import sys

# ── Load .env into os.environ BEFORE any module imports ──────────
# NoneBot2 loads .env into its own pydantic config model but does NOT
# set os.environ. Many downstream modules (agent_router, permissions,
# etc.) read os.environ directly at import time. This call ensures
# all .env values are available through os.environ from the start.
from dotenv import load_dotenv as _load_dotenv
_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "QQBot", ".env")
_load_dotenv(_ENV_PATH)

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.rule import command


def init():
    # 初始化 NoneBot
    # DeepSeek API key from environment variable (set in QQBot/.env)
    nonebot.init(command_start={"/", ""}, command_sep={" ",},
                 DEEPSEEK_API_KEY=os.getenv("DEEPSEEK_API_KEY"),
                 DEEPSEEK_API_BASE=os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1"))

    # 获取驱动器并注册适配器
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)

    # 加载插件
    nonebot.load_builtin_plugins("echo")  # 内置插件
    nonebot.load_plugins("QQBot/plugins")  # 本地插件

if __name__ == "__main__":
    init()
    nonebot.run(port=8081)