import importlib.util,json,sqlite3,tempfile,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('migration',Path(__file__).resolve().parents[1]/'scripts/migrate_legacy_cognition.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class MigrationTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.data=self.root/'data';self.code=self.root/'code';self.data.mkdir();self.code.mkdir()
  self.mapping={'users':{'u':{'platform':'qq_official','bot_id':'bot','character':'rubi'}}}
  self.write('u/profile.json',{'user_id':'u','nickname':'小明','preferences':{'风格':'简洁'},'facts':['喜欢登山'],'interests':['文学']})
  self.write('memory/tiers/u.json',{'user_id':'u','short':[{'id':'s','content':'可能喜欢咖啡','count':2}], 'medium':[{'id':'m','content':'住在杭州','count':5,'last_confirmed_ts':123}], 'long':[]})
 def tearDown(self):self.tmp.cleanup()
 def write(self,path,data):
  p=self.data/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(data,ensure_ascii=False))
 def run_migration(self,apply=False):return m.migrate(self.data,self.code,self.mapping,apply)
 def test_preview_and_idempotent_apply(self):
  before=m.scan(self.data,self.code)
  preview=self.run_migration();self.assertEqual(5,preview['facts']);self.assertFalse((self.data/'_cognition').exists())
  first=self.run_migration(True);second=self.run_migration(True)
  self.assertEqual('unchanged',second['status']);self.assertEqual(before,m.scan(self.data,self.code))
  self.assertTrue((Path(first['backup'])/'sources.tar.gz').exists())
  with sqlite3.connect(self.data/'_cognition/cognitive_state.sqlite3') as db:
   self.assertEqual(0,db.execute('select sum(supports) from facts').fetchone()[0])
   self.assertEqual(123,db.execute("select updated from facts where content='住在杭州'").fetchone()[0])
 def test_blocks_runtime_edits(self):
  self.run_migration(True)
  with sqlite3.connect(self.data/'_cognition/cognitive_state.sqlite3') as db:db.execute('delete from facts')
  with self.assertRaisesRegex(ValueError,'Target changed'):self.run_migration(True)
 def test_refresh_before_runtime(self):
  self.run_migration(True)
  self.write('u/profile.json',{'user_id':'u','facts':['新事实']})
  # Backup names must remain unique even when refreshing in the same second.
  result=self.run_migration(True)
  self.assertEqual(2,result['facts'])
 def test_missing_owner_mapping_aborts(self):
  self.mapping['users']={}
  with self.assertRaises(ValueError):self.run_migration(True)
  self.assertFalse((self.data/'_cognition').exists())
 def test_named_session_matches_scope_and_committed_wins(self):
  self.write('u/sessions/_index.json',{'sessions':[{'name':'会话','session_id':'id','metadata':{'persona_key':'other'},'last_active':456}]})
  self.write('u/sessions/会话/snapshot_00001.json',[{'role':'user','content':'obsolete'}])
  self.write('u/sessions/会话/committed_context.json',[{'role':'user','content':'问题'},{'role':'assistant','content':'回答'}])
  self.run_migration(True)
  with sqlite3.connect(self.data/'_cognition/cognitive_state.sqlite3') as db:
   rows=db.execute("select scope,content from fragments where kind='conversation'").fetchall()
   self.assertEqual(1,len(rows));self.assertEqual(['qq_official:bot','private','other','id'],json.loads(rows[0][0]))
   self.assertEqual('问题',json.loads(rows[0][1])['user'])
 def test_source_change_fails_closed(self):
  from unittest.mock import patch
  actual=m.scan(self.data,self.code)
  with patch.object(m,'scan',side_effect=[actual,{}]):
   with self.assertRaisesRegex(ValueError,'Sources changed'):self.run_migration(True)
  self.assertFalse((self.data/'_cognition').exists())

 def test_pending_transaction_prevents_snapshot(self):
  self.write('u/profile.json.transaction.json',{'writes':[]})
  with self.assertRaisesRegex(ValueError,'Pending profile transaction'):self.run_migration(True)
  self.assertFalse((self.data/'_cognition').exists())
 def test_wal_prevents_replacement_even_if_main_file_unchanged(self):
  self.run_migration(True)
  (self.data/'_cognition/cognitive_state.sqlite3-wal').write_bytes(b'pending')
  with self.assertRaisesRegex(ValueError,'sidecar'):self.run_migration(True)
 def test_existing_unmanaged_target_is_never_replaced(self):
  target=self.data/'_cognition/cognitive_state.sqlite3';target.parent.mkdir();target.write_bytes(b'existing')
  with self.assertRaisesRegex(ValueError,'without migration receipt'):self.run_migration(True)
  self.assertEqual(b'existing',target.read_bytes())

if __name__=='__main__':unittest.main()
