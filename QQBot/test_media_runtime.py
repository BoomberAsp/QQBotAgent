import asyncio,hashlib,io,json,os,socket,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock
from PIL import Image
from agent import media_ledger as ledger
from agent import media_tools as media
from agent.group_runtime import GroupRun,current_group_run
from lib.media_download import validate_url,PublicResolver,MediaError,Candidates

class MediaTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.reg=self.root/'registry.json'
  self.data={'media':[],'image_hosts':['images.example'],'review_policy':'v1'}
  self.save()
  p=patch.dict(os.environ,MEDIA_DATA_ROOT=str(self.root/'runtime'),PERSONA_RESOURCE_REGISTRY=str(self.reg));p.start();self.addCleanup(p.stop)
  self.run=GroupRun('hero','',[],'',set(),lambda:True,scope='g',actor='a',event='e')
  token=current_group_run.set(self.run);self.addCleanup(current_group_run.reset,token)
 def save(self):self.reg.write_text(json.dumps(self.data))
 def sticker(self):
  raw=io.BytesIO();Image.new('RGB',(8,8),'red').save(raw,'PNG');p=self.root/'s.png';p.write_bytes(raw.getvalue())
  self.data['media']=[dict(id='s',path='s.png',personas=['hero'],visibility='public',mood=['happy'],review_status='approved',rights_status='approved',sha256=hashlib.sha256(raw.getvalue()).hexdigest())];self.save();return p
 async def test_proactive_sticker_uncertain_no_retry(self):
  import ast
  from types import SimpleNamespace
  from unittest.mock import MagicMock
  self.sticker();self.data['media'][0]['mood']=['greeting'];self.save()
  source=(Path(__file__).parent/'plugins/qq_official.py').read_text()
  fn=next(n for n in ast.parse(source).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_try_proactive_sticker')
  greeting=MagicMock();mail=MagicMock();mail.load_user.return_value={'active_enabled':True}
  ns=dict(json=json,time=time,Path=Path,greeting=greeting,_GREET_QUOTA_FILE='quota',_GREET_TARGETS_FILE='targets',_mailbox_manager=mail,MessageSegment=SimpleNamespace(file_image=lambda p:str(p)))
  exec(compile(ast.Module(body=[fn],type_ignores=[]),'proactive_fixture','exec'),ns)
  bot=SimpleNamespace(self_id='bot',send_to_c2c=AsyncMock(side_effect=TimeoutError()))
  store=MagicMock();store.identity.return_value={'canonical':'private-a'}
  with patch('agent.group_runtime.get_store',return_value=store):
   self.assertTrue(await ns['_try_proactive_sticker'](bot,'a','hero'))
  bot.send_to_c2c.assert_awaited_once();greeting.record_active_send.assert_called_once();greeting.mark_greeted.assert_not_called()
  with ledger.db() as c:self.assertEqual(c.execute('SELECT status FROM tickets').fetchone()[0],'uncertain')
 async def test_group_feature_off_filters_all_media(self):
  from agent.group_features import GroupFeatureManager
  manager=GroupFeatureManager(str(self.root/'features'));manager.set_feature('g','image',False)
  self.assertTrue({'send_sticker','fetch_image','discover_images','group_media'}<=manager.get_disabled_tools('g'))
 async def test_url_constraints(self):
  for url in ['http://images.example/a','https://u:p@images.example/a','https://images.example:444/a','https://images.example.evil/a']:
   with self.assertRaises(ValueError):validate_url(url,{'images.example'})
  self.assertEqual(validate_url('https://images.example/a',{'images.example'}),'images.example')
 async def test_dns_rejects_private_mixed_and_mapped(self):
  loop=asyncio.get_running_loop()
  for ip in ['127.0.0.1','10.0.0.1','169.254.169.254','::ffff:127.0.0.1']:
   with patch.object(loop,'getaddrinfo',AsyncMock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,0,'',(ip,443))])):
    with self.assertRaises(MediaError):await PublicResolver().resolve('images.example',443)
 async def test_catalog_id_and_tamper(self):
  p=self.sticker()
  self.assertIn('"id": "s"',await media.send_sticker())
  self.assertIn('没有',await media.send_sticker('wrong'))
  self.assertFalse(self.run.media)
  p.write_bytes(b'changed');self.assertIn('没有',await media.send_sticker('s'))
 async def test_mood_queue_and_sent_ring(self):
  self.sticker();self.assertIn('排队',await media.send_sticker(mood='happy'))
  ticket=self.run.media_tickets['s'];ledger.outcome(ticket,'sent')
  ledger.outcome(ticket,'dropped')
  with ledger.db() as c:self.assertEqual(c.execute('SELECT status FROM tickets WHERE id=?',(ticket,)).fetchone()[0],'sent')
  self.assertEqual(ledger.reserve('g','b','hero','e2','s'),'')
 async def test_quota_failed_release_and_uncertain_hold(self):
  t=ledger.reserve('g','a','hero','e','x');self.assertTrue(t)
  ledger.outcome(t,'failed');t2=ledger.reserve('g','a','hero','e','x');self.assertTrue(t2)
  ledger.outcome(t2,'uncertain');self.assertFalse(ledger.reserve('g','a','hero','e','x'))
 async def test_candidates_scope_expiry(self):
  token=ledger.candidate('g','https://images.example/a')
  self.assertEqual(ledger.candidate_url('other',token),'')
  with ledger.db() as c:c.execute('UPDATE candidates SET expires=0')
  self.assertEqual(ledger.candidate_url('g',token),'')
 async def test_discovery_never_sends_and_denied_robots(self):
  self.data['page_hosts']=['images.example'];self.save()
  with patch('lib.media_download.download',AsyncMock(return_value=(b'User-agent: *\nDisallow: /','text/plain','https://images.example/robots.txt'))):
   self.assertIn('不允许',await media.discover_images('https://images.example/page'))
  self.assertFalse(self.run.media)
 async def test_fetch_review_cache_then_approval(self):
  self.sticker();raw=(self.root/'s.png').read_bytes()
  download=AsyncMock(return_value=(raw,'image/png','https://images.example/a'))
  with patch('lib.media_download.download',download):
   result=await media.fetch_image('https://images.example/a');self.assertIn('人工审核',result);self.assertFalse(self.run.media)
   digest=result.split('：')[-1];self.data['approved_hashes']={digest:'v1'};self.save()
   self.assertIn('排队',await media.fetch_image('https://images.example/a'));self.assertEqual(download.await_count,1)
 async def test_decode_invalid_and_mime(self):
  self.sticker();raw=(self.root/'s.png').read_bytes()
  with self.assertRaises(ValueError):await asyncio.to_thread(media.normalize,b'not image')
  with self.assertRaises(ValueError):await asyncio.to_thread(media.normalize,raw,'image/jpeg')
 async def test_candidate_parser_bound(self):
  p=Candidates('https://images.example/page');p.feed(''.join('<img src="/'+str(n)+'.png">' for n in range(20)))
  self.assertEqual(len(p.urls),6);self.assertEqual(p.urls[0],'https://images.example/0.png')

