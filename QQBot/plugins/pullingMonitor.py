import time
from datetime import timedelta
from typing import Union
import random

import pandas as pd
import numpy as np
from nb_cli.cli.commands import self
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
from nonebot.rule import to_me
import os

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


# 修正后的图片消息段
pull_1 = fix_image_path("pull_1.png")
blue_end = fix_image_path("blue_end.png")
blue_middle = fix_image_path("blue_middle.png")
blue_or_purple_spaceship_close = fix_image_path("blue_or_purple_spaceship_close.png")
blue_spaceship_open = fix_image_path("blue_spaceship_open.png")
gold_spaceship_close = fix_image_path("gold_spaceship_close.png")
gold_spaceship_open = fix_image_path("gold_spaceship_open.png")
golden_end = fix_image_path("golden_end.png")
purple_middle = fix_image_path("purple_middle.png")
purple_end = fix_image_path("purple_end.png")
purple_spaceship_open = fix_image_path("purple_spaceship_open.png")
red_end = fix_image_path("red_end.png")
red_spaceship_close = fix_image_path("red_spaceship_close.png")
red_spaceship_open = fix_image_path("red_spaceship_open.png")
single_pull_start = fix_image_path("single_pull_start.png")
ten_pull_start = fix_image_path("ten_pull_start.png")


# 三色五星角色
THREE_COLOR_FIVE_STAR = {"厄德莱雅": "反差的甜美", "洁莉摩尔": "完美的渴求", "巴尔托丝": "突破的尝试",
                         "亚毕丝": "强化的渴望", "妮诺凯西": "坦率的恋慕", "哈蒂": "小丑的邀约",
                         "萝莎莱": "无尽的采样", "蓓蕾卡": "羞涩的实证", "玳莲": "醉与醒之间",
                         "卢妮艾塔": "部长的假面","瑟妮卡": "新奇的感受", "戴琳娜": "团长的交易",
                         "夏妮": "幸福的时刻", "菲莉斯": "真正的主角",
                         "诺可琳": "偶像的心声", "奈兰希尔": "足尖的诱惑", "璐茜娅": "残酷的真相",
                         "艾瑞儿": "驰骋的快感", "赫赛迪雅": "束缚与控制", "菈法瑞": "深吻的后庭",
                         "吉妮兹": "难忘的惩罚", "爱莉卡": "冷静的反面", "泰里莎": "生猛的刺激",
                         "盖妲": "爱人的画像", "莎莉丝特": "贪吃的后果", "多恩": "高效的治疗",
                         "埃帕蒂": "拘束与放荡", "普利蔓": "热情的力量", "奥德雅": "忘利的商人",
                         "赫萝薇克": "暗巷的交锋"}

# 限定三色五星
SPECIAL_THREE_COLOR_FIVE_STAR = {"夏日佩忒拉": "热情的沙浴", "夏日乔伊斯": "坦率训练课", "乌尔德": "窒息的征服",
                                 "璀璨誓约的露比": "纯白的真心", "兔女郎爱莉卡": "私密的报答", "东云": "灵感的来源",
                                 "新春的蜜娜": "激情的禁果", "圣诞的蜜拉贝儿": "寂寞的救赎", "朱音": "征服与臣服",
                                 "杏仁ミル": "失守的防线", "彩伽": "羞涩的请求", "Projekt Melody": "赛博攻防战",
                                 "hongkongdoll": "专有的服务", "碧海的诺可琳": "迫切的激情", "崔西里亚": "女王的消遣",
                                 "安泰西亚": "姐妹的默契", "Aoi Hinamori": "疗愈的夜袭", "赤鬼伯伯": "魅魔的逆袭",
                                 "柯丝玛尔": "圣诞节礼物", "新春的莎莉丝特": "美味的封肉", "泳池魅影吉妮兹": "泳池畔私语",
                                 "归还者璐茜娅":"办公室秘辛", "南瓜鬼怪莱拉":"狂放的禁果", "继任者弥卡伊勒":"秘密的战略",
                                 "猫祭":"隐藏的炽热", "啦啦队蓓蕾卡": "热烈的声援"}

# 光暗五星角色
SPECIAL_FIVE_STAR = ["蝶子", "璀璨誓约的伊娥丝", "奥柏丝蒂恩", "纳克莎", "盖儿", "光明女神玳莲", "绝望的夏妮", "奥丽芙",
                     "欧贝恩丝", "守护者艾瑞儿", "菲莉西娅", "莱莎曼德", "克莱儿", "柏妮丝", "复制体吉妮兹", "乔瑟琳",
                     "异变的奥普怀妮", "异变的埃帕蒂", "璐茜玛", "天宫咲夜","彼岸花茱莉亚", "贝儿"]

