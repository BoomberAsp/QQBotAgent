"""Real SQLite/FTS plus actual tool/injection/delegation entry functions; no QQ/network."""
import asyncio
import ast
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0,str(Path(__file__).parent))
from lib import story_router as router
from lib.hsr_story import HsrStoryKB, render


@pytest.fixture
def configured(tmp_path,monkeypatch):
    release=tmp_path/'story_kb/hsr'; release.mkdir(parents=True)
    db=sqlite3.connect(release/'story.db')
    db.executescript('''
    CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
    CREATE TABLE unit(unit_id TEXT PRIMARY KEY,title TEXT,work TEXT);
    CREATE TABLE scene(scene_id TEXT PRIMARY KEY,unit_id TEXT,title TEXT);
    CREATE TABLE block(block_id TEXT PRIMARY KEY,scene_id TEXT,branch_id TEXT,condition_raw TEXT,ord INTEGER,
      speaker_raw TEXT,knowledge_scope TEXT,kind TEXT,raw_text TEXT);
    CREATE TABLE chunk(id INTEGER PRIMARY KEY,chunk_id TEXT,text TEXT,unit_id TEXT,scene_id TEXT,block_id TEXT,structure_status TEXT);
    CREATE TABLE branch(branch_id TEXT PRIMARY KEY,parent_branch_id TEXT,condition_raw TEXT,status TEXT);
    CREATE VIRTUAL TABLE chunk_fts USING fts5(title_terms,body_terms);
    ''')
    db.executemany('INSERT INTO meta VALUES (?,?)',{'schema_version':'5','schema_dialect':'local-v2','game':'hsr','lang':'zh-CN','build_id':'test-hsr'}.items())
    db.execute("INSERT INTO unit VALUES ('kafka','卡芙卡/语音','voice')")
    db.execute("INSERT INTO unit VALUES ('mission','混乱行至深处','main')")
    for index,(scene,title,text,branch,condition,status) in enumerate([
        ('voice','道别','拜拜，开拓者，下一次试着让我意外吧。',None,None,'verified'),
        ('voice2','初次见面','嗨，列车团…嗯，你们逮住我啦。',None,None,'verified'),
        ('story','剧情','这儿？一个空间站，不重要。','a','这…是…哪里？','verified'),
        ('story','剧情','太好了，你还记得我。','b','卡芙…卡？','verified'),
        ('unassigned','未归属','我们需要和军团交手么？',None,None,'unassigned')],1):
        uid='kafka' if scene.startswith('voice') else 'mission'
        db.execute('INSERT OR IGNORE INTO scene VALUES (?,?,?)',(scene,uid,title))
        if branch: db.execute('INSERT INTO branch VALUES (?,?,?,?)',(branch,None,condition,'mapped'))
        db.execute('INSERT INTO block VALUES (?,?,?,?,?,?,?,?,?)',(f'b{index}',scene,branch,condition,index,'卡芙卡','unknown','game_dialogue',text))
        db.execute('INSERT INTO chunk VALUES (?,?,?,?,?,?,?)',(index,f'c{index}',text,uid,scene,f'b{index}',status))
        from lib.hsr_story import tokens
        db.execute('INSERT INTO chunk_fts(rowid,title_terms,body_terms) VALUES (?,?,?)',(index,' '.join(tokens('卡芙卡 '+title)),' '.join(tokens(text))))
    db.commit();db.close()
    digest=hashlib.sha256((release/'story.db').read_bytes()).hexdigest()
    (release/'manifest.json').write_text(json.dumps({'build_id':'test-hsr','database_sha256':digest}),encoding='utf8')
    path=tmp_path/'registry.json'
    data={'schema':1,'kbs':{'hsr':{'game':'hsr','driver':'hsr_local','path':'story_kb/hsr/story.db'},'ark':{'game':'ark_re','driver':'ark_legacy'}},
          'bindings':{'herta':{'kb_id':'hsr','auto_inject':True},'assistant':{'kb_id':'ark'},'rubi':{'kb_id':'ark'}}}
    path.write_text(json.dumps(data),encoding='utf8')
    monkeypatch.setenv('STORY_KB_REGISTRY',str(path));monkeypatch.setenv('STORY_KB_DATA_ROOT',str(tmp_path))
    router._registries.clear();router._readers.clear()
    token=router.current_story_scope.set(None)
    yield tmp_path,path,data
    router.current_story_scope.reset(token)
    router._registries.clear();router._readers.clear()


