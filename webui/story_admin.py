"""Admin view of the same published story sources used by the bot."""
from pathlib import Path
from contextlib import closing
import sqlite3

from . import config  # Makes QQBot importable and forwards trusted dotenv paths.
from .readonly_sql import execute
from lib import story_router
from lib.story_kb import StoryKB, default_db_path
from lib.story_sources import discover_dbs


def _entry(kid):
    version,data=story_router.registry()
    if version=='unavailable':
        raise ValueError('剧情库注册表不可用，请检查 STORY_KB_REGISTRY')
    entry=data['kbs'].get(kid)
    if entry is None:
        raise ValueError('剧情库不存在')
    return entry,data


def _sources(kid, entry):
    if entry['driver']=='hsr_local':
        return [('hsr',story_router.hsr_database_path(kid))]
    paths=discover_dbs()
    wiki=Path(default_db_path()).resolve()
    if not any(Path(path).resolve()==wiki for _,path in paths):
        paths.append(('wiki',str(wiki)))
    return [(name,Path(path).resolve()) for name,path in paths]


def _open(kid, entry, source):
    paths=dict(_sources(kid,entry))
    if source not in paths:
        raise ValueError('请选择该剧情库已注册的数据库来源')
    path=paths[source]
    if not path.is_file():
        raise ValueError('数据库文件不存在：'+str(path))
    if entry['driver']=='hsr_local':
        reader=story_router.get_hsr_reader(kid)
        # Revalidate the cached release signature before every admin query.
        if reader.signature()!=reader.stamp:
            raise ValueError('HSR 发布文件已变更，请刷新')
    elif not StoryKB(str(path)).available:
        raise ValueError('Ark 数据库格式不兼容或无法读取：'+str(path))
    return sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=2)


def catalog():
    version,data=story_router.registry()
    result={'revision':version,'knowledge_bases':{},'error':''}
    if version=='unavailable':
        result['error']='剧情库注册表不可用，请检查 STORY_KB_REGISTRY'
        return result
    for kid,entry in data['kbs'].items():
        item={'id':kid,'game':entry['game'],'driver':entry['driver'],
              'title':'崩坏：星穹铁道' if entry['game']=='hsr' else '星陨计划 / Ark Re:Code',
              'bindings':{p:b for p,b in data['bindings'].items() if b['kb_id']==kid},
              'available':False,'sources':[], 'error':''}
        try:
            for name,path in _sources(kid,entry):
                source={'id':name,'path':str(path),'available':False,'error':'','tables':[],
                        'example_sql':('SELECT c.chunk_id, u.title, c.text FROM chunk c JOIN unit u ON u.unit_id=c.unit_id LIMIT 50'
                                       if entry['game']=='hsr' else 'SELECT id, scene_id, text_en FROM chunk LIMIT 50')}
                try:
                    with closing(_open(kid,entry,name)) as conn:
                        source['tables']=[row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                        source['meta']=dict(conn.execute('SELECT key,value FROM meta'))
                    source['available']=True
                    source['bytes']=path.stat().st_size
                except (OSError,ValueError,KeyError,sqlite3.Error) as error:
                    source['error']=str(error)
                item['sources'].append(source)
            item['available']=any(source['available'] for source in item['sources'])
        except (OSError,ValueError,KeyError) as error:
            item['error']=str(error)
        result['knowledge_bases'][kid]=item
    return result


def query(kid, source, sql):
    if not sql.strip() or len(sql)>10000:
        raise ValueError('SQL不能为空且不能超过10000字')
    entry,_=_entry(kid)
    try:
        return execute(_open(kid,entry,source),sql)
    except sqlite3.Error as error:
        raise ValueError('数据库读取失败：'+str(error)) from error


def search(kid, query, persona=''):
    _,data=_entry(kid)
    if not query.strip() or len(query)>400:
        raise ValueError('关键词不能为空且不能超过400字')
    bound=[p for p,b in data['bindings'].items() if b['kb_id']==kid]
    persona=persona or next(iter(bound),'')
    if persona not in bound:
        raise ValueError('所选人格未绑定该剧情库')
    scope=story_router.make_scope(persona,'webui:diagnostic')
    if scope is None or scope.kb_id!=kid:
        raise ValueError('人格绑定已变更，请刷新')
    text=story_router.tool_search(query,scope=scope)
    auto=story_router.hsr_injection(persona,query,scope) if scope.game=='hsr' else ''
    return {'persona':persona,'game':scope.game,'text':text[:12000],'auto_reference':auto}