# 三色四星角色
THREE_COLOR_FOUR_STAR = ["卡罗琳", "吉赛尔", "安熙恩", "芭索萝", "翠丝特", "朵琳", "格萝妮娅", "哈鲁缇", "卡丽希",
                         "寇希尔", "路易丝", "洛蒂", "迪尔德丽", "克拉菈", "莉娜", "伊娥丝", "乔依丝", "艾米",
                         "玛蒂尔达", "蜜丽恩", "茱莉亚", "奥普怀妮"]

# 三色三星角色
THREE_COLOR_THREE_STAR = ["露比", "安特亚", "欧戴摩亚", "拉娜", "珍", "贝琳", "布洛瑟姆", "葛瑞丝", "史嘉蕾", "茵罗洛", "瓦妮莎", "派狄亚", "欧尔嘉",
                      "希蓓尔", "弥卡伊勒", "尤菲米亚", "邦妮", "翠西", "西尔维纳", "梅芙", "辛狄", "耶莉卡",
                          "塔亚", "缇娜", "奎妮", "帕梅勒", "萨菲尔", "神乐"]

# 光暗四星角色
SPECIAL_FOUR_STAR = ["艾乐莱思", "复制体芭索萝", "疯狂的格萝妮娅", "复仇的蔻希尔", "复制体迪尔德丽", "复制体吉赛尔",
                     "复制体卡丽希", "复制体卡罗琳", "复制体克拉菈", "复制体玛蒂尔达", "复制体乔依丝", "复仇的路易丝"]

# 光暗三星角色
SPECIAL_THREE_STAR = ["潘倪克思", "皮提特", "佩忒菈", "希诺莉", "潘黛希亚", "费南雪", "贾妮丝", "凯蒂达", "华勒弗", "穆丽儿",
                      "伊比", "塔琳", "莱拉", "涅瓦", "维尔莉特"]

# 三星羁绊
THREE_STAR_BONDS = ["醉酒的借口", "迷离的双眼", "网红的流量", "纾压的管道", "私密的癖好", "神恩的狂热", "病娇的威胁",
                    "独处的心愿", "欢愉的影像", "暧昧的触手", "感官的记忆", "情动的浪潮", "恶毒的媚药", "恩爱的抚痕",
                    "小小的虐待", "密林的激战", "堕落的奇迹", "圣女的秘密", "公主的渴望", "主人与宠物", "意外的娇羞"]

# 四星羁绊
FOUR_STAR_BONDS = ["诱人的猫咪", "善意的委托", "不只是取样", "战前的欢愉", "初次的悸动", "爱怜的疼惜", "必要的强化",
                   "压抑的愉悦", "非人的体验", "毒舌的傲娇", "狂暴与欲望", "强势的反扑", "无人的店面", "催促的回眸",
                   "狂野的呼唤", "潜藏的爱意", "市集的角落", "爱意的共鸣"]

# 概率配置 (百分比)
PROBABILITIES = {
    # 常规招募
    "常规招募": {
        "三色五星角色": 1.25,
        "三色四星角色": 4.5,
        "三色三星角色": 41.0,
        "光暗五星角色": 0.15,
        "光暗四星角色": 0.5,
        "光暗三星角色": 4.35,
        "五星羁绊": 1.75,
        "四星羁绊": 6.5,
        "三星羁绊": 40.0
    },
    # 限定/非限定UP池
    "几率up招募": {
        "当前up角色": 1.0,
        "三色四星角色": 4.5,
        "三色三星角色": 41.25,
        "当前up五星羁绊": 1.05,
        "非当前up五星羁绊": 0.7,
        "四星羁绊": 6.5,
        "三星羁绊": 45.0
    },
    # 神秘招募
    "神秘招募": {
        "当前up光暗五星角色": 0.625,
        "光暗四星角色": 0.9,
        "三色五星角色": 0.625,
        "三色四星角色": 3.6,
        "三色三星角色": 41.0,
        "五星羁绊": 1.75,
        "四星羁绊": 6.5,
        "三星羁绊": 45.0
    },
    # 银河招募
    "银河招募": {
        "光暗五星角色": 2.5,
        "光暗四星角色": 27.5,
        "光暗三星角色": 70.0
    }
}
# from utils import group_command_rule
ten_draws = on_command("十连抽", priority=5, rule=to_me())
draw = on_command("单抽", priority=5, rule=to_me())


