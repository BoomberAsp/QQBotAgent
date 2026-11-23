"""Submission instructions shared by the model, commands and QQ buttons."""
import re

from lib.button_spec import Btn
from .group_reply import GroupReply

SUBMIT_COMMAND = "/收表情"
GUIDE_COMMAND = "/投稿帮助"
SUBMIT_TEMPLATE = f"{SUBMIT_COMMAND} 描述："
SUBMIT_EXAMPLE = f"{SUBMIT_COMMAND} 安慰 描述：拍拍肩膀"

_INSTRUCTIONS = (
    f"在要使用表情包的群里 @ 机器人，发 {SUBMIT_COMMAND}。同一条消息附一张有权分享的图片，或引用你自己发的图。",
    f"用途和描述都可选。示例 {SUBMIT_EXAMPLE}。描述最多 300 字。",
    "审核通过后供当前人格在本群使用。用 /我的表情 查进度，或用 /撤回表情 加编号撤回。",
)

GROUP_PROMPT = (
    "用户问怎么投稿或希望把自己的表情包给机器人复用时，按下面的实际用法说明，仅在群已开启图片功能时可投稿。"
    "手动投稿用命令。自动收录只适用于群和成员均已开启的 OneBot 原生表情包，普通发图照常处理。"
    "只有收到系统投稿回执才能确认手动提交，审核通过前不能使用。"
    + " ".join(_INSTRUCTIONS)
)
PRIVATE_PROMPT = (
    "用户问怎么给机器人投稿表情包时，引导到要使用表情包的群。私聊发图不会创建投稿。"
    + " ".join(_INSTRUCTIONS)
)

_STICKER = re.compile(r"表情包|表情图|表情动图|斗图素材|贴图素材|表情库|投稿表情")
_ACTION = re.compile(
    r"投(?:稿|递|(?:个|张)?表情包)|上[传交]|提交|入库|收(?:录|下|取|张|这|我的)|存进(?:你|机器人)|接收|"
    r"(?:发|传|交|送)给(?:你|机器人)|给(?:你|机器人)(?:以后|下次)?用"
)
_ASK = re.compile(r"怎么|咋|如何|在哪|哪里|要怎样|能|可以|想|我要|请|帮忙|[？?吗嘛么]")
_EXISTING = re.compile(r"审核进度|投稿状态|投稿编号|撤回|下架|通过(?:审核)?了|审核了|还没审核|(?:上传|投稿|提交)了|收到吗|有没有收到")


def is_submission_question(text: str) -> bool:
    text = text.strip()
    if not text or text[0] in "/#／＃" or len(text) > 240 or _EXISTING.search(text):
        return False
    return bool(_STICKER.search(text) and _ACTION.search(text)
                and (_ASK.search(text) or text in {"表情包投稿", "上传表情包", "投稿表情包"}))


def submission_guide(*, group: bool, image_enabled: bool = True, with_button: bool = True) -> GroupReply:
    if group and not image_enabled:
        return GroupReply("表情包投稿", ["本群的图片功能已关闭，请群管理员开启后再投稿。"])
    lines = list(_INSTRUCTIONS)
    if group:
        lines.insert(0, "若看到「填写投稿」，可点它预填命令，再补描述、附图并发送。")
        button = Btn("填写投稿", SUBMIT_TEMPLATE, enter=False, tip="请在群里 @机器人并发送 /收表情")
    else:
        lines.insert(0, "表情包投稿在群聊办理。")
        button = Btn("投稿示例", GUIDE_COMMAND, tip="请发送 /投稿帮助")
    return GroupReply("表情包投稿", lines, [[button]] if with_button else [])
