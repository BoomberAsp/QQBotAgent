"""
角色面社交互动工具（Bond-System-Design.md §4.4 / P2）。

这些工具只在**角色面**（split 人格 + social 平面）经 allowed_tools 下发，
与任务面物理隔离。工具本身不扮演角色——它把「发生了什么」以中立事实
observation 的形式回给角色面 LLM，由角色以自己的口吻自然回应。

当前实现：
- give_gift: 送礼。按角色卡 Gift Preferences 匹配偏好档位，结算好感/心情，
  写入共同经历日志（bond_events）。
- create_image / send_voice（P4，§7）: 角色面多模态生成。三重门控（等级门
  + 配额门 + 内容过滤）→ 桥接语（send_progress immediate）→ 生成 → 资产卡
  + 审计 + 记账 → 出站媒体 → 中立 observation 交角色面演绎。

所有协作对象经参数注入，可独立单测（与 delegation.py / creation_tools.py 同模式）。
"""

import random
from typing import Any, Callable, Dict, Optional

from agent import bond_content
from agent.asset_memory import AssetCard, make_asset_id, prompt_hash_of
from agent.outbound import OutAudio, OutImage
from lib import media_gen

_TIER_LABEL = {"loved": "非常喜爱", "normal": "觉得还不错", "disliked": "不太喜欢"}


async def give_gift(
    *,
    user_id: str,
    gift_name: str,
    char_id: str,
    card_content: Optional[str],
    bond_manager,
) -> str:
    """Settle a gift and return a neutral observation for the character LLM.

    Returns a string the character-plane LLM reads as a tool observation; it
    then reacts in character. Daily-cap and level-ups are folded into the text.
    """
    gift_name = (gift_name or "").strip()
    if not user_id or not char_id:
        return "[Error] 缺少用户或角色上下文，无法送礼。"
    if not gift_name:
        return "[Error] 礼物名为空，请在 gift_name 参数里填用户送的礼物。"

    prefs = bond_content.parse_gift_preferences(card_content or "")
    tier = bond_content.match_gift(gift_name, prefs)
    result = bond_manager.give_gift(user_id, char_id, tier)

    if not result.get("accepted"):
        return (
            f"用户想送你「{gift_name}」，但今天收礼次数已达上限"
            f"（{result.get('gifts_today')}/{result.get('daily_gift_limit')}）。"
            "请以角色口吻温柔地告诉用户明天再来，并谢谢这份心意。"
        )

    label = _TIER_LABEL.get(tier, "觉得还不错")
    bond_manager.append_event(
        user_id, char_id, f"用户送了「{gift_name}」给你，你{label}"
    )
    parts = [
        f"用户送给你「{gift_name}」，你{label}这份礼物"
        f"（好感 +{result.get('exp_gained', 0)}）。"
    ]
    for up in result.get("level_ups", []):
        bond_manager.append_event(
            user_id, char_id, f"羁绊提升到了 Lv{up['level']}（{up['title']}）"
        )
        parts.append(f"你们的好感提升到了 Lv{up['level']}（{up['title']}）！")
    parts.append("请以角色口吻自然地回应这份礼物和此刻的心情（不要报数值）。")
    return " ".join(parts)


# give_gift 工具的 JSON Schema（注册进 ToolRegistry 用）
GIVE_GIFT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "gift_name": {
            "type": "string",
            "description": "用户送出的礼物名称（从用户消息里提炼，如「一朵花」「草莓蛋糕」）",
        },
    },
    "required": ["gift_name"],
}


# ── P3: 剧情场景收尾 ──────────────────────────────────────────────

