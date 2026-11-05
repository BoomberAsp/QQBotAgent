from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import FileResponse
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

@router.post('/knowledge')
async def publish(request:Request):
    import json
    try:data=json.loads(await body(request))
    except ValueError:raise HTTPException(400,'JSON无效')
    return await call(service.publish_kb,str(data.get('id','')),data.get('personas',[]),data.get('documents',[]))

@router.post('/stickers/{persona}')
async def upload(persona:str,request:Request,description:str='',mood:str=''):
    return await call(service.upload,persona,description,await body(request),mood)

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
