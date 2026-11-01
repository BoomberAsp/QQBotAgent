import asyncio,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
root=Path(__file__).resolve().parents[1];sys.path[:0]=[str(root),str(root/'QQBot')]
from lib.bond_names import reconcile
from tools import wiki_scraper as ws, bond_detail as bd
A={'A1':{'id':'A1','names':{'en':'Title','zh_hans':'官方名'}}}
class Names(unittest.TestCase):
 def test_priority_and_idempotence(self):
  e={'id':'A1','title':'Title','name_cn':'旧译名','_card_hash':'stale'}
  reconcile(e,A);self.assertEqual(e['name_cn'],'官方名');self.assertIn('旧译名',e['name_aliases'])
  self.assertNotIn('_card_hash',e)
  first=json.dumps(e);reconcile(e,A);self.assertEqual(first,json.dumps(e))
  e.update(name_cn='人工名',_manual_fields=['name_cn']);reconcile(e,A);self.assertEqual(e['name_cn'],'人工名')
  for invalid in ['A1','T_Item_Name_A1','English']:
   e={'id':'A1','title':'Title','name_cn':'旧译名'};reconcile(e,{'A1':{'names':{'zh_hans':invalid}}});self.assertEqual(e['name_cn'],'旧译名')
  e={'title':'Title'};reconcile(e,A);self.assertEqual(e['id'],'A1')
  e={'id':'WRONG','title':'Title','name_cn':'旧译名'};reconcile(e,A);self.assertEqual(e['name_cn'],'旧译名')
 def test_refresh_unchanged_and_lookup(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);(p/'artifacts.json').write_text(json.dumps({'artifacts':A}))
   source={'title':'Title','id':'A1','stars':'5','desc_en':'description'}
   old={**source,'name_cn':'旧译名','_src_hash':ws.WikiScraper._source_hash(source)}
   class Fake(ws.WikiScraper):
    async def _fetch_template_pages(self,*a):return [('Title','raw')]
    def _parse_bond_full(self,*a):return dict(source)
    async def _fetch_bond_release_map(self):return {}
    def _load_bond_cache(self):return {'Title':dict(old)}
    async def _translate_bond_details(self,*a):raise AssertionError('unchanged cache must not translate')
    def _download_bond_icon(self,*a):return None
    async def _background_download_then_render(self,*a):pass
   with patch.dict(os.environ,GAMEDATA_DIR=tmp):
    scraper=Fake();scraper._cache_dir=tmp;scraper._bond_detail_cache=str(p/'bond_details.json')
    result=asyncio.run(scraper.refresh_bonds());self.assertEqual(result['Title']['name_cn'],'官方名')
    saved=json.loads((p/'bond_details.json').read_text());self.assertIn('旧译名',saved['data']['Title']['name_aliases'])
    with patch.object(bd,'_CACHE_PATH',str(p/'bond_details.json')),patch.object(bd,'_data',None):
     self.assertEqual(bd.lookup_bond('旧译名')['id'],'A1');self.assertEqual(bd.lookup_bond('官方名')['id'],'A1')
if __name__=='__main__':unittest.main()
