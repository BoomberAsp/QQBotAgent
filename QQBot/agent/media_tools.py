"""Scoped sticker selection, image discovery and reviewed image delivery."""
import asyncio,hashlib,json,os,subprocess,sys,time
from pathlib import Path
from . import media_ledger as ledger
from .group_resources import registry,local_file
from .group_runtime import current_group_run

NORMALIZER='''import io,sys,resource
resource.setrlimit(resource.RLIMIT_AS,(536870912,536870912))
resource.setrlimit(resource.RLIMIT_CPU,(8,8))
from PIL import Image,ImageOps
raw=sys.stdin.buffer.read(8388609)
if len(raw)>8388608:raise ValueError()
with Image.open(io.BytesIO(raw)) as im:
 if len(sys.argv)>1 and sys.argv[1] and {'PNG':'image/png','JPEG':'image/jpeg','WEBP':'image/webp','GIF':'image/gif'}.get(im.format)!=sys.argv[1]:raise ValueError()
 if im.format not in ('PNG','JPEG','WEBP','GIF') or im.width*im.height>20000000 or getattr(im,'n_frames',1)>100:raise ValueError()
 if im.width*im.height*getattr(im,'n_frames',1)>100000000:raise ValueError()
 im.seek(0);im.load();im=ImageOps.exif_transpose(im);im.thumbnail((1536,1536));im.convert('RGBA').save(sys.stdout.buffer,format='PNG')
'''

def normalize(raw,mime=""):
 p=subprocess.run([sys.executable,'-c',NORMALIZER,mime],input=raw,capture_output=True,timeout=10)
 if p.returncode or len(p.stdout)>8*1024*1024:raise ValueError('图片解码失败或超限')
 return p.stdout

def context():
 from .context import _current_evidence_id,_current_user_id,_current_personality
 run=current_group_run.get()
 if run:
  return run.scope,run.actor,run.persona,run.event,run.valid
 evidence=json.loads(_current_evidence_id.get())
 platform,bot,group,user,event=evidence
 if group or not event:raise ValueError('缺少可信私聊上下文')
 from .group_runtime import get_store
 from types import SimpleNamespace
 ident=get_store().identity(SimpleNamespace(platform=platform,self_id=bot,group_id='',is_group=False,user_id=user))
 return json.dumps([platform,bot,'private',user]),ident['canonical'],_current_personality.get(),event,lambda:get_store().resolve(ident['identity'])['revision']==ident['revision']

def published(persona,scope,asset,path):
 data,base=registry()
 if asset.startswith('image:'):
  digest=asset[6:]
  return data.get('approved_hashes',{}).get(digest)==data.get('review_policy','v1') and Path(path).is_file() and hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest
 for a in data.get('media',[]):
  if a.get('id')!=asset or persona not in a.get('personas',[]):continue
  if a.get('visibility')!='public' and scope not in a.get('scopes',[]):continue
  if a.get('review_status')!='approved' or a.get('rights_status')!='approved':return False
  try:return local_file(base,a['path'])==Path(path) and hashlib.sha256(Path(path).read_bytes()).hexdigest()==a.get('sha256')
  except (OSError,ValueError,KeyError):return False
 return False

async def deliver(path,asset,repeat=False):
 scope,actor,persona,event,valid=context()
 if not valid() or not published(persona,scope,asset,path):return '资源未批准、已下架或文件发生变化。'
 ticket=ledger.reserve(scope,actor,persona,event,asset,repeat)
 if not ticket:return '媒体冷却、重复或额度限制，本次未发送。'
 run=current_group_run.get()
 if run:
  if run.media:ledger.outcome(ticket,'dropped');return '本轮已有图片。'
  run.media.append((path,asset));run.media_tickets[asset]=ticket
  return '已排队，尚未确认投递；不要声称已发送。'
 from .context import _turn_sender
 from .outbound import OutImage
 from . import turn
 sender=_turn_sender.get() or turn.default_sender
 try:
  if not valid() or not published(persona,scope,asset,path):status='dropped'
  elif hasattr(sender,'send_media_confirmed'):status=await sender.send_media_confirmed(OutImage(path=path))
  else:await sender.send_image(OutImage(path=path));status='sent'
 except asyncio.CancelledError:
  ledger.outcome(ticket,'uncertain');raise
 except Exception:status='uncertain'
 ledger.outcome(ticket,status)
 return {'sent':'图片已由平台确认接受。','uncertain':'发送结果不确定，不自动重试。'}.get(status,'图片未发送。')