def drawing_cards(pool_type: str, upper_character: str = None):
    """
    抽卡核心函数
    :param pool_type: 卡池类型 ('常规招募', '几率up招募', '神秘招募', '银河招募')
    :param upper_character: UP角色名称
    :return: 抽卡结果 (角色/羁绊名称, 星级, 类型)
    """
    # 获取当前卡池概率配置
    prob_config = PROBABILITIES[pool_type]

    # 构建概率分布
    categories = []
    weights = []

    if pool_type == "常规招募":
        # 三色五星角色
        if THREE_COLOR_FIVE_STAR:
            weight = prob_config["三色五星角色"] / len(THREE_COLOR_FIVE_STAR)
            for char in THREE_COLOR_FIVE_STAR:
                categories.append((char, 5, "角色"))
                weights.append(weight)

        # 三色四星角色
        if THREE_COLOR_FOUR_STAR:
            weight = prob_config["三色四星角色"] / len(THREE_COLOR_FOUR_STAR)
            for char in THREE_COLOR_FOUR_STAR:
                categories.append((char, 4, "角色"))
                weights.append(weight)

        # 三色三星角色
        if THREE_COLOR_THREE_STAR:
            weight = prob_config["三色三星角色"] / len(THREE_COLOR_THREE_STAR)
            for char in THREE_COLOR_THREE_STAR:
                categories.append((char, 3, "角色"))
                weights.append(weight)

        # 光暗五星角色
        if SPECIAL_FIVE_STAR:
            weight = prob_config["光暗五星角色"] / len(SPECIAL_FIVE_STAR)
            for char in SPECIAL_FIVE_STAR:
                categories.append((char, 5, "角色"))
                weights.append(weight)

        # 光暗四星角色
        if SPECIAL_FOUR_STAR:
            weight = prob_config["光暗四星角色"] / len(SPECIAL_FOUR_STAR)
            for char in SPECIAL_FOUR_STAR:
                categories.append((char, 4, "角色"))
                weights.append(weight)

        # 光暗三星角色
        if SPECIAL_THREE_STAR:
            weight = prob_config["光暗三星角色"] / len(SPECIAL_THREE_STAR)
            for char in SPECIAL_THREE_STAR:
                categories.append((char, 3, "角色"))
                weights.append(weight)

        # 五星羁绊 (使用所有五星角色的羁绊)
        all_five_star_bonds = list(THREE_COLOR_FIVE_STAR.values()) + list(SPECIAL_THREE_COLOR_FIVE_STAR.values())
        if all_five_star_bonds:
            weight = prob_config["五星羁绊"] / len(all_five_star_bonds)
            for bond in all_five_star_bonds:
                categories.append((bond, 5, "羁绊"))
                weights.append(weight)

        # 四星羁绊
        if FOUR_STAR_BONDS:
            weight = prob_config["四星羁绊"] / len(FOUR_STAR_BONDS)
            for bond in FOUR_STAR_BONDS:
                categories.append((bond, 4, "羁绊"))
                weights.append(weight)

        # 三星羁绊
        if THREE_STAR_BONDS:
            weight = prob_config["三星羁绊"] / len(THREE_STAR_BONDS)
            for bond in THREE_STAR_BONDS:
                categories.append((bond, 3, "羁绊"))
                weights.append(weight)

    elif pool_type == "几率up招募" and upper_character:
        # 当前UP角色
        is_common_5 = upper_character in THREE_COLOR_FIVE_STAR
        is_special_5 = upper_character in SPECIAL_THREE_COLOR_FIVE_STAR
        up_char_weight = prob_config["当前up角色"]
        categories.append((upper_character, 5, "角色"))
        weights.append(up_char_weight)

        # 当前UP角色的羁绊
        if is_common_5:
            up_bond = THREE_COLOR_FIVE_STAR[upper_character]
        elif is_special_5:
            up_bond = SPECIAL_THREE_COLOR_FIVE_STAR[upper_character]
        else:
            up_bond = None

        if up_bond:
            categories.append((up_bond, 5, "羁绊"))
            weights.append(prob_config["当前up五星羁绊"])

        # 非当前UP五星羁绊
        non_up_bonds = []
        if is_special_5:
            for char, bond in THREE_COLOR_FIVE_STAR.items():
                if char != upper_character:
                    non_up_bonds.append(bond)
            for char, bond in SPECIAL_THREE_COLOR_FIVE_STAR.items():
                if char != upper_character:
                    non_up_bonds.append(bond)
        elif is_common_5:
            for char, bond in THREE_COLOR_FIVE_STAR.items():
                if char != upper_character:
                    non_up_bonds.append(bond)

        if non_up_bonds:
            weight = prob_config["非当前up五星羁绊"] / len(non_up_bonds)
            for bond in non_up_bonds:
                categories.append((bond, 5, "羁绊"))
                weights.append(weight)

        # 三色四星角色
        if THREE_COLOR_FOUR_STAR:
            weight = prob_config["三色四星角色"] / len(THREE_COLOR_FOUR_STAR)
            for char in THREE_COLOR_FOUR_STAR:
                categories.append((char, 4, "角色"))
                weights.append(weight)

        # 三色三星角色
        if THREE_COLOR_THREE_STAR:
            weight = prob_config["三色三星角色"] / len(THREE_COLOR_THREE_STAR)
            for char in THREE_COLOR_THREE_STAR:
                categories.append((char, 3, "角色"))
                weights.append(weight)

        # 四星羁绊
        if FOUR_STAR_BONDS:
            weight = prob_config["四星羁绊"] / len(FOUR_STAR_BONDS)
            for bond in FOUR_STAR_BONDS:
                categories.append((bond, 4, "羁绊"))
                weights.append(weight)

        # 三星羁绊
        if THREE_STAR_BONDS:
            weight = prob_config["三星羁绊"] / len(THREE_STAR_BONDS)
            for bond in THREE_STAR_BONDS:
                categories.append((bond, 3, "羁绊"))
                weights.append(weight)

    elif pool_type == "神秘招募" and upper_character:
        # 当前UP光暗五星角色
        if upper_character in SPECIAL_FIVE_STAR:
            categories.append((upper_character, 5, "角色"))
            weights.append(prob_config["当前up光暗五星角色"])

        # 光暗四星角色
        if SPECIAL_FOUR_STAR:
            weight = prob_config["光暗四星角色"] / len(SPECIAL_FOUR_STAR)
            for char in SPECIAL_FOUR_STAR:
                categories.append((char, 4, "角色"))
                weights.append(weight)

        # 三色五星角色
        if THREE_COLOR_FIVE_STAR:
            weight = prob_config["三色五星角色"] / len(THREE_COLOR_FIVE_STAR)
            for char in THREE_COLOR_FIVE_STAR:
                categories.append((char, 5, "角色"))
                weights.append(weight)

        # 三色四星角色
        if THREE_COLOR_FOUR_STAR:
            weight = prob_config["三色四星角色"] / len(THREE_COLOR_FOUR_STAR)
            for char in THREE_COLOR_FOUR_STAR:
                categories.append((char, 4, "角色"))
                weights.append(weight)

        # 三色三星角色
        if THREE_COLOR_THREE_STAR:
            weight = prob_config["三色三星角色"] / len(THREE_COLOR_THREE_STAR)
            for char in THREE_COLOR_THREE_STAR:
                categories.append((char, 3, "角色"))
                weights.append(weight)

        # 五星羁绊 (使用所有五星角色的羁绊)
        all_five_star_bonds = list(THREE_COLOR_FIVE_STAR.values())
        if all_five_star_bonds:
            weight = prob_config["五星羁绊"] / len(all_five_star_bonds)
            for bond in all_five_star_bonds:
                categories.append((bond, 5, "羁绊"))
                weights.append(weight)

        # 四星羁绊
        if FOUR_STAR_BONDS:
            weight = prob_config["四星羁绊"] / len(FOUR_STAR_BONDS)
            for bond in FOUR_STAR_BONDS:
                categories.append((bond, 4, "羁绊"))
                weights.append(weight)

        # 三星羁绊
        if THREE_STAR_BONDS:
            weight = prob_config["三星羁绊"] / len(THREE_STAR_BONDS)
            for bond in THREE_STAR_BONDS:
                categories.append((bond, 3, "羁绊"))
                weights.append(weight)

    elif pool_type == "银河招募":
        # 光暗五星角色
        if SPECIAL_FIVE_STAR:
            weight = prob_config["光暗五星角色"] / len(SPECIAL_FIVE_STAR)
            for char in SPECIAL_FIVE_STAR:
                categories.append((char, 6, "角色"))
                weights.append(weight)

        # 光暗四星角色
        if SPECIAL_FOUR_STAR:
            weight = prob_config["光暗四星角色"] / len(SPECIAL_FOUR_STAR)
            for char in SPECIAL_FOUR_STAR:
                categories.append((char, 4, "角色"))
                weights.append(weight)

        # 光暗三星角色
        if SPECIAL_THREE_STAR:
            weight = prob_config["光暗三星角色"] / len(SPECIAL_THREE_STAR)
            for char in SPECIAL_THREE_STAR:
                categories.append((char, 3, "角色"))
                weights.append(weight)

    # 执行抽卡
    if not categories:
        return ("未找到可抽取角色", 0, "错误")

    result = random.choices(categories, weights=weights, k=1)[0]
    return result


