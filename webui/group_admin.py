"""WebUI group administration using the same transactional service as chat."""
import json
from pathlib import Path
from fastapi import APIRouter,Request,HTTPException
from starlette.concurrency import run_in_threadpool
from . import config,audit
from agent.group_state import GroupStore,Conflict
from agent.group_service import GroupService
router=APIRouter(prefix='/api/groups')

def service():return GroupService(GroupStore(Path(config.bot_env('GROUP_DATA_ROOT') or config.USER_DATA_ROOT.parent/'groups_store')))

async def call(fn,*args,**kwargs):
    try:return await run_in_threadpool(fn,*args,**kwargs)
    except Conflict as e:raise HTTPException(409,dict(error=str(e),config_revision=e.revision))
    except KeyError:raise HTTPException(404,'群或条目不存在')
    except PermissionError as e:raise HTTPException(403,str(e))
    except (ValueError,TypeError) as e:raise HTTPException(400,str(e))

async def payload(request,revision=True):
    if request.headers.get('x-admin-write')!='1':raise HTTPException(403,'缺少管理请求标识')
    origin=request.headers.get('origin')
    if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'):raise HTTPException(403,'不允许跨站写入')
    raw=bytearray()
    async for part in request.stream():
        raw.extend(part)
        if len(raw)>8*1024*1024:raise HTTPException(413,'请求过大')
    try:
        data=json.loads(raw or b'{}')
        if not isinstance(data,dict):raise ValueError()
        expected=int(request.headers['if-match'].strip('"')) if revision else None
        if expected is not None and expected<0:raise ValueError()
    except (ValueError,KeyError):raise HTTPException(400,'需要JSON对象及有效If-Match配置版本')
    return data,expected

def note(request,gid,fields):audit.log_action('group.write',gid+' '+','.join(fields),request.client.host if request.client else '')

@router.get('')
async def listing():return await call(lambda:service().listing())

@router.get('/catalog')
async def catalog():
    from agent.group_commands import COMMANDS
    from agent.group_resources import registry
    s=service()
    return dict(personas=[p for p in s.personas.list_personalities() if s.personas.is_builtin(p['key'])],knowledge_bases=list(registry()[0].get('knowledge_bases',{})),commands=[dict(name=c.name,usage=c.usage,who=c.who) for c in COMMANDS])

@router.get('/legacy')
async def legacy():return await call(lambda:service().legacy(config.QQBOT_DIR/'data'))

@router.post('/legacy/{ident}')
async def migrate(ident:str,request:Request):
    data,rev=await payload(request,revision=False)
    if data.get('action') not in ('ignore','apply'):raise HTTPException(400,'迁移动作无效')
    s=service()
    if data['action']=='ignore':await call(s.migrate,ident,'','webui',None,True)
    else:
        if type(data.get('config_revision')) is not int:raise HTTPException(400,'缺少目标配置版本')
        await call(s.migrate,ident,await call(s.scope,data.get('gid','')),'webui',data['config_revision'])
    note(request,ident,['legacy']);return {'ok':True}

@router.get('/{gid}')
async def detail(gid:str):
    s=service();return await call(s.snapshot,await call(s.scope,gid))

@router.put('/{gid}/config')
async def patch_config(gid:str,request:Request):
    data,rev=await payload(request);s=service();scope=await call(s.scope,gid)
    await call(s.patch,scope,data,'webui','admin',rev);note(request,gid,data.keys());return await call(s.snapshot,scope)

@router.post('/{gid}/reset')
async def reset(gid:str,request:Request):
    data,rev=await payload(request)
    if set(data)-{'confirm','clear'} or type(data.get('clear',False)) is not bool or data.get('confirm')!=('清除数据' if data.get('clear') else '重置'):raise HTTPException(400,'请提供明确的操作确认')
    s=service();scope=await call(s.scope,gid);await call(s.store.reset,scope,clear=data.get('clear',False),expected=rev)
    note(request,gid,['reset']);return await call(s.snapshot,scope)

@router.post('/{gid}/memory')
async def memory(gid:str,request:Request):
    data,rev=await payload(request)
    if set(data)-{'text','clear','confirm'} or type(data.get('clear',False)) is not bool:raise HTTPException(400,'记忆操作无效')
    if data.get('clear'):
        if data.get('confirm')!='清空':raise HTTPException(400,'请确认清空')
    elif not isinstance(data.get('text'),str) or not data['text'].strip():raise HTTPException(400,'请输入记忆内容')
    s=service();scope=await call(s.scope,gid);await call(s.store.memory,scope,text=data.get('text'),clear=data.get('clear',False),expected=rev)
    note(request,gid,['memory']);return await call(s.snapshot,scope)

@router.post('/{gid}/facts/{fid}')
async def delete_fact(gid:str,fid:str,request:Request):
    data,rev=await payload(request)
    if data!={'confirm':'删除'}:raise HTTPException(400,'请确认删除该群来源事实')
    s=service();scope=await call(s.scope,gid);await call(s.delete_fact,scope,fid,rev)
    note(request,gid,['fact']);return await call(s.snapshot,scope)

@router.get('/{gid}/deliveries')
async def deliveries(gid:str):
    state=await detail(gid)
    from agent.media_ledger import db
    def media():
        with db() as c:
            c.row_factory=__import__('sqlite3').Row
            return [dict(r) for r in c.execute('SELECT id,event,asset,status,created FROM tickets WHERE scope=? ORDER BY created DESC LIMIT 20',(state['scope'],))]
    return dict(messages=state['deliveries'],media=await call(media))
