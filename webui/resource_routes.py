from functools import partial

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool
from . import resource_admin as service
router=APIRouter(prefix='/api/resources')

async def body(request):
    if request.headers.get('x-resource-admin')!='1':raise HTTPException(403,'缺少管理请求标识')
    origin=request.headers.get('origin')
    if origin and origin.rstrip('/')!=str(request.base_url).rstrip('/'):raise HTTPException(403,'不允许跨站写入')
    data=bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data)>8*1024*1024:raise HTTPException(413,'请求超过8 MiB')
    return bytes(data)

async def call(fn,*args):
    try:return await run_in_threadpool(fn,*args)
    except (ValueError,KeyError,TypeError) as e:raise HTTPException(400,str(e)) from e
    except OSError as e:raise HTTPException(400,'资源读写失败，请检查配置和目录权限') from e

@router.get('')
async def state():return await call(service.state)

@router.post('/query')
async def query(request:Request):
    import json
    try:data=json.loads(await body(request))
    except ValueError:raise HTTPException(400,'JSON无效')
    return await call(service.query,str(data.get('kb','')),str(data.get('sql','')))

@router.get('/stories')
async def stories():
    from . import story_admin
    return await call(story_admin.catalog)

@router.post('/story-query')
async def story_query(request:Request):
    import json
    from . import story_admin
    try:data=json.loads(await body(request))
    except ValueError:raise HTTPException(400,'JSON无效')
    if not isinstance(data,dict):raise HTTPException(400,'请求必须是 JSON 对象')
    return await call(story_admin.query,str(data.get('kb','')),str(data.get('source','')),str(data.get('sql','')))

@router.post('/story-search')
async def story_search(request:Request):
    import json
    from . import story_admin
    try:data=json.loads(await body(request))
    except ValueError:raise HTTPException(400,'JSON无效')
    if not isinstance(data,dict):raise HTTPException(400,'请求必须是 JSON 对象')
    return await call(story_admin.search,str(data.get('kb','')),str(data.get('query','')),str(data.get('persona','')))

@router.post('/knowledge')
async def publish(request:Request):
    import json
    try:data=json.loads(await body(request))
    except ValueError:raise HTTPException(400,'JSON无效')
    return await call(service.publish_kb,str(data.get('id','')),data.get('personas',[]),data.get('documents',[]))

@router.post('/stickers/{persona}')
async def upload(persona:str,request:Request,description:str='',mood:str=''):
    return await call(service.upload,persona,description,await body(request),mood)

@router.patch("/stickers/{persona}/{sid}")
async def edit_sticker_description(persona: str, sid: str, request: Request):
    payload = await object_body(request)
    return await call(service.update_sticker_description, persona, sid, payload.get("description"))


@router.delete('/stickers/{persona}/{sid}')
async def delete(persona:str,sid:str,request:Request):
    await body(request)
    return await call(service.delete_sticker,persona,sid)

@router.get('/stickers/{persona}/{sid}')
async def preview(persona:str,sid:str):
    data=await call(service.state)
    item=next((a for a in data.get('media',[]) if a.get('id')==sid and persona in a.get('personas',[])),None)
    if not item:raise HTTPException(404,'表情包不存在')
    path=await call(service.safe_file,item['path'])
    return FileResponse(path,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})


@router.get('/reviews')
async def reviews():return await call(service.review_queue)

@router.get('/reviews/{digest}')
async def review_preview(digest:str):
    return FileResponse(await call(service.review_image,digest),media_type='image/png',headers={'Cache-Control':'no-store'})

@router.post('/reviews/{digest}')
async def review_approve(digest:str,request:Request):
    await body(request)
    return await call(service.approve_image,digest)


@router.post('/media-policy')
async def media_policy(request:Request):
    import json
    try:payload=json.loads(await body(request))
    except ValueError:raise HTTPException(400,'JSON无效')
    return await call(service.media_policy,payload)

@router.get('/deliveries')
async def deliveries():return await call(service.delivery_records)


async def object_body(request):
    import json
    try:
        data = json.loads(await body(request))
    except ValueError as error:
        raise HTTPException(400, "JSON无效") from error
    if not isinstance(data, dict):
        raise HTTPException(400, "请求必须是 JSON 对象")
    return data


@router.get("/sticker-intake/candidates")
async def sticker_candidates():
    from agent import sticker_intake
    from lib.sticker_usage import USE_LABELS
    return {"candidates": await call(sticker_intake.pending), "uses": USE_LABELS}


@router.get("/sticker-intake/candidates/{ident}")
async def sticker_candidate_preview(ident: str):
    from agent import sticker_intake
    content = await call(sticker_intake.preview_bytes, ident)
    return Response(content, media_type="image/png",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/sticker-intake/review")
async def sticker_review(request: Request):
    from agent import sticker_intake
    payload = await object_body(request)
    action = payload.get("action")
    if action not in ("approve", "reject"):
        raise HTTPException(400, "请选择批准或拒绝")
    if action == "approve" and payload.get("confirmed") is not True:
        raise HTTPException(400, "请确认内容及复用许可")
    return await call(partial(sticker_intake.review, approve=action == "approve"), payload.get("items"))


@router.get("/sticker-intake/sources")
async def sticker_sources():
    from agent import sticker_subscriptions
    return await call(sticker_subscriptions.sources)


@router.post("/sticker-intake/sources")
async def sticker_source_save(request: Request):
    from agent import sticker_subscriptions
    return await call(sticker_subscriptions.save_source, await object_body(request))


@router.delete("/sticker-intake/sources/{ident}")
async def sticker_source_remove(ident: str, request: Request):
    await body(request)
    from agent import sticker_subscriptions
    return await call(sticker_subscriptions.remove_source, ident)


@router.post("/sticker-intake/sources/{ident}/sync")
async def sticker_source_sync(ident: str, request: Request):
    await body(request)
    from agent import sticker_subscriptions
    return await sticker_subscriptions.sync_source(ident, force=True)
