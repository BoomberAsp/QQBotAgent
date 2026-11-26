"""Read-only Ark character lookup by the effects in skills and applied statuses."""
import asyncio
import json
import unicodedata

from lib.gamedata_source import LEGACY_LABEL_TO_ID, get_source
from lib.wiki_catalog import WikiCatalog

EFFECT_NAMES = {
    'immunity': ('免疫', '免疫buff', '免疫状态', 'immune'),
    'hit_rate': ('命中率', '命中率提升', '普通命中率', '基础命中率', 'acc', 'hitrate'),
    'effect_hit': ('效果命中', '效果命中率', '状态命中', '状态命中率', 'effecthitrate'),
    'status': ('状态', '施加状态', 'buff', 'debuff'),
    'dispel': ('驱散', '净化', '解除状态', '解除正向状态', '解除负向状态'),
    'cooldown': ('冷却', '冷却延长', '延长技能冷却', '技能延长'),
    'status_duration': ('状态回合', '状态延长', '延长状态', '状态持续时间'),
    'action_gauge': ('行动值', '行动值提升', '行动值降低', '拉条', '推条'),
}
OPERATIONS = ('auto', 'grant', 'increase', 'decrease', 'remove', 'dispel', 'any')
TARGETS = ('any', 'self', 'allies', 'team', 'enemy')
ACTIVATIONS = ('any', 'constant', 'conditional')
RELEASE_STATES = ('released', 'unreleased', 'unknown', 'all')
POLARITIES = ('any', 'positive', 'negative', 'unclassified')
ALIAS_OPERATION = {'拉条': 'increase', '行动值提升': 'increase', '推条': 'decrease', '行动值降低': 'decrease'}
ALIAS_POLARITY = {'buff': 'positive', 'debuff': 'negative', '净化': 'negative', '解除负向状态': 'negative', '解除正向状态': 'positive'}
ROLE_ALIASES = {'安泰西娅': 'H118'}


def _key(value):
    return ''.join(ch for ch in unicodedata.normalize('NFKC', value).casefold() if ch.isalnum())


def _error(message):
    return json.dumps({'error': message}, ensure_ascii=False)


def _status_keys(sid, data):
    return {_key(sid), *(_key(n) for n in data.get('names', {}).values())}


def _matches(row, effect, operation, target, activation, status, statuses, roles, polarity):
    kind = row['effect']
    compatible_dispel = effect == 'dispel' and kind == 'status' and row['operation'] == 'remove'
    can_cleanse_status = kind == 'dispel' and operation in {'remove', 'dispel'} and any(
        item['polarity'] == row.get('polarity') and item.get('removable') is True for item in statuses.values())
    if effect and kind != effect and not compatible_dispel and not (effect == 'status' and can_cleanse_status):
        return False
    if polarity != 'any' and row.get('polarity') != polarity:
        return False
    if operation != 'any' and row['operation'] != operation:
        if (operation, row['operation']) != ('remove', 'dispel'):
            return False
    row_target = row['target']
    if target == 'allies' and row_target not in {'ally', 'allies', 'team'}:
        return False
    if target not in {'any', 'allies'} and row_target != target:
        return False
    if activation != 'any' and row['activation'] != activation:
        return False
    if roles is not None and row['role_id'] not in roles:
        return False
    if status and not can_cleanse_status:
        if row.get('status_id'):
            return row['status_id'] in statuses
        return any(node['id'] in statuses or status in {_key(node.get('name', '')), *_status_keys(node['id'], node)}
                   for node in row['path'] if node['kind'] == 'status')
    return True


def _role_ids(query, source, catalog):
    key = _key(query)
    if not key:
        return None
    resolved = ROLE_ALIASES.get(key)
    matching = {rid for rid, role in source.items() if key in {
        _key(rid), *(_key(n) for n in role['names'].values()), *(_key(n) for n in role['aliases'])}}
    if resolved:
        matching.add(resolved)
    catalog_names = {rid for rid, row in catalog.items() if key == _key(row.get('name_cn') or '')}
    return matching | catalog_names


