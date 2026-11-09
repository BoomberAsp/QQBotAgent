"""Published knowledge/media registry. No user paths or private stores are accepted."""
import json
import os
import re
from pathlib import Path


def registry():
    path = os.environ.get('PERSONA_RESOURCE_REGISTRY',str(Path(__file__).resolve().parent.parent/'data/persona_resources/registry.json'))
    if not path:
        return {}, None
    p=Path(path).resolve()
    try:
        return json.loads(p.read_text(encoding='utf-8')), p.parent
    except (OSError,ValueError):
        return {}, None


def local_file(root, name):
    p=(root/str(name)).resolve()
    if not p.is_relative_to(root) or not p.is_file():
        raise ValueError('Invalid published resource')
    return p


def retrieve(persona, query, scope, limit=4):
    config,root=registry()
    if root is None:
        return ''
    hits=[]
    terms=set(re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]',query.lower()))
    for binding in config.get('personas',{}).get(persona,[]):
        kb=config.get('knowledge_bases',{}).get(binding,{})
        try:
            docs=json.loads(local_file(root,kb['path']).read_text(encoding='utf-8'))
            for doc in docs:
                # Resource privacy remains independent of story chronology.
                if doc.get('visibility')!='public' and scope not in doc.get('scopes',[]):
                    continue
                body=str(doc.get('text',''))[:4000]
                title=str(doc.get('title',''))[:120]
                score=sum(t in (title+' '+body).lower() for t in terms)
                if score:
                    hits.append((score,dict(kb=binding,id=doc.get('id',''),title=title,
                                           source=doc.get('source',''),text=body)))
        except (OSError,ValueError,KeyError,TypeError):
            continue
    hits.sort(key=lambda h:h[0],reverse=True)
    return json.dumps([h[1] for h in hits[:max(1,min(int(limit),8))]],ensure_ascii=False)[:6000] if hits else ''


def story_search(query='',limit=4):
    from .group_runtime import current_group_run
    run=current_group_run.get()
    if run is None or not run.valid():
        return '本工具仅用于有效群任务。'
    return retrieve(run.persona,query,run.scope,limit) or '当前人格可用知识库中未找到资料。'


async def group_media(asset_id=''):
    from .media_tools import send_sticker
    return await send_sticker(sticker_id=asset_id)


def media_still_published(persona,scope,asset,path):
    data,root=registry()
    if root is None:return False
    for a in data.get('media',[]):
        if a.get('id')==asset and persona in a.get('personas',[]) and (a.get('visibility')=='public' or scope in a.get('scopes',[])):
            try:return str(local_file(root,a['path']))==str(path)
            except (OSError,ValueError,KeyError):return False
    return False
