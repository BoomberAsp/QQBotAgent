"""Intent for questions addressed to a persona with a bound game."""
import re


GAMEPLAY_TOOLS = frozenset({
    "character_detail", "list_characters", "bond_detail", "calculate_speed", "parse_battle_screenshots",
    "search_character_effects",
})
_MECHANICS = re.compile(
    r"玩法|攻略|游戏机制|配队|配装|装备推荐|抽不抽|值得抽|值得养|怎么养|怎么打|怎么配|强度|倍率|"
    r"面板|词条|材料|升星|培养|突破|数值|属性|战意|速度阈值|伤害计算|遗器|光锥推荐"
)
_SKILL = re.compile(r"技能|羁绊效果")
_EFFECT = re.compile(r"免疫|命中率|效果命中|状态命中|基础命中|普通命中")
_EFFECT_LOOKUP = re.compile(r"(?:哪些|什么|有哪|筛选).{0,24}角色|角色.{0,24}(?:哪些|有谁)|谁.{0,12}(?:能给|能解除|可驱散|自带)|反查|免疫\s*buff", re.I)
_RELEASE = re.compile(r"卡池|复刻|上线时间|实装时间|(?:何时|什么时候|几号)(?:上线|实装|出(?=[？?\s]|$))|上线了吗|实装了吗")
_STORY = re.compile(r"剧情|世界观|主线|支线|身世|设定|经历|结局|后来发生|人物关系|台词|对白|出场")
_RELATIONSHIP = re.compile(
    r"(?:认识|认得|见过|听说过)(?P<name>[\w·・ -]{2,24}?)(?:吗|么|嘛|[?？]|$)"
    r"|(?:和|跟).{2,20}(?:什么关系|怎么认识|熟吗)"
)
_GENERIC_TARGETS = {"我们", "自己", "你们", "大家", "他们", "她们", "这人", "我吗", "你吗"}
_QUESTION_END = re.compile(
    r"(?:最近|现在|如今|后来|最后|目前)?(?:过得)?"
    r"(?:怎么样了?|如何|咋样了?|怎么了|还好吗|还活着吗|在哪(?:里)?|去哪了|是谁|是什么样的人|有消息吗|呢)$"
)
_TIME_CUE = re.compile(r"最近|现在|如今|后来|最后|目前")
_REFERENCE = {"他", "她", "它", "这人", "那人", "这位", "那位", "这件事", "那件事", "这个角色", "那个角色"}
_ORDINARY_SUBJECT = {"我", "你", "大家", "我们", "你们", "今天", "明天", "昨天", "天气", "今天天气"}


def game_intent(text, *, in_character=True, game='ark_re'):
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
        effect_lookup = game == 'ark_re' and _EFFECT.search(clause) and _EFFECT_LOOKUP.search(clause)
        gameplay |= bool(_MECHANICS.search(clause) or effect_lookup
                         or (not story_clause and (_SKILL.search(clause) or _RELEASE.search(clause))))
    if story and gameplay:
        return "mixed"
    return "story" if story else "gameplay" if gameplay else ""


def _mentioned_entities(text, entities):
    """Keep distinct mentions and prefer a full title over an overlapping short name."""
    found, spans = set(), []
    text = text.casefold()
    for name in sorted(entities, key=lambda item: (-len(item), item)):
        pattern = re.escape(name)
        if name.isascii():
            pattern = r"(?<![a-z0-9_])" + pattern + r"(?![a-z0-9_])"
        for match in re.finditer(pattern, text):
            start, end = match.span()
            if not any(start < right and end > left for left, right in spans):
                spans.append((start, end))
                found.add(entities[name])
    return found


def classify_group_followup(text, *, entity_terms, related_context, game='ark_re'):
    """Resolve undecided turns using the bound corpus and one scoped conversation anchor."""
    phrase = re.sub(r"^(?:请问|想问(?:一下)?|你觉得)[：:\s]*", "", text.strip()).rstrip("？?。！! ")
    if not phrase or len(phrase) > 80 or phrase.startswith(("/", "#", "／")):
        return "undecided", text
    suffix = _QUESTION_END.search(phrase)
    subject = (phrase[:suffix.start()] if suffix else phrase).strip(" 「」『』《》\"“”")
    stripped = re.sub(r"^那(?:么)?\s*", "", subject)
    if subject in _ORDINARY_SUBJECT or stripped in _ORDINARY_SUBJECT:
        return "undecided", text

    entities = {name.casefold(): name for name in entity_terms() if len(name) >= 2}
    target = entities.get(subject.casefold()) or entities.get(stripped.casefold())
    reference = bool(suffix and (stripped in _REFERENCE or (not stripped and phrase.endswith("呢"))))
    if not (target or reference):
        return "undecided", text

    time_cue = _TIME_CUE.match(suffix[0]) if suffix else None
    time_context = f" {suffix[0]}" if time_cue else ""
    anchor = related_context()
    if target:
        same_target = target.casefold() in anchor.casefold()
        continuation = (stripped != subject and target.casefold() != subject.casefold()) or phrase.endswith("呢")
        if (same_target or continuation) and game_intent(anchor, in_character=False, game=game) == "gameplay":
            return "gameplay", text
        return "story", target + time_context

    if not anchor:
        return "undecided", text
    # Without a story entity or explicit game signal this may be an ordinary
    # conversation about a real person. Keep the ordinary model path in that case.
    matches = _mentioned_entities(anchor, entities)
    prior = game_intent(anchor, in_character=False, game=game)
    if len(matches) != 1:
        return ("clarify" if matches or prior else "undecided"), text
    return ("gameplay" if prior == "gameplay" else "story"), next(iter(matches)) + time_context


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
        return ("如果确实在聊作品，先按人物或事件的剧情理解。结合被回复的消息或当前发言人的紧邻追问判断；"
                "同一话题明确在聊玩法时按玩法理解。对象或话题有歧义时，先问一句再使用相应的工具。普通聊天照常回应。")
    if intent == "clarify":
        return "这条游戏追问所指的人物或事件还不明确。先用一句话确认对象，等用户确认再查资料。"
    return ""
