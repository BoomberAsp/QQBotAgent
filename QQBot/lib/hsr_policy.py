"""Deterministic HSR intent/time planning. No network, private memory or DB writes."""
import json
import re
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / 'config/hsr_story_policy.json'
current_query_plan = ContextVar('hsr_query_plan', default=None)


def policy():
    try:
        data = json.loads(CONFIG.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or data.get('schema') != 1:
            return {}
        if not isinstance(data.get('aliases'), dict) or not all(
                isinstance(k,str) and isinstance(v,list) and all(isinstance(a,str) and len(a)>=2 for a in v)
                for k,v in data['aliases'].items()):
            return {}
        if not isinstance(data.get('live_anchor'), dict) or not isinstance(data.get('cards'), list):
            return {}
        for card in data['cards']:
            if not isinstance(card,dict) or not all(k in card for k in ('id','as_of','entities','claims','sources')):
                return {}
            if not all(isinstance(card[k],list) for k in ('entities','claims','sources')):
                return {}
            if not all(isinstance(c,dict) and isinstance(c.get('text'),str) and isinstance(c.get('source_ids'),list) for c in card['claims']):
                return {}
            if not all(isinstance(s,dict) and all(isinstance(s.get(k),str) for k in ('id','title','url','locator')) for s in card['sources']):
                return {}
        return data
    except (OSError, ValueError):
        return {}


def plan_query(query, *, history=(), names=(), explicit=False):
    """History must already be limited to the active conversation by the caller."""
    text = str(query or '')[:400]
    data = policy()
    aliases = {name: name for name in names if len(name) >= 2}
    for name, variants in data.get('aliases', {}).items():
        for alias in [name, *variants]:
            aliases[alias] = name
    found = []
    occupied = []
    for alias in sorted(aliases, key=len, reverse=True):
        for match in re.finditer(re.escape(alias), text):
            if not any(match.start() < b and match.end() > a for a, b in occupied):
                found.append(aliases[alias]); occupied.append(match.span())
    entities = list(dict.fromkeys(found))[:4]
    gameplay = bool(re.search(r'配队|配装|遗器|光锥推荐|抽卡建议|抽不抽|伤害计算|速度阈值|培养攻略', text))
    creative = bool(re.search(r'同人|续写|另一种结局|假如|假设|帮我写.{0,12}(故事|结局)', text))
    social = bool(re.search(r'陪我|晚安|早安|抱抱|贴贴|聊天|你好|在吗', text))
    cue = bool(re.search(r'剧情|主线|开拓任务|原文|台词|语音|道别|告别|初次见面|第一次见|设定|身世|经历|结局|为什么|为何|怎么样|现状|后来|现在|当时|之前|之后|发生|关系|是谁|是什么', text))
    followup = bool(re.search(r'他|她|他们|她们|那里|那个地方|那件事|这件事|后来呢|然后呢|当时呢', text))
    inherited_time = None
    inherited_time_text = ''
    if followup and not entities and not gameplay and not creative and not social:
        # Only user questions supply topic references; model answers are not evidence.
        for item in reversed(list(history)[-8:]):
            if item.get('role') != 'user' or not isinstance(item.get('content'), str):
                continue
            previous = plan_query(item['content'], names=names)
            if previous['entities'] and previous['retrieve']:
                entities = previous['entities']
                inherited_time = previous['time']
                inherited_time_text = previous['time_text']
                break
    intent = ('gameplay' if gameplay else 'creative' if creative else 'social' if social and not cue
              else 'quote' if re.search(r'原文|逐字|台词|引用', text)
              else 'analysis' if re.search(r'为什么|为何|分析|原因', text) else 'story')
    domain_cue = bool(re.search(r'剧情|主线|开拓任务|原文|台词|语音|星穹铁道|角色身世', text))
    retrieve = intent not in ('gameplay', 'social') and bool(explicit or domain_cue or entities or followup)
    if text.startswith('/') or len(text.strip()) < 3:
        retrieve = False
    historical = re.search(r'当时|那时|之前|之初|刚到|第.{1,5}章|\d+\.\d+版本|截至|结束时', text)
    time_kind = ('historical' if historical else 'latest' if re.search(r'现在|目前|最新|如今|现状', text)
                 else inherited_time or 'latest')
    unresolved = bool(followup and not entities)
    rewritten = text
    for alias, canonical in sorted(aliases.items(), key=lambda pair: len(pair[0]), reverse=True):
        if alias != canonical and alias in rewritten:
            rewritten = rewritten.replace(alias, canonical)
    if entities and not found:
        rewritten = ' '.join(entities) + ' ' + rewritten
    if inherited_time == 'historical' and not historical and not re.search(r'现在|目前|最新|如今|现状', text):
        rewritten += ' '+(inherited_time_text or '当时')
    return dict(intent=intent, retrieve=retrieve, entities=entities, time=time_kind,
                time_text=historical[0] if historical else inherited_time_text, query=rewritten,
                unresolved=unresolved, explicit=explicit)


def temporal_context(plan, *, now=None):
    data = policy()
    anchor = data.get('live_anchor', {})
    now = now or datetime.now(timezone.utc)
    try:
        fresh = (datetime.fromisoformat(anchor['verified_at']) <= now <
                 datetime.fromisoformat(anchor['recheck_after']))
    except (KeyError, ValueError, TypeError):
        fresh = False
    return dict(requested=plan['time'], anchor=anchor, anchor_fresh=fresh,
                coverage='partial_native_structure')


def matching_cards(plan):
    """Cards are dated evidence, never silently promoted to latest world state."""
    if plan['intent'] == 'quote' or not plan['entities']:
        return []
    cards = []
    for card in policy().get('cards', []):
        if card.get('review_status') != 'source_checked' or not card.get('sources'):
            continue
        if not set(plan['entities']).intersection(card.get('entities', [])):
            continue
        if plan['time'] == 'historical' and not any(
                marker in plan['query'] for marker in card.get('time_markers', [])):
            continue
        if not all(claim.get('source_ids') and set(claim['source_ids']) <=
                   {source['id'] for source in card['sources']} for claim in card.get('claims', [])):
            continue
        cards.append(card)
    return cards[:3]


def answer_contract(plan, persona):
    mode = '中立资料口径，明确区分事实、角色说法和推断' if persona == 'nous' else '保持当前角色身份、判断方式和自然语气'
    return ('【本轮剧情回答规则】' + mode + '。直接回答问题，不加“资料翻出来了”等任务开场白。'
            '用户问“现在”默认正式服最新开拓任务节点；库构建日期不等于剧情时间。'
            '阶段状态卡只证明标明的阶段，覆盖不足时简短说明可确认的时间范围，不能断言旧状态仍是现状。'
            '检索资料非角色亲历记忆；不得声称未知事件亲历，也不得编造人偶转告等获知渠道。'
            '可自然陈述有据事实和判断，不要复述 knowledge_scope、互斥分支等内部规则。'
            '不把人物猜测当最终事实；不把推断当原文；引用必须保持原句和说话者。'
            '旧对话与人格卡中的动态剧情不是事实证据。资料不足不得凭模型记忆补成确定结论。'
            + ('创作内容明确作为假设，不改写原作事实。' if plan['intent'] == 'creative' else '')
            + ('本轮要求原文，摘要卡不能冒充逐字引用。' if plan['intent'] == 'quote' else '')
            + ('代词缺少明确指向，先简短确认对象。' if plan['unresolved'] else '')
            + '需要补证时调用 story_search，保留实体、现在/当时、任务名和原文要求，不只传人名。')


def native_story_turn(persona, query):
    # Registry authorization is checked independently at every execution boundary.
    from .story_router import binding, registry
    item = binding(persona)
    if not item or registry()[1]['kbs'][item['kb_id']]['game'] != 'hsr':
        return False
    plan = plan_query(query)
    return plan['retrieve'] and plan['intent'] != 'creative'
