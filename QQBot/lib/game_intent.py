"""Intent for questions addressed to a persona with a bound game."""
import re


GAMEPLAY_TOOLS = frozenset({
    "character_detail", "bond_detail", "calculate_speed", "parse_battle_screenshots",
})
_MECHANICS = re.compile(
    r"玩法|攻略|游戏机制|配队|配装|装备推荐|抽不抽|值得抽|值得养|怎么养|怎么打|怎么配|强度|倍率|"
    r"面板|词条|材料|升星|培养|突破|数值|属性|战意|速度阈值|伤害计算|遗器|光锥推荐"
)
_SKILL = re.compile(r"技能|羁绊效果")
_STORY = re.compile(r"剧情|世界观|主线|支线|身世|设定|经历|结局|后来发生|人物关系|台词|对白|出场")
_RELATIONSHIP = re.compile(
    r"(?:认识|认得|见过|听说过)(?P<name>[\w·・ -]{2,24}?)(?:吗|么|嘛|[?？]|$)"
    r"|(?:和|跟).{2,20}(?:什么关系|怎么认识|熟吗)"
)
_GENERIC_TARGETS = {"我们", "自己", "你们", "大家", "他们", "她们", "这人", "我吗", "你吗"}


def game_intent(text, *, in_character=True):
    """Classify explicit game cues and in-character relationship questions."""
    text = (text or "").strip()
    if not text or text.startswith(("/", "#", "／")):
        return ""
    story = False
    gameplay = False
    for clause in re.split(r"[。！!；;\n]|[？?](?=\s*\S)", text):
        match = _RELATIONSHIP.search(clause) if in_character else None
        relationship = match and (match["name"] or "") not in _GENERIC_TARGETS
        story_clause = bool(_STORY.search(clause) or relationship)
        story |= story_clause
        gameplay |= bool(_MECHANICS.search(clause) or (not story_clause and _SKILL.search(clause)))
    if story and gameplay:
        return "mixed"
    return "story" if story else "gameplay" if gameplay else ""


def game_reply_rule(intent):
    if intent == "story":
        return (
            "本轮问的是原作人物、关系或剧情。以当前角色口吻先回应人物身份、关系或剧情经历，"
            "需要核对时查绑定作品的 story_search。技能、属性、配队和抽卡信息留给玩法问题。"
            "资料有明确在场证据才说自己认识或经历过；查不到就说不确定，避免编造交情或剧情。"
        )
    if intent == "gameplay":
        return "本轮问的是游戏玩法。先给技能、数值或实际操作的答案，可用玩法工具核实并保持当前语气。"
    if intent == "mixed":
        return "本轮同时问剧情和玩法。按用户问题顺序分别回答，标明哪些是原作世界观，哪些是游戏机制。"
    if intent == "undecided":
        return "像「某角色怎么样」这种问法，用上文判断是在聊原作还是玩法；两者都说得通时，先问一句想了解哪方面。"
    return ""
