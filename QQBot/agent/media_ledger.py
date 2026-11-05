"""Durable media quota reservations, dedupe and outcome records."""
import hashlib,json,os,secrets,sqlite3,time
from pathlib import Path
from contextlib import contextmanager

# label, default, minimum, maximum. Shared by the panel and both runtime gates.
LIMITS = {
 'auto_global_daily': ('全局自动发言 / 自然日',1000,0,100000),
 'auto_global_seconds': ('全局自动发言间隔（秒）',1,0,86400),
 'media_event': ('单条触发消息图片上限',1,0,5),
 'media_actor': ('同一成员图片 / 滚动24小时',10,0,10000),
 'media_scope': ('同一会话图片 / 滚动窗口',2,0,10000),
 'media_window': ('会话图片窗口（秒）',300,1,86400),
 'recent_assets': ('同会话、同人格近期去重条数',8,0,100),
 'attempt_scope': ('单会话抓图尝试 / 滚动24小时',30,0,100000),
 'attempt_global': ('全局抓图尝试 / 滚动24小时',1000,0,1000000),
}

def policy(conn=None):
 if conn is None:
  with db() as connection:return policy(connection)
 row=conn.execute('SELECT revision,settings FROM limits WHERE id=1').fetchone()
 values={k:v[1] for k,v in LIMITS.items()}
 if row:values.update(json.loads(row[1]))
 return {'revision':row[0] if row else 0,'values':values}

def set_policy(patch,revision):
 for k,v in patch.items():
  if k not in LIMITS or type(v) is not int or not LIMITS[k][2]<=v<=LIMITS[k][3]:
   raise ValueError('配额字段或数值范围错误：'+k)
 with db() as conn:
  conn.execute('BEGIN IMMEDIATE')
  current=policy(conn)
  if revision!=current['revision']:
   from .group_state import Conflict
   raise Conflict(current['revision'])
  current['values'].update(patch)
  conn.execute('INSERT OR REPLACE INTO limits VALUES (1,?,?)',(revision+1,json.dumps(current['values'])))
  return policy(conn)

def recent_assets(conn,scope,persona,limit):
 return {row[0] for row in conn.execute("SELECT asset FROM tickets WHERE scope=? AND persona=? AND status IN ('queued','sent','uncertain') ORDER BY created DESC LIMIT ?",(scope,persona,limit))}

def root():
 p=Path(os.environ.get('MEDIA_DATA_ROOT',str(Path(__file__).resolve().parent.parent/'data/media_runtime')));p.mkdir(parents=True,exist_ok=True);return p
@contextmanager
def db():
 c=sqlite3.connect(root()/'ledger.sqlite3',timeout=10)
 try:
  c.execute('CREATE TABLE IF NOT EXISTS limits(id INTEGER PRIMARY KEY,revision INTEGER,settings TEXT)')
  c.execute('CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY,scope TEXT,actor TEXT,persona TEXT,event TEXT,asset TEXT,status TEXT,created REAL)')
  c.execute('CREATE TABLE IF NOT EXISTS attempts(scope TEXT,created REAL)')
  c.execute('CREATE TABLE IF NOT EXISTS candidates(id TEXT PRIMARY KEY,scope TEXT,url TEXT,expires REAL)')
  c.execute('CREATE TABLE IF NOT EXISTS cache(key TEXT PRIMARY KEY,path TEXT,hash TEXT,expires REAL,policy TEXT)')
  with c:yield c
 finally:c.close()

def attempt(scope):
 now=time.time()
 with db() as c:
  c.execute('BEGIN IMMEDIATE');c.execute('DELETE FROM attempts WHERE created<?',(now-86400,))
  limits=policy(c)['values']
  if c.execute('SELECT count(*) FROM attempts WHERE scope=?',(scope,)).fetchone()[0]>=limits['attempt_scope']:return False
  if c.execute('SELECT count(*) FROM attempts').fetchone()[0]>=limits['attempt_global']:return False
  c.execute('INSERT INTO attempts VALUES (?,?)',(scope,now));return True

def reserve(scope,actor,persona,event,asset,repeat=False):
 now=time.time();ticket=secrets.token_hex(12)
 with db() as c:
  c.execute('BEGIN IMMEDIATE')
  c.execute("UPDATE tickets SET status='uncertain' WHERE status='queued' AND created<?",(now-600,))
  active="status IN ('queued','sent','uncertain')"
  limits=policy(c)['values']
  if c.execute(f'SELECT count(*) FROM tickets WHERE scope=? AND event=? AND {active}',(scope,event)).fetchone()[0]>=limits['media_event']:return ''
  if c.execute(f'SELECT count(*) FROM tickets WHERE actor=? AND created>? AND {active}',(actor,now-86400)).fetchone()[0]>=limits['media_actor']:return ''
  if c.execute(f'SELECT count(*) FROM tickets WHERE scope=? AND created>? AND {active}',(scope,now-limits['media_window'])).fetchone()[0]>=limits['media_scope']:return ''
  if not repeat and asset in recent_assets(c,scope,persona,limits['recent_assets']):return ''
  c.execute('INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?)',(ticket,scope,actor,persona,event,asset,'queued',now))
 return ticket

def outcome(ticket,status):
 if status not in ('sent','failed','dropped','uncertain'):raise ValueError('invalid outcome')
 with db() as c:c.execute("UPDATE tickets SET status=? WHERE id=? AND status='queued'",(status,ticket))

def candidate(scope,url):
 token=secrets.token_hex(12)
 with db() as c:
  c.execute('DELETE FROM candidates WHERE expires<?',(time.time(),))
  c.execute('INSERT INTO candidates VALUES (?,?,?,?)',(token,scope,url,time.time()+600))
 return token

def candidate_url(scope,token):
 with db() as c:r=c.execute('SELECT url FROM candidates WHERE id=? AND scope=? AND expires>?',(token,scope,time.time())).fetchone()
 return r[0] if r else ''
