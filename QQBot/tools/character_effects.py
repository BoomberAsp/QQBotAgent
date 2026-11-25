"""Read-only Ark character lookup by the effects in skills and applied statuses."""
import asyncio
import json
import unicodedata

from lib.gamedata_source import get_source
from lib.wiki_catalog import WikiCatalog

EFFECT_NAMES = {
    'immunity': ('免疫', '免疫buff', '免疫状态', 'immune'),
    'hit_rate': ('命中率', '命中率提升', '普通命中率', '基础命中率', 'acc', 'hitrate'),
    'effect_hit': ('效果命中', '效果命中率', '状态命中', '状态命中率', 'effecthitrate'),
}
OPERATIONS = ('auto', 'grant', 'increase', 'decrease', 'remove', 'dispel', 'any')
TARGETS = ('any', 'self', 'allies', 'team', 'enemy')
ACTIVATIONS = ('any', 'constant', 'conditional')
RELEASE_STATES = ('released', 'unreleased', 'unknown', 'all')
ROLE_ALIASES = {'安泰西娅': 'H118'}


def _key(value):
    return ''.join(ch for ch in unicodedata.normalize('NFKC', value).casefold() if ch.isalnum())


def _error(message):
    return json.dumps({'error': message}, ensure_ascii=False)


def _matches(row, effect, operation, target, activation, status, roles):
    if effect and row['effect'] != effect:
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
    if status:
        return any(status in {_key(node['id']), _key(node.get('name', '')),
                              *(_key(n) for n in node.get('names', {}).values())}
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


def _search(effect, operation, target, activation, status, role, release_status, offset, limit):
    source = get_source()
    index = source.character_effects
    if not index:
        return _error('当前 Ark 数据版本未载入角色效果索引。')
    catalog = {row['id']: row for row in WikiCatalog(game_dir=source.base_dir).query()['items'] if row.get('id')}
    role_ids = _role_ids(role, index['roles'], catalog)
    groups = {}
    filtered_release = set()
    for row in index['records']:
        if not _matches(row, effect, operation, target, activation, status, role_ids):
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
        not any(r['activation'] == 'constant' for r in group['effects']), group['id']))
    items = []
    for group in roles[offset:offset + limit]:
        summaries = []
        for row in group['effects'][:6]:
            path = [{k: n[k] for k in ('kind', 'id', 'name', 'level', 'skill_id') if k in n}
                    for n in row['path']]
            if status:
                for item, node in zip(path, row['path']):
                    descriptions = node.get('descriptions') or {}
                    if descriptions:
                        item['description'] = descriptions.get('zh_hans') or descriptions.get('en')
            summaries.append({**{k: row[k] for k in ('effect', 'operation', 'target', 'value', 'activation', 'conditions')},
                              'path': path,
                              'source': [{k: ev[k] for k in ('table', 'row', 'column')} for ev in row['evidence']]})
        items.append({**group, 'matched_evidence': len(group['effects']), 'effects': summaries})
    return json.dumps({'game_build': index['game_build'], 'coverage': index.get('coverage', {}),
                       'matched': len(roles), 'filtered_by_release_status': len(filtered_release),
                       'next_offset': offset + limit if offset + limit < len(roles) else None,
                       'items': items}, ensure_ascii=False)


async def search_character_effects(effect='', operation='auto', target='any', activation='any',
                                   status='', role='', release_status='released', offset=0, limit=8):
    from lib.story_router import active, current_story_scope

    scope = current_story_scope.get()
    if scope is not None and (scope.game != 'ark_re' or not active(scope)):
        return _error('本轮没有有效的 Ark 资料作用域。')
    if not all(isinstance(val, str) for val in (effect, operation, target, activation, status, role, release_status)):
        return _error('效果、操作、目标、触发、状态、角色和上线状态须为文本。')
    if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 20:
        return _error('offset 须为非负整数，limit 须在 1 到 20 之间。')
    if operation not in OPERATIONS or target not in TARGETS or activation not in ACTIVATIONS or release_status not in RELEASE_STATES:
        return _error('不支持的操作、目标、触发或上线状态筛选。')
    term = _key(effect)
    canonical = next((name for name, aliases in EFFECT_NAMES.items() if term in {_key(name), *map(_key, aliases)}), '')
    if effect.strip() and not canonical:
        return _error('目前支持免疫、普通命中率和效果命中；仅说「命中」时请确认所指属性。')
    status_key = _key(status)
    if not canonical and not status_key:
        return _error('请指定效果或特殊 Buff 名称。')
    if operation == 'auto':
        operation = 'grant' if canonical == 'immunity' else 'increase' if canonical else 'any'
    result = await asyncio.to_thread(_search, canonical, operation, target, activation, status_key,
                                     role, release_status, offset, limit)
    return result if scope is None or active(scope) else _error('本轮 Ark 资料作用域已失效。')
