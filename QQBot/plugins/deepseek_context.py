from nonebot import on_command, on_message
from nonebot.rule import to_me
from nonebot.adapters.onebot.v11 import MessageEvent, GROUP
from nonebot.params import CommandArg
from collections import defaultdict
from ..plugins import deepseek_chat
import asyncio

from ..lib.deepseek_client import deepseek_client

# 思考时长限制
Maximum_timeout = 180.0

# 提醒间隔
interval = 21

# 存储对话上下文
conversation_context = defaultdict(list)
MAX_CONTEXT_LENGTH = 10  # 最大上下文长度

# 清除上下文的命令
clear_ctx = on_command("clear", aliases={"清除上下文", "新对话"}, priority=5, rule=to_me())


@clear_ctx.handle()
async def handle_clear(event: MessageEvent):
    user_id = event.get_user_id()
    conversation_context[user_id].clear()
    await clear_ctx.finish("已清除对话上下文，开始新对话")


# 带上下文的聊天
context_chat = on_command("chat",aliases={"聊天"},rule=to_me(), priority=10)


@context_chat.handle()
async def handle_context_chat(event: MessageEvent):
    user_id = event.get_user_id()
    user_message = event.get_plaintext().strip()

    if not user_message:
        return

    # 获取当前用户的对话历史
    history = conversation_context[user_id]

    await context_chat.send("正在思考中...")

    reminder = asyncio.create_task(deepseek_chat.send_thinking_reminder(cmd_handle=context_chat, interval=interval))

    try:
        response = await deepseek_client.chat_completion(user_message, history)
        reminder.cancel()
        try:
            await reminder
        except asyncio.CancelledError:
            pass


        # 更新上下文
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": response})

        # 限制上下文长度
        if len(history) > MAX_CONTEXT_LENGTH * 2:
            history = history[-(MAX_CONTEXT_LENGTH * 2):]
            conversation_context[user_id] = history

        # 发送回复
        if len(response) > 500:
            chunks = [response[i:i + 500] for i in range(0, len(response), 500)]
            for chunk in chunks:
                await context_chat.send(chunk)
                await asyncio.sleep(0.5)  # 避免消息发送过快
        else:
            await context_chat.send(response)
    except Exception as e:
        reminder.cancel()
        try:
            await reminder
        except asyncio.CancelledError:
            pass
        await context_chat.send(f"处理请求时出现错误：{str(e)}")