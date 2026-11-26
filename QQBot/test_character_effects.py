import asyncio
import json
from types import SimpleNamespace

from lib import gamedata_source, story_router
from lib.game_intent import game_intent
from tools import character_effects


def test_effect_search_filters_mechanism_owner_activation_and_release(monkeypatch):
    def effect(rid, name, operation, target, activation='conditional', status=None):
        path = [{'kind': 'role', 'id': rid}]
        if status:
            path.append({'kind': 'status', 'id': 'H118S2', 'name': status})
        return dict(role_id=rid, effect=name, operation=operation, target=target, activation=activation,
                    value=0.5, conditions=[], path=path,
                    evidence=[dict(table='Status', row='example', column='DynamicField1')])

    names = {'H118': '安泰西亚', 'H002': '同名角色', 'H003': '同名角色', 'H004': '未上线角色'}
    active = {rid: {'id': rid, 'name_cn': name, 'release_status': 'released' if rid != 'H004' else 'unreleased'}
              for rid, name in names.items()}
    records = [effect('H118', 'hit_rate', 'increase', 'self', status='自视甚高的欺侮'),
               effect('H002', 'hit_rate', 'decrease', 'enemy'), effect('H003', 'effect_hit', 'increase', 'self'),
               effect('H002', 'immunity', 'grant', 'team'), effect('H003', 'immunity', 'grant', 'ally'),
               effect('H118', 'immunity', 'grant', 'self'), effect('H118', 'immunity', 'dispel', 'enemy'),
               effect('H004', 'immunity', 'grant', 'team')]
    source = SimpleNamespace(base_dir='unused', character_effects={'game_build': 'test', 'records': records,
                              'roles': {rid: {'names': {'zh_hans': name}, 'aliases': []} for rid, name in names.items()}})
    monkeypatch.setattr(character_effects, 'get_source', lambda: source)
    monkeypatch.setattr(character_effects, 'WikiCatalog', lambda **_: SimpleNamespace(query=lambda: {'items': list(active.values())}))

    def search(**args):
        return json.loads(asyncio.run(character_effects.search_character_effects(**args)))

    result = search(effect='免疫', target='allies', role='同名角色', limit=1)
    assert result['matched'] == 2 and result['next_offset'] == 1
    assert search(effect='免疫', target='allies', offset=1, limit=1)['items'][0]['id'] == 'H003'
    assert search(effect='免疫', target='allies', release_status='unreleased')['items'][0]['id'] == 'H004'
    assert search(effect='免疫', operation='remove', target='enemy')['items'][0]['effects'][0]['operation'] == 'dispel'
    assert search(effect='普通命中率', target='self', role='安泰西娅')['items'][0]['id'] == 'H118'
    assert search(effect='hit_rate', activation='constant')['matched'] == 0
    assert search(effect='effect_hit', role='H118')['matched'] == 0
    assert search(status='自视甚高的欺侮')['items'][0]['id'] == 'H118'
    assert 'error' in search(effect='命中')


def test_index_reload_and_release_gate(tmp_path, monkeypatch):
    monkeypatch.setenv('GAMEDATA_DIR', str(tmp_path))
    assert not gamedata_source.get_source().character_effects
    data = {'schema': 1, 'game_build': 'index-build', 'roles': {}, 'records': []}
    (tmp_path / 'character_effects.json').write_text(json.dumps(data), encoding='utf-8')
    assert gamedata_source.get_source().character_effects == data
    (tmp_path / 'manifest.json').write_text('{"game_build":"different-build"}', encoding='utf-8')
    assert not gamedata_source.get_source().character_effects


def test_effect_search_stays_in_the_bound_game(monkeypatch):
    token = story_router.current_story_scope.set(SimpleNamespace(game='hsr'))
    try:
        error = json.loads(asyncio.run(character_effects.search_character_effects(effect='免疫')))
        assert 'Ark' in error['error']
    finally:
        story_router.current_story_scope.reset(token)
    assert game_intent('有哪些提供免疫buff的角色？') == 'gameplay'
    assert game_intent('哪些自带命中率的角色？') == 'gameplay'
    assert game_intent('谁能解除对方免疫？') == 'gameplay'
    assert game_intent('最近免疫力怎么样？') == ''


def test_v2_search_matches_polarity_named_status_and_signed_effects(monkeypatch):
    def record(rid, effect, operation, target, **fields):
        return dict(role_id=rid, effect=effect, operation=operation, target=target,
                    activation='conditional', conditions=[], value=None,
                    path=[{'kind': 'role', 'id': rid}], evidence=[], **fields)

    statuses = {
        'Stun': {'names': {'zh_hans': '眩晕'}, 'descriptions': {'zh_hans': '无法行动。'}, 'polarity': 'negative', 'removable': True},
        'Sticky': {'names': {'zh_hans': '不可解除的护盾'}, 'polarity': 'positive', 'removable': False},
        'Hidden': {'names': {'zh_hans': '孤立标记'}, 'polarity': 'unclassified', 'removable': None},
    }
    records = [record('H001', 'status', 'grant', 'enemy', status_id='Stun', polarity='negative'),
               record('H002', 'status', 'remove', 'enemy', status_id='Sticky', polarity='positive'),
               record('H003', 'dispel', 'dispel', 'team', polarity='negative'),
               record('H004', 'dispel', 'dispel', 'enemy', polarity='positive'),
               record('H001', 'action_gauge', 'decrease', 'enemy'),
               record('H002', 'action_gauge', 'increase', 'team'),
               record('H003', 'action_gauge', 'decrease', 'unknown'),
               record('H005', 'cooldown', 'increase', 'enemy'),
               record('H006', 'status_duration', 'increase', 'team', polarity='positive')]
    active = [{'id': f'H{i:03}', 'release_status': 'released', 'name_cn': f'角色{i}'} for i in range(1, 7)]
    data = {'schema': 2, 'game_build': 'test', 'roles': {}, 'statuses': statuses, 'records': records}
    monkeypatch.setattr(character_effects, 'get_source', lambda: SimpleNamespace(base_dir='unused', character_effects=data))
    monkeypatch.setattr(character_effects, 'WikiCatalog', lambda **_: SimpleNamespace(query=lambda: {'items': active}))

    def search(**args):
        return json.loads(asyncio.run(character_effects.search_character_effects(**args)))

    def roles(**args):
        return [item['id'] for item in search(**args)['items']]

    assert roles(effect='status', status='眩晕') == ['H001']
    assert roles(effect='status', operation='remove', status='Stun') == ['H003']
    assert roles(effect='status', operation='remove', status='不可解除的护盾') == ['H002']
    hidden = search(status='孤立标记')
    assert hidden['matched'] == 0 and hidden['status_definitions'][0]['id'] == 'Hidden'
    assert roles(effect='净化') == ['H003']
    assert set(roles(effect='驱散', polarity='positive')) == {'H002', 'H004'}
    assert roles(effect='推条') == ['H001', 'H003']
    assert roles(effect='推条', target='enemy') == ['H001']
    assert roles(effect='拉条', target='allies') == ['H002']
    assert roles(effect='技能延长') == ['H005']
    assert roles(effect='状态延长', polarity='positive') == ['H006']

    for text in ('哪些角色能给队友拉条？', '谁会推条？', '谁能延长技能冷却？',
                 '有哪些角色可以附加眩晕？', '哪些角色能净化负向状态？'):
        assert game_intent(text) == 'gameplay'
