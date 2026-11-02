"""Scoped facts, version history and task working state. SQLite is authoritative.
Legacy tier files remain readable through legacy APIs; runtime scoped retrieval
never falls back across scopes. All scope/owner values come from trusted context.
"""
import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextvars import ContextVar
from contextlib import contextmanager

current_scope = ContextVar("cognitive_scope", default="")
current_generation = ContextVar("cognitive_generation", default=None)


def scope_key(platform, channel, character, scene=""):
    return json.dumps([platform, channel or "private", character or "assistant", scene], ensure_ascii=False)


def terms(text):
    text = str(text).lower()
    return set(re.findall(r"[a-z0-9_]+", text)) | {text[i:i+2] for i in range(len(text)-1)
        if all("\u4e00" <= c <= "\u9fff" for c in text[i:i+2])}


class CognitiveState:
    def __init__(self, root):
        os.makedirs(root, exist_ok=True)
        self.path = os.path.join(root, "cognitive_state.sqlite3")
        with self.db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0,1):
                raise ValueError("不支持的认知存储版本")
            db.executescript("""
            PRAGMA user_version=1;
            CREATE TABLE IF NOT EXISTS scopes(owner TEXT, scope TEXT, generation INTEGER DEFAULT 0,
              paused INTEGER DEFAULT 0, PRIMARY KEY(owner,scope));
            CREATE TABLE IF NOT EXISTS facts(id TEXT PRIMARY KEY, owner TEXT, scope TEXT, content TEXT,
              predicate TEXT DEFAULT '', version INTEGER, supports INTEGER, updated REAL);
            CREATE TABLE IF NOT EXISTS history(id TEXT, version INTEGER, content TEXT, replaced REAL,
              PRIMARY KEY(id,version));
            CREATE TABLE IF NOT EXISTS evidence(id TEXT, event TEXT, PRIMARY KEY(id,event));
            CREATE TABLE IF NOT EXISTS events(owner TEXT, scope TEXT, event TEXT, PRIMARY KEY(owner,scope,event));
            CREATE TABLE IF NOT EXISTS batches(owner TEXT, scope TEXT, batch TEXT, PRIMARY KEY(owner,scope,batch));
            CREATE TABLE IF NOT EXISTS tasks(owner TEXT, scope TEXT, payload TEXT, revision INTEGER,
              PRIMARY KEY(owner,scope));
            CREATE TABLE IF NOT EXISTS fragments(id TEXT PRIMARY KEY, owner TEXT, scope TEXT,
              kind TEXT, content TEXT, created REAL);
            CREATE INDEX IF NOT EXISTS facts_scope ON facts(owner,scope);
            CREATE INDEX IF NOT EXISTS fragments_scope ON fragments(owner,scope,kind,created);
            """)
    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA secure_delete=ON")
        try:
            with db:
                yield db
        finally:
            db.close()
    def _scope(self, db, owner, scope):
        db.execute("INSERT OR IGNORE INTO scopes(owner,scope) VALUES (?,?)", (owner,scope))
        return db.execute("SELECT * FROM scopes WHERE owner=? AND scope=?", (owner,scope)).fetchone()
    def seen(self, owner, scope, event):
        with self.db() as db:
            return db.execute("SELECT 1 FROM events WHERE owner=? AND scope=? AND event=?",(owner,scope,event)).fetchone() is not None

    def generation(self, owner, scope):
        with self.db() as db:
            return self._scope(db,owner,scope)["generation"]
    def _version(self, db, row, content):
        db.execute("INSERT OR IGNORE INTO history VALUES (?,?,?,?)", (row["id"],row["version"],row["content"],time.time()))
        db.execute("UPDATE facts SET content=?,version=version+1,updated=? WHERE id=?", (content,time.time(),row["id"]))
    def ingest(self, owner, scope, generation, batch, candidates, event_ids):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._scope(db,owner,scope)
            fresh_events = [e for e in event_ids if not db.execute("SELECT 1 FROM events WHERE owner=? AND scope=? AND event=?",(owner,scope,e)).fetchone()]
            db.executemany("INSERT OR IGNORE INTO events VALUES (?,?,?)",[(owner,scope,e) for e in event_ids])
            if state["paused"] or generation != state["generation"]:
                return "superseded"
            if not fresh_events:
                return "duplicate"
            event_ids = fresh_events
            if db.execute("SELECT 1 FROM batches WHERE owner=? AND scope=? AND batch=?", (owner,scope,batch)).fetchone():
                return "duplicate"
            for c in candidates:
                content = str(c.get("content") or "").strip()[:300]
                if not content:
                    continue
                predicate = str(c.get("predicate") or "")[:60]
                row = db.execute("SELECT * FROM facts WHERE owner=? AND scope=? AND id=?", (owner,scope,c.get("matched_id",""))).fetchone()
                if row is None and predicate:
                    row = db.execute("SELECT * FROM facts WHERE owner=? AND scope=? AND predicate=?", (owner,scope,predicate)).fetchone()
                if row is None:
                    row = db.execute("SELECT * FROM facts WHERE owner=? AND scope=? AND content=?", (owner,scope,content)).fetchone()
                fid = row["id"] if row else uuid.uuid4().hex[:16]
                # Evidence IDs must be a subset of this batch. Missing evidence is
                # represented as batch-level support, not fabricated per-message provenance.
                supplied = c.get("evidence_turn_ids")
                ids = [e for e in supplied if e in event_ids] if isinstance(supplied,list) else ["batch:"+batch]
                if not ids:
                    continue
                fresh = [e for e in ids if not db.execute("SELECT 1 FROM evidence WHERE id=? AND event=?",(fid,e)).fetchone()]
                if not fresh:
                    continue
                if row:
                    if c.get("judge") == "update" or predicate:
                        if content != row["content"]:
                            self._version(db,row,content)
                    db.execute("UPDATE facts SET supports=supports+?,updated=? WHERE id=?",(len(fresh),time.time(),fid))
                else:
                    db.execute("INSERT INTO facts VALUES (?,?,?,?,?,1,?,?)",(fid,owner,scope,content,predicate,len(fresh),time.time()))
                db.executemany("INSERT OR IGNORE INTO evidence VALUES (?,?)",[(fid,e) for e in fresh])
            db.execute("INSERT INTO batches VALUES (?,?,?)",(owner,scope,batch))
        return "applied"
    def list(self, owner, scope, query="", limit=15):
        with self.db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM facts WHERE owner=? AND scope=?",(owner,scope))]
        q = terms(query)
        rows.sort(key=lambda r:(r["id"] == query, len(q & terms(r["content"])), r["updated"],r["supports"]),reverse=True)
        if query:
            rows = [r for r in rows if r["id"] == query or q & terms(r["content"]) or query.lower() in r["content"].lower()]
        return rows[:limit]
    def correct(self, owner, scope, fid, content):
        content = content.strip()[:300]
        if not content:
            raise ValueError("修正内容不能为空")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM facts WHERE owner=? AND scope=? AND id=?",(owner,scope,fid)).fetchone()
            if row is None:
                raise ValueError("当前范围内没有这条记忆")
            self._version(db,row,content)
            self._scope(db,owner,scope)
            db.execute("UPDATE scopes SET generation=generation+1 WHERE owner=? AND scope=?",(owner,scope))
            # Summaries and task notes may repeat an old value: invalidate all
            # projections in this scope rather than leaving stale derived text.
            db.execute("DELETE FROM fragments WHERE owner=? AND scope=?",(owner,scope))
            db.execute("DELETE FROM tasks WHERE owner=? AND scope=?",(owner,scope))
    def forget(self, owner, scope, fid=None):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._scope(db,owner,scope)
            rows = db.execute("SELECT id FROM facts WHERE owner=? AND scope=?"+(" AND id=?" if fid else ""),
                (owner,scope,fid) if fid else (owner,scope)).fetchall()
            if fid and not rows:
                raise ValueError("当前范围内没有这条记忆")
            for row in rows:
                for table in ("history","evidence","facts"):
                    db.execute("DELETE FROM "+table+" WHERE id=?",(row["id"],))
            db.execute("UPDATE scopes SET generation=generation+1 WHERE owner=? AND scope=?",(owner,scope))
            db.execute("DELETE FROM fragments WHERE owner=? AND scope=?",(owner,scope))
            db.execute("DELETE FROM tasks WHERE owner=? AND scope=?",(owner,scope))
    def clear_context(self, owner, scope):
        """Invalidate working context without deleting durable facts."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._scope(db,owner,scope)
            db.execute("UPDATE scopes SET generation=generation+1 WHERE owner=? AND scope=?",(owner,scope))
            db.execute("DELETE FROM fragments WHERE owner=? AND scope=?",(owner,scope))
            db.execute("DELETE FROM tasks WHERE owner=? AND scope=?",(owner,scope))

    def versions(self, owner, scope, fid):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT h.* FROM history h JOIN facts f ON f.id=h.id WHERE f.owner=? AND f.scope=? AND f.id=? ORDER BY h.version",(owner,scope,fid))]

    def pause(self, owner, scope, paused):
        with self.db() as db:
            self._scope(db,owner,scope)
            db.execute("UPDATE scopes SET paused=?,generation=generation+1 WHERE owner=? AND scope=?",(int(paused),owner,scope))
    def task(self, owner, scope, update=None, expected_revision=None, expected_generation=None):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tasks WHERE owner=? AND scope=?",(owner,scope)).fetchone()
            payload = json.loads(row["payload"]) if row else {}
            if expected_revision is not None and expected_revision != (row["revision"] if row else 0):
                raise ValueError("任务状态已变化，请重新读取")
            if update is not None:
                if expected_generation is not None and self._scope(db,owner,scope)["generation"] != expected_generation:
                    raise ValueError("任务上下文已被清理或纠正，请重新开始")
                payload.update(update)
                if len(json.dumps(payload,ensure_ascii=False)) > 4000:
                    raise ValueError("任务状态超过 4000 字符，请精简结果与引用，保留约束")
                rev = row["revision"]+1 if row else 1
                db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?,?,?)",(owner,scope,json.dumps(payload,ensure_ascii=False),rev))
            return payload
    def archive(self, owner, scope, kind, content, expected_generation=None, source_id=""):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._scope(db,owner,scope)
            if state["paused"] or (expected_generation is not None and state["generation"] != expected_generation):
                return ""
            fid = hashlib.sha256(json.dumps([owner,scope,kind,source_id,content],ensure_ascii=False).encode()).hexdigest()[:20]
            db.execute("INSERT OR IGNORE INTO fragments VALUES (?,?,?,?,?,?)",(fid,owner,scope,kind,content,time.time()))
            return fid
    def view(self, owner, scope, turns=10):
        with self.db() as db:
            rows=db.execute("SELECT content FROM fragments WHERE owner=? AND scope=? AND kind='conversation' ORDER BY created DESC LIMIT ?",(owner,scope,turns)).fetchall()
        messages=[]
        for row in reversed(rows):
            try:
                pair=json.loads(row["content"])
                messages.extend([{"role":"user","content":pair["user"]},{"role":"assistant","content":pair["assistant"]}])
            except (KeyError,ValueError):
                continue
        return messages

    def recall(self, owner, scope, query, limit=4):
        with self.db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM fragments WHERE owner=? AND scope=?",(owner,scope))]
        q = terms(query)
        rows.sort(key=lambda r:(r["id"]==query,len(q & terms(r["content"])),r["created"]),reverse=True)
        return [r for r in rows if r["id"]==query or q & terms(r["content"])][:limit]


def pack_blocks(blocks, cap=2400):
    """Whole-block budget: never cut a constraint or reference mid-sentence."""
    kept=[]
    used=0
    for block in blocks:
        if block and used+len(block)+(2 if kept else 0)<=cap:
            kept.append(block)
            used+=len(block)+(2 if len(kept)>1 else 0)
    return "\n\n".join(kept)


def tool_summary(content, cap=600):
    text = str(content)
    try:
        data = json.loads(text)
        if isinstance(data,dict):
            keys = [k for k in ("error","status","success","result","message","path","url","count","data") if k in data]
            if not keys:
                keys=list(data)[:6]
            return json.dumps({k:data[k] for k in keys},ensure_ascii=False)[:cap]
    except (ValueError,TypeError):
        pass
    lines=text.splitlines()
    important=[line for line in lines if any(k in line.lower() for k in ("error","失败","错误","结果","http","路径","exit","未完成"))]
    return "\n".join(dict.fromkeys(important+lines[:3]+lines[-2:]))[:cap]
