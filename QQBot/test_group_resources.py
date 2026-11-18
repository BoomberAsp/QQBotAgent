import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent.group_resources import retrieve, local_file
from agent.group_state import GroupStore

class ResourceTests(unittest.TestCase):
 def test_chronology_is_not_an_access_filter(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp)
   docs=[dict(id='base',title='星空',text='基础设定',chapter=0,visibility='public'),dict(id='future',title='星空',text='未来秘密',chapter=3,visibility='public'),dict(id='unchaptered',title='星空',text='无章节资料',visibility='public'),dict(id='private',title='星空',text='私有资料',chapter=0,visibility='private')]
   (root/'kb.json').write_text(json.dumps(docs))
   (root/'registry.json').write_text(json.dumps(dict(personas={'hero':['world']},knowledge_bases={'world':{'path':'kb.json'}})))
   with patch.dict(os.environ,PERSONA_RESOURCE_REGISTRY=str(root/'registry.json')):
    out=retrieve('hero','星空','g')
    self.assertIn('基础设定',out);self.assertIn('未来秘密',out);self.assertIn('无章节资料',out);self.assertNotIn('私有资料',out)
    self.assertIn('未来秘密',retrieve('hero','星空','g'))
    self.assertEqual(retrieve('other','星空','g'),'')
    with self.assertRaises(ValueError):local_file(root,'../escape.json')
 def test_old_progress_is_archived_without_resetting_group(self):
  with tempfile.TemporaryDirectory() as tmp:
   store=GroupStore(tmp);before=store.group('g')
   cfg={**before['config'],'story_progress':{'world':3},'memo':'keep'}
   with store.db() as db:
    db.execute('UPDATE groups SET config=? WHERE scope=?',(json.dumps(cfg),'g'))
   store.commit('g',before['generation'],'e','u','question','answer')
   history=store.history('g')
   store=GroupStore(tmp);after=store.group('g')
   self.assertNotIn('story_progress',after['config'])
   self.assertEqual(after['config']['memo'],'keep')
   self.assertEqual(after['generation'],before['generation']+1)
   self.assertEqual(store.history('g'),history)
   with store.db() as db:
    self.assertEqual(json.loads(db.execute('SELECT value FROM retired_story_progress').fetchone()[0]),{'world':3})
   self.assertEqual(GroupStore(tmp).group('g')['generation'],after['generation'])
 def test_retired_admin_patch_is_rejected(self):
  from agent.group_service import GroupService
  with tempfile.TemporaryDirectory() as tmp:
   service=GroupService(GroupStore(tmp),personas=object())
   with self.assertRaises(ValueError):
    service.patch('g',{'story_progress':{'world':3}},'u','admin')
 def test_digest_keeps_speaker_and_clears_on_reset(self):
  with tempfile.TemporaryDirectory() as tmp:
   s=GroupStore(tmp);s.group('g')
   for n in range(25):s.commit('g',0,str(n),'member'+str(n),'topic'+str(n),'answer')
   h=s.history('g');self.assertIn('member0',h[0]['content']);self.assertIn('source',h[0]['content'])
   s.reset('g');self.assertEqual(s.history('g'),[])

if __name__=='__main__':unittest.main()