async def complete_scene(
    *,
    user_id: str,
    char_id: str,
    scene_title: str,
    bond_manager,
    memento: str = "",
) -> Dict[str, Any]:
    """Settle a finished bond scene; return {"ok", "result", "observation"}.

    Settlement is one-shot and guarded by ``bond_manager.complete_scene``
    (+scene_exp, mood bump, mark scenes_done — a repeat completion yields
    accepted=False and no EXP). The character-plane LLM receives
    ``observation`` and speaks a closing line in the same turn. The turn.py
    closure flags ``_pending_bond_scene_done`` and returns ``observation`` as
    the tool string; the post-run hook consumes ``result`` (level_ups / exp /
    memento) to send the level-up notice + commemorative card and to close the
    special session. Mirrors give_gift (settlement + event log live here).
    """
    scene_title = (scene_title or "").strip()
    if not user_id or not char_id:
        return {"ok": False, "result": {},
                "observation": "[Error] 缺少用户或角色上下文，无法收尾场景。"}
    title = scene_title or "这段剧情"
    memento = (memento or "").strip()

    bond_result = bond_manager.complete_scene(user_id, char_id, title)
    result = dict(bond_result)
    result["scene_title"] = title
    result["char_id"] = char_id
    result["memento"] = memento

    if not bond_result.get("accepted"):
        return {
            "ok": True, "result": result,
            "observation": (
                f"剧情「{title}」其实早已完成过、回忆也已珍藏。"
                f"请以角色口吻温柔地告诉用户这一点，并邀请 TA 去体验别的剧情。"
            ),
        }

    bond_manager.append_event(user_id, char_id, f"完成了剧情场景「{title}」")
    parts = [f"剧情「{title}」已走向结局，你们的羁绊因此更深了。"]
    for up in bond_result.get("level_ups", []):
        bond_manager.append_event(
            user_id, char_id, f"羁绊提升到了 Lv{up['level']}（{up['title']}）"
        )
        parts.append(f"你们的好感提升到了 Lv{up['level']}（{up['title']}）！")
    parts.append(
        "请以角色口吻说一段收尾的话，为这段共同经历画上温柔的句点"
        "（可以回望场景中最打动人的一个瞬间，或轻声约定下一次）。"
        "不要报数值；本轮结束后系统会送出一张纪念卡片。"
    )
    return {"ok": True, "result": result, "observation": " ".join(parts)}


# complete_scene 工具的 JSON Schema。LLM 无需传场景名（turn.py 闭包从当前
# bond_scene 会话 metadata 读取）；memento 为可选的卡面纪念语。
COMPLETE_SCENE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "memento": {
            "type": "string",
            "description": "以角色口吻写的一句纪念语（≤40 字），将镌刻在纪念卡片上，概括本场景最难忘的瞬间",
        },
    },
    "required": [],
}


# ── P4: 角色面多模态生成（Bond-System-Design.md §7）──────────────────
#
# 三重门控（§7.2）：等级门 + 配额门（bond.check_media_quota）+ 内容过滤
# （media_gen.review_prompt，前置、fail-closed）。生成是**工具内阻塞**的
# （10~60s），所以门控通过后立刻发桥接语（send_progress immediate=True，
# 官方途径绕过缓冲秒达）；媒体本体走 Sender 缓冲，回合结束随正文一起 flush。
# 所有失败分支都返回中立 observation，由角色面 LLM 以自己的口吻演绎婉拒
# ——同 give_gift 拒绝分支模式，绝不把异常抛进 agent 循环。

IMAGE_BRIDGING_LINES = (
    "稍等一下哦，我这就画…✍️",
    "嘿嘿，交给我吧！等我一下下…🎨",
    "等我挑个好看的构图，马上就好…",
    "好哦，别眨眼，很快就画完啦…",
)
VOICE_BRIDGING_LINES = (
    "稍等，我清一下嗓子…🎤",
    "让我录一小段给你，等一下下哦…",
    "唔…让我想想用什么语气，稍等…",
)

# 画质正向后缀 + 负面约束（生成侧固定，不进审查——非用户可控部分）
_IMAGE_QUALITY_SUFFIX = "高质量，光影细腻，细节丰富，构图干净"
_IMAGE_NEGATIVE = "多余手指，畸形手部，面部崩坏，模糊，低分辨率，水印，文字，logo"


def build_image_prompt(description: str, anchor: str, display_name: str) -> str:
    """外观锚点全文强制前缀 + 角色名 + 用户描述 + 画质约束（§7.4）。

    Appearance Anchor 是跨时间面部/服饰一致性的锚——永远放在 prompt
    最前面；缺失时优雅降级（仅角色名+描述，一致性弱化但不报错）。
    """
    parts = []
    anchor = (anchor or "").strip()
    if anchor:
        parts.append(f"角色外观（必须严格保持一致）: {anchor}")
    if display_name:
        parts.append(f"角色: {display_name}")
    desc = (description or "").strip()
    if desc:
        parts.append(f"画面: {desc}")
    parts.append(_IMAGE_QUALITY_SUFFIX)
    return "。".join(parts)


def _level_gate_observation(kind: str, level: int, required: int) -> str:
    what = "画画" if kind == "image" else "录语音"
    return (
        f"你和用户的羁绊还不够深（当前 Lv{level}，{what}需要 Lv{required}）。"
        f"请以角色口吻婉转地告诉用户：等关系更亲密一些，你就愿意为 TA {what}了"
        f"（不要报数值，用角色自己的方式表达）。"
    )