@draw.handle()
async def handle_draw(event: Union[MessageEvent, GroupMessageEvent], matcher: Matcher, state: T_State,
                      args: Message = CommandArg()):
    # 存储用户信息用于后续验证
    if isinstance(event, GroupMessageEvent):
        state["user_id"] = event.user_id
        state["group_id"] = event.group_id
    else:
        state["user_id"] = event.user_id

    user_input = args.extract_plain_text().strip()
    state["pool_type"] = None
    state["upper_character"] = None
    if not user_input:
        print("无输入")
        await matcher.pause(Message([MessageSegment.text("请选择卡池：\n"
                                                         "1. 常规招募\n"
                                                         "2. 神秘招募[空格]角色名\n"
                                                         "3. up招募[空格]角色名\n"
                                                         "4. 银河招募\n"
                                                         "在下一条消息中选择一个池子告诉我吧~\n"
                                                         "不必再@我哟，我等你~"),
                                     MessageSegment.at(event.user_id)]))

    # 确定卡池类型
    user_input_seg = user_input.split(" ")
    if user_input_seg[0] in ["普池", "常规招募", "普通", "普通池", "三色池", "三色", "1", "1."]:
        state["pool_type"] = "常规招募"
    elif user_input_seg[0] in ["神秘", "光暗", "神秘招募", "光暗招募", "特殊招募", "光暗池", "神秘池", "2", "2."]:
        state["pool_type"] = "神秘招募"
        if len(user_input_seg) >= 2:
            state["upper_character"] = user_input_seg[1]
    elif user_input_seg[0] in ["银河", "银河招募", "4", "4."]:
        state["pool_type"] = "银河招募"
    elif user_input_seg[0] in SPECIAL_THREE_COLOR_FIVE_STAR or user_input_seg[0] in THREE_COLOR_FIVE_STAR:
        state["pool_type"] = "几率up招募"
        state["upper_character"] = user_input_seg[0]
    elif user_input_seg[0] in ["几率up招募", "up", "up招募", "3", "3."]:
        state["pool_type"] = "几率up招募"
        if len(user_input_seg) >= 2:
            state["upper_character"] = user_input_seg[1]
    elif user_input_seg[0] in SPECIAL_FIVE_STAR:
        state["pool_type"] = "神秘招募"
        state["upper_character"] = user_input_seg[0]
    else:  # 无效输入
        await matcher.reject(
            Message([MessageSegment.text(f"无效的卡池或角色: \"{user_input_seg[0]}\"，请使用有效的卡池名称或UP角色名\n"
                                         f"请选择卡池：\n"
                                         "1. 常规招募\n"
                                         "2. 神秘招募[空格]角色名\n"
                                         "3. up招募[空格]角色名\n"
                                         "4. 银河招募\n"
                                         "在下一条消息中选择一个池子告诉我吧~\n"
                                         "不必再@我哟，我等你~"),
                     MessageSegment.at(event.user_id)]))

    # 需要额外信息的情况
    if state["pool_type"] in ["几率up招募", "神秘招募"] and not state["upper_character"]:
        if state["pool_type"] == "几率up招募":
            await matcher.reject(Message([MessageSegment.text("请指定UP角色名，例如：厄德莱雅 或者 圣诞的蜜拉贝儿\n"
                                                              "我会自动识别角色是什么类型的哟。"),
                                          MessageSegment.at(event.user_id)]))
        else:  # 神秘招募
            await matcher.reject(Message([MessageSegment.text("请指定光暗UP角色名，例如：蝶子 或者 守护者艾瑞儿\n"
                                                              "我会自动识别角色是什么类型的哟。"),
                                          MessageSegment.at(event.user_id)]))

    # 执行抽卡
    if state["upper_character"]:
        await matcher.send(f"{state["upper_character"]} 招募概率提高！")
    else:
        await matcher.send(f"{state["pool_type"]}")
    await matcher.send("正在抽卡……")
    result = drawing_cards(state["pool_type"], state.get("upper_character"))
    t, star = format_result(result, 1)
    if event.group_id != "1032070842" and event.group_id != 1032070842:
        await pulling_anime(matcher, star, True)
    else:
        await single_pulling_anime(matcher, star)

    text = Message([t, MessageSegment.at(event.user_id)])
    await matcher.finish(text)