def test_hsr_tool_and_injection_are_neutral_and_same_source(configured):
    scope=router.make_scope('herta','private:onebot:u1')
    router.current_story_scope.set(scope)
    text=router.tool_search('请查卡芙卡的道别语音并注明来源')
    injected=router.hsr_injection('herta','卡芙卡 道别',scope)
    for result in (text,injected):
        assert '拜拜，开拓者' in result and 'test-hsr / c1' in result
        assert '非角色亲历记忆' in result and '不得声称' in result
        assert '星陨计划' not in result
    assert len(injected)<=1800


def test_default_unassigned_and_branch_isolation(configured):
    kb=HsrStoryKB(configured[0]/'story_kb/hsr/story.db')
    assert not kb.search('我们需要和军团交手')
    hit=kb.search('空间站')[0]
    assert hit['conditions']==['这…是…哪里？']
    assert '还记得我' not in hit['text']
    assert '选项条件：这…是…哪里？' in render([hit])


def test_unbound_and_missing_hsr_never_fall_back(configured,monkeypatch):
    assert router.make_scope('ugc') is None
    assert '未绑定' in router.tool_search('卡芙卡')
    (configured[0]/'story_kb/hsr/story.db').unlink()
    assert 'HSR 资料库不可用' in router.tool_search('卡芙卡',scope=router.make_scope('herta'))


def test_hot_config_revocation_and_last_good(configured):
    _,path,data=configured
    scope=router.make_scope('herta')
    path.write_text('{broken',encoding='utf8')
    assert router.active(scope)  # invalid updates preserve the last-known-good config
    data['bindings'].pop('herta');path.write_text(json.dumps(data),encoding='utf8')
    assert not router.active(scope)
    assert '失效' in router.tool_search('卡芙卡',scope=scope)
    assert not router.hsr_injection('herta','卡芙卡',scope)


@pytest.mark.parametrize('mode',['hash','game','dialect','build'])
def test_bad_release_rejected(configured,mode):
    root,_,_=configured; release=root/'story_kb/hsr'
    if mode=='hash':
        with (release/'story.db').open('ab') as stream:stream.write(b'bad')
    else:
        db=sqlite3.connect(release/'story.db')
        key={'game':'game','dialect':'schema_dialect','build':'build_id'}[mode]
        db.execute('UPDATE meta SET value=? WHERE key=?',('wrong',key));db.commit();db.close()
        m=json.loads((release/'manifest.json').read_text());m['database_sha256']=hashlib.sha256((release/'story.db').read_bytes()).hexdigest()
        (release/'manifest.json').write_text(json.dumps(m))
    assert 'HSR 资料库不可用' in router.tool_search('卡芙卡',scope=router.make_scope('herta'))


def test_sql_readonly_and_connections_per_thread(configured):
    from concurrent.futures import ThreadPoolExecutor
    kb=HsrStoryKB(configured[0]/'story_kb/hsr/story.db')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:kb.search('卡芙卡 道别'),range(16)))
    assert all(r[0]['chunk_id']=='c1' for r in results)
    with kb.connection() as db,pytest.raises(sqlite3.OperationalError):
        db.execute('DELETE FROM meta')


def test_context_concurrency_and_generation(configured):
    async def run(persona,audience):
        token=router.current_story_scope.set(router.make_scope(persona,audience))
        try:
            await asyncio.sleep(0)
            return router.current_story_scope.get()
        finally:router.current_story_scope.reset(token)
    async def gather():return await asyncio.gather(run('herta','group:g1'),run('assistant','private:u2'))
    one,two=asyncio.run(gather())
    assert (one.game,one.audience)==('hsr','group:g1')
    assert (two.game,two.audience)==('ark_re','private:u2')
    scope=router.make_scope('herta','group:g1',valid=lambda:False)
    assert '失效' in router.tool_search('卡芙卡',scope=scope)