def _search(effect, operation, target, activation, status, role, release_status, offset, limit, polarity):
    source = get_source()
    index = source.character_effects
    if not index:
        return _error('当前 Ark 数据版本未载入角色效果索引。')
    schema = index.get('schema', 1)
    if schema == 1 and (effect in {'status', 'dispel', 'cooldown', 'status_duration', 'action_gauge'} or polarity != 'any'):
        return _error('当前 Ark 索引是旧版。请载入 v2 效果索引再查询这些效果。')
    status_catalog = index.get('statuses', {})
    aliases = {sid for label, sid in LEGACY_LABEL_TO_ID.items() if status and status == _key(label)}
    statuses = {sid: item for sid, item in status_catalog.items()
                if status and (sid in aliases or status in _status_keys(sid, item))}
    catalog = {row['id']: row for row in WikiCatalog(game_dir=source.base_dir).query()['items'] if row.get('id')}
    role_ids = _role_ids(role, index['roles'], catalog)
    groups = {}
    filtered_release = set()
    for row in index['records']:
        if not _matches(row, effect, operation, target, activation, status, statuses, role_ids, polarity):
            continue
        role_id = row['role_id']
        entry = catalog.get(role_id) or {}
        state = entry.get('release_status') or 'unknown'
        if release_status != 'all' and state != release_status:
            filtered_release.add(role_id)
            continue
        names = index['roles'].get(role_id, {}).get('names', {})
        name = entry.get('name_cn') or names.get('zh_hans') or names.get('en') or role_id
        group = groups.setdefault(role_id, {'id': role_id, 'name': name, 'release_status': state, 'effects': []})
        group['effects'].append(row)

    roles = sorted(groups.values(), key=lambda group: (
        not any(r['target'] != 'unknown' for r in group['effects']),
        not any(r['activation'] == 'constant' for r in group['effects']), group['id']))
    items = []
    for group in roles[offset:offset + limit]:
        summaries = []
        evidence = sorted(group['effects'], key=lambda row: (row['target'] == 'unknown', row['operation'] == 'dispel'))
        for row in evidence[:6]:
            path = [{k: n[k] for k in ('kind', 'id', 'name', 'level', 'skill_id') if k in n}
                    for n in row['path']]
            if status:
                for item, node in zip(path, row['path']):
                    metadata = status_catalog.get(node['id'], node) if node['kind'] == 'status' else {}
                    descriptions = metadata.get('descriptions') or {}
                    if descriptions:
                        item['description'] = descriptions.get('zh_hans') or descriptions.get('en')
            summaries.append({**{k: row[k] for k in ('effect', 'operation', 'target', 'value', 'activation', 'conditions',
                                                    'polarity', 'status_id') if k in row},
                              'path': path,
                              'source': [{k: ev[k] for k in ('table', 'row', 'column')} for ev in row['evidence']]})
        items.append({**group, 'matched_evidence': len(group['effects']), 'effects': summaries})
    result = {'game_build': index['game_build'], 'schema': schema, 'coverage': index.get('coverage', {}),
                       'matched': len(roles), 'filtered_by_release_status': len(filtered_release),
                       'next_offset': offset + limit if offset + limit < len(roles) else None,
                       'items': items}
    if status and schema == 2:
        result['status_definition_count'] = len(statuses)
        result['status_definitions'] = [dict(id=sid, **entry) for sid, entry in list(statuses.items())[:20]]
    return json.dumps(result, ensure_ascii=False)


async def search_character_effects(effect='', operation='auto', target='any', activation='any',
                                   status='', role='', release_status='released', offset=0, limit=8, polarity='any'):
    from lib.story_router import active, current_story_scope

    scope = current_story_scope.get()
    if scope is not None and (scope.game != 'ark_re' or not active(scope)):
        return _error('本轮没有有效的 Ark 资料作用域。')
    if not all(isinstance(val, str) for val in (effect, operation, target, activation, status, role, release_status, polarity)):
        return _error('效果、操作、目标、触发、状态、角色、上线状态和状态正负分类须为文本。')
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 20:
        return _error('offset 须为非负整数，limit 须在 1 到 20 之间。')
    if (operation not in OPERATIONS or target not in TARGETS or activation not in ACTIVATIONS
            or release_status not in RELEASE_STATES or polarity not in POLARITIES):
        return _error('不支持的操作、目标、触发、上线状态或状态正负分类筛选。')
    term = _key(effect)
    canonical = next((name for name, aliases in EFFECT_NAMES.items() if term in {_key(name), *map(_key, aliases)}), '')
    if effect.strip() and not canonical:
        return _error('请使用支持的效果名称。任意正向或负向状态可直接在 status 传状态名或 ID。')
    status_key = _key(status)
    if not canonical and not status_key:
        return _error('请指定效果或特殊 Buff 名称。')
    if operation == 'auto':
        if canonical in {'immunity', 'status'}:
            operation = 'grant'
        elif canonical in {'hit_rate', 'effect_hit', 'cooldown', 'status_duration'}:
            operation = 'increase'
        else:
            operation = ALIAS_OPERATION.get(term, 'any')
    if polarity == 'any':
        polarity = ALIAS_POLARITY.get(term, 'any')
    result = await asyncio.to_thread(_search, canonical, operation, target, activation, status_key,
                                     role, release_status, offset, limit, polarity)
    return result if scope is None or active(scope) else _error('本轮 Ark 资料作用域已失效。')
