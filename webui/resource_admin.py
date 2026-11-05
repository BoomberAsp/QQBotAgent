"""Admin resource registry operations; SQL executes read-only against approved sources."""
import fcntl
import io
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from PIL import Image
from . import config


def registry_path():
    return Path(config.bot_env('PERSONA_RESOURCE_REGISTRY',str(config.QQBOT_DIR/'data/persona_resources/registry.json'))).resolve()

@contextmanager
def edit_registry():
    p=registry_path();p.parent.mkdir(parents=True,exist_ok=True)
    with open(str(p)+'.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        data=json.loads(p.read_text()) if p.exists() else dict(personas={},knowledge_bases={},media=[])
        yield data,p.parent
        tmp=p.with_name(p.name+'.'+uuid.uuid4().hex+'.tmp')
        try:
            tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2));os.replace(tmp,p)
        finally:
            tmp.unlink(missing_ok=True)


def state():
    p=registry_path()
    return json.loads(p.read_text()) if p.exists() else dict(personas={},knowledge_bases={},media=[])


def safe_file(name):
    root=registry_path().parent
    p=(root/name).resolve()
    if not p.is_relative_to(root) or not p.is_file():raise ValueError('资源文件不存在或路径越界')
    return p


def valid_id(value):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',value):raise ValueError('ID仅支持字母、数字、下划线和短横线')
    return value


def publish_kb(kid,personas,docs):
    valid_id(kid)
    if not personas or not isinstance(docs,list) or len(docs)>20000:raise ValueError('需填写人格并提供文档数组（最多20000条）')
    for persona in personas:valid_id(persona)
    ids=set()
    for d in docs:
        if not isinstance(d,dict) or not isinstance(d.get('text'),str) or len(d['text'])>20000:raise ValueError('每条文档需有text，最多20000字')
        if not d.get('id') or d['id'] in ids:raise ValueError('文档ID不能为空或重复')
        ids.add(d['id'])
        if type(d.get('chapter')) is not int or d['chapter']<0:raise ValueError('chapter必须为非负整数')
        if d.get('visibility') not in ('public','scoped'):raise ValueError('visibility必须为public或scoped')
        if d['visibility']=='scoped' and not isinstance(d.get('scopes'),list):raise ValueError('scoped文档需提供scopes数组')
    with edit_registry() as (data,root):
        folder=root/'knowledge';folder.mkdir(exist_ok=True)
        rel='knowledge/'+kid+'-'+uuid.uuid4().hex+'.json'
        (root/rel).write_text(json.dumps(docs,ensure_ascii=False))
        data.setdefault('knowledge_bases',{})[kid]={'path':rel}
        for bindings in data.setdefault('personas',{}).values():
            while kid in bindings:bindings.remove(kid)
        for persona in personas:data['personas'].setdefault(persona,[]).append(kid)
    return {'ok':True}


def query(kid,sql):
    if len(sql)>10000:raise ValueError('SQL过长')
    kb=state().get('knowledge_bases',{}).get(kid)
    if not kb:raise ValueError('知识库不存在')
    p=safe_file(kb['path'])
    if p.suffix=='.json':
        conn=sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE documents(id TEXT,title TEXT,text TEXT,source TEXT,chapter INTEGER,visibility TEXT,scopes TEXT)')
        docs=json.loads(p.read_text())
        conn.executemany('INSERT INTO documents VALUES (?,?,?,?,?,?,?)',[(d.get('id'),d.get('title'),d.get('text'),d.get('source'),d.get('chapter'),d.get('visibility'),json.dumps(d.get('scopes',[]))) for d in docs]);conn.commit()
    else:
        conn=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
    start=time.monotonic()
    try:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,1000000)
        conn.execute('PRAGMA query_only=ON')
        allowed={sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_RECURSIVE}
        conn.set_authorizer(lambda action,a,b,c,d:sqlite3.SQLITE_OK if action in allowed and not(action==sqlite3.SQLITE_FUNCTION and (b or '').lower() in ('load_extension','readfile','writefile')) else sqlite3.SQLITE_DENY)
        conn.set_progress_handler(lambda:int(time.monotonic()-start>2),1000)
        cur=conn.execute(sql);rows=cur.fetchmany(201)
        return {'columns':[c[0] for c in cur.description or []],'rows':[[str(v)[:2000] if v is not None else None for v in row] for row in rows[:200]],'truncated':len(rows)>200}
    except sqlite3.Error as e:raise ValueError('查询被拒绝或执行失败：'+str(e)) from e
    finally:conn.close()


