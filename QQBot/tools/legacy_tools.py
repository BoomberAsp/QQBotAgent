"""
Legacy Tools — Wrap existing plugin functionality as agent tools.

These tools bridge the existing game/entertainment features
(gacha, speed calc, translation) into the agent tool system.
"""

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
    from lib.deepseek_client import deepseek_client as _dc
    if _dc is None:
        from lib.deepseek_client import DeepSeekClient
        _dc = DeepSeekClient()

    prompt = f"请解释以下代码：\n```\n{code}\n```\n请用中文详细解释这段代码的功能和作用。"
    return await _dc.chat_completion(prompt)


# ── Translation ──────────────────────────────────────────────────

async def translate_text(text: str, target_language: str = "Chinese") -> str:
    """Translate text to the target language.

    Args:
        text: Text to translate.
        target_language: Target language (default: Chinese).
    """
    from lib.deepseek_client import deepseek_client as _dc
    if _dc is None:
        from lib.deepseek_client import DeepSeekClient
        _dc = DeepSeekClient()

    prompt = f"请将以下内容翻译成{target_language}：\n{text}"
    return await _dc.chat_completion(prompt)
