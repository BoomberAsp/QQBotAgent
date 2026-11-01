import json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
root=Path(__file__).resolve().parents[1];sys.path[:0]=[str(root),str(root/'QQBot')]
from lib import character_entry as ce
from tools import character_detail as cd
from scripts import render_skill_cards as cards
from webui import data_reader as dr
from lib.game_names import reconcile
class RoleNames(unittest.TestCase):
 def test_names_card_and_hot_reload(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp);rp=p/'roles.json';wp=p/'character_details.json'
   role={'id':'H1','names':{'en':'Title','zh_hans':'官方名'},'skills':[]}
   old={'id':'H1','title':'Title','name_cn':'旧译名','_card_hash':'stale'}
   def write_roles():rp.write_text(json.dumps({'roles':{'H1':role}},ensure_ascii=False))
   def write_wiki():wp.write_text(json.dumps({'data':{'Title':old}},ensure_ascii=False))
   write_roles();write_wiki()
   with patch.dict(os.environ,GAMEDATA_DIR=tmp),patch.object(cd,'_CACHE_PATH',str(wp)),patch.object(cd,'_data',None),patch.object(ce,'wiki_cache_dir',return_value=tmp),patch.object(dr,'_gamedata_roles',return_value={'H1':role}),patch.dict(dr._ALIAS_DETAILS_FILES,char=wp):
    ce.invalidate()
    self.assertEqual(cd.lookup_character('旧译名')['name_cn'],'官方名')
    self.assertEqual(dr.wiki_details_entry('char','Title')['entry']['name_cn'],'官方名')
    built=cards.build_entry('H1',role,old,{})
    self.assertEqual(built['name_cn'],'官方名');self.assertNotEqual(built.get('_card_hash'),'stale')
    self.assertIn('旧译名',built['name_aliases'])
    def load(_):return {'roles':json.loads(rp.read_text())['roles']}
    with patch.object(cards,'load_gamedata',side_effect=load),patch.object(cards,'build_maps'):
     self.assertEqual(ce.build_entry('H1')['name_cn'],'官方名')
     role['names']['zh_hans']='更新的官方名';write_roles()
     self.assertEqual(ce.build_entry('更新的官方名')['name_cn'],'更新的官方名')
     self.assertEqual(cd.lookup_character('旧译名')['name_cn'],'更新的官方名')
     old.update(name_cn='人工名称',_manual_fields=['name_cn']);write_wiki()
     self.assertEqual(ce.build_entry('H1')['name_cn'],'人工名称')
     self.assertEqual(cd.lookup_character('人工名称')['id'],'H1')
     self.assertEqual(dr.wiki_details_entry('char','Title')['entry']['name_cn'],'人工名称')
    ce.invalidate()
 def test_heal_keeps_old_name(self):
  from tools.wiki_scraper import WikiScraper
  e={'id':'H1','title':'Title','name_cn':'旧译名'}
  WikiScraper._heal_name_cn(e,{'title':'字典名'})
  reconcile(e,{'H1':{'names':{'zh_hans':'官方名'}}})
  self.assertEqual(e['name_cn'],'官方名');self.assertEqual(e['name_aliases'],['旧译名','字典名'])
if __name__=='__main__':unittest.main()
