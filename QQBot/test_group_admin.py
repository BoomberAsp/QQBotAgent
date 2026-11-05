import json,tempfile,unittest
from types import SimpleNamespace as NS
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from agent.group_state import GroupStore,Conflict,key
from agent.group_service import GroupService
from agent.group_commands import parse,authorized

PM=NS(is_builtin=lambda x:x in ('assistant','rubi','herta'),get_default=lambda:'assistant',list_personalities=lambda:[dict(key='rubi',display_name='露比')])
class GroupAdminTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.store=GroupStore(self.tmp.name);self.service=GroupService(self.store,PM);self.scope=key('onebot','bot','group','100')
  self.store.group(self.scope)
 def test_aliases_and_permissions(self):
  self.assertEqual(parse('#toggle personality 露比')[0].name,'/群人格')
  self.assertEqual(parse('/toggle image off')[1],'image 关')
  self.assertEqual(parse('/toggle')[0].name,'/群面板')
  self.assertFalse(authorized('admin','regular','owner'))
  self.assertTrue(authorized('group_or_bot','regular','owner'))
 def test_switch_notice_pending_and_stale_confirm(self):
  s=self.service;scope=self.scope
  self.store.commit(scope,0,'a','u','hello','old')
  state=s.patch(scope,dict(persona='rubi',history='keep'),'actor','admin',0,prompt=True)
  p=state['config']['persona_pending'];self.assertEqual(len(self.store.history(scope)),3)
  self.assertIn('切换',self.store.history(scope)[-1]['content'])
  s.patch(scope,dict(persona='herta',history='keep'),'actor','admin',state['config_revision'],prompt=True)
  with self.assertRaises(ValueError):s.choose_history(scope,'actor','admin',p['operation_id'],'clear')
  p=self.store.group(scope)['config']['persona_pending']
  with self.assertRaises(ValueError):s.choose_history(scope,'other','admin',p['operation_id'],'clear')
  s.choose_history(scope,'actor','admin',p['operation_id'],'clear')
  self.assertEqual(self.store.history(scope),[])
 def test_revision_metadata_and_atomic_feature_patch(self):
  s=self.service;scope=self.scope
  s.patch(scope,{'memo':'secret'},'web','admin',0)
  self.assertEqual(self.store.group(scope)['generation'],0)
  with self.assertRaises(Conflict):s.patch(scope,{'quiet':''},'web','admin',0)
  with ThreadPoolExecutor(2) as pool:
   results=[pool.submit(s.patch,scope,{'features':{k:False}},'web','admin') for k in ['image','voice']]
   for result in results:result.result()
  features=self.store.group(scope)['config']['features']
  self.assertFalse(features['image']);self.assertFalse(features['voice'])
 def test_config_validation_and_reset(self):
  for patch in [{'active_enabled':True},{'features':{'image':1}},{'receive_all':1},{'mode':'natural'},{'quiet':'25:00-09:00'}]:
   with self.assertRaises(ValueError):self.service.patch(self.scope,patch,'web','admin')
  state=self.service.patch(self.scope,{'receive_all':True,'mode':'natural'},'web','admin')
  state=self.service.patch(self.scope,{'receive_all':False},'web','admin',state['config_revision'])
  self.assertEqual(state['config']['mode'],'mention')
 def test_scope_bound_single_use_token(self):
  s=self.service;t=s.token(self.scope,'actor',dict(kind='patch',patch={'features':{'image':False}}))
  with self.assertRaises(ValueError):s.consume(self.scope,'other',t,'admin')
  s.consume(self.scope,'actor',t,'admin')
  with self.assertRaises(ValueError):s.consume(self.scope,'actor',t,'admin')
 def test_platform_unknown_and_out_of_order(self):
  self.assertEqual(self.store.group(self.scope)['config']['platform_full_events'],'unknown')
  self.service.observe(self.scope,'full',20)
  self.service.observe(self.scope,'reject',30)
  self.service.observe(self.scope,'receive',25)
  c=self.store.group(self.scope)['config'];self.assertFalse(c['active_enabled']);self.assertEqual(c['last_full_event_at'],20)
 def test_legacy_explicit_target_and_protection(self):
  root=Path(self.tmp.name)/'legacy';root.mkdir();(root/'group_features.json').write_text(json.dumps({'100':{'image':False}}))
  entries=self.service.legacy(root);self.assertEqual(len(entries),1)
  other=key('onebot','bot','group','200');self.store.group(other)
  with self.assertRaises(ValueError):self.service.migrate(entries[0]['id'],other,'web',0)
  self.service.migrate(entries[0]['id'],'','web',None,True)
  self.assertEqual(self.service.legacy(root)[0]['status'],'ignored')

if __name__=='__main__':unittest.main()
