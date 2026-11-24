"""Regression coverage for the opt-in HSR role evidence policy."""
import json

from lib import hsr_perspective, story_router
from test_hsr_story_router import configured  # noqa: F401


def enable_perspective(configured, monkeypatch):
    _, registry_path, registry = configured
    registry['kbs']['hsr']['build_id'] = 'test-hsr'
    registry['bindings']['herta']['character_perspective'] = True
    registry['bindings']['herta']['character_perspective_groups'] = ['test:group:1']
    registry['bindings']['nousson'] = {'kb_id': 'hsr'}
    registry_path.write_text(json.dumps(registry), encoding='utf-8')
    data = {
        'revision': 'test-perspective', 'build_id': 'test-hsr',
        'topics': [{'pattern': '卡芙卡.{0,10}印象', 'chunks': ['c1']}],
        'evidence': {'c1': {'mode': 'informed', 'scope': '只支持语音提及的内容。',
                           'speaker': '卡芙卡', 'kind': 'game_dialogue'}},
    }
    monkeypatch.setattr(story_router, 'profile', lambda build: data if build == 'test-hsr' else None)
    return data


def test_scope_and_build_gate(configured, monkeypatch):
    data = enable_perspective(configured, monkeypatch)
    make_scope = story_router.make_scope
    assert story_router.character_perspective(make_scope('herta', 'private:onebot:123')) is data
    assert story_router.character_perspective(make_scope('herta', 'test:group:1')) is data
    assert story_router.character_perspective(make_scope('herta', 'test:group:2')) is None
    assert story_router.character_perspective(make_scope('herta', 'private', lambda: False)) is None
    assert story_router.character_perspective(make_scope('nousson', 'private')) is None
    reader = story_router.get_hsr_reader('hsr')
    assert 'perspective_revision' not in reader.retrieve('卡芙卡 道别', perspective={**data, 'build_id': 'stale'})


def test_story_tool_and_injection_bind_the_same_annotation(configured, monkeypatch):
    data = enable_perspective(configured, monkeypatch)
    scope = story_router.make_scope('herta')
    # The legacy fixture has no provenance columns. Use the ordinary FTS route.
    data['topics'] = []
    query = '卡芙卡 道别'
    outputs = [story_router.hsr_injection('herta', query, scope), story_router.tool_search(query, scope=scope)]
    for output in outputs:
        assert '明确获知' in output and '只支持语音提及的内容。' in output
        assert 'test-hsr / c1' in output
    assert '明确获知' not in story_router.tool_search(query, scope=story_router.make_scope('nousson'))
    data['evidence']['c1']['speaker'] = '错配的主体'
    result = story_router.hsr_injection('herta', query, scope)
    assert '视角未知' in result and '只支持语音提及的内容。' not in result


def test_group_preload_and_tool_share_policy(configured, monkeypatch):
    from types import SimpleNamespace
    from agent.group_resources import retrieve, story_search
    from agent.group_runtime import current_group_run

    data = enable_perspective(configured, monkeypatch)
    data['topics'] = []
    group = 'test:group:1'
    query = '卡芙卡 道别'
    scope = story_router.make_scope('herta', group)
    story_router.current_story_scope.set(scope)
    token = current_group_run.set(SimpleNamespace(persona='herta', scope=group, valid=lambda: True))
    try:
        assert '明确获知' in retrieve('herta', query, group)
        assert '明确获知' in story_search(query)
        assert '明确获知' not in retrieve('herta', query, 'test:group:2')
        assert not retrieve('herta', query, group, valid=lambda: False)
    finally:
        current_group_run.reset(token)


def test_witness_followup_requires_scoped_history(configured, monkeypatch):
    enable_perspective(configured, monkeypatch)
    scope = story_router.make_scope('herta')
    assert story_router.bound_query_plan(scope, '你当时在场吗？')['unresolved']
    assert story_router.bound_query_plan(scope, '你当时在场吗？', history=[
        {'role': 'assistant', 'content': '卡芙卡前去空间站'},
    ])['unresolved']
    plan = story_router.bound_query_plan(scope, '你当时在场吗？', history=[
        {'role': 'user', 'content': '卡芙卡当时遇到了什么？'},
    ])
    assert plan['character_witness'] and plan['entities'] == ['卡芙卡'] and not plan['unresolved']


def test_persona_cues_keep_forms_and_exclude_other_intents(configured, monkeypatch):
    data = enable_perspective(configured, monkeypatch)
    data['topics'] = [{'pattern': '实验失败', 'chunks': ['c1']}]
    plan = hsr_perspective.query_plan('实验失败了，太沮丧了', data)
    assert plan['character_context'] and plan['retrieve']
    for query in ('/实验失败', '实验失败的台词原文', '假如实验失败了', '实验失败了怎么配队'):
        assert 'character_context' not in hsr_perspective.query_plan(query, data)
    forms = hsr_perspective.query_plan('黑塔人偶和大黑塔的关系', data)
    assert set(forms['entities']) == {'黑塔人偶', '大黑塔'}
