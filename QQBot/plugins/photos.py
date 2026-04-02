import os
import httpx
from nonebot.internal.matcher import Matcher
from nonebot.plugin import on_message, on_command, on_notice
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageSegment,
    GroupIncreaseNoticeEvent,
    MessageEvent
)
from nonebot.typing import T_State
from nonebot.params import CommandArg
from PIL import Image
from io import BytesIO

# 使用项目相对路径
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_PATH = os.path.join(CURRENT_DIR, "..", "images")
BASE_PATH = os.path.abspath(BASE_PATH)

# 确保目录存在
os.makedirs(BASE_PATH, exist_ok=True)


# 修正图片路径函数
def fix_image_path(filename):
    file_path = os.path.join(BASE_PATH, filename)
    # 检查文件是否存在
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"图片文件不存在: {file_path}")
    return MessageSegment.image(f"file://{file_path}")


async def download_image(url: str) -> bytes:
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            raise ValueError("图片下载失败")
        return resp.content


def save_image_as(img_data: bytes, filename: str, format: str = "PNG"):
    """
    将字节数据保存为指定格式的图片文件

    参数:
    - img_data: 图片字节数据
    - filename: 保存路径（包含文件名）
    - format: 图片格式 (PNG, JPEG, WEBP等)
    """
    # 确保目录存在
    os.makedirs(os.path.dirname(filename), exist_ok=True)

    # 从字节数据创建图像
    img = Image.open(BytesIO(img_data))

    # 保存为指定格式
    img.save(filename, format=format)
    return filename


# def store_image(url: Union[str, ]):
#     ...