"""Durable media quota reservations, dedupe and outcome records."""
import hashlib,json,os,secrets,sqlite3,time
from pathlib import Path
from contextlib import contextmanager

def root():
 p=Path(os.environ.get('MEDIA_DATA_ROOT',str(Path(__file__).resolve().parent.parent/'data/media_runtime')));p.mkdir(parents=True,exist_ok=True);return p
@contextmanager
def db():
 c=sqlite3.connect(root()/'ledger.sqlite3',timeout=10)
 try:
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
  if c.execute('SELECT count(*) FROM attempts WHERE scope=?',(scope,)).fetchone()[0]>=30:return False
  if c.execute('SELECT count(*) FROM attempts').fetchone()[0]>=1000:return False
  c.execute('INSERT INTO attempts VALUES (?,?)',(scope,now));return True

def reserve(scope,actor,persona,event,asset,repeat=False):
 now=time.time();ticket=secrets.token_hex(12)
 with db() as c:
  c.execute('BEGIN IMMEDIATE')
  c.execute("UPDATE tickets SET status='uncertain' WHERE status='queued' AND created<?",(now-600,))
  active="status IN ('queued','sent','uncertain')"
  if c.execute(f'SELECT count(*) FROM tickets WHERE scope=? AND event=? AND {active}',(scope,event)).fetchone()[0]:return ''
  if c.execute(f'SELECT count(*) FROM tickets WHERE actor=? AND created>? AND {active}',(actor,now-86400)).fetchone()[0]>=10:return ''
  if c.execute(f'SELECT count(*) FROM tickets WHERE scope=? AND created>? AND {active}',(scope,now-300)).fetchone()[0]>=2:return ''
  recent=c.execute(f'SELECT asset FROM tickets WHERE scope=? AND persona=? AND {active} ORDER BY created DESC LIMIT 8',(scope,persona)).fetchall()
  if not repeat and (asset,) in recent:return ''
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
