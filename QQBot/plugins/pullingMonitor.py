import time
import random

from nonebot.internal.matcher import Matcher
# on_message, on_command, on_notice imports removed (handlers disabled)
from nonebot.adapters.onebot.v11 import MessageSegment
import os

from lib.gacha_names import name_key

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

_gacha_data = None
_gacha_stamp = None


def _load_gacha_data():
    """Reload when the cache or the name references change on disk."""
    global _gacha_data, _gacha_stamp
    from tools.wiki_scraper import WikiScraper

    scraper = WikiScraper()
    stamp = scraper.gacha_stamp()
    if _gacha_data is not None and stamp == _gacha_stamp:
        return _gacha_data

    static = scraper._load_static_fallback()
    banners = static["banners"]
    wiki_data = scraper.get_cached_data()
    pools = wiki_data["pools"] if wiki_data else static["pools"]
    scraper.normalize_gacha_pools(pools)
    _gacha_data = {"pools": pools, "banners": banners or (wiki_data or {}).get("banners", {})}
    _gacha_stamp = stamp
    return _gacha_data


def _up_entry(name: str | None, pools: dict, pool_keys: tuple[str, ...]):
    wanted = name_key(name)
    if not wanted:
        return None
    options = [item for key in pool_keys for item in pools.get(key, {}).get("items", ())]
    for item in options:
        if wanted == name_key(item["name"]):
            return item
    matches = [item for item in options if wanted in {
        name_key(alias) for alias in (item.get("id"), item.get("title"), *item.get("name_aliases", []))
        if alias
    }]
    return matches[0] if len(matches) == 1 else None


# ── Drawing Cards ──────────────────────────────────────────────────

def draw_cards(pool_type: str, upper_character: str | None = None, count: int = 1):
    """Draw from a single snapshot of the given banner pool.

    Args:
        pool_type: Banner type ('常规招募', '几率up招募', '神秘招募', '银河招募').
        upper_character: Rate-up character name (required for 几率up/神秘).

    Returns:
        A list of (name, star_rating, type_string) tuples.
    """
    data = _load_gacha_data()
    pools = data["pools"]
    banners = data["banners"]

    if pool_type not in banners:
        return [(f"未知卡池: {pool_type}", 0, "错误")]

    banner = banners[pool_type]
    three_color_pools = ("three_color_five_star", "special_three_color_five_star")
    up_pools = {
        "几率up招募": three_color_pools,
        "神秘招募": ("special_five_star",),
    }
    up_item = _up_entry(upper_character, pools, up_pools.get(pool_type, ()))
    if banner.get("up_required") and up_item is None:
        expected = "光暗五星" if pool_type == "神秘招募" else "三色五星"
        return [(f"{pool_type}需要指定卡池内的{expected}UP角色", 0, "错误")]

    up_bond = (up_item or {}).get("bond") if (up_item or {}).get("bond_star", 5) == 5 else None
    up_bond_id = (up_item or {}).get("bond_id")
    categories = []
    weights = []

    for cat in banner["categories"]:
        pool_name = cat["pool"]
        prob = cat["prob"]
        star_override = cat.get("star")

        # ── Dynamic pools (depend on upper_character) ──────────
        if pool_name in {"up_character", "up_character_special"}:
            if up_item:
                default_star = 6 if pool_name == "up_character_special" else 5
                categories.append((up_item["name"], star_override or default_star, "角色"))
                weights.append(prob)
            continue

        if pool_name == "up_bond":
            if up_bond:
                categories.append((up_bond, star_override or 5, "羁绊"))
                weights.append(prob)
            continue

        if pool_name == "non_up_bonds_five_star":
            bonds = pools.get("bonds_five_star_all", {}).get("items", ())
            other_bonds = [
                bond for bond in bonds
                if not up_bond or (bond.get("id") or bond["name"]) != (up_bond_id or up_bond)
            ]
            if other_bonds:
                for bond in other_bonds:
                    categories.append((bond["name"], 5, "羁绊"))
                    weights.append(prob / len(other_bonds))
            continue

        # ── Static pools ───────────────────────────────────────
        pool = pools.get(pool_name)
        if not pool or not pool.get("items"):
            continue

        star = star_override or (6 if pool_name == "special_five_star" else pool["star"])
        item_type = "羁绊" if pool["type"] == "bond" else "角色"
        weight = prob / len(pool["items"])

        for item in pool["items"]:
            categories.append((item["name"], star, item_type))
            weights.append(weight)

    if not categories:
        return [("未找到可抽取角色", 0, "错误")]

    return random.choices(categories, weights=weights, k=count)


def drawing_cards(pool_type: str, upper_character: str | None = None):
    """Draw a single (name, star, type) result."""
    return draw_cards(pool_type, upper_character)[0]


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

    stars = [star for _, star, _ in result if star in (3, 4, 5, 6)]
    stats = " | ".join(f"{star}★×{stars.count(star)}" for star in (6, 5, 4, 3))
    result_text += f"\n统计: {stats}"
    return MessageSegment.text(result_text), max(stars, default=0)


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