@draw.handle()
async def handle_secondary_input(
        event: Union[MessageEvent, GroupMessageEvent],
        matcher: Matcher,
        state: T_State
):
    # 验证用户身份
    if isinstance(event, GroupMessageEvent):
        if state["user_id"] != event.user_id or state["group_id"] != event.group_id:
            return
    else:
        if state["user_id"] != event.user_id:
            return

    # 获取用户输入
    user_input = event.get_plaintext().strip()

    # 处理取消命令
    if user_input in ["取消", "退出", "cancel", "exit", "0"]:
        await matcher.finish("已取消模拟抽卡")
    else:
        user_input_seg = user_input.split(" ")
        if user_input_seg[0] in ["普池", "常规招募", "普通", "普通池", "三色池", "三色", "1", "1."]:
            state["pool_type"] = "常规招募"
        elif user_input_seg[0] in ["神秘", "光暗", "神秘招募", "光暗招募", "特殊招募", "光暗池", "神秘池", "2", "2."]:
            state["pool_type"] = "神秘招募"
            if len(user_input_seg) >= 2:
                state["upper_character"] = user_input_seg[1]
        elif user_input_seg[0] in ["银河", "银河招募", "4", "4."]:
            state["pool_type"] = "银河招募"
        elif user_input_seg[0] in SPECIAL_THREE_COLOR_FIVE_STAR or user_input_seg[0] in THREE_COLOR_FIVE_STAR:
            state["pool_type"] = "几率up招募"
            state["upper_character"] = user_input_seg[0]
        elif user_input_seg[0] in ["几率up招募", "up", "up招募", "3", "3."]:
            state["pool_type"] = "几率up招募"
            if len(user_input_seg) >= 2:
                state["upper_character"] = user_input_seg[1]
        elif user_input_seg[0] in SPECIAL_FIVE_STAR:
            state["pool_type"] = "神秘招募"
            state["upper_character"] = user_input_seg[0]
        else:  # 无效输入
            await matcher.reject(
                Message(
                    [MessageSegment.text(f"无效的卡池或角色: \"{user_input_seg[0]}\"，请使用有效的卡池名称或UP角色名\n"
                                         f"请选择卡池：\n"
                                         "1. 常规招募\n"
                                         "2. 神秘招募[空格]角色名\n"
                                         "3. up招募[空格]角色名\n"
                                         "4. 银河招募\n"
                                         "在下一条消息中选择一个池子告诉我吧~\n"
                                         "不必再@我哟，我等你~"),
                     MessageSegment.at(event.user_id)]))

        # 需要额外信息的情况
        if state["pool_type"] in ["几率up招募", "神秘招募"] and not state["upper_character"]:
            if state["pool_type"] == "几率up招募":
                await matcher.reject(Message([MessageSegment.text("请指定UP角色名，例如：厄德莱雅 或者 圣诞的蜜拉贝儿\n"
                                                                  "我会自动识别角色是什么类型的哟。"),
                                              MessageSegment.at(event.user_id)]))
            else:  # 神秘招募
                await matcher.reject(Message([MessageSegment.text("请指定光暗UP角色名，例如：蝶子 或者 守护者艾瑞儿\n"
                                                                  "我会自动识别角色是什么类型的哟。"),
                                              MessageSegment.at(event.user_id)]))

        # 执行抽卡
        if state["upper_character"]:
            await matcher.send(f"{state["upper_character"]} 招募概率提高！")
        else:
            await matcher.send(f"{state["pool_type"]}")
        await matcher.send("正在抽卡……")
        result = drawing_cards(state["pool_type"], state.get("upper_character"))
        t, star = format_result(result, 1)
        if event.group_id != "1032070842" and event.group_id != 1032070842:
            await pulling_anime(matcher, star, True)
        else:
            await single_pulling_anime(matcher, star)
        text = Message([t, MessageSegment.at(event.user_id)])
        await matcher.finish(text)



