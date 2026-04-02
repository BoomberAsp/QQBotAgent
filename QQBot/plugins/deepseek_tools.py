from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent, Message
from nonebot.params import CommandArg

from ..lib.deepseek_client import deepseek_client

# 代码解释
explain_code = on_command("explain", aliases={"解释代码", "代码解释"}, priority=5)


@explain_code.handle()
async def handle_explain_code(event: MessageEvent, args: Message = CommandArg()):
    code = args.extract_plain_text().strip()
    if not code:
        await explain_code.finish("请提供要解释的代码")

    prompt = f"请解释以下代码：\n```\n{code}\n```\n请用中文详细解释这段代码的功能和作用。"

    await explain_code.send("正在分析代码...")
    response = await deepseek_client.chat_completion(prompt)
    await explain_code.finish(response)


# 翻译功能
translate = on_command("translate", aliases={"翻译"}, priority=5)


@translate.handle()
async def handle_translate(event: MessageEvent, args: Message = CommandArg()):
    text = args.extract_plain_text().strip()
    if not text:
        await translate.finish("请提供要翻译的文本")

    prompt = f"请将以下内容翻译成中文：\n{text}"

    await translate.send("正在翻译...")
    response = await deepseek_client.chat_completion(prompt)
    await translate.finish(response)