async def send_sticker(sticker_id='',mood='',repeat=False):
 try:
  scope,actor,persona,event,valid=context();data,base=registry()
  assets=[a for a in data.get('media',[]) if persona in a.get('personas',[]) and (a.get('visibility')=='public' or scope in a.get('scopes',[]))]
  assets.sort(key=lambda a:a.get('id',''))
  if not sticker_id and not mood:
   return json.dumps([dict(id=a['id'],description=a.get('description',''),mood=a.get('mood',[])) for a in assets if a.get('review_status')=='approved' and a.get('rights_status')=='approved'][:24],ensure_ascii=False)[:1800]
  for a in assets:
   if (sticker_id and a.get('id')!=sticker_id) or (not sticker_id and mood not in a.get('mood',[])):continue
   path=str(local_file(base,a['path']))
   if published(persona,scope,a['id'],path):
    result=await deliver(path,a['id'],repeat)
    if sticker_id or '限制' not in result:return result
  return '当前人格没有符合条件且已批准的表情包。'
 except (ValueError,KeyError,OSError,TypeError):return '表情包上下文或资源不可用。'

async def discover_images(url=''):
 async with _download_slots:
  return await _discover_images(url)

async def _discover_images(url=''):
 try:
  from lib.media_download import download,Candidates
  scope,*_=context();data,_=registry();hosts=set(data.get('image_hosts',[]))
  if not ledger.attempt(scope):return '图片处理额度已用完。'
  # HTML fetching is opt-in per host; approved hosts are deployment policy.
  if __import__('urllib.parse',fromlist=['urlsplit']).urlsplit(url).hostname not in data.get('page_hosts',[]):return '网页来源尚未批准采集。'
  from urllib.parse import urlsplit,urlunsplit
  from urllib.robotparser import RobotFileParser
  checked=set()
  async def robots_check(target):
   if target in checked:return
   parsed=urlsplit(target);robots=urlunsplit((parsed.scheme,parsed.netloc,'/robots.txt','',''))
   rules,_,_=await download(robots,hosts,256*1024)
   robot=RobotFileParser();robot.parse(rules.decode('utf-8',errors='replace').splitlines())
   if not robot.can_fetch('QQBotAgent-Media',target):raise ValueError('robots denied')
   checked.add(target)
  # Initial check also ensures mocked/download backends cannot skip this gate.
  await robots_check(url)
  raw,mime,final=await download(url,set(data.get('page_hosts',[])),1024*1024,before_request=robots_check)
  if mime not in ('text/html','application/xhtml+xml'):return '来源不是HTML页面。'
  parser=Candidates(final);parser.feed(raw.decode('utf-8',errors='replace'))
  return json.dumps([dict(candidate_id=ledger.candidate(scope,u),url=u) for u in parser.urls],ensure_ascii=False)
 except Exception:return '无法获取图片候选；来源或robots不允许、网络或响应不符合策略。'

async def fetch_image(url='',candidate_id=''):
 async with _download_slots:
  return await _fetch_image(url,candidate_id)

async def _fetch_image(url='',candidate_id=''):
 try:
  from lib.media_download import download
  scope,*_=context();data,_=registry();policy=data.get('review_policy','v1')
  if candidate_id:
   url=ledger.candidate_url(scope,candidate_id)
   if not url:return '候选已过期或不属于当前会话。'
  if not ledger.attempt(scope):return '图片处理额度已用完。'
  key=hashlib.sha256((scope+'|'+url).encode()).hexdigest()
  async with _locks.setdefault(key,asyncio.Lock()):
   with ledger.db() as c:cached=c.execute('SELECT path,hash,expires,policy FROM cache WHERE key=?',(key,)).fetchone()
   if cached and cached[2]>time.time() and cached[3]==policy and Path(cached[0]).is_file():
    return await deliver(cached[0],'image:'+cached[1])
   raw,mime,final=await download(url,set(data.get('image_hosts',[])))
   if mime not in ('image/png','image/jpeg','image/webp','image/gif'):return '响应不是允许的图片类型。'
   content=await asyncio.to_thread(normalize,raw,mime);digest=hashlib.sha256(content).hexdigest()
   folder=ledger.root()/'images';folder.mkdir(exist_ok=True)
   if sum(p.stat().st_size for p in folder.glob('*.png'))+len(content)>512*1024*1024:return '图片缓存已满，请管理员清理。'
   path=folder/(digest+'.png');tmp=folder/(digest+'.'+__import__('secrets').token_hex(6)+'.part')
   try:tmp.write_bytes(content);os.replace(tmp,path)
   finally:tmp.unlink(missing_ok=True)
   with ledger.db() as c:c.execute('INSERT OR REPLACE INTO cache VALUES (?,?,?,?,?)',(key,str(path),digest,time.time()+86400,policy))
   if data.get('approved_hashes',{}).get(digest)!=policy:
    return '图片已进入人工审核队列，批准前不会发送。审核编号：'+digest
   return await deliver(str(path),'image:'+digest)
 except Exception:return '图片处理失败，未发送；请检查来源和审核状态。'

import weakref
_locks=weakref.WeakValueDictionary()

_download_slots=asyncio.Semaphore(4)
