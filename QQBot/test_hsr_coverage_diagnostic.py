"""An updated client corpus must not be described as an old database."""
import hashlib
import json
import sqlite3

from test_hsr_story_router import configured
from lib.hsr_story import HsrStoryKB, render_result


def test_updated_client_is_distinct_from_task_coverage(configured):
    release=configured[0]/'story_kb/hsr'
    with sqlite3.connect(release/'story.db') as db:
        db.execute("INSERT INTO meta VALUES ('game_version','4.7.0')")
        db.execute("INSERT INTO meta VALUES ('completeness','partial_native_structure')")
    manifest=json.loads((release/'manifest.json').read_text())
    manifest['database_sha256']=hashlib.sha256((release/'story.db').read_bytes()).hexdigest()
    manifest['native_mission_graph']='not_decoded'
    (release/'manifest.json').write_text(json.dumps(manifest))
    kb=HsrStoryKB(release/'story.db')
    result=kb.retrieve('现在最新开拓任务标题是什么')
    assert result['database']['client_version']=='4.7.0'
    assert result['database']['native_mission_graph']=='not_decoded'
    text=render_result(result,persona='nous',budget=3200)
    assert '客户端版本 4.7.0' in text
    assert '不能据此断言数据库未更新' in text
    assert result['database']['counts']


def test_missing_metadata_stays_unknown(configured):
    kb=HsrStoryKB(configured[0]/'story_kb/hsr/story.db')
    assert kb.coverage['client_version']=='unknown'
    assert kb.coverage['completeness']=='unknown'
