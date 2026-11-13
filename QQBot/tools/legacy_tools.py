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


# ── Gacha UI helpers ─────────────────────────────────────────────

GACHA_POOLS = (
    ("常规招募", "常规招募"),
    ("几率up招募", "几率UP"),
    ("神秘招募", "神秘招募"),
    ("银河招募", "银河招募"),
)


def gacha_pool_rows(*, ten: bool = False, as_command: bool = True):
    """卡池选择键盘：默认 2×2。

    ``as_command=True`` 供群聊命令层使用（data 为 ``/抽卡 <池>``）；
    False 供官方私聊使用（data 为自然语言 ``单抽 <池>``，交给 agent
    工具链，避免把 /抽卡 当成未知斜杠命令）。
    """
    from lib.button_spec import Btn, max_per_row

    if as_command:
        verb = "/十连抽" if ten else "/抽卡"
    else:
        verb = "十连抽" if ten else "单抽"
    buttons = [
        Btn(label=label, data=f"{verb} {pool}", action="cmd")
        for pool, label in GACHA_POOLS
    ]
    per = max(1, min(2, max_per_row()))
    return [buttons[i:i + per] for i in range(0, len(buttons), per)]


# ── Gacha Pull ───────────────────────────────────────────────────

async def gacha_pull(pool_type: str, count: int = 1, up_character: str = None) -> str:
    """Simulate game gacha/recruitment pulls.

    Args:
        pool_type: Banner type ('常规招募', '几率up招募', '神秘招募', '银河招募')
        count: Number of pulls (1 or 10).
        up_character: Rate-up character name (optional, supports fuzzy matching).
    """
    from plugins.pullingMonitor import drawing_cards, format_result

    # Trigger background wiki refresh if cache is stale
    from tools.wiki_scraper import WikiScraper
    from lib.model_router import model_router
    scraper = WikiScraper(llm_client=model_router.flash_client)
    if scraper.is_stale():
        asyncio.create_task(scraper.refresh())

    if pool_type not in ["常规招募", "几率up招募", "神秘招募", "银河招募"]:
        return f"[Gacha Error] 无效的卡池类型: '{pool_type}'。可选: 常规招募, 几率up招募, 神秘招募, 银河招募"

    if count not in [1, 10]:
        return "[Gacha Error] count 必须是 1 (单抽) 或 10 (十连抽)"

    # Validate up_character for rate-up pools
    if pool_type == "几率up招募" and not up_character:
        return "[Gacha Error] 几率UP招募需要指定 up_character (UP角色名)"

    # ── Fuzzy name resolution ──
    resolved_note = ""
    actual_up = up_character
    if up_character:
        from tools.name_resolver import get_resolver
        resolver = get_resolver()
        resolved = resolver.resolve_character(up_character)
        if resolved and resolved != up_character:
            resolved_note = f"[名称解析: 「{up_character}」→「{resolved}」]\n"
            actual_up = resolved

    results = [drawing_cards(pool_type, actual_up) for _ in range(count)]
    text_segment, _ = format_result(results, count)

    # buttons P1：结果页按钮预约——_send_response 的官方 md 路径一次性消费
    # （键盘只能骑 msg_type=2 代理回复），OneBot/split 路径忽略并清空。
    # data 用自然语言：点击=替用户打字，经 agent 循环天然复用本工具。
    try:
        from agent.context import _turn_keyboard
        from lib.button_spec import Btn
        _turn_keyboard.set([[
            Btn(label="再抽一次", data="再抽一次", action="cmd"),
            Btn(label="再来十连", data="再来一次十连抽", action="cmd"),
            Btn(label="看动画", data="播放抽卡动画", action="cmd"),
        ]])
    except Exception:  # noqa: BLE001 — 按钮是增强，绝不影响抽卡结果
        pass
    return resolved_note + str(text_segment)


def _frame_file_path(frame) -> str:
    """MessageSegment.image(file://...) → local path ('' when absent)."""
    if frame is None:
        return ""
    try:
        raw = frame.data.get("file", "")
    except AttributeError:
        return ""
    if not isinstance(raw, str):
        return ""
    if raw.startswith("file://"):
        raw = raw[len("file://"):]
    return raw


def _gif_enabled() -> bool:
    raw = (os.environ.get("QQ_GACHA_GIF") or "on").strip().lower()
    return raw not in ("off", "0", "false", "no", "disable", "disabled")


def _gif_width() -> int:
    raw = (os.environ.get("QQ_GACHA_GIF_WIDTH") or "").strip()
    try:
        value = int(raw) if raw else 1280
    except ValueError:
        value = 1280
    return max(640, min(1920, value))


def _gif_mode() -> str:
    raw = (os.environ.get("QQ_GACHA_GIF_FRAMES") or "full").strip().lower()
    return "key" if raw in ("key", "3", "frames3", "three") else "full"


async def _send_image_once(sender, path: str, label: str) -> bool:
    """Send one image immediately when the platform supports it.

    Official route: QQOfficialSender.send_media_confirmed() reserves a
    passive-reply slot and reports the platform outcome. OneBot has no
    such method; its normal sender path is already immediate.
    """
    from agent.context import _turn_sender
    from agent.outbound import OutImage

    image = OutImage(path=path, label=label)
    raw = _turn_sender.get()
    confirmed = getattr(raw, "send_media_confirmed", None)
    if confirmed is not None:
        try:
            status = await confirmed(image)
        except Exception:  # noqa: BLE001
            return False
        # 'uncertain' may already have been delivered; never duplicate.
        return status in ("sent", "uncertain")
    try:
        await sender.send_image(image)
        return True
    except Exception:  # noqa: BLE001
        return False


