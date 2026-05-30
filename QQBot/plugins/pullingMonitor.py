import time
from datetime import timedelta
from typing import Union
import random

import pandas as pd
import numpy as np
from nb_cli.cli.commands import self
from nonebot.internal.matcher import Matcher
# on_message, on_command, on_notice imports removed (handlers disabled)
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


# 修正后的图片消息段 (graceful fallback if images missing)
def _safe_image(filename):
    try:
        return fix_image_path(filename)
    except FileNotFoundError:
        return None

pull_1 = _safe_image("pull_1.png")
blue_end = _safe_image("blue_end.png")
blue_middle = _safe_image("blue_middle.png")
blue_or_purple_spaceship_close = _safe_image("blue_or_purple_spaceship_close.png")
blue_spaceship_open = _safe_image("blue_spaceship_open.png")
gold_spaceship_close = _safe_image("gold_spaceship_close.png")
gold_spaceship_open = _safe_image("gold_spaceship_open.png")
golden_end = _safe_image("golden_end.png")
purple_middle = _safe_image("purple_middle.png")
purple_end = _safe_image("purple_end.png")
purple_spaceship_open = _safe_image("purple_spaceship_open.png")
red_end = _safe_image("red_end.png")
red_spaceship_close = _safe_image("red_spaceship_close.png")
red_spaceship_open = _safe_image("red_spaceship_open.png")
single_pull_start = _safe_image("single_pull_start.png")
ten_pull_start = _safe_image("ten_pull_start.png")


# ── Gacha Data Loading ─────────────────────────────────────────────

_gacha_data = None  # Lazy-loaded cache


def _load_gacha_data():
    """Load gacha pools and banners from config/gacha_data.json.

    Returns a dict with two keys:
        pools:   dict[str, dict] — pool_name → {star, type, items: [{name, bond?}]}
        banners: dict[str, dict] — banner_name → {up_required, categories: [{pool, prob, star?}]}

    Derived pools are computed at load time:
        bonds_five_star_all      — all bonds from 三色 + 限定 five-star characters
        bonds_five_star_tricolor — bonds from 三色 five-star characters only
    """
    global _gacha_data
    if _gacha_data is not None:
        return _gacha_data

    import json
    config_path = os.path.join(CURRENT_DIR, "..", "config", "gacha_data.json")
    config_path = os.path.abspath(config_path)

    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    pools = data["pools"]
    banners = data["banners"]

    # ── Build derived bond pools from character data ────────────
    # bonds_five_star_all: 三色 + 限定 five-star character bonds
    bonds_all = []
    for pool_key in ("three_color_five_star", "special_three_color_five_star"):
        for item in pools[pool_key]["items"]:
            if "bond" in item:
                bonds_all.append({"name": item["bond"]})

    # bonds_five_star_tricolor: 三色 five-star bonds only
    bonds_tricolor = []
    for item in pools["three_color_five_star"]["items"]:
        if "bond" in item:
            bonds_tricolor.append({"name": item["bond"]})

    pools["bonds_five_star_all"] = {
        "description": "五星羁绊（全部）",
        "star": 5,
        "type": "bond",
        "items": bonds_all,
    }
    pools["bonds_five_star_tricolor"] = {
        "description": "五星羁绊（三色）",
        "star": 5,
        "type": "bond",
        "items": bonds_tricolor,
    }

    _gacha_data = {"pools": pools, "banners": banners}
    return _gacha_data


def _find_character_bond(character_name: str, pools: dict) -> str | None:
    """Look up a character's bond string from all five-star character pools."""
    for pool_key in ("three_color_five_star", "special_three_color_five_star"):
        for item in pools[pool_key]["items"]:
            if item["name"] == character_name and "bond" in item:
                return item["bond"]
    return None


def _is_special_five_star(character_name: str, pools: dict) -> bool:
    """Check if a character is in the 光暗五星 pool."""
    return any(
        item["name"] == character_name
        for item in pools["special_five_star"]["items"]
    )


# ── Drawing Cards ──────────────────────────────────────────────────

