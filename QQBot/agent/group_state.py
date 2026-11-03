"""Group sessions, verified identity links and provenance-filtered user facts.

No NoneBot dependency. SQLite transactions serialize cross-group/private writes;
conversation generations and identity revisions prevent stale background commits.
"""
import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def key(*parts):
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def scope_for(view):
    return key(view.platform, view.self_id, "group", view.group_id)


def identity_for(view):
    # OneBot QQ numbers are the same authenticated identity in group and C2C.
    return key(view.platform, view.self_id,
               "member" if view.is_group and view.platform != "onebot" else "user",
               view.group_id if view.is_group and view.platform != "onebot" else "",
               view.user_id)


DEFAULTS = dict(persona="assistant", mode="mention", receive_all=False,
                quiet="", active_enabled=True, status="active",
                cooldown=180, auto_daily_cap=10, follow_seconds=120,
                follow_limit=2, scheduled_push_enabled=False, features={"gacha":True,"image":True,"voice":True},
                memo="",persona_pending=None,platform_full_events="unknown",proactive_stickers=False,story_progress={})


class GroupStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "groups.sqlite3"
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript('''
            CREATE TABLE IF NOT EXISTS notices(id INTEGER PRIMARY KEY,scope TEXT,created REAL,text TEXT);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS group_actions(token TEXT PRIMARY KEY,scope TEXT,actor TEXT,revision INTEGER,action TEXT,expires REAL);
            CREATE TABLE IF NOT EXISTS legacy_migrations(id TEXT PRIMARY KEY,source TEXT,entry TEXT,target TEXT,status TEXT,actor TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS groups(scope TEXT PRIMARY KEY, config TEXT,
              generation INTEGER DEFAULT 0, memory TEXT DEFAULT '[]');
            CREATE TABLE IF NOT EXISTS turns(scope TEXT, generation INTEGER, event TEXT,
              actor TEXT, user_text TEXT, answer TEXT, created REAL,
              PRIMARY KEY(scope,event));
            CREATE TABLE IF NOT EXISTS events(scope TEXT,event TEXT,created REAL,
              PRIMARY KEY(scope,event));
            CREATE TABLE IF NOT EXISTS identities(identity TEXT PRIMARY KEY,
              target TEXT, bond_user TEXT, revision INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS bindings(code TEXT PRIMARY KEY, origin TEXT,
              scope TEXT, target TEXT, nonce TEXT, expires REAL);
            CREATE TABLE IF NOT EXISTS facts(id TEXT PRIMARY KEY, identity TEXT,
              scope TEXT, event TEXT, text TEXT, visibility TEXT, created REAL,
              status TEXT DEFAULT 'active', revision INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS facts_identity ON facts(identity,status);
            CREATE TABLE IF NOT EXISTS quotas(scope TEXT, day TEXT, count INTEGER,
              last REAL, PRIMARY KEY(scope,day));
            CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,scope TEXT,
              event TEXT,status TEXT,created REAL);
            ''')
            db.execute('BEGIN IMMEDIATE')
            if 'config_revision' not in {r[1] for r in db.execute('PRAGMA table_info(groups)')}:
                db.execute('ALTER TABLE groups ADD COLUMN config_revision INTEGER NOT NULL DEFAULT 0')
            db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version','2')")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.execute("PRAGMA busy_timeout=10000")
                db.execute("PRAGMA secure_delete=ON")
                yield db
        finally:
            db.close()

    def group(self, scope):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO groups(scope,config) VALUES (?,?)",
                       (scope, json.dumps(DEFAULTS)))
            row = dict(db.execute("SELECT * FROM groups WHERE scope=?", (scope,)).fetchone())
        row["config"] = {**json.loads(json.dumps(DEFAULTS)), **json.loads(row["config"])}
        row["memory"] = json.loads(row["memory"])
        return row

    def update_state(self,scope,change,expected=None,invalidate=True):
        self.group(scope)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row=dict(db.execute("SELECT * FROM groups WHERE scope=?",(scope,)).fetchone())
            if expected is not None and row['config_revision']!=expected:
                raise Conflict(row['config_revision'])
            config={**json.loads(json.dumps(DEFAULTS)),**json.loads(row['config'])}
            memory=json.loads(row['memory'])
            change(db,config,memory,row)
            db.execute("UPDATE groups SET config=?,memory=?,generation=generation+?,config_revision=config_revision+1 WHERE scope=?",(json.dumps(config,ensure_ascii=False),json.dumps(memory,ensure_ascii=False),int(invalidate),scope))
        return self.group(scope)

    def configure(self,scope,**changes):
        def apply(db,config,memory,row):config.update(changes)
        return self.update_state(scope,apply,invalidate=set(changes)!={'memo'})

    def reset(self,scope,*,clear=False,expected=None):
        def apply(db,config,memory,row):
            db.execute('DELETE FROM turns WHERE scope=?',(scope,))
            db.execute('DELETE FROM notices WHERE scope=?',(scope,))
            config['persona_pending']=None
            if clear:
                memory.clear()
                self.revoke_facts(db,scope)
        return self.update_state(scope,apply,expected)

    @staticmethod
    def revoke_facts(db,scope,fid=None):
        rows=db.execute("SELECT identity FROM facts WHERE scope=? AND status='active'"+(" AND id=?" if fid else ""),(scope,fid) if fid else (scope,)).fetchall()
        for r in rows:
            db.execute('UPDATE identities SET revision=revision+1 WHERE target=(SELECT target FROM identities WHERE identity=?)',(r[0],))
        db.execute("UPDATE facts SET status='deleted',revision=revision+1 WHERE scope=?"+(" AND id=?" if fid else ""),(scope,fid) if fid else (scope,))

    def memory(self,scope,text=None,clear=False,expected=None):
        def apply(db,config,memory,row):
            if clear:memory.clear()
            elif text:
                if len(text)>500:raise ValueError('群记忆最多500字')
                if len(memory)>=30:raise ValueError('群记忆最多30条，请先清理')
                memory.append(text)
        return self.update_state(scope,apply,expected)['memory']

    def claim(self, scope, event):
        if not event:
            return False  # no durable identity, no automatic execution
        with self.db() as db:
            result = db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?)", (scope,event,time.time()))
            db.execute("DELETE FROM events WHERE created<?", (time.time()-7*86400,))
            return result.rowcount == 1

    def history(self, scope, limit=20):
        with self.db() as db:
            rows = db.execute("SELECT actor,user_text,answer,created FROM turns WHERE scope=? ORDER BY created DESC LIMIT ?",
                              (scope, limit)).fetchall()
        with self.db() as db:
            older=db.execute("SELECT event,actor,user_text,answer FROM turns WHERE scope=? ORDER BY created DESC LIMIT 20 OFFSET ?",(scope,limit)).fetchall()
        result = []
        if older:
            digest=[dict(source=r['event'],speaker=r['actor'],topic=r['user_text'][:120],answer_excerpt=r['answer'][:180]) for r in reversed(older)]
            result.append(dict(role="user",content="[较早话题的摘录摘要；不完整，保留发言归属与来源]\n"+json.dumps(digest,ensure_ascii=False)))
        with self.db() as db:
            notices=[dict(n) for n in db.execute('SELECT created,text FROM notices WHERE scope=? ORDER BY created DESC LIMIT 10',(scope,))]
        timeline=[(r['created'],'turn',r) for r in rows]+[(n['created'],'notice',n) for n in notices]
        for _,kind,row in sorted(timeline,key=lambda item:item[0]):
            if kind=='notice':
                result.append(dict(role="system",content=row['text']))
                continue
            result.extend([dict(role="user", content=f"成员[{row['actor']}]：{row['user_text']}"),
                           dict(role="assistant", content=row["answer"])])
        return result

    def commit(self, scope, generation, event, actor, text, answer):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT generation FROM groups WHERE scope=?", (scope,)).fetchone()
            if row is None or row[0] != generation:
                return False
            db.execute("INSERT OR IGNORE INTO turns VALUES (?,?,?,?,?,?,?)",
                       (scope,generation,event,actor,text[:4000],answer[:6000],time.time()))
            db.execute("DELETE FROM turns WHERE scope=? AND event NOT IN (SELECT event FROM turns WHERE scope=? ORDER BY created DESC LIMIT 40)", (scope,scope))
            return True

    def identity(self, view):
        ident = identity_for(view)
        # Preserve existing private bond storage; unmatched official members get
        # isolated state until both endpoints prove control (never infer aliases).
        bond = str(view.user_id) if not view.is_group or view.platform == "onebot" else "member_"+hashlib.sha256(ident.encode()).hexdigest()[:24]
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO identities(identity,target,bond_user) VALUES (?,?,?)", (ident,ident,bond))
        return self.resolve(ident)

    def resolve(self, ident):
        with self.db() as db:
            row = db.execute("SELECT * FROM identities WHERE identity=?", (ident,)).fetchone()
            if row is None:
                raise ValueError("未知身份")
            origin = dict(row)
            target = db.execute("SELECT * FROM identities WHERE identity=?", (row["target"],)).fetchone()
        return dict(identity=ident, canonical=origin["target"], revision=origin["revision"],
                    bond_user=target["bond_user"] if target else origin["bond_user"])

    def begin_binding(self, ident, scope):
        code = secrets.token_hex(5)
        with self.db() as db:
            db.execute("DELETE FROM bindings WHERE expires<? OR origin=?", (time.time(),ident))
            db.execute("INSERT INTO bindings VALUES (?,?,?,?,?,?)", (code,ident,scope,"","",time.time()+600))
        return code

    def claim_binding(self, code, target):
        nonce = secrets.token_hex(5)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM bindings WHERE code=? AND expires>?", (code,time.time())).fetchone()
            if not row or row["target"] or row["origin"] == target:
                raise ValueError("绑定码已失效或已被认领，请在群里重新申请。")
            db.execute("UPDATE bindings SET target=?,nonce=? WHERE code=?", (target,nonce,code))
        return nonce

    def confirm_binding(self, ident, scope, code, nonce):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM bindings WHERE code=? AND origin=? AND scope=? AND nonce=? AND expires>? AND target!=''",
                             (code,ident,scope,nonce,time.time())).fetchone()
            if not row:
                raise ValueError("绑定确认不匹配或已过期。")
            # Keep origin facts and provisional bond intact; no destructive merge.
            db.execute("UPDATE identities SET target=?,revision=revision+1 WHERE identity=?", (row["target"],ident))
            db.execute("DELETE FROM bindings WHERE code=?", (code,))

    def unlink(self, ident):
        with self.db() as db:
            db.execute("UPDATE identities SET target=identity,revision=revision+1 WHERE identity=? OR target=?", (ident,ident))
            db.execute("DELETE FROM bindings WHERE origin=? OR target=?", (ident,ident))

    def add_fact(self, identity, scope, event, text, visibility, expected_revision, generation=None):
        if visibility not in ("private","source_group","public") or not text.strip():
            return False
        fid = hashlib.sha256(key(identity,scope,event,text).encode()).hexdigest()[:16]
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM identities WHERE identity=?", (identity,)).fetchone()
            if not row or row[0] != expected_revision:
                return False
            if generation is not None:
                g = db.execute("SELECT generation FROM groups WHERE scope=?", (scope,)).fetchone()
                if not g or g[0] != generation:
                    return False
            # A deleted id is a tombstone: old extraction cannot recreate it.
            db.execute("INSERT OR IGNORE INTO facts(id,identity,scope,event,text,visibility,created) VALUES (?,?,?,?,?,?,?)",
                       (fid,identity,scope,event,text.strip()[:300],visibility,time.time()))
        return True

    def facts(self, identity, audience, scope=""):
        resolved = self.resolve(identity)
        with self.db() as db:
            rows = db.execute('''SELECT f.* FROM facts f JOIN identities i ON i.identity=f.identity
                WHERE i.target=? AND f.status='active' ORDER BY f.created DESC''', (resolved["canonical"],)).fetchall()
        # Filtering happens BEFORE ranking/prompt assembly. Do not search anyone
        # else's facts, including public facts, through a model-supplied user id.
        return [dict(r) for r in rows if audience == "private" or r["visibility"] == "public"
                or (r["visibility"] == "source_group" and r["scope"] == scope)][:30]

    def change_fact(self, identity, fid, *, delete=False, public=False):
        allowed = {f["id"] for f in self.facts(identity,"private")}
        if fid not in allowed:
            return False
        with self.db() as db:
            db.execute("UPDATE facts SET status=?,visibility=CASE WHEN ? THEN 'public' ELSE visibility END,revision=revision+1 WHERE id=?",
                       ("deleted" if delete else "active",int(public),fid))
            # Cancel all in-flight extraction on linked identities.
            db.execute("UPDATE identities SET revision=revision+1 WHERE target=?", (self.resolve(identity)["canonical"],))
        return True

    def can_auto(self, scope, config, now=None):
        """Read-only preflight; only actual send attempts reserve automatic quota."""
        now = time.time() if now is None else now
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        with self.db() as db:
            for k, cap, cooldown in ((scope,config["auto_daily_cap"],config["cooldown"]),("__bot__",100,1)):
                row=db.execute("SELECT count,last FROM quotas WHERE scope=? AND day=?",(k,day)).fetchone()
                if row and (row["count"]>=cap or now-row["last"]<cooldown):
                    return False
        return True

    def reserve_auto(self, scope, config, now=None):
        now = time.time() if now is None else now
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            for k, cap, cooldown in ((scope,config["auto_daily_cap"],config["cooldown"]), ("__bot__",100,1)):
                row = db.execute("SELECT count,last FROM quotas WHERE scope=? AND day=?", (k,day)).fetchone()
                if row and (row["count"] >= cap or now-row["last"] < cooldown):
                    return False
            for k in (scope,"__bot__"):
                db.execute("INSERT INTO quotas VALUES (?,?,1,?) ON CONFLICT(scope,day) DO UPDATE SET count=count+1,last=excluded.last", (k,day,now))
            db.execute("DELETE FROM quotas WHERE day<?", (time.strftime("%Y-%m-%d",time.localtime(now-7*86400)),))
        return True

    def delivery(self, scope, event, status, ticket=None):
        ticket = ticket or secrets.token_hex(12)
        with self.db() as db:
            db.execute("INSERT INTO deliveries VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status",
                       (ticket,scope,event,status,time.time()))
            db.execute("DELETE FROM deliveries WHERE created<?", (time.time()-30*86400,))
        return ticket


class Conflict(ValueError):
    def __init__(self,revision):
        self.revision=revision
        super().__init__('配置已变化，请刷新后重试')