def test_actual_delegation_preserves_origin_and_restores(configured):
    from agent.context import _current_personality,_delegate_depth
    from agent.delegation import run_delegated_task
    class Agent:
        async def run(self,*args,**kwargs):
            assert _current_personality.get()=='assistant'
            assert router.current_story_scope.get().persona=='herta'
            assert 'story_search' in kwargs['allowed_tools']
            return router.tool_search('卡芙卡 道别')
    class Model:
        flash_client=reasoning_client=None
        async def classify_complexity(self,task):return 'simple'
    class Permissions:
        def get_role(self,user):return SimpleNamespace(value='user')
        def get_allowed_tools(self,role):return {'story_search','delegate_task'}
    _current_personality.set('herta');_delegate_depth.set(0)
    scope=router.make_scope('herta','private:u1');router.current_story_scope.set(scope)
    result=asyncio.run(run_delegated_task('查卡芙卡',agent=Agent(),model_router=Model(),perm_manager=Permissions(),user_id='u1'))
    assert '拜拜，开拓者' in result
    assert router.current_story_scope.get() is scope and _current_personality.get()=='herta'
    assert _delegate_depth.get()==0


def test_group_paths_use_hsr_and_fail_closed(configured):
    from agent.group_resources import retrieve,story_search
    from agent.group_runtime import current_group_run
    run=SimpleNamespace(persona='herta',scope='group:g1',valid=lambda:True)
    token=current_group_run.set(run)
    try:
        router.current_story_scope.set(router.make_scope('herta',run.scope,run.valid))
        assert '拜拜，开拓者' in retrieve('herta','卡芙卡 道别',run.scope)
        assert '拜拜，开拓者' in story_search('卡芙卡 道别')
        router.current_story_scope.set(router.make_scope('herta','private:u1'))
        assert '失效' in story_search('卡芙卡')
        run.valid=lambda:False
        assert '有效群任务' in story_search('卡芙卡')
    finally:current_group_run.reset(token)


def test_real_turn_functions_without_heavy_bot_bootstrap(configured):
    # Compile the production entry functions themselves, avoiding module-level service startup.
    file=Path(__file__).parent/'agent/turn.py'
    tree=ast.parse(file.read_text(encoding='utf8'))
    names={'_story_persona_allowed','_story_query_from','_build_story_context','_story_search'}
    functions=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name in names]
    import re
    namespace={'__name__':'agent.turn_under_test','__package__':'agent','re':re,'_STORY_QUERY_STRIP_RE':re.compile(r'\[(?:用户上传了|系统提醒)[^\]]*\]'),
               '_STORY_INJECT_CHARS':1800}
    exec(compile(ast.Module(body=functions,type_ignores=[]),str(file),'exec'),namespace)
    router.current_story_scope.set(router.make_scope('herta','private:u1'))
    assert namespace['_story_persona_allowed']('herta')
    assert not namespace['_story_persona_allowed']('ugc')
    block,character=namespace['_build_story_context']('herta','卡芙卡 道别',None)
    assert '拜拜，开拓者' in block and character==''
    assert '拜拜，开拓者' in namespace['_story_search']('卡芙卡 道别')


def test_auto_off_does_not_disable_explicit_tool(configured):
    _,path,data=configured
    data['bindings']['herta']['auto_inject']=False
    path.write_text(json.dumps(data),encoding='utf8')
    scope=router.make_scope('herta')
    assert router.hsr_injection('herta','卡芙卡 道别',scope)==''
    assert '拜拜，开拓者' in router.tool_search('卡芙卡 道别',scope=scope)


def test_wrong_persona_scope_cannot_be_used_for_injection(configured):
    scope=router.make_scope('herta')
    assert router.hsr_injection('assistant','卡芙卡',scope)==''


def test_invalid_cold_registry_denies(configured,monkeypatch):
    _,path,_=configured
    path.write_text('{bad',encoding='utf8')
    router._registries.clear()
    assert router.make_scope('herta') is None


def test_async_tool_bridge_copies_scope_to_worker(configured):
    source=Path(__file__).parent/'agent/turn.py'
    tree=ast.parse(source.read_text(encoding='utf8'))
    function=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_story_search_async')
    namespace={'asyncio':asyncio,'_story_search':router.tool_search}
    exec(compile(ast.Module(body=[function],type_ignores=[]),str(source),'exec'),namespace)
    router.current_story_scope.set(router.make_scope('herta','private:u1'))
    assert '拜拜，开拓者' in asyncio.run(namespace['_story_search_async']('卡芙卡 道别'))
