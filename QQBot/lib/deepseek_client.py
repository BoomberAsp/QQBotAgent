import httpx
from nonebot import get_driver
import json


class DeepSeekClient:
    def __init__(self):
        self.api_key = get_driver().config.DEEPSEEK_API_KEY
        self.api_base = get_driver().config.DEEPSEEK_API_BASE

    async def chat_completion(self, message: str, history: list = None, timeout_set:float =180.0):
        """调用DeepSeek聊天补全API"""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        messages = []
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": message})

        data = {
            "model": "deepseek-chat",  # 或其他可用模型
            "messages": messages,
            "stream": False
        }

        async with httpx.AsyncClient() as client:

            try:
                response = await client.post(
                    f"{self.api_base}/chat/completions",
                    headers=headers,
                    json=data,
                    timeout=timeout_set
                )
                response.raise_for_status()
                result = response.json()
                return result["choices"][0]["message"]["content"]
            except Exception as e:
                if e == httpx.ConnectTimeout:
                    return f"思考超时，最大时长{timeout_set}秒。"
                else:
                    return f"调用DeepSeek API时出错: {str(e)}"


# 创建全局客户端实例
deepseek_client = DeepSeekClient()