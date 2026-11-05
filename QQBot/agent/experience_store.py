"""Private experience journal and versioned releases. No model/network calls.
Transactions open cognition first, then ATTACH experience, so forgetting and
experience writes share the same generation fence and SQLite commit boundary.
"""
import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA = 1
EXTRACTOR = 'observed-tools-v1'


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def experience_path(cognition):
    return Path(cognition.path).parent.parent / '_experience' / 'experience.sqlite3'


def check_atomic_journals(db):
    for schema in ('main','experience'):
        if db.execute(f'PRAGMA {schema}.journal_mode').fetchone()[0] not in ('delete','truncate','persist'):
            raise ValueError('Experience transactions require SQLite rollback journals')


def invalidate_attached(db, cognition_path, owner, scope):
    """Called inside the cognition mutation transaction; retain only ID audit."""
    path = Path(cognition_path).parent.parent / '_experience' / 'experience.sqlite3'
    if not path.exists():
        return
    db.execute('ATTACH DATABASE ? AS experience', (str(path),))
    check_atomic_journals(db)
    if db.execute('PRAGMA experience.user_version').fetchone()[0] != SCHEMA:
        raise ValueError('Unsupported experience schema; refusing incomplete forget')
    for table in ('jobs', 'cases', 'versions', 'observations'):
        db.execute(f'DELETE FROM experience.{table} WHERE owner=? AND scope=?', (owner,scope))
    db.execute('INSERT INTO experience.audit(event,subject,created) VALUES (?,?,?)',
               ('scope_invalidated',fingerprint([owner,scope]),time.time()))