class PackTests(unittest.TestCase):
 def test_pack_approval_hash_and_immutable_version(self):
  from lib.sticker_library import publish
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);raw=io.BytesIO();Image.new('RGB',(8,8),'red').save(raw,'PNG');(root/'s.png').write_bytes(raw.getvalue())
   manifest={'schema':2,'pack_id':'hero_pack','persona':'hero','version':'1','stickers':[dict(id='hero_happy',file='s.png',sha256=hashlib.sha256(raw.getvalue()).hexdigest(),width=8,height=8,bytes=len(raw.getvalue()),mood=['happy'],rights_status='pending',review_status='approved',source_id='self',author='uploader',license='permission')]}
   path=root/'manifest.json';path.write_text(json.dumps(manifest));reg=root/'published/registry.json'
   with self.assertRaises(ValueError):publish(path,reg)
   manifest['stickers'][0]['rights_status']='approved';path.write_text(json.dumps(manifest))
   self.assertEqual(publish(path,reg),1)
   self.assertEqual(publish(path,reg),1)
   (root/'s.png').write_bytes(b'changed')
   with self.assertRaises(ValueError):publish(path,reg)

class TransportTests(unittest.IsolatedAsyncioTestCase):
 async def test_redirect_to_loopback_never_requested(self):
  from lib.media_download import download
  class Response:
   status=302;headers={'Location':'https://127.0.0.1/private'}
   async def __aenter__(self):return self
   async def __aexit__(self,*args):pass
  class Session:
   calls=[]
   async def __aenter__(self):return self
   async def __aexit__(self,*args):pass
   def get(self,url,**kwargs):self.calls.append(url);return Response()
  session=Session()
  with patch('aiohttp.ClientSession',return_value=session),patch('aiohttp.TCPConnector'):
   with self.assertRaises(MediaError):await download('https://images.example/a',{'images.example','127.0.0.1'})
  self.assertEqual(session.calls,['https://images.example/a'])
 async def test_stream_limit_without_length(self):
  from lib.media_download import download
  class Content:
   async def iter_chunked(self,n):yield b'x'*5;yield b'y'*6
  class Response:
   status=200;headers={'Content-Type':'image/png'};content_length=None;content=Content()
   async def __aenter__(self):return self
   async def __aexit__(self,*args):pass
  class Session:
   async def __aenter__(self):return self
   async def __aexit__(self,*args):pass
   def get(self,*a,**k):return Response()
  with patch('aiohttp.ClientSession',return_value=Session()),patch('aiohttp.TCPConnector'):
   with self.assertRaises(MediaError):await download('https://images.example/a',{'images.example'},10)

if __name__=='__main__':unittest.main()
