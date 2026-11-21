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
  if asset in run.media_tickets:ledger.outcome(ticket,'dropped');return '本轮已选择这张图片。'
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

def sticker_candidates(persona, scope, text="", *, use="", mood="", include_cooling=False):
    from lib.sticker_usage import asset_uses, infer_uses, parse_uses

    data, base = registry()
    requested = set(parse_uses(use)) if use else set(infer_uses(text))
    eligible = [
        item for item in data.get("media", [])
        if persona in item.get("personas", [])
        and (item.get("visibility") == "public" or scope in item.get("scopes", []))
        and item.get("review_status") == item.get("rights_status") == "approved"
        and (not mood or mood in item.get("mood", []))
        and (not use or requested & asset_uses(item))
    ]
    if not eligible:
        return [], base
    with ledger.db() as conn:
        last, cooling, recent = ledger.sticker_history(conn, scope, persona)

    def rank(asset):
        ident = asset["id"]
        matches = asset_uses(asset) & requested
        scoped = asset.get("visibility") == "scoped"
        return (not matches, ident in recent, not scoped, last.get(ident, 0), ident)

    return sorted((item for item in eligible if include_cooling or item["id"] not in cooling), key=rank), base


def group_sticker_candidates(persona, scope, text=""):
    from lib.sticker_usage import USE_LABELS, asset_uses

    assets, _ = sticker_candidates(persona, scope, text)
    return [
        {"id": item["id"], "description": str(item.get("description", ""))[:120],
         "mood": item.get("mood", [])[:8],
         "uses": [USE_LABELS[key] for key in sorted(asset_uses(item)) if key in USE_LABELS]}
        for item in assets[:24]
    ]


async def send_sticker(sticker_id="", mood="", repeat=False, use=""):
    try:
        scope, _, persona, _, _ = context()
        assets, base = sticker_candidates(
            persona, scope, use=use, mood=mood, include_cooling=bool(sticker_id or repeat))
        if not sticker_id and not mood and not use:
            return json.dumps(group_sticker_candidates(persona, scope), ensure_ascii=False)[:3800]
        for asset in assets:
            ident = asset["id"]
            if sticker_id and ident != sticker_id:
                continue
            try:
                path = str(local_file(base, asset["path"]))
            except (OSError, ValueError, KeyError):
                continue
            if not published(persona, scope, ident, path):
                continue
            result = await deliver(path, ident, repeat)
            if sticker_id or "限制" not in result:
                return result
        return "当前人格没有符合条件且已批准的表情包。"
    except (ValueError, KeyError, OSError, TypeError):
        return "表情包上下文或资源不可用。"


def approved_url(url, hosts):
    from lib.media_download import validate_url
    if not isinstance(url, str):
        return ""
    try:
        validate_url(url, hosts)
    except (ValueError, UnicodeError):
        return ""
    return url


def _image_search_results(query):
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen

    endpoint = os.environ.get("SEARXNG_ENDPOINT", "http://localhost:8082").rstrip("/")
    params = urlencode({"q": query, "format": "json", "categories": "images",
                        "language": "zh-CN", "safesearch": "2"})
    request = Request(f"{endpoint}/search?{params}", headers={"Accept": "application/json"})
    with urlopen(request, timeout=12) as response:
        data = json.loads(response.read(2 * 1024 * 1024 + 1))
    return data.get("results", [])


async def search_images(query="", limit=4):
    """Find source links and scoped candidates without downloading image bytes."""
    query = query.strip()[:300]
    if not query:
        return "请提供图片搜索关键词。"
    async with _download_slots:
        try:
            scope, _, _, _, valid = context()
            data, _ = registry()
            page_hosts = set(data.get("page_hosts", []))
            image_hosts = set(data.get("image_hosts", []))
            if not page_hosts and not image_hosts:
                return "当前未配置图片或来源网页的允许域名。"
            if not valid() or not ledger.attempt(scope):
                return "图片搜索暂不可用，或已达到本会话的图片处理额度。"
            results = await asyncio.to_thread(_image_search_results, query)
            if not valid():
                return "图片搜索会话已失效。"
            candidates = []
            seen = set()
            limit = max(1, min(int(limit), 6))
            for item in results[:60]:
                if not isinstance(item, dict):
                    continue
                source = approved_url(item.get("url"), page_hosts)
                image = approved_url(item.get("img_src") or item.get("image"), image_hosts)
                target = image or source
                if not target or target in seen:
                    continue
                seen.add(target)
                result = {"title": str(item.get("title") or "图片结果")[:150]}
                if source:
                    result["source_url"] = source
                if image:
                    result["image_url"] = image
                    result["candidate_id"] = ledger.candidate(scope, image)
                candidates.append(result)
                if len(candidates) == limit:
                    break
            if not candidates:
                return "没有找到符合当前域名白名单的图片结果。"
            return json.dumps(candidates, ensure_ascii=False)
        except (OSError, ValueError, TypeError, AttributeError):
            return "图片搜索暂时失败，可以换关键词或提供来源页。"


async def discover_images(url=''):
    async with _download_slots:
        return await _discover_images(url)


async def _discover_images(url=''):
    from lib.media_download import Candidates, read_source_page

    try:
        scope, _, _, _, valid = context()
        data, _ = registry()
        page_hosts = set(data.get("page_hosts", []))
        image_hosts = set(data.get("image_hosts", []))
        if not approved_url(url, page_hosts):
            return "网页来源尚未批准采集。"
        if not valid() or not ledger.attempt(scope):
            return "图片处理额度已用完，或会话已失效。"

        raw, mime, final = await read_source_page(url, page_hosts)
        if mime not in ("text/html", "application/xhtml+xml"):
            return "来源不是HTML页面。"
        parser = Candidates(final, allow_url=lambda image: bool(approved_url(image, image_hosts)))
        parser.feed(raw.decode("utf-8", errors="replace"))
        if not valid():
            return "图片搜索会话已失效。"
        if not parser.urls:
            return "页面没有可用的白名单图片候选。"
        return json.dumps([{"candidate_id": ledger.candidate(scope, image), "url": image,
                            "source_url": final} for image in parser.urls], ensure_ascii=False)
    except Exception:
        return "无法获取图片候选；来源或robots不允许、网络或响应不符合策略。"


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
  hosts=set(data.get('image_hosts',[]))
  if not approved_url(url, hosts):return '图片来源域未批准。'
  if not ledger.attempt(scope):return '图片处理额度已用完。'
  key=hashlib.sha256(json.dumps([scope, url, sorted(hosts)]).encode()).hexdigest()
  async with _locks.setdefault(key,asyncio.Lock()):
   with ledger.db() as c:cached=c.execute('SELECT path,hash,expires,policy FROM cache WHERE key=?',(key,)).fetchone()
   if cached and cached[2]>time.time() and cached[3]==policy and Path(cached[0]).is_file():
    if data.get('approved_hashes',{}).get(cached[1])!=policy:
     return '图片已进入人工审核队列，批准前不会发送。审核编号：'+cached[1]
    return await deliver(cached[0],'image:'+cached[1])
   raw,mime,final=await download(url,hosts)
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
