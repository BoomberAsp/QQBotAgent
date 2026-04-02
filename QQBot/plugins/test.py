from nonebot import get_driver, logger
from nonebot.plugin import on_command
from nonebot.adapters.onebot.v11 import Message

driver = get_driver()


@driver.on_startup
async def check_config():
    logger.info(f"命令前缀: {driver.config.command_start}")
    logger.info(f"超级用户: {driver.config.superusers}")