@ten_draws.handle()
async def handle_ten_draws(event: Union[MessageEvent, GroupMessageEvent], matcher: Matcher, state: T_State,
                           args: Message = CommandArg()):
    # 存储用户信息用于后续验证
    if isinstance(event, GroupMessageEvent):
        state["user_id"] = event.user_id
        state["group_id"] = event.group_id
    else:
        state["user_id"] = event.user_id

    user_input = args.extract_plain_text().strip()
    state["pool_type"] = None
    state["upper_character"] = None
    if not user_input:
        print("无输入")
        await matcher.pause(Message([MessageSegment.text("请选择卡池：\n"
                                                         "1. 常规招募\n"
                                                         "2. 神秘招募[空格]角色名\n"
                                                         "3. up招募[空格]角色名\n"
                                                         "4. 银河招募\n"
                                                         "在下一条消息中选择一个池子告诉我吧~\n"
                                                         "不必再@我哟，我等你~"),
                                     MessageSegment.at(event.user_id)]))

    # 确定卡池类型
    user_input_seg = user_input.split(" ")
    if user_input_seg[0] in ["普池", "常规招募", "普通", "普通池", "三色池", "三色", "1", "1."]:
        state["pool_type"] = "常规招募"
    elif user_input_seg[0] in ["神秘", "光暗", "神秘招募", "光暗招募", "特殊招募", "光暗池", "神秘池", "2", "2."]:
        state["pool_type"] = "神秘招募"
        if len(user_input_seg) >= 2:
            state["upper_character"] = user_input_seg[1]
    elif user_input_seg[0] in ["银河", "银河招募", "4", "4."]:
        state["pool_type"] = "银河招募"
    elif user_input_seg[0] in SPECIAL_THREE_COLOR_FIVE_STAR or user_input_seg[0] in THREE_COLOR_FIVE_STAR:
        state["pool_type"] = "几率up招募"
        state["upper_character"] = user_input_seg[0]
    elif user_input_seg[0] in ["几率up招募", "up", "up招募", "3", "3."]:
        state["pool_type"] = "几率up招募"
        if len(user_input_seg) >= 2:
            state["upper_character"] = user_input_seg[1]
    elif user_input_seg[0] in SPECIAL_FIVE_STAR:
        state["pool_type"] = "神秘招募"
        state["upper_character"] = user_input_seg[0]
    else:  # 无效输入
        await matcher.reject(
            Message([MessageSegment.text(f"无效的卡池或角色: \"{user_input_seg[0]}\"，请使用有效的卡池名称或UP角色名\n"
                                         f"请选择卡池：\n"
                                         "1. 常规招募\n"
                                         "2. 神秘招募[空格]角色名\n"
                                         "3. up招募[空格]角色名\n"
                                         "4. 银河招募\n"
                                         "在下一条消息中选择一个池子告诉我吧~\n"
                                         "不必再@我哟，我等你~"),
                     MessageSegment.at(event.user_id)]))

    # 需要额外信息的情况
    if state["pool_type"] in ["几率up招募", "神秘招募"] and not state["upper_character"]:
        if state["pool_type"] == "几率up招募":
            await matcher.reject(Message([MessageSegment.text("请指定UP角色名，例如：厄德莱雅 或者 圣诞的蜜拉贝儿\n"
                                                              "我会自动识别角色是什么类型的哟。"),
                                          MessageSegment.at(event.user_id)]))
        else:  # 神秘招募
            await matcher.reject(Message([MessageSegment.text("请指定光暗UP角色名，例如：蝶子 或者 守护者艾瑞儿\n"
                                                              "我会自动识别角色是什么类型的哟。"),
                                          MessageSegment.at(event.user_id)]))


    # 执行十连抽
    if state["upper_character"]:
        await matcher.send(f"{state["upper_character"]} 招募概率提高！")
    else:
        await matcher.send(f"{state["pool_type"]}")
    await matcher.send("正在抽卡……")
    results = [drawing_cards(state["pool_type"], state.get("upper_character")) for _ in range(10)]
    t, star = format_result(results, 10)
    if event.group_id != "1032070842" and event.group_id != 1032070842:
        await pulling_anime(matcher, star, False)
    else:
        await single_pulling_anime(matcher, star)
    text = Message([t, MessageSegment.at(event.user_id)])
    await matcher.finish(text)


