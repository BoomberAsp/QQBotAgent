"""Reviewed roleplay permissions tied to an immutable HSR build and exact chunks."""
import json
import re
from functools import lru_cache
from pathlib import Path

CONFIG = Path(__file__).resolve().parents[1] / 'config/hsr_herta_perspective.json'
MODES = {
    'firsthand': '本人自述，只可回忆标注所指的经历',
    'informed': '明确获知，只可描述标注所指的内容和渠道',
    'viewpoint': '可供判断，可表达有据的态度与推断',
    'unknown': '视角未知，只可转述有据部分，不得声称亲历或补写消息来源',
}
ALIASES = {'黑塔人偶': '黑塔人偶', '阮·梅': '阮·梅', '阮梅': '阮·梅', '阮•梅': '阮·梅',
           '艾丝妲': '艾丝妲', '螺丝咕姆': '螺丝咕姆', '银狼': '银狼', '姬子': '姬子', '瓦尔特': '瓦尔特',
           '模拟宇宙': '模拟宇宙', '旧瓶新友': '旧瓶新友', '魔女的科学剩宴': '魔女的科学剩宴'}


@lru_cache(maxsize=1)
def profile(build_id):
    try:
        data = json.loads(CONFIG.read_text(encoding='utf-8'))
        if data['schema'] != 1 or data['build_id'] != build_id or data['persona'] != 'herta':
            return None
        records = data['evidence']
        for record in records.values():
            if record['mode'] not in MODES or not record['scope'] or not record['kind']:
                raise ValueError('Invalid HSR perspective evidence')
        for topic in data['topics']:
            re.compile(topic['pattern'])
            if not set(topic['chunks']) <= records.keys():
                raise ValueError('Missing HSR perspective evidence')
        return data
    except (OSError, ValueError, KeyError, TypeError, AttributeError, re.error):
        return None


def relevant_chunks(query, data):
    if data is None:
        return ()
    return tuple(dict.fromkeys(chunk for topic in data['topics'] if re.search(topic['pattern'], query)
                               for chunk in topic['chunks']))[:6]


def query_plan(query, data=None, **kwargs):
    from .hsr_policy import plan_query

    plan = plan_query(query, alias_overrides=ALIASES if data else None, **kwargs)
    if data is None or plan['intent'] in {'quote', 'gameplay', 'creative'}:
        return plan
    if str(query).lstrip().startswith('/'):
        return plan
    if re.search(r'你.{0,12}(?:亲眼|亲历|在场)|你(?:当时|那时)(?:在|见到|看到)', str(query)):
        if not plan['entities']:
            resolved = plan_query('那件事 ' + str(query), alias_overrides=ALIASES, **kwargs)
            plan.update(query=resolved['query'] if resolved['entities'] else plan['query'],
                        entities=resolved['entities'], unresolved=not resolved['entities'])
        plan.update(retrieve=True, character_witness=True)
        return plan
    if plan['time'] == 'historical' or re.search(r'目前|最新|如今|现状|结局|后来|现在怎么样', plan['query']):
        return plan
    if not relevant_chunks(plan['query'], data):
        return plan
    people = set(plan['entities']) & {'阮·梅', '艾丝妲', '螺丝咕姆', '银狼', '姬子', '瓦尔特'}
    if len(people) > 1:
        return plan
    plan.update(retrieve=True, unresolved=False, character_context=True)
    return plan


def annotate(hit, data):
    record = data['evidence'].get(hit['chunk_id'])
    if record and record['kind'] == hit['kind'] and record['speaker'] == hit['speaker_raw']:
        return record['mode'], record['scope']
    return 'unknown', ''


def instructions(plan):
    rule = ('直接回应开拓者。大黑塔的判断可以体现她关注的变量、对当事人的评价或下一步会怎么做。'
            '未被问起的原作台词用自然转述，不模仿逐字引语。用户问的是自己的事，就围绕当下回应。'
            '回忆具体事件时，只使用标为本人自述的对应内容；明确获知只按标注叙述。'
            '场景、时点、次数和获知渠道逐项受本轮证据约束；角色卡或历史里额外的细节不能补进具体叙事。'
            '人偶对白、玩家选项与相邻对白各有独立主体。除非当前会话已经建立，别虚构和用户的既有经历。'
            '视角和来源标签供你判断；用户要求出处或原文时给自然可读的任务、语音或档案来源。')
    if plan.get('character_witness'):
        rule += '这轮在问你是否亲历具体事件。无对应的本人在场证据时，用一句角色口吻说明能确认到哪一步。'
    return rule