async def _play_gacha_frames_legacy(sender, frames, star_label: str,
                                    interval: float) -> str:
    """Pre-GIF behavior, kept as the QQ_GACHA_GIF=off escape hatch."""
    from agent.outbound import OutImage

    sent_count = 0
    for frame in frames:
        path = _frame_file_path(frame)
        if not path or not os.path.isfile(path):
            continue
        try:
            await sender.send_image(OutImage(path=path, label="gacha frame"))
            sent_count += 1
            await asyncio.sleep(interval)
        except Exception:  # noqa: BLE001
            pass
    if sent_count == 0:
        return "[Gacha] 动画播放失败: 所有图片资源缺失。"
    return f"[Gacha] {star_label}抽卡动画已播放 ({sent_count}/{len(frames)} 帧)。"


async def play_gacha_animation(star_level: int, is_single: bool = False, interval: float = 0.75) -> str:
    """Play the gacha animation as one animated GIF.

    The six PNG frames are built into a 1280×720 GIF and sent as ONE
    image message, so a c2c turn keeps 3 passive-reply slots for the
    text result instead of spending 6 on frames. The tool waits for one
    GIF round before returning, so the result text does not spoil the
    reveal. ``QQ_GACHA_GIF=off`` restores the legacy per-frame path.

    Args:
        star_level: Highest star level (3=blue, 4=purple, 5=gold, 6=red).
        is_single: True for single pull, False for ten-pull.
        interval: Base frame interval in seconds (default 0.75).
    """
    from agent.context import _current_group_id, _current_sender
    from plugins.pullingMonitor import (
        pull_1, blue_end, blue_middle, blue_or_purple_spaceship_close,
        blue_spaceship_open, gold_spaceship_close, gold_spaceship_open,
        golden_end, purple_middle, purple_end, purple_spaceship_open,
        red_end, red_spaceship_close, red_spaceship_open,
        single_pull_start, ten_pull_start,
    )

    sender = _current_sender.get()
    if sender is None:
        return "[Gacha] 当前环境不支持发送图片（非QQ聊天上下文）。"

    # ── Group feature toggle check ────────────────────────────────
    group_id = _current_group_id.get()
    if group_id:
        from agent.group_features import get_group_features
        gf = get_group_features()
        gf.refresh()
        if not gf.is_enabled(group_id, "image"):
            return "[Gacha] 该群聊的图片发送功能已被管理员关闭，无法播放抽卡动画。"

    if star_level not in (3, 4, 5, 6):
        return f"[Gacha] 无效的星级: {star_level}。可选: 3(蓝), 4(紫), 5(金), 6(红)"

    star_label = {3: "蓝色", 4: "紫色", 5: "金色", 6: "红色"}.get(
        star_level, str(star_level))

    # ── Build animation sequence (same order as the legacy player) ──
    frames = []
    frames.append(single_pull_start if is_single else ten_pull_start)
    frames.append(pull_1)
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

    if not _gif_enabled():
        return await _play_gacha_frames_legacy(sender, frames, star_label, interval)

    paths = [_frame_file_path(frame) for frame in frames]
    paths = [path for path in paths if path and os.path.isfile(path)]
    if not paths:
        return "[Gacha] 动画播放失败: 所有图片资源缺失。"

    mode = _gif_mode()
    selected = paths
    if mode == "key" and len(paths) > 3:
        selected = []
        for path in (paths[2], paths[3], paths[-1]):
            if path not in selected:
                selected.append(path)

    try:
        from lib.gacha_animation import ensure_gif
        gif_path, total_ms = await asyncio.to_thread(
            ensure_gif, selected,
            star_level=star_level, is_single=is_single, mode=mode,
            width=_gif_width(), interval=interval,
        )
    except Exception:  # noqa: BLE001 — GIF 是增强，失败必须降级
        gif_path, total_ms = "", 0

    if gif_path and await _send_image_once(sender, gif_path, "gacha animation"):
        # GIF animates client-side; wait one round so the text result
        # lands after the reveal. Cap at 6s against a bad duration.
        await asyncio.sleep(min(max(total_ms, 0) / 1000.0, 6.0))
        return f"[Gacha] {star_label}抽卡动画已发送（GIF，{len(selected)} 帧）。"

    fallback = selected[-1] if selected else paths[-1]
    if await _send_image_once(sender, fallback, "gacha frame"):
        return f"[Gacha] {star_label}抽卡动画已降级为静态图。"
    return "[Gacha] 动画发送失败: 图片资源缺失或平台拒绝。"


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

    # buttons P1：结果页按钮预约（消费规则同 gacha_pull）
    try:
        from agent.context import _turn_keyboard
        from lib.button_spec import Btn
        _turn_keyboard.set([[
            Btn(label="再算一组", data="再帮我算一组测速", action="cmd"),
            Btn(label="乱速概率", data="帮我算乱速概率", action="cmd"),
        ]])
    except Exception:  # noqa: BLE001 — 按钮是增强，绝不影响测速结果
        pass
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