class ExperienceStore:
    def __init__(self, cognition):
        self.cognition = cognition
        self.path = experience_path(cognition)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, SCHEMA):
                raise ValueError('Unsupported experience schema')
            db.executescript('''
                PRAGMA user_version=1;
                PRAGMA secure_delete=ON;
                CREATE TABLE IF NOT EXISTS receipts(owner TEXT,scope TEXT,task_id TEXT,
                    PRIMARY KEY(owner,scope,task_id));
                CREATE TABLE IF NOT EXISTS observations(owner TEXT,scope TEXT,task_id TEXT,
                    seq INTEGER,payload TEXT,PRIMARY KEY(owner,scope,task_id,seq));
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,owner TEXT,scope TEXT,
                    generation INTEGER,task_id TEXT,payload TEXT,status TEXT,attempts INTEGER,
                    ready REAL,lease TEXT,lease_until REAL);
                CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY,owner TEXT,scope TEXT,
                    generation INTEGER,revision INTEGER,status TEXT,payload TEXT,expires REAL);
                CREATE TABLE IF NOT EXISTS versions(id TEXT,revision INTEGER,owner TEXT,scope TEXT,
                    payload TEXT,PRIMARY KEY(id,revision));
                CREATE TABLE IF NOT EXISTS releases(id TEXT PRIMARY KEY,created REAL);
                CREATE TABLE IF NOT EXISTS members(release_id TEXT,case_id TEXT,revision INTEGER,
                    PRIMARY KEY(release_id,case_id));
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,event TEXT,subject TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS applications(owner TEXT,scope TEXT,task_id TEXT,release_id TEXT,
                    case_id TEXT,revision INTEGER,mode TEXT,outcome TEXT,created REAL,
                    PRIMARY KEY(owner,scope,task_id,case_id,revision,mode));
                CREATE INDEX IF NOT EXISTS case_scope ON cases(owner,scope,status);
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def transaction(self):
        with self.cognition.db() as db:
            db.execute('ATTACH DATABASE ? AS experience', (str(self.path),))
            check_atomic_journals(db)
            db.execute('PRAGMA experience.secure_delete=ON')
            db.execute('BEGIN IMMEDIATE')
            yield db

    def _valid(self, db, owner, scope, generation):
        row = db.execute('SELECT generation,paused FROM scopes WHERE owner=? AND scope=?', (owner,scope)).fetchone()
        return row is not None and not row['paused'] and row['generation'] == generation

    def audit(self, db, event, subject):
        db.execute('INSERT INTO experience.audit(event,subject,created) VALUES (?,?,?)', (event,subject,time.time()))

    def observe(self, owner, scope, generation, task_id, seq, payload, record=None):
        with self.transaction() as db:
            if not self._valid(db,owner,scope,generation): return False
            if db.execute('SELECT 1 FROM experience.receipts WHERE owner=? AND scope=? AND task_id=?', (owner,scope,task_id)).fetchone(): return False
            db.execute('INSERT OR IGNORE INTO experience.observations VALUES (?,?,?,?,?)', (owner,scope,task_id,seq,dumps(payload)))
            if record is not None:
                jid=fingerprint([owner,scope,task_id,EXTRACTOR])[:24]
                db.execute("INSERT INTO experience.jobs VALUES (?,?,?,?,?,?,?,0,?,'',0) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,ready=excluded.ready WHERE status='recording'",
                    (jid,owner,scope,generation,task_id,dumps(record),'recording',time.time()+1800))
            return True

    def submit(self, owner, scope, generation, task_id, record):
        jid = fingerprint([owner,scope,task_id,EXTRACTOR])[:24]
        with self.transaction() as db:
            if not self._valid(db,owner,scope,generation): return ''
            if not db.execute('SELECT 1 FROM experience.observations WHERE owner=? AND scope=? AND task_id=?', (owner,scope,task_id)).fetchone(): return ''
            if db.execute('SELECT 1 FROM experience.receipts WHERE owner=? AND scope=? AND task_id=?', (owner,scope,task_id)).fetchone(): return jid
            db.execute('INSERT INTO experience.receipts VALUES (?,?,?)', (owner,scope,task_id))
            db.execute("INSERT INTO experience.jobs VALUES (?,?,?,?,?,?,?,0,0,?,0) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,status='pending',ready=0 WHERE status='recording'", (jid,owner,scope,generation,task_id,dumps(record),'pending',''))
            self.audit(db,'enqueued',jid)
        return jid

    def claim(self, now=None):
        now = time.time() if now is None else now
        with self.transaction() as db:
            row = db.execute("SELECT * FROM experience.jobs WHERE (status IN ('pending','retry_wait','recording') AND ready<=?) OR (status='running' AND lease_until<=?) ORDER BY ready,id LIMIT 1", (now,now)).fetchone()
            if row is None: return None
            if row['attempts'] >= 3:
                db.execute("UPDATE experience.jobs SET status='failed' WHERE id=?",(row['id'],))
                return None
            db.execute('INSERT OR IGNORE INTO experience.receipts VALUES (?,?,?)',(row['owner'],row['scope'],row['task_id']))
            lease=uuid.uuid4().hex
            db.execute("UPDATE experience.jobs SET status='running',lease=?,lease_until=?,attempts=attempts+1 WHERE id=?", (lease,now+60,row['id']))
            result=dict(row);result['lease']=lease;result['attempts']+=1
            result['observations']=[json.loads(r[0]) for r in db.execute('SELECT payload FROM experience.observations WHERE owner=? AND scope=? AND task_id=? ORDER BY seq', (row['owner'],row['scope'],row['task_id']))]
            return result

    def finish(self, job, case):
        with self.transaction() as db:
            row=db.execute("SELECT * FROM experience.jobs WHERE id=? AND lease=? AND status='running'", (job['id'],job['lease'])).fetchone()
            if row is None: return False
            if not self._valid(db,row['owner'],row['scope'],row['generation']):
                db.execute('DELETE FROM experience.jobs WHERE id=?',(row['id'],));return False
            payload=dumps(case)
            db.execute('INSERT OR IGNORE INTO experience.cases VALUES (?,?,?,?,1,?,?,?)', (job['id'],job['owner'],job['scope'],job['generation'],'candidate',payload,time.time()+90*86400))
            db.execute('INSERT OR IGNORE INTO experience.versions VALUES (?,1,?,?,?)', (job['id'],job['owner'],job['scope'],payload))
            db.execute("UPDATE experience.jobs SET status='completed',payload='{}',lease='' WHERE id=?", (job['id'],))
            self.audit(db,'candidate',job['id'])
            return True

    def fail(self, job):
        with self.transaction() as db:
            db.execute("UPDATE experience.jobs SET status=?,ready=?,lease='' WHERE id=? AND lease=? AND status='running'",
                ('failed' if job['attempts']>=3 else 'retry_wait',time.time()+30*2**min(job['attempts'],5),job['id'],job['lease']))
            self.audit(db,'extractor_error',job['id'])

    def list_cases(self, owner, scope):
        with self.transaction() as db:
            rows=db.execute('SELECT * FROM experience.cases WHERE owner=? AND scope=? ORDER BY id',(owner,scope)).fetchall()
            return [{**dict(r),'payload':json.loads(r['payload'])} for r in rows]

    def review(self, owner, scope, cid, revision, review):
        """Trusted operator API only. Not registered as an LLM tool."""
        required=('reviewer','evidence_sha256','lesson','applicability','steps','outcome','checked')
        if any(not review.get(k) for k in required) or review['checked'] is not True:
            raise ValueError('Review requires explicit checks, evidence, conditions and steps')
        if review['outcome'] not in ('success','partial','failure'):
            raise ValueError('Unknown outcomes cannot be validated')
        if not isinstance(review['steps'],list) or not all(isinstance(s,str) and s.strip() for s in review['steps']):
            raise ValueError('Steps must be nonempty strings')
        if len(dumps(review))>6000: raise ValueError('Review exceeds 6000 characters')
        conditions=review.get('conditions',{})
        if not isinstance(conditions,dict) or set(conditions)-{'session_kind'} or any(v not in ('named','temporary') for v in conditions.values()):
            raise ValueError('Unsupported applicability conditions')
        with self.transaction() as db:
            row=db.execute('SELECT * FROM experience.cases WHERE id=? AND owner=? AND scope=?',(cid,owner,scope)).fetchone()
            if row is None or row['revision']!=revision: raise ValueError('Case missing or revision changed')
            if not self._valid(db,owner,scope,row['generation']) or row['status'] in ('retired','quarantined'):
                raise ValueError('Case no longer eligible')
            case=json.loads(row['payload'])
            if review.get('observation_id') not in case['evidence_refs']:
                raise ValueError('Review must reference an observed tool result')
            digest=review['evidence_sha256']
            if len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
                raise ValueError('Evidence must have a SHA-256 digest')
            case['conditions']=conditions
            case.update({k:review[k] for k in ('lesson','applicability','steps','outcome')})
            case['exclusions']=review.get('exclusions','')
            case['verification']={'basis':'operator_artifact_check','reviewer':review['reviewer'],'evidence_sha256':review['evidence_sha256'],'checked_at':time.time(),'version':'operator-v1'}
            payload=dumps(case);rev=revision+1
            db.execute("UPDATE experience.cases SET revision=?,status='validated',payload=? WHERE id=?",(rev,payload,cid))
            db.execute('INSERT INTO experience.versions VALUES (?,?,?,?,?)',(cid,rev,owner,scope,payload))
            self.audit(db,'validated',cid+':'+str(rev))
        return rev

    def release(self, selections):
        """Replace active collection atomically. Exact reviewed revisions required."""
        rid=uuid.uuid4().hex[:16]
        with self.transaction() as db:
            for cid,rev in selections:
                r=db.execute('SELECT * FROM experience.cases WHERE id=? AND revision=?',(cid,rev)).fetchone()
                if r is None or r['status'] not in ('validated','published') or r['expires']<=time.time() or not self._valid(db,r['owner'],r['scope'],r['generation']):
                    raise ValueError('Release includes unreviewed, expired or invalid case')
            db.execute('INSERT INTO experience.releases VALUES (?,?)',(rid,time.time()))
            for cid,rev in selections:
                db.execute('INSERT INTO experience.members VALUES (?,?,?)',(rid,cid,rev))
                db.execute("UPDATE experience.cases SET status='published' WHERE id=?",(cid,))
            db.execute("INSERT OR REPLACE INTO experience.settings VALUES ('active_release',?)",(rid,))
            self.audit(db,'release',rid)
        return rid

    def rollback(self, rid):
        with self.transaction() as db:
            if not db.execute('SELECT 1 FROM experience.releases WHERE id=?',(rid,)).fetchone(): raise ValueError('Unknown release')
            db.execute("INSERT OR REPLACE INTO experience.settings VALUES ('active_release',?)",(rid,))
            self.audit(db,'rollback',rid)

    def retire(self, owner, scope, cid, quarantine=False):
        with self.transaction() as db:
            cur=db.execute('UPDATE experience.cases SET status=? WHERE id=? AND owner=? AND scope=?',('quarantined' if quarantine else 'retired',cid,owner,scope))
            if cur.rowcount!=1: raise ValueError('Case not visible')
            self.audit(db,'quarantined' if quarantine else 'retired',cid)

    def retrieve(self, owner, scope, generation, query, versions, environment, release_id=None, limit=2, context=None):
        from .cognitive_state import terms
        with self.transaction() as db:
            if not self._valid(db,owner,scope,generation): return '',[]
            active=db.execute("SELECT value FROM experience.settings WHERE key='active_release'").fetchone()
            rid=release_id or (active[0] if active else '')
            rows=db.execute("SELECT c.*,v.payload AS frozen_payload,m.revision AS frozen_revision FROM experience.members m JOIN experience.cases c ON c.id=m.case_id JOIN experience.versions v ON v.id=m.case_id AND v.revision=m.revision WHERE m.release_id=? AND c.owner=? AND c.scope=? AND c.status='published' AND c.generation=? AND c.expires>?", (rid,owner,scope,generation,time.time())).fetchall()
            matches=[];q=terms(query)
            for row in rows:
                case=json.loads(row['frozen_payload'])
                if case['environment']!=environment or not case.get('verification'):continue
                if any((context or {}).get(k)!=v for k,v in case.get('conditions',{}).items()):continue
                if any(versions.get(k)!=v for k,v in case['tool_versions'].items()):continue
                overlap=len(q & terms(case['problem_summary']))
                if not overlap:continue
                matches.append({**case,'case_id':row['id'],'revision':row['frozen_revision'],'score':overlap})
            matches.sort(key=lambda c:(-c['score'],c['case_id']))
            # Equivalent methods from duplicate task reports do not crowd the budget.
            picked=[];seen=set()
            for case in matches:
                signature=fingerprint([case['steps'],case['lesson'],case['outcome']])
                if signature in seen:continue
                seen.add(signature);picked.append(case)
                if len(picked)>=limit:break
            return rid,picked

    def application(self, owner,scope,task_id,rid,cases,mode):
        with self.transaction() as db:
            for case in cases:
                row=db.execute('SELECT * FROM experience.cases WHERE id=? AND owner=? AND scope=?',(case['case_id'],owner,scope)).fetchone()
                if not row or row['status']!='published' or not self._valid(db,owner,scope,row['generation']):continue
                db.execute('INSERT OR IGNORE INTO experience.applications VALUES (?,?,?,?,?,?,?,?,?)',(owner,scope,task_id,rid,case['case_id'],case['revision'],mode,'unknown',time.time()))

    def feedback(self, owner,scope,task_id,cid,outcome):
        if outcome not in ('confirmed_success','counterexample','unknown'): raise ValueError('Invalid feedback')
        with self.transaction() as db:
            cur=db.execute("UPDATE experience.applications SET outcome=? WHERE owner=? AND scope=? AND task_id=? AND case_id=? AND mode='injected'",(outcome,owner,scope,task_id,cid))
            if not cur.rowcount:raise ValueError('Application not visible')
            if outcome=='counterexample':db.execute("UPDATE experience.cases SET status='quarantined' WHERE id=? AND owner=? AND scope=?",(cid,owner,scope))
            self.audit(db,'feedback_'+outcome,cid)

    def purge_expired(self, now=None):
        now=time.time() if now is None else now
        with self.transaction() as db:
            rows=db.execute('SELECT * FROM experience.cases WHERE expires<=?',(now,)).fetchall()
            for row in rows:
                case=json.loads(row['payload'])
                for task_id in case['source_task_ids']:
                    db.execute('DELETE FROM experience.observations WHERE owner=? AND scope=? AND task_id=?',(row['owner'],row['scope'],task_id))
                db.execute('DELETE FROM experience.jobs WHERE id=?',(row['id'],))
                db.execute('DELETE FROM experience.versions WHERE id=?',(row['id'],))
                db.execute('DELETE FROM experience.cases WHERE id=?',(row['id'],))
                self.audit(db,'expired',row['id'])
            # Jobs that never produced a case and orphan observations also expire.
            pending=db.execute("SELECT id,payload,owner,scope,task_id FROM experience.jobs WHERE status!='completed'").fetchall()
            for row in pending:
                if json.loads(row['payload']).get('ts',now) < now-90*86400:
                    db.execute('DELETE FROM experience.jobs WHERE id=?',(row['id'],))
                    db.execute('DELETE FROM experience.observations WHERE owner=? AND scope=? AND task_id=?',(row['owner'],row['scope'],row['task_id']))
            for row in db.execute('SELECT owner,scope,task_id,seq,payload FROM experience.observations').fetchall():
                if json.loads(row['payload']).get('ts',now) < now-90*86400:
                    db.execute('DELETE FROM experience.observations WHERE owner=? AND scope=? AND task_id=? AND seq=?',(row['owner'],row['scope'],row['task_id'],row['seq']))
            return len(rows)

    def set_mode(self, mode):
        if mode not in ('off','shadow','inject'):raise ValueError('Invalid experience mode')
        with self.transaction() as db:
            db.execute("INSERT OR REPLACE INTO experience.settings VALUES ('mode',?)",(mode,))
            self.audit(db,'mode',mode)
