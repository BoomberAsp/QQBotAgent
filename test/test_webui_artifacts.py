import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
root=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(root),str(root/'QQBot')]
from webui import data_reader as d

class ArtifactViews(unittest.TestCase):
    def test_merge_aliases_readonly_and_hot_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); wiki=p/'bond_details.json'; dic=p/'aliases.json'; gd=p/'artifacts.json'
            wiki.write_text(json.dumps({'data':{'Old Title':{'id':'A1','name_cn':'人工译名','effect':'manual','_manual_fields':['effect']}}}),encoding='utf-8')
            dic.write_text('{}',encoding='utf-8')
            entries={'A1':{'id':'A1','names':{'en':'New Title','zh_hans':'官方名'},'star':5},'A2':{'id':'A2','names':{'en':'New Bond','zh_hans':'新羁绊'},'star':4},'A9999':{'id':'A9999','names':{'zh_hans':'A9999'}}}
            gd.write_text(json.dumps({'artifacts':entries}),encoding='utf-8')
            before=wiki.read_bytes()
            with patch.object(d.config,'GAMEDATA_DIR',p), patch.dict(d._ALIAS_FILES,bond=dic), patch.dict(d._ALIAS_DETAILS_FILES,bond=wiki):
                from webui import auth
                from webui.main import app
                from fastapi.testclient import TestClient
                with patch.object(auth, 'require_setup', return_value=False), patch.object(auth, 'is_authenticated', return_value=True):
                    client=TestClient(app)
                    response=client.get('/api/wiki/details/gamedata?kind=bond&rid=A2')
                    self.assertEqual(response.status_code,200)
                    self.assertEqual(response.json()['kind'],'gamedata_bond')
                    self.assertEqual(client.get('/api/wiki/details/gamedata?kind=bond&rid=absent').status_code,404)
                rows=d.wiki_details_list('bond')['items']
                self.assertEqual(len(rows),4)
                self.assertEqual(len(d.wiki_details_list('bond','A2')['items']),1)
                missing=d.wiki_aliases_missing('bond')['items']
                self.assertEqual(len(missing),2)
                official=next(x for x in missing if x['source']=='wiki')
                self.assertEqual(official['canonical'],'官方名')
                self.assertIn('人工译名',official['aliases_hint'])
                self.assertIn('官方名',next(x for x in missing if x['source']=='wiki')['aliases_hint'])
                self.assertEqual(d.wiki_details_gamedata_entry('A2','bond')['entry']['id'],'A2')
                self.assertIn('error',d.wiki_details_gamedata_entry('A2','invalid'))
                self.assertEqual(d.wiki_details_entry('bond','Old Title')['entry']['effect'],'manual')
                self.assertEqual(wiki.read_bytes(),before)
                dic.write_text(json.dumps({'A1':'人工译名'}),encoding='utf-8')
                self.assertEqual(len(d.wiki_aliases_missing('bond')['items']),1)
                gd.write_text('{bad',encoding='utf-8')
                self.assertEqual(len(d.wiki_details_list('bond')['items']),1)
                gd.unlink()
                self.assertEqual(d._gamedata_artifacts(),{})

if __name__=='__main__': unittest.main()