def drawing_cards(pool_type: str, upper_character: str = None):
    """Draw a gacha card from the given banner pool.

    Args:
        pool_type: Banner type ('常规招募', '几率up招募', '神秘招募', '银河招募').
        upper_character: Rate-up character name (required for 几率up/神秘).

    Returns:
        (name, star_rating, type_string) — e.g. ("厄德莱雅", 5, "角色").
    """
    data = _load_gacha_data()
    pools = data["pools"]
    banners = data["banners"]

    if pool_type not in banners:
        return (f"未知卡池: {pool_type}", 0, "错误")

    banner = banners[pool_type]
    categories = []
    weights = []

    for cat in banner["categories"]:
        pool_name = cat["pool"]
        prob = cat["prob"]
        star_override = cat.get("star")

        # ── Dynamic pools (depend on upper_character) ──────────
        if pool_name == "up_character":
            if not upper_character:
                continue
            star = star_override or 5
            categories.append((upper_character, star, "角色"))
            weights.append(prob)
            continue

        if pool_name == "up_character_special":
            if not upper_character or not _is_special_five_star(upper_character, pools):
                continue
            star = star_override or 5
            categories.append((upper_character, star, "角色"))
            weights.append(prob)
            continue

        if pool_name == "up_bond":
            if not upper_character:
                continue
            bond = _find_character_bond(upper_character, pools)
            if bond:
                star = star_override or 5
                categories.append((bond, star, "羁绊"))
                weights.append(prob)
            continue

        if pool_name == "non_up_bonds_five_star":
            if not upper_character:
                continue
            up_bond = _find_character_bond(upper_character, pools)
            # Collect all five-star bonds except the UP character's bond
            all_bonds = []
            for pool_key in ("three_color_five_star", "special_three_color_five_star"):
                for item in pools[pool_key]["items"]:
                    if "bond" in item and item["bond"] != up_bond:
                        all_bonds.append(item["bond"])
            if all_bonds:
                weight = prob / len(all_bonds)
                for bond in all_bonds:
                    categories.append((bond, 5, "羁绊"))
                    weights.append(weight)
            continue

        # ── Static pools ───────────────────────────────────────
        pool = pools.get(pool_name)
        if not pool or not pool.get("items"):
            continue

        star = star_override or pool["star"]
        item_type = "羁绊" if pool["type"] == "bond" else "角色"
        weight = prob / len(pool["items"])

        for item in pool["items"]:
            categories.append((item["name"], star, item_type))
            weights.append(weight)

    if not categories:
        return ("未找到可抽取角色", 0, "错误")

    result = random.choices(categories, weights=weights, k=1)[0]
    return result


# @draw.handle()
# async def handle_draw(...):
#     [DEPRECATED] Gacha draw handler — superseded by agent gacha_pull tool.


# @draw.handle()
# async def handle_secondary_input(...):
#     [DEPRECATED] Gacha draw secondary input — superseded by agent gacha_pull tool.


# @ten_draws.handle()
# async def handle_ten_draws(...):
#     [DEPRECATED] Ten-draw handler — superseded by agent gacha_pull tool.


# @ten_draws.handle()
# async def handle_secondary_input(...):
#     [DEPRECATED] Ten-draw secondary input — superseded by agent gacha_pull tool.


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


async def pulling_anime(matcher: Matcher, level: int, single: bool, interval: float = 0.75):
    """
    输入抽卡的星级
    :param single: 是否为单抽
    :param matcher: 上一级函数调用的matcher
    :param level: 抽卡结果中最高星级的等级：红色6，金色5，紫色4，蓝色3。
    :param interval: 动画帧之间间隔秒数。
    :return: 无
    """
    if level == 3: # 蓝色
        if single:
            await matcher.send(single_pull_start)

        else:
            await matcher.send(ten_pull_start)
        time.sleep(interval)

        await matcher.send(pull_1)
        time.sleep(interval)
        await matcher.send(blue_middle)
        time.sleep(interval)
        await matcher.send(blue_end)
        time.sleep(interval)
        await matcher.send(blue_or_purple_spaceship_close)
        time.sleep(interval)
        await matcher.send(blue_spaceship_open)

    if level == 4: # 紫色
        if single:
            await matcher.send(single_pull_start)

        else:
            await matcher.send(ten_pull_start)
        time.sleep(interval)
        await matcher.send(pull_1)
        time.sleep(interval)
        await matcher.send(blue_middle)
        time.sleep(interval)
        await matcher.send(purple_end)
        time.sleep(interval)
        await matcher.send(blue_or_purple_spaceship_close)
        time.sleep(interval)
        await matcher.send(purple_spaceship_open)

    if level == 5: # 金色
        if single:
            await matcher.send(single_pull_start)

        else:
            await matcher.send(ten_pull_start)
        time.sleep(interval)
        await matcher.send(pull_1)
        time.sleep(interval)
        await matcher.send(purple_middle)
        time.sleep(interval)
        await matcher.send(golden_end)
        time.sleep(interval)
        await matcher.send(gold_spaceship_close)
        time.sleep(interval)
        await matcher.send(gold_spaceship_open)

    if level == 6: # 红色
        if single:
            await matcher.send(single_pull_start)

        else:
            await matcher.send(ten_pull_start)
        time.sleep(interval)
        await matcher.send(pull_1)
        time.sleep(interval)
        await matcher.send(purple_middle)
        time.sleep(interval)
        await matcher.send(red_end)
        time.sleep(interval)
        await matcher.send(red_spaceship_close)
        time.sleep(interval)
        await matcher.send(red_spaceship_open)


async def single_pulling_anime(matcher: Matcher, level: int, interval: float = 0.75):
    if level == 3:
        time.sleep(interval)
        await matcher.send(blue_spaceship_open)
    elif level == 4:
        time.sleep(interval)
        await matcher.send(purple_spaceship_open)
    elif level == 5:
        time.sleep(interval)
        await matcher.send(gold_spaceship_open)
    elif level == 6:
        await matcher.send(red_spaceship_open)