async def create_image(
    *,
    user_id: str,
    char_id: str,
    role: str,
    description: str,
    card_content: Optional[str],
    display_name: str,
    bond_manager,
    registry,
    asset_memory,
    review_client=None,
    send_progress: Optional[Callable] = None,
    send_image: Optional[Callable] = None,
) -> str:
    """Generate one in-character image and send it; return the observation.

    去重：同 prompt 在 media_dedupe_ttl_hours 窗口内直接复用旧文件
    （不再计费），但仍记一次日配额（防刷平台）。参考立绘存在时走
    图像编辑端点（I2I 面部一致，§7.4）。
    """
    description = (description or "").strip()
    if not user_id or not char_id:
        return "[Error] 缺少用户或角色上下文，无法画图。"
    if not description:
        return "用户想要一张图但没有说清内容。请以角色口吻问问 TA 想画什么。"

    try:
        # ── 三重门 ──
        ok, level, required = bond_manager.media_level_gate(
            user_id, char_id, "image")
        if not ok:
            return _level_gate_observation("image", level, required)
        if not media_gen.is_image_available():
            return ("画图服务当前不可用（未配置）。请以角色口吻向用户道歉，"
                    "说现在画不了，之后再试试。")
        quota_ok, quota_msg = bond_manager.check_media_quota(
            user_id, char_id, "image", role)
        if not quota_ok:
            if quota_msg.startswith("daily_limit"):
                return ("今天的画图次数已经用完了。请以角色口吻温柔地告诉用户"
                        "明天再来找你画（可以撒个娇），不要报数值。")
            return ("画图服务当前不可用。请以角色口吻向用户道歉，说现在画不了。")

        prompt = build_image_prompt(
            description,
            bond_content.parse_appearance_anchor(card_content or ""),
            display_name,
        )
        phash = prompt_hash_of(prompt)
        model = media_gen.get_model_name("image")

        # ── prompt_hash 去重（窗口内复用旧文件，零计费）──
        try:
            ttl = float(bond_manager.get_config("media_dedupe_ttl_hours", 168))
        except (TypeError, ValueError):
            ttl = 168.0
        hit = asset_memory.find_by_prompt_hash(phash, ttl_hours=ttl)
        if hit is not None and hit.path:
            asset_memory.append_audit(
                user_id, char_id, "image", model, phash, "dedupe")
            bond_manager.record_media_use(user_id, char_id, "image")
            if send_image is not None:
                await send_image(OutImage(path=hit.path,
                                          label=description[:30]))
            return (
                f"你之前画过几乎一样的画（用户请求：{description[:60]}），"
                f"刚把那幅旧作重新发给了用户。请以角色口吻自然地配一句话"
                f"（可以俏皮地说这是你珍藏的旧作，或顺势答应下次画点新的）。"
            )

        # ── 内容过滤（前置，审用户可控部分）──
        review_ok, reason = await media_gen.review_prompt(
            description, review_client)
        if not review_ok:
            asset_memory.append_audit(
                user_id, char_id, "image", model, phash, "review_reject")
            return ("这个画图请求没有通过内容安全审查。请以角色口吻婉拒用户"
                    "（可以说这种画你画不了/不想画），不要提及审查细节。")

        # ── 桥接语（生成阻塞期间秒达）──
        if send_progress is not None:
            try:
                await send_progress(random.choice(IMAGE_BRIDGING_LINES))
            except Exception:  # noqa: BLE001
                pass

        # ── 生成（参考立绘存在 → I2I 编辑端点，面部一致）──
        asset_id = make_asset_id()
        out_path = asset_memory.binary_path(user_id, char_id, asset_id, "png")
        reference = None
        try:
            reference = registry.reference_image_path(char_id)
        except Exception:  # noqa: BLE001
            reference = None
        path = await media_gen.generate_image(
            prompt, out_path=out_path,
            reference_image_path=reference,
            negative_prompt=_IMAGE_NEGATIVE,
        )
        if not path:
            asset_memory.append_audit(
                user_id, char_id, "image", model, phash, "gen_failed")
            return ("画图生成失败了（服务繁忙，或内容未通过平台审核）。"
                    "请以角色口吻向用户道歉，说这次没画成，稍后再试试。")

        # ── 资产卡 + 去重索引 + 审计 + 记账 ──
        card = AssetCard(
            id=asset_id, type="image", char_id=char_id, user_id=user_id,
            path=path, model=model, prompt=prompt,
            tags=["generated"], prompt_hash=phash,
        )
        asset_memory.write_card(card)
        asset_memory.record_prompt_hash(phash, card)
        asset_memory.append_audit(user_id, char_id, "image", model, phash, "ok")
        bond_manager.record_media_use(user_id, char_id, "image")
        if send_image is not None:
            await send_image(OutImage(path=path, label=description[:30]))
        return (
            f"你画好了用户要的图（{description[:60]}）并已发送给用户。"
            f"请以角色口吻自然地配一句话，像是把作品递给 TA"
            f"（可以描述画里的一个小细节，不要报任何编号或数值）。"
        )
    except Exception as e:  # noqa: BLE001
        return f"[Error] 画图工具内部异常: {type(e).__name__}: {e}"


