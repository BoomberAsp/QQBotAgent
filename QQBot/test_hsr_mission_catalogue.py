"""Mission catalogue is useful evidence, but neither a release calendar nor dialogue."""
import hashlib
import json
import sqlite3

import pytest

from test_hsr_story_router import configured
from lib.hsr_story import HsrStoryKB, render_result, tokens


@pytest.fixture
def catalogue_kb(configured):
    release=configured[0]/'story_kb/hsr'
    with sqlite3.connect(release/'story.db') as db:
        db.executescript('''
        CREATE TABLE mission_chapter(chapter_id INTEGER PRIMARY KEY,unit_id TEXT,title TEXT,
          stage TEXT,sequence_label TEXT,display_priority INTEGER,provenance TEXT);
        CREATE TABLE mission_node(mission_id INTEGER PRIMARY KEY,unit_id TEXT,chapter_id INTEGER,
          mission_type TEXT,in_raid INTEGER,title TEXT,conditions TEXT,provenance TEXT);
        CREATE TABLE mission_edge(edge_id TEXT,from_mission_id INTEGER,to_mission_id INTEGER,
          relation TEXT,resolution TEXT,provenance TEXT);
        ''')
        loc=json.dumps(dict(table='mission_chapter',asset_hash='123',byte_offset=456))
        db.execute('INSERT INTO mission_chapter VALUES (?,?,?,?,?,?,?)',
            (105010,'ch','此刻，将群星付之一笑','二相乐园','第五幕•第八节',1039,loc))
        db.execute('INSERT INTO mission_chapter VALUES (?,?,?,?,?,?,?)',
            (999999,'old','旧章节','旧世界','旧节',1,loc))
        for mid,cid,raid,title in [(1054701,105010,0,'世界图像的时代'),
                                  (1054703,105010,1,'副本同名任务'),(9999999,999999,0,'旧任务')]:
            db.execute('INSERT INTO mission_node VALUES (?,?,?,?,?,?,?,?)',
                (mid,str(mid),cid,'main',raid,title,'{}',loc))
        db.execute("INSERT INTO meta VALUES ('game_version','4.7.0')")
        db.execute("INSERT INTO unit VALUES ('ch','此刻，将群星付之一笑','mission_catalog')")
        db.execute("INSERT INTO scene VALUES ('chs','ch','任务资料（非对白）')")
        db.execute("INSERT INTO block VALUES ('chb','chs',NULL,NULL,NULL,NULL,'unknown','chapter_title','此刻，将群星付之一笑')")
        db.execute("INSERT INTO chunk VALUES (50,'chc','此刻，将群星付之一笑','ch','chs','chb','verified')")
        db.execute('INSERT INTO chunk_fts(rowid,title_terms,body_terms) VALUES (50,?,?)',
                   (' '.join(tokens('此刻，将群星付之一笑')), ' '.join(tokens('此刻，将群星付之一笑'))))
    manifest=json.loads((release/'manifest.json').read_text())
    manifest['database_sha256']=hashlib.sha256((release/'story.db').read_bytes()).hexdigest()
    manifest['native_mission_graph']='metadata_only'
    (release/'manifest.json').write_text(json.dumps(manifest))
    return HsrStoryKB(release/'story.db')


def test_latest_title_uses_catalogue_without_claiming_release_or_story_coverage(catalogue_kb):
    result=catalogue_kb.retrieve('现在最新开拓任务标题是什么')
    assert result['status']=='catalogue_evidence'
    assert result['catalogue'][0]['title']=='此刻，将群星付之一笑'
    assert result['catalogue'][0]['selection']=='display_priority_candidates'
    text=render_result(result,persona='nous')
    assert '此刻，将群星付之一笑' in text and len(text)<=1800
    assert '不代表已核验的最新正式服剧情' in text
    assert result['database']['verified_main_dialogue_chunks']==2


def test_explicit_chapter_membership_excludes_raid_copies(catalogue_kb):
    result=catalogue_kb.retrieve('此刻，将群星付之一笑有哪些任务')
    assert len(result['catalogue'])==1
    assert [m['mission_id'] for m in result['catalogue'][0]['missions']]==[1054701]
    text=render_result(result,persona='nous',budget=3200)
    assert '世界图像的时代' in text and '副本同名任务' not in text
    assert '任务编号顺序也不是剧情顺序' in text


def test_catalogue_title_does_not_satisfy_dialogue_quote(catalogue_kb):
    result=catalogue_kb.retrieve('此刻，将群星付之一笑的对白原文')
    assert result['catalogue'] and not result['hits']
    text=render_result(result,persona='nous',budget=3200)
    assert '对白与演出尚未恢复' in text


@pytest.mark.parametrize('query',['翁法罗斯现在怎么样了','3.7版本最新开拓任务标题','999.9最新主线标题'])
def test_world_state_or_unknown_historical_version_not_replaced_by_newest_id(catalogue_kb,query):
    assert not catalogue_kb.retrieve(query)['catalogue']


def test_old_database_remains_readable(configured):
    kb=HsrStoryKB(configured[0]/'story_kb/hsr/story.db')
    assert not kb.has_catalogue
    assert kb.retrieve('卡芙卡道别')['hits']