def upload(persona,description,raw,mood=""):
    moods=[m.strip() for m in mood.split(",") if m.strip()]
    if any(m not in ("happy","sad","angry","greeting","thanks","surprised","tsundere","comfort") for m in moods):raise ValueError("不支持的情绪标签")
    valid_id(persona)
    if len(raw)>8*1024*1024:raise ValueError('图片超过8 MiB')
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if im.format not in ('PNG','JPEG','WEBP','GIF') or im.width*im.height>20000000:raise ValueError('图片格式或尺寸超限')
            if getattr(im,'n_frames',1)>1:raise ValueError('当前上传仅支持静态表情包，请先转换动图')
            im.load();im.thumbnail((1536,1536));out=io.BytesIO();im.convert('RGBA').save(out,format='PNG')
    except (OSError,Image.DecompressionBombError) as e:raise ValueError('图片无法解码') from e
    sid='sticker_'+uuid.uuid4().hex
    with edit_registry() as (data,root):
        (root/'uploads').mkdir(exist_ok=True)
        rel='uploads/'+sid+'.png';(root/rel).write_bytes(out.getvalue())
        data.setdefault('media',[]).append(dict(id=sid,path=rel,personas=[persona],description=description[:300],visibility='public',review_status='approved',origin='webui',rights_status='approved',sha256=__import__('hashlib').sha256(out.getvalue()).hexdigest(),mood=moods,pack_id=persona+"_webui",version=sid,source_id=sid))
    return {'ok':True,'id':sid}


def delete_sticker(persona,sid):
    with edit_registry() as (data,root):
        item=next((a for a in data.get('media',[]) if a.get('id')==sid and persona in a.get('personas',[])),None)
        if not item:raise ValueError('该人格下未找到表情包')
        item['personas'].remove(persona)
        if not item['personas']:data['media'].remove(item)
    # Unpublish immediately. Keep the blob for in-flight operations and manual GC.
    return {'ok':True}


def review_queue():
    from agent.media_ledger import root
    data=state(); approved=data.get('approved_hashes',{});policy=data.get('review_policy','v1')
    return [p.stem for p in (root()/'images').glob('*.png') if approved.get(p.stem)!=policy][:200]

def review_image(digest):
    if not re.fullmatch('[0-9a-f]{64}',digest):raise ValueError('审核编号无效')
    from agent.media_ledger import root
    p=root()/'images'/(digest+'.png')
    if not p.is_file():raise ValueError('图片不存在')
    return p

def approve_image(digest):
    import hashlib
    p=review_image(digest)
    if hashlib.sha256(p.read_bytes()).hexdigest()!=digest:raise ValueError('图片哈希变化')
    with edit_registry() as (data,root):
        data.setdefault('approved_hashes',{})[digest]=data.get('review_policy','v1')
    return {'ok':True}


def media_policy(payload):
    hosts=payload.get('image_hosts',[]);pages=payload.get('page_hosts',[])
    if not isinstance(hosts,list) or not isinstance(pages,list) or len(hosts)>100 or len(pages)>100:raise ValueError('域名列表无效')
    if any(not isinstance(h,str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?',h) for h in hosts+pages):raise ValueError('请使用小写精确域名，不含协议、路径或通配符')
    if set(pages)-set(hosts):raise ValueError('网页域也必须列入image_hosts')
    quiet=payload.get('proactive_quiet','22:00-08:00')
    if not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d',quiet):raise ValueError('安静时段格式错误')
    if type(payload.get('proactive_private_stickers',False)) is not bool:raise ValueError('主动贴图开关必须为布尔值')
    with edit_registry() as (data,root):
        data.update(image_hosts=hosts,page_hosts=pages,proactive_quiet=quiet,proactive_private_stickers=payload.get('proactive_private_stickers',False))
    return {'ok':True}


def delivery_records():
    from agent.media_ledger import db
    with db() as c:
        c.row_factory=__import__('sqlite3').Row
        return [dict(row) for row in c.execute('SELECT id,scope,persona,event,asset,status,created FROM tickets ORDER BY created DESC LIMIT 100')]