async def send_voice(
    *,
    user_id: str,
    char_id: str,
    role: str,
    text: str,
    bond_manager,
    registry,
    asset_memory,
    review_client=None,
    send_progress: Optional[Callable] = None,
    send_audio: Optional[Callable] = None,
) -> str:
    """Synthesize one in-character voice message (fixed voice_id) and send it.

    voice_id 解析链在 registry.get_voice_id（UGC meta → 卡片注释 →
    voice_pool 确定性分配 → default）——同一角色永远同一声纹（P4 硬要求）。
    """
    text = (text or "").strip()
    if not user_id or not char_id:
        return "[Error] 缺少用户或角色上下文，无法发送语音。"
    if not text:
        return ("用户想听语音但没有说清内容。请以角色口吻问问 TA 想听你说什么，"
                "或告诉 TA 你会用语音回一句话。")

    try:
        ok, level, required = bond_manager.media_level_gate(
            user_id, char_id, "voice")
        if not ok:
            return _level_gate_observation("voice", level, required)
        if not media_gen.is_tts_available():
            return ("语音服务当前不可用（未配置）。请以角色口吻向用户道歉，"
                    "说现在发不了语音，之后再试试。")
        quota_ok, quota_msg = bond_manager.check_media_quota(
            user_id, char_id, "voice", role)
        if not quota_ok:
            if quota_msg.startswith("daily_limit"):
                return ("今天的语音次数已经用完了。请以角色口吻温柔地告诉用户"
                        "明天再来听（可以撒个娇），不要报数值。")
            return ("语音服务当前不可用。请以角色口吻向用户道歉，说现在发不了语音。")

        try:
            max_chars = int(bond_manager.get_config("media_tts_max_chars", 300))
        except (TypeError, ValueError):
            max_chars = 300
        text = text[:max_chars]
        phash = prompt_hash_of(text)
        model = media_gen.get_model_name("voice")

        review_ok, reason = await media_gen.review_prompt(text, review_client)
        if not review_ok:
            asset_memory.append_audit(
                user_id, char_id, "voice", model, phash, "review_reject")
            return ("这段语音内容没有通过内容安全审查。请以角色口吻婉拒用户"
                    "（可以说这种话你说不出口），不要提及审查细节。")

        if send_progress is not None:
            try:
                await send_progress(random.choice(VOICE_BRIDGING_LINES))
            except Exception:  # noqa: BLE001
                pass

        voice_id = ""
        try:
            voice_id = registry.get_voice_id(char_id) or ""
        except Exception:  # noqa: BLE001
            voice_id = ""

        asset_id = make_asset_id()
        out_path = asset_memory.binary_path(user_id, char_id, asset_id, "mp3")
        path = await media_gen.generate_speech(
            text, voice_id, out_path=out_path, max_chars=max_chars)
        if not path:
            asset_memory.append_audit(
                user_id, char_id, "voice", model, phash, "gen_failed")
            return ("语音合成失败了（服务繁忙，或内容未通过平台审核）。"
                    "请以角色口吻向用户道歉，说这次没录成，稍后再试试。")

        card = AssetCard(
            id=asset_id, type="voice", char_id=char_id, user_id=user_id,
            path=path, model=model, transcript=text, prompt=text,
            tags=["generated"], prompt_hash=phash,
        )
        asset_memory.write_card(card)
        asset_memory.record_prompt_hash(phash, card)
        asset_memory.append_audit(user_id, char_id, "voice", model, phash, "ok")
        bond_manager.record_media_use(user_id, char_id, "voice")
        if send_audio is not None:
            await send_audio(OutAudio(path=path, label=text[:20]))
        return (
            f"你刚刚用自己的声音录了一段语音发给用户，内容是：「{text[:80]}」。"
            f"请以角色口吻自然地补一句话（不要整段重复语音内容，可以点一下"
            f"「刚才那段语音」）。"
        )
    except Exception as e:  # noqa: BLE001
        return f"[Error] 语音工具内部异常: {type(e).__name__}: {e}"


# create_image / send_voice 工具的 JSON Schema（角色面 allowed_tools 下发）
CREATE_IMAGE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": "要画的内容描述（从用户请求提炼：场景/动作/表情/服装等，"
                           "≤100字；角色外观由系统自动附加，无需重复描述长相）",
        },
    },
    "required": ["description"],
}

SEND_VOICE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "要以角色声音说出的话（用你自己的口吻写好完整台词，"
                           "≤300字；系统会用你固定的音色合成语音发给用户）",
        },
    },
    "required": ["text"],
}