@ten_draws.handle()
async def handle_secondary_input(
        event: Union[MessageEvent, GroupMessageEvent],
        matcher: Matcher,
        state: T_State
):

    # 验证用户身份
    if isinstance(event, GroupMessageEvent):
        if state["user_id"] != event.user_id or state["group_id"] != event.group_id:
            return
    else:
        if state["user_id"] != event.user_id:
            return

    # 获取用户输入
    user_input = event.get_plaintext().strip()

    # 处理取消命令
    if user_input in ["取消", "退出", "cancel", "exit", "0"]:
        await matcher.finish("已取消模拟抽卡")
    else:
        user_input_seg = user_input.split(" ")
        if user_input_seg[0] in ["普池", "常规招募", "普通", "普通池", "三色池", "三色", "1", "1."]:
            state["pool_type"] = "常规招募"
        elif user_input_seg[0] in ["神秘", "光暗", "神秘招募", "光暗招募", "特殊招募", "光暗池", "神秘池", "2", "2."]:
            state["pool_type"] = "神秘招募"
            if len(user_input_seg) >= 2:
                state["upper_character"] = user_input_seg[1]
        elif user_input_seg[0] in ["银河", "银河招募", "4", "4."]:
            state["pool_type"] = "银河招募"
        elif user_input_seg[0] in SPECIAL_THREE_COLOR_FIVE_STAR or user_input_seg[0] in THREE_COLOR_FIVE_STAR:
            state["pool_type"] = "几率up招募"
            state["upper_character"] = user_input_seg[0]
        elif user_input_seg[0] in ["几率up招募", "up", "up招募", "3", "3."]:
            state["pool_type"] = "几率up招募"
            if len(user_input_seg) >= 2:
                state["upper_character"] = user_input_seg[1]
        elif user_input_seg[0] in SPECIAL_FIVE_STAR:
            state["pool_type"] = "神秘招募"
            state["upper_character"] = user_input_seg[0]
        else:  # 无效输入
            await matcher.reject(
                Message(
                    [MessageSegment.text(f"无效的卡池或角色: \"{user_input_seg[0]}\"，请使用有效的卡池名称或UP角色名\n"
                                         f"请选择卡池：\n"
                                         "1. 常规招募\n"
                                         "2. 神秘招募[空格]角色名\n"
                                         "3. up招募[空格]角色名\n"
                                         "4. 银河招募\n"
                                         "在下一条消息中选择一个池子告诉我吧~\n"
                                         "不必再@我哟，我等你~"),
                     MessageSegment.at(event.user_id)]))

        # 需要额外信息的情况
        if state["pool_type"] in ["几率up招募", "神秘招募"] and not state["upper_character"]:
            if state["pool_type"] == "几率up招募":
                await matcher.reject(Message([MessageSegment.text("请指定UP角色名，例如：厄德莱雅 或者 圣诞的蜜拉贝儿\n"
                                                                  "我会自动识别角色是什么类型的哟。"),
                                              MessageSegment.at(event.user_id)]))
            else:  # 神秘招募
                await matcher.reject(Message([MessageSegment.text("请指定光暗UP角色名，例如：蝶子 或者 守护者艾瑞儿\n"
                                                                  "我会自动识别角色是什么类型的哟。"),
                                              MessageSegment.at(event.user_id)]))

        # 执行十连抽
        if state["upper_character"]:
            await matcher.send(f"{state["upper_character"]} 招募概率提高！")
        else:
            await matcher.send(f"{state["pool_type"]}")
        await matcher.send("正在抽卡……")
        results = [drawing_cards(state["pool_type"], state.get("upper_character")) for _ in range(10)]
        t, star = format_result(results, 10)
        if event.group_id != "1032070842" and event.group_id != 1032070842:
            await pulling_anime(matcher, star, False)
        else:
            await single_pulling_anime(matcher, star)
        text = Message([t, MessageSegment.at(event.user_id)])
        await matcher.finish(text)


