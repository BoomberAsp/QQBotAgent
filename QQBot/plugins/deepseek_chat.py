import asyncio

from nonebot import on_command, on_message
from nonebot.rule import to_me
from nonebot.adapters.onebot.v11 import MessageEvent, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.typing import T_State

from ..lib.deepseek_client import deepseek_client



# 单独命令调用
deepseek_cmd = on_command("deepseek", aliases={"ds", "思考","DeepSeek","DS"}, priority=5, rule=to_me())


async def send_thinking_reminder(cmd_handle, interval=10):
    """定期发送思考中提示的后台任务"""
    try:
        while True:
            await asyncio.sleep(interval)
            await cmd_handle.send("仍在思考中，请稍候...")
    except asyncio.CancelledError:
        # 任务被取消，正常退出
        pass


@deepseek_cmd.handle()
async def handle_deepseek_command(event: MessageEvent, args: Message = CommandArg()):
    user_message = args.extract_plain_text().strip()
    if not user_message:
        await deepseek_cmd.finish("请提供要询问的内容")

    await deepseek_cmd.send("正在思考中...")
    # 创建后台任务定期发送提示
    reminder_task = asyncio.create_task(send_thinking_reminder(deepseek_cmd, interval=21))


    try:

        response = await deepseek_client.chat_completion(user_message)
        reminder_task.cancel()

        try:
            await reminder_task
        except asyncio.CancelledError:
            pass

        if len(response) > 500:
            chunks = [response[i:i + 500] for i in range(0, len(response), 500)]
            for chunk in chunks:
                await deepseek_cmd.send(chunk)
        else:
            await deepseek_cmd.send(response)
    except Exception as e:
        reminder_task.cancel()
        try:
            await reminder_task
        except asyncio.CancelledError:
            pass
        await deepseek_cmd.send(f"处理请求时出现错误：{str(e)}")