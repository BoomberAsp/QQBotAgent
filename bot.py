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
import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.rule import command


def init():
    # 初始化 NoneBot
    nonebot.init(command_start={"/", ""}, command_sep={" ",},
                 DEEPSEEK_API_KEY="sk-f4861354ed3b48c7b9d3ae4f9ed4507b",
DEEPSEEK_API_BASE="https://api.deepseek.com/v1")

    # 获取驱动器并注册适配器
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)

    # 加载插件
    nonebot.load_builtin_plugins("echo")  # 内置插件
    nonebot.load_plugins("QQBot/plugins")  # 本地插件

if __name__ == "__main__":
    init()
    nonebot.run(port=8081)