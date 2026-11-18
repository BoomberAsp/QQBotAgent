"""Real SQLite/FTS and HTTP checks for the admin story catalog and queries."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'QQBot')]
from webui import story_admin, resource_admin
from webui.resource_routes import router as routes
from lib import story_router, story_kb
from test_hsr_story_router import configured  # Real local-v2 SQLite/FTS fixture.
from test_story_kb import bsk, _unit_main, ZH_FIXTURE


@pytest.fixture
def databases(configured,monkeypatch):
    root,registry,data=configured
    monkeypatch.setenv('USER_DATA_ROOT',str(root))
    monkeypatch.setenv('STORY_KB_DIR',str(root/'story_kb'))
    monkeypatch.setenv('STORY_GDATA_DB',str(root/'story/story_gd.db'))
    wiki=root/'story_kb/story.db'
    bsk.build_index([_unit_main('Main Story/Episode 1/Chapter 1/Stage 1-5',1,'Resonant Awakening')],ZH_FIXTURE,str(wiki),source_note='webui-fixture')
    gd=root/'story/story_gd.db';gd.parent.mkdir();shutil.copy2(wiki,gd)
    monkeypatch.setattr(story_kb,'_story_kb',None)
    monkeypatch.setattr(story_kb,'_KB_STAMP',{'sig':None})
    monkeypatch.setattr(resource_admin,'registry_path',lambda:root/'resources/registry.json')
    app=FastAPI();app.include_router(routes)
    return root,registry,data,TestClient(app)


def test_catalog_shows_both_games_and_physical_ark_sources(databases):
    root,_,_,client=databases
    response=client.get('/api/resources/stories');assert response.status_code==200
    entries=response.json()['knowledge_bases']
    assert set(entries)=={'hsr','ark'}
    assert entries['hsr']['available'] and entries['ark']['available']
    assert set(entries['hsr']['bindings'])=={'herta'}
    assert set(entries['ark']['bindings'])=={'rubi','assistant'}
    assert [s['id'] for s in entries['ark']['sources']]==['gamedata','wiki']
    source=entries['hsr']['sources'][0]
    assert source['path']==str(root/'story_kb/hsr/story.db')
    assert source['meta']['build_id']=='test-hsr' and 'chunk_fts' in source['tables']
    for entry in entries.values():
        for source in entry['sources']:
            result=story_admin.query(entry['id'],source['id'],source['example_sql'])
            assert result['rows']


def test_keyword_search_matches_runtime_and_checks_persona(databases):
    hsr=story_admin.search('hsr','卡芙卡 道别','herta')
    assert '拜拜，开拓者' in hsr['text'] and '拜拜，开拓者' in hsr['auto_reference']
    for persona in ['rubi','assistant']:
        result=story_admin.search('ark','露比 雨夜',persona)
        assert result['game']=='ark_re' and '露比' in result['text']
        assert '卡芙卡' not in result['text']
    with pytest.raises(ValueError,match='未绑定'):
        story_admin.search('hsr','卡芙卡','assistant')


def test_missing_release_is_visible_and_does_not_break_ark(databases):
    root,registry,data,_=databases
    data['kbs']['hsr']['path']='story_kb/hsr/missing/story.db'
    registry.write_text(json.dumps(data))
    entries=story_admin.catalog()['knowledge_bases']
    assert not entries['hsr']['available'] and entries['ark']['available']
    assert '文件不存在' in entries['hsr']['sources'][0]['error']
    with pytest.raises(ValueError,match='文件不存在'):
        story_admin.query('hsr','hsr','SELECT 1')
    assert not (root/'story_kb/hsr/missing').exists()
    assert 'HSR 资料库不可用' in story_admin.search('hsr','卡芙卡')['text']


def test_hash_mismatch_disables_sql_as_well_as_runtime(databases):
    root,_,_,_=databases
    manifest=root/'story_kb/hsr/manifest.json'
    value=json.loads(manifest.read_text());value['database_sha256']='0'*64
    manifest.write_text(json.dumps(value))
    source=story_admin.catalog()['knowledge_bases']['hsr']['sources'][0]
    assert not source['available'] and 'hash mismatch' in source['error']
    with pytest.raises(ValueError,match='hash mismatch'):
        story_admin.query('hsr','hsr','SELECT 1')


@pytest.mark.parametrize('sql',[
    'DELETE FROM chunk',"ATTACH DATABASE ':memory:' AS other",'PRAGMA writable_schema=ON',
    "SELECT load_extension('x')","SELECT writefile('/tmp/story-admin-should-not-exist','x')",
    'CREATE TABLE unwanted(id INT)','SELECT 1; SELECT 2',
])
def test_sql_writes_and_side_effects_are_rejected(databases,sql):
    root,_,_,_=databases
    for kid,source,path in [('hsr','hsr',root/'story_kb/hsr/story.db'),('ark','wiki',root/'story_kb/story.db')]:
        before=hashlib.sha256(path.read_bytes()).digest()
        with pytest.raises(ValueError):story_admin.query(kid,source,sql)
        assert hashlib.sha256(path.read_bytes()).digest()==before


def test_sources_are_registered_ids_and_results_are_bounded(databases):
    for kid,source in [('hsr','wiki'),('hsr','../story.db'),('ark','/etc/passwd')]:
        with pytest.raises(ValueError):story_admin.query(kid,source,'SELECT 1')
    result=story_admin.query('hsr','hsr',"WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<205) SELECT x,printf('%2500s','x') FROM n")
    assert len(result['rows'])==200 and result['truncated']
    assert len(result['rows'][0][1])==2000


def test_http_origin_guard_and_actionable_failures(databases):
    _,_,_,client=databases
    headers={'X-Resource-Admin':'1'}
    for path,payload in [('/story-query',{'kb':'hsr','source':'hsr','sql':'SELECT COUNT(*) FROM chunk'}),('/story-search',{'kb':'hsr','persona':'herta','query':'卡芙卡 道别'})]:
        assert client.post('/api/resources'+path,json=payload).status_code==403
        assert client.post('/api/resources'+path,json=payload,headers={**headers,'Origin':'https://evil.test'}).status_code==403
        assert client.post('/api/resources'+path,json=payload,headers=headers).status_code==200
        assert client.post('/api/resources'+path,json=[],headers=headers).status_code==400
    assert client.post('/api/resources/story-query',headers=headers,json={'kb':'not-registered','source':'hsr','sql':'SELECT 1'}).status_code==400


def test_json_and_story_with_same_id_stay_separate(databases):
    resource_admin.publish_kb('hsr',['custom'],[dict(id='one',text='自定义资料',chapter=0,visibility='public')])
    assert resource_admin.query('hsr','SELECT text FROM documents')['rows']==[['自定义资料']]
    assert story_admin.query('hsr','hsr',"SELECT value FROM meta WHERE key='game'")['rows']==[['hsr']]
    assert set(story_admin.catalog()['knowledge_bases']['hsr']['bindings'])=={'herta'}


@pytest.mark.parametrize('explicit_environment',[False,True])
def test_panel_honors_bot_dotenv_without_overriding_environment(databases,explicit_environment):
    root,registry,_,_=databases
    env=dict(os.environ)
    for key in ['USER_DATA_ROOT','STORY_KB_DATA_ROOT','STORY_KB_REGISTRY','STORY_KB_DIR','STORY_GDATA_DB']:
        env.pop(key,None)
    expected_root=str(root)
    if explicit_environment:
        expected_root=str(root/'explicit')
        env['USER_DATA_ROOT']=expected_root
        env['STORY_KB_DATA_ROOT']=str(root)
    dotenv={'USER_DATA_ROOT':str(root),'STORY_KB_REGISTRY':str(registry)}
    script='''import sys,json,os,dotenv
sys.path.insert(0,sys.argv[1])
values=json.loads(sys.argv[2])
dotenv.dotenv_values=lambda *a,**kw:values
from webui import config
from lib import story_router
assert os.environ['USER_DATA_ROOT']==sys.argv[3]
scope=story_router.make_scope('herta')
assert '拜拜，开拓者' in story_router.tool_search('卡芙卡 道别',scope=scope)
'''
    result=subprocess.run([sys.executable,'-c',script,str(ROOT),json.dumps(dotenv),expected_root],env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
