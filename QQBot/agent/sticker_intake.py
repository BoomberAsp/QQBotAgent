"""Shared candidate inbox and reviewed sticker publication."""
import hashlib
import io
import json
import os
import re
from urllib.parse import urlsplit
import tempfile
import time
import uuid
from pathlib import Path

from PIL import Image

from lib.sticker_usage import infer_uses, parse_uses
from . import media_ledger as ledger
from .group_resources import edit_registry
from .media_tools import normalize

CANDIDATE_BYTES = 8 * 1024 * 1024
_PENDING_LIMIT = 120
_SCOPE_LIMIT = 20
_CANDIDATE_AGE = 14 * 86400
_REJECTION_AGE = 30 * 86400
_ID = re.compile(r"stick_[0-9a-f]{20}")
_PERSONA = re.compile(r"[A-Za-z0-9_-]{1,80}")
_MOOD = {"happy", "sad", "angry", "greeting", "thanks", "surprised", "tsundere", "comfort"}


def setup(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS sticker_inbox(
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, persona TEXT NOT NULL, owner TEXT NOT NULL,
        origin TEXT NOT NULL, digest TEXT NOT NULL, preview BLOB, thumb BLOB NOT NULL,
        metadata TEXT NOT NULL, created REAL NOT NULL, status TEXT NOT NULL,
        UNIQUE(scope, persona, digest))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sticker_inbox_activity(
        id TEXT PRIMARY KEY, repeats INTEGER NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sticker_consent(
        scope TEXT NOT NULL, owner TEXT NOT NULL, enabled INTEGER NOT NULL,
        PRIMARY KEY(scope, owner))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sticker_subscriber_runs(
        source TEXT PRIMARY KEY, checked REAL NOT NULL, outcome TEXT NOT NULL)""")


def owner_key(scope, user_id):
    return hashlib.sha256(json.dumps([scope, user_id]).encode()).hexdigest()


def consent(scope, owner="", enabled=None):
    with ledger.db() as conn:
        setup(conn)
        if enabled is not None:
            conn.execute("INSERT OR REPLACE INTO sticker_consent VALUES(?,?,?)", (scope, owner, bool(enabled)))
        row = conn.execute("SELECT enabled FROM sticker_consent WHERE scope=? AND owner=?", (scope, owner)).fetchone()
        return bool(row and row[0])


def automatic_consent(scope, owner):
    with ledger.db() as conn:
        setup(conn)
        return conn.execute(
            "SELECT COUNT(*) FROM sticker_consent WHERE scope=? AND owner IN ('',?) AND enabled=1",
            (scope, owner),
        ).fetchone()[0] == 2


def _prune(conn, now):
    conn.execute("DELETE FROM sticker_inbox WHERE status='pending' AND created<?", (now - _CANDIDATE_AGE,))
    conn.execute("DELETE FROM sticker_inbox WHERE status IN ('rejected','withdrawn') AND created<?",
                 (now - _REJECTION_AGE,))
    conn.execute("DELETE FROM sticker_inbox_activity WHERE id NOT IN (SELECT id FROM sticker_inbox WHERE status='pending')")


def make_candidate(raw, *, persona, scope, owner="", origin, description="", uses=None,
                   source_url="", author="", license="", mood=(), mime=""):
    if not isinstance(persona, str) or not _PERSONA.fullmatch(persona):
        raise ValueError("人格 ID 无效")
    if origin == "group" and (not scope or not owner):
        raise ValueError("投稿需要有效的群及投稿者")
    if origin not in {"group", "web"} or len(raw) > CANDIDATE_BYTES:
        raise ValueError("来源无效或图片超过 8 MiB")
    description = str(description).strip()[:300]
    tags = parse_uses(uses) if uses else infer_uses(description)
    mood = tuple(dict.fromkeys(mood))
    if not set(mood) <= _MOOD:
        raise ValueError("情绪标签无效")
    content = normalize(raw, mime)
    with Image.open(io.BytesIO(content)) as image:
        canvas = Image.new("RGBA", image.size, "white")
        canvas.alpha_composite(image.convert("RGBA"))
        thumb = canvas.convert("RGB").resize((16, 16), Image.Resampling.LANCZOS).tobytes()
    digest = hashlib.sha256(content).hexdigest()
    ident = "stick_" + uuid.uuid4().hex[:20]
    metadata = {"description": description, "uses": tags, "mood": mood,
                "source_url": str(source_url)[:2048], "author": str(author)[:120],
                "license": str(license)[:160]}
    return ident, scope, persona, owner, origin, digest, content, thumb, json.dumps(metadata, ensure_ascii=False)


def submit(candidate, *, automatic=False):
    ident, scope, persona, owner, origin, digest, content, thumb, metadata = candidate
    now = time.time()
    with ledger.db() as conn:
        setup(conn)
        conn.execute("BEGIN IMMEDIATE")
        _prune(conn, now)
        if automatic and origin == "group":
            count = conn.execute(
                "SELECT COUNT(*) FROM sticker_consent WHERE scope=? AND owner IN ('',?) AND enabled=1",
                (scope, owner),
            ).fetchone()[0]
            if count != 2:
                raise ValueError("自动投稿授权已关闭")
        rows = conn.execute(
            """SELECT id, digest, thumb, status, owner FROM sticker_inbox WHERE scope=? AND persona=?
               ORDER BY created DESC LIMIT 200""", (scope, persona),
        ).fetchall()
        for existing, recorded, preview, status, existing_owner in rows:
            # Compare a small colour thumbnail as well as the exact normalized hash.
            if digest == recorded or len(preview) == len(thumb) and sum(
                abs(a - b) for a, b in zip(preview, thumb)
            ) <= len(thumb) * 3:
                if automatic and origin == "group" and status == "pending":
                    conn.execute(
                        """INSERT INTO sticker_inbox_activity VALUES (?,1)
                           ON CONFLICT(id) DO UPDATE SET repeats=repeats+1""", (existing,)
                    )
                return existing if not owner or owner == existing_owner else "", False
        size, total = conn.execute(
            "SELECT COALESCE(SUM(length(preview)),0), COUNT(*) FROM sticker_inbox WHERE status='pending'"
        ).fetchone()
        if total >= _PENDING_LIMIT or size + len(content) > 256 * 1024 * 1024:
            raise ValueError("候选库已满，请先审核")
        if conn.execute("SELECT COUNT(*) FROM sticker_inbox WHERE status='pending' AND scope=?",
                        (scope,)).fetchone()[0] >= _SCOPE_LIMIT:
            raise ValueError("该会话的待审核表情包已达上限")
        conn.execute("INSERT INTO sticker_inbox VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (ident, scope, persona, owner, origin, digest, content, thumb, metadata, now, "pending"))
        return ident, True


def pending():
    with ledger.db() as conn:
        setup(conn)
        _prune(conn, time.time())
        rows = conn.execute(
            """SELECT c.id, scope, persona, origin, metadata, created, COALESCE(repeats,0)
               FROM sticker_inbox c LEFT JOIN sticker_inbox_activity a ON c.id=a.id
               WHERE status='pending' ORDER BY COALESCE(repeats,0) DESC, created DESC LIMIT ?""",
            (_PENDING_LIMIT,),
        ).fetchall()
    return [{"id": ident, "scope": scope, "persona": persona, "origin": origin, "repeats": repeats,
             "created": created, **json.loads(metadata)}
            for ident, scope, persona, origin, metadata, created, repeats in rows]


def preview_bytes(ident):
    if not _ID.fullmatch(ident):
        raise ValueError("候选编号无效")
    with ledger.db() as conn:
        setup(conn)
        row = conn.execute("SELECT preview FROM sticker_inbox WHERE id=? AND status='pending' AND created>=?",
                           (ident, time.time() - _CANDIDATE_AGE)).fetchone()
    if not row or not row[0]:
        raise ValueError("候选不存在或已处理")
    return row[0]


def _install(folder, digest, content):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (digest + ".png")
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("候选图片校验失败")
        fd, stage = tempfile.mkstemp(prefix=".candidate-", dir=folder)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(content)
            os.replace(stage, path)
        finally:
            Path(stage).unlink(missing_ok=True)
    return path


def validate_description(value):
    if not isinstance(value, str) or len(value) > 300:
        raise ValueError("描述必须为文本，最多 300 字")
    return value.strip()


def review(items, *, approve):
    if not isinstance(items, list) or not 1 <= len(items) <= 30:
        raise ValueError("请每批选择 1 到 30 个候选")
    choices = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not _ID.fullmatch(item["id"]):
            raise ValueError("候选编号无效")
        changes = {}
        if "uses" in item:
            changes["uses"] = parse_uses(item["uses"])
        if approve and "description" in item:
            changes["description"] = validate_description(item["description"])
        for key, limit in (("author", 120), ("license", 160), ("source_url", 2048)):
            if key in item:
                if not isinstance(item[key], str) or len(item[key]) > limit:
                    raise ValueError("来源、作者或许可字段超限")
                changes[key] = item[key].strip()
        choices[item["id"]] = changes

    with ledger.db() as conn:
        setup(conn)
        conn.execute("BEGIN IMMEDIATE")
        _prune(conn, time.time())
        with edit_registry() as (registry, root):
            published = registry.setdefault("media", [])
            results = []
            for ident, changes in choices.items():
                row = conn.execute(
                    """SELECT scope, persona, origin, digest, preview, metadata, status
                       FROM sticker_inbox WHERE id=?""", (ident,),
                ).fetchone()
                if not row:
                    results.append({"id": ident, "status": "missing"})
                    continue
                scope, persona, origin, digest, content, metadata, old_status = row
                target = "published" if approve else "rejected"
                if old_status != "pending":
                    results.append({"id": ident, "status": old_status})
                    continue
                fields = json.loads(metadata)
                if approve:
                    fields["uses"] = changes.get("uses", fields.get("uses", []))
                    if "description" in changes:
                        fields["description"] = changes["description"]
                    if origin == "web":
                        fields.update(changes)
                        source = urlsplit(fields.get("source_url", ""))
                        if (source.scheme != "https" or not source.hostname or source.username or source.password
                                or not fields.get("author") or not fields.get("license")):
                            results.append({"id": ident, "status": "missing_rights"})
                            continue
                    path = _install(root / "intake", digest, content)
                    entry = {**fields, "id": ident, "path": str(path.relative_to(root)),
                             "personas": [persona], "visibility": "scoped" if scope else "public",
                             "scopes": [scope] if scope else [], "sha256": digest, "origin": origin,
                             "source_id": ident, "review_status": "approved", "rights_status": "approved"}
                    published[:] = [old for old in published if old.get("id") != ident]
                    published.append(entry)
                else:
                    published[:] = [old for old in published if old.get("id") != ident]
                conn.execute("UPDATE sticker_inbox SET status=?, preview=NULL, metadata=?, created=? WHERE id=?",
                             (target, json.dumps(fields, ensure_ascii=False), time.time(), ident))
                results.append({"id": ident, "status": target})
            return results


def submissions(scope, owner):
    with ledger.db() as conn:
        setup(conn)
        _prune(conn, time.time())
        return conn.execute(
            "SELECT id, status FROM sticker_inbox WHERE scope=? AND owner=? ORDER BY created DESC LIMIT 10",
            (scope, owner),
        ).fetchall()


def withdraw(ident, scope, owner):
    with ledger.db() as conn:
        setup(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM sticker_inbox WHERE id=? AND scope=? AND owner=?",
                           (ident, scope, owner)).fetchone()
        if not row or row[0] not in {"pending", "published"}:
            return False
        with edit_registry() as (registry, _):
            registry["media"] = [item for item in registry.get("media", []) if item.get("id") != ident]
            conn.execute("UPDATE sticker_inbox SET status='withdrawn', preview=NULL, created=? WHERE id=?",
                         (time.time(), ident))
        return True