def format_result(result, count):
    """格式化抽卡结果用于输出"""
    if count == 1:
        name, star, item_type = result
        star_str = "★" * star
        color = {3: "蓝色", 4: "紫色", 5: "金色", 6: "红色"}.get(star, "白色")
        return MessageSegment.text(f"抽卡结果: {star_str} {color} {item_type}【{name}】"), int(star)

    # 十连结果格式化
    result_text = "十连抽结果:\n"
    for i, (name, star, item_type) in enumerate(result, 1):
        star_str = "★" * star
        color = {3: "蓝色", 4: "紫色", 5: "金色", 6: "红色"}.get(star, "白色")
        result_text += f"{i}. {star_str} {color} {item_type}【{name}】\n"

    # 添加统计信息
    stars = [r[1] for r in result]
    gold_count = sum(s == 5 for s in stars)
    purple_count = sum(s == 4 for s in stars)
    red_count = sum(s == 6 for s in stars)
    result_text += f"\n统计: 6★×{red_count} | 5★×{gold_count} | 4★×{purple_count} | 3★×{10 - gold_count - purple_count - red_count}"
    if red_count != 0:
        return MessageSegment.text(result_text), 6
    elif gold_count != 0:
        return MessageSegment.text(result_text), 5
    elif purple_count != 0:
        return MessageSegment.text(result_text), 4
    else:
        return MessageSegment.text(result_text), 3


async def pulling_anime(matcher: Matcher, level: int, single: bool):
    """
    输入抽卡的星级
    :param single: 是否为单抽
    :param matcher: 上一级函数调用的matcher
    :param level: 抽卡结果中最高星级的等级：红色6，金色5，紫色4，蓝色3。
    :return: 无
    """
    if level == 3: # 蓝色
        if single:
            await matcher.send(single_pull_start)

        else:
            await matcher.send(ten_pull_start)
        time.sleep(0.75)

        await matcher.send(pull_1)
        time.sleep(0.75)
        await matcher.send(blue_middle)
        time.sleep(0.75)
        await matcher.send(blue_end)
        time.sleep(0.75)
        await matcher.send(blue_or_purple_spaceship_close)
        time.sleep(0.75)
        await matcher.send(blue_spaceship_open)

    if level == 4: # 紫色
        if single:
            await matcher.send(single_pull_start)

        else:
            await matcher.send(ten_pull_start)
        time.sleep(0.75)
        await matcher.send(pull_1)
        time.sleep(0.75)
        await matcher.send(blue_middle)
        time.sleep(0.75)
        await matcher.send(purple_end)
        time.sleep(0.75)
        await matcher.send(blue_or_purple_spaceship_close)
        time.sleep(0.75)
        await matcher.send(purple_spaceship_open)

    if level == 5: # 金色
        if single:
            await matcher.send(single_pull_start)

        else:
            await matcher.send(ten_pull_start)
        time.sleep(0.75)
        await matcher.send(pull_1)
        time.sleep(0.75)
        await matcher.send(purple_middle)
        time.sleep(0.75)
        await matcher.send(golden_end)
        time.sleep(0.75)
        await matcher.send(gold_spaceship_close)
        time.sleep(0.75)
        await matcher.send(gold_spaceship_open)

    if level == 6: # 红色
        if single:
            await matcher.send(single_pull_start)

        else:
            await matcher.send(ten_pull_start)
        time.sleep(0.75)
        await matcher.send(pull_1)
        time.sleep(0.75)
        await matcher.send(purple_middle)
        time.sleep(0.75)
        await matcher.send(red_end)
        time.sleep(0.75)
        await matcher.send(red_spaceship_close)
        time.sleep(0.75)
        await matcher.send(red_spaceship_open)


async def single_pulling_anime(matcher: Matcher, level: int):
    if level == 3:
        time.sleep(0.75)
        await matcher.send(blue_spaceship_open)
    elif level == 4:
        time.sleep(0.75)
        await matcher.send(purple_spaceship_open)
    elif level == 5:
        time.sleep(0.75)
        await matcher.send(gold_spaceship_open)
    elif level == 6:
        await matcher.send(red_spaceship_open)