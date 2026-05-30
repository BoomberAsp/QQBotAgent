"""
Legacy Tools — Wrap existing plugin functionality as agent tools.

These tools bridge the existing game/entertainment features
(gacha, speed calc, translation) into the agent tool system.
"""

import asyncio
import sys
import os

# Ensure plugins directory is importable
_plugins_dir = os.path.join(os.path.dirname(__file__), "..", "plugins")
if _plugins_dir not in sys.path:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── Gacha Pull ───────────────────────────────────────────────────

def gacha_pull(pool_type: str, count: int = 1, up_character: str = None) -> str:
    """Simulate game gacha/recruitment pulls.

    Args:
        pool_type: Banner type ('常规招募', '几率up招募', '神秘招募', '银河招募')
        count: Number of pulls (1 or 10).
        up_character: Rate-up character name (optional).
    """
    from plugins.pullingMonitor import drawing_cards, format_result

    if pool_type not in ["常规招募", "几率up招募", "神秘招募", "银河招募"]:
        return f"[Gacha Error] 无效的卡池类型: '{pool_type}'。可选: 常规招募, 几率up招募, 神秘招募, 银河招募"

    if count not in [1, 10]:
        return "[Gacha Error] count 必须是 1 (单抽) 或 10 (十连抽)"

    # Validate up_character for rate-up pools
    if pool_type == "几率up招募" and not up_character:
        return "[Gacha Error] 几率UP招募需要指定 up_character (UP角色名)"

    results = [drawing_cards(pool_type, up_character) for _ in range(count)]
    text_segment, _ = format_result(results, count)
    return str(text_segment)


async def play_gacha_animation(star_level: int, is_single: bool = False, interval: float = 0.75) -> str:
    """Play gacha pull animation frames in QQ chat.

    Reads animation images and sends them sequentially with configurable intervals.
    Uses the _send_msg context variable set by agent_router to send images.

    Args:
        star_level: Highest star level (3=blue, 4=purple, 5=gold, 6=red).
        is_single: True for single pull, False for ten-pull.
        interval: Delay between animation frames in seconds (default 0.75).
    """
    from agent.context import _send_msg
    from plugins.pullingMonitor import (
        pull_1, blue_end, blue_middle, blue_or_purple_spaceship_close,
        blue_spaceship_open, gold_spaceship_close, gold_spaceship_open,
        golden_end, purple_middle, purple_end, purple_spaceship_open,
        red_end, red_spaceship_close, red_spaceship_open,
        single_pull_start, ten_pull_start,
    )
    from nonebot.adapters.onebot.v11 import MessageSegment

    send = _send_msg.get()
    if send is None:
        return "[Gacha] 当前环境不支持发送图片（非QQ聊天上下文）。"

    if star_level not in (3, 4, 5, 6):
        return f"[Gacha] 无效的星级: {star_level}。可选: 3(蓝), 4(紫), 5(金), 6(红)"

    # ── Build animation sequence ─────────────────────────────────
    frames = []

    # First frame: start animation
    if is_single:
        frames.append(single_pull_start)
    else:
        frames.append(ten_pull_start)

    # Second frame: pull action
    frames.append(pull_1)

    # Middle + End + Spaceship sequence per star level
    if star_level == 3:
        frames.extend([blue_middle, blue_end,
                       blue_or_purple_spaceship_close, blue_spaceship_open])
    elif star_level == 4:
        frames.extend([blue_middle, purple_end,
                       blue_or_purple_spaceship_close, purple_spaceship_open])
    elif star_level == 5:
        frames.extend([purple_middle, golden_end,
                       gold_spaceship_close, gold_spaceship_open])
    elif star_level == 6:
        frames.extend([purple_middle, red_end,
                       red_spaceship_close, red_spaceship_open])

    # ── Play frames ─────────────────────────────────────────────
    sent_count = 0
    for frame in frames:
        if frame is None:
            continue  # Image file missing, skip silently
        try:
            await send(frame)
            sent_count += 1
            await asyncio.sleep(interval)
        except Exception:
            pass  # One frame failed, continue with the rest

    if sent_count == 0:
        return "[Gacha] 动画播放失败: 所有图片资源缺失。"

    star_label = {3: "蓝色", 4: "紫色", 5: "金色", 6: "红色"}.get(star_level, str(star_level))
    return f"[Gacha] {star_label}抽卡动画已播放 ({sent_count}/{len(frames)} 帧)。"


# ── Speed Calculation ────────────────────────────────────────────

def calculate_speed(battle_data: str) -> str:
    """Calculate enemy speed from battle action value data.

    Args:
        battle_data: Formatted battle data with ally/enemy sections.
    """
    from plugins.group import parse_speed_data, compute_speed_results

    data, error = parse_speed_data(battle_data)
    if error:
        return f"[Speed Error] {error}"

    allies, enemies = data
    results, error, warning = compute_speed_results(allies, enemies)

    if error:
        return f"[Speed Error] {error}"
    if not results:
        return "[Speed] 未计算出有效结果，请检查敌方角色行动值是否增加。"

    lines = ["测速结果:"]
    for res in results:
        lines.append(
            f"敌方: {res['敌方名称']}\n"
            f"  - 平均速度: {res['估算速度(平均)']}\n"
            f"  - 最大速度: {res['最大速度']}\n"
            f"  - 参考角色: {res['参考角色数']}名"
        )

    if warning:
        lines.append("\n注意: 某一敌方测算的平均速度与最大速度相差过大，请检查数据是否输入错误！")

    return "\n".join(lines)


# ── Speed Probability Comparison ─────────────────────────────────

def compare_speed_probability(speed_1: int, speed_2: int) -> str:
    """Calculate speed randomization probability between two speeds.

    Args:
        speed_1: First speed value.
        speed_2: Second speed value.
    """
    from plugins.speed import compute_prob

    smaller = min(speed_1, speed_2)
    bigger = max(speed_1, speed_2)
    prob = compute_prob(smaller, bigger)
    return f"对于速度 {smaller} 与 {bigger}，乱速概率: {prob}"


# ── Code Explanation ─────────────────────────────────────────────

async def explain_code_tool(code: str) -> str:
    """Explain what a code snippet does (delegates to LLM via DeepSeek).

    This is a meta-tool: it formulates a prompt for code explanation
    and calls the same LLM. In the agent context, the agent can also
    just explain code directly without this tool.

    Args:
        code: Code snippet to explain.
    """
    from lib.model_router import model_router

    prompt = f"请解释以下代码：\n```\n{code}\n```\n请用中文详细解释这段代码的功能和作用。"
    return await model_router.flash_client.chat_completion(prompt)


# ── Translation ──────────────────────────────────────────────────

async def translate_text(text: str, target_language: str = "Chinese") -> str:
    """Translate text to the target language.

    Args:
        text: Text to translate.
        target_language: Target language (default: Chinese).
    """
    from lib.model_router import model_router

    prompt = f"请将以下内容翻译成{target_language}：\n{text}"
    return await model_router.flash_client.chat_completion(prompt)
