"""Per-identity survey drafts: state machine, TTL and atomic persistence (P0).

Drafts are keyed by the canonical identity (the same key the rest of the
agent uses), so a draft started in a group can still be reviewed in private
chat. Disk storage survives restarts; an in-process cache avoids a file read
on every chat message.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass, field

from .model import Answer, SurveyDef

DEFAULT_TTL_SEC = 1800.0


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def default_ttl() -> float:
    return _env_float("SURVEY_DRAFT_TTL_SEC", DEFAULT_TTL_SEC)


def _default_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))      # QQBot/lib/survey
    return os.path.normpath(os.path.join(here, "..", "..", "data", "survey"))


@dataclass
class SurveyDraft:
    draft_id: str
    identity: str
    origin_scope: str
    origin_group_id: str
    svid: str
    title: str
    schema_hash: str
    answers: dict[int, Answer] = field(default_factory=dict)
    skipped: set[int] = field(default_factory=set)
    follow_up_for: int | None = None
    state: str = "collecting"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: float = 0.0

    def touch(self, ttl: float | None = None) -> None:
        now = time.time()
        self.updated_at = now
        self.expires_at = now + (default_ttl() if ttl is None else ttl)

    def expired(self, now: float | None = None) -> bool:
        return self.expires_at <= (time.time() if now is None else now)

    def to_dict(self) -> dict:
        return {
            "draft_id": self.draft_id,
            "identity": self.identity,
            "origin_scope": self.origin_scope,
            "origin_group_id": self.origin_group_id,
            "svid": self.svid,
            "title": self.title,
            "schema_hash": self.schema_hash,
            "answers": {
                str(subject_id): {
                    "kind": answer.kind,
                    "text": answer.text,
                    "option_id": answer.option_id,
                }
                for subject_id, answer in self.answers.items()
            },
            "skipped": sorted(self.skipped),
            "follow_up_for": self.follow_up_for,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SurveyDraft":
        answers: dict[int, Answer] = {}
        for key, raw in (data.get("answers") or {}).items():
            try:
                subject_id = int(key)
            except (TypeError, ValueError):
                continue
            answers[subject_id] = Answer(
                kind=str(raw.get("kind") or "text"),
                text=str(raw.get("text") or ""),
                option_id=raw.get("option_id"),
            )
        followed = data.get("follow_up_for")
        skipped = set()
        for item in data.get("skipped") or []:
            try:
                skipped.add(int(item))
            except (TypeError, ValueError):
                continue
        return cls(
            draft_id=str(data.get("draft_id") or ""),
            identity=str(data.get("identity") or ""),
            origin_scope=str(data.get("origin_scope") or ""),
            origin_group_id=str(data.get("origin_group_id") or ""),
            svid=str(data.get("svid") or ""),
            title=str(data.get("title") or ""),
            schema_hash=str(data.get("schema_hash") or ""),
            answers=answers,
            skipped=skipped,
            follow_up_for=int(followed) if followed not in (None, "") else None,
            state=str(data.get("state") or "collecting"),
            created_at=float(data.get("created_at") or 0),
            updated_at=float(data.get("updated_at") or 0),
            expires_at=float(data.get("expires_at") or 0),
        )


def _atomic_write_json(path: str, payload: dict) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".survey-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


class DraftStore:
    """JSON-backed draft store with an in-process cache."""

    def __init__(self, base_dir: str | None = None):
        self.base_dir = base_dir or _default_dir()
        self.drafts_dir = os.path.join(self.base_dir, "drafts")
        self.submitted_dir = os.path.join(self.base_dir, "submitted")
        self.receipts_dir = os.path.join(self.base_dir, "receipts")
        self._cache: dict[str, SurveyDraft] = {}

    # -- paths ---------------------------------------------------------
    @staticmethod
    def _digest(identity: str) -> str:
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]

    def _draft_path(self, identity: str) -> str:
        return os.path.join(self.drafts_dir, self._digest(identity) + ".json")

    def _submitted_path(self, identity: str, svid: str,
                        member_key: str = "") -> str:
        suffix = f"_{member_key[:10]}" if member_key else ""
        return os.path.join(
            self.submitted_dir,
            f"{svid}_{self._digest(identity)}{suffix}.json",
        )

    @staticmethod
    def member_key(svid: str, member_id: str) -> str:
        """Stable, PII-free dedupe key for one game account in one survey."""
        normalized = "".join(str(member_id).split()).upper()
        return hashlib.sha256(f"{svid}:{normalized}".encode("utf-8")).hexdigest()[:32]

    def _member_index_path(self, svid: str) -> str:
        return os.path.join(self.submitted_dir, f"_members_{svid}.json")

    def _member_index(self, svid: str) -> dict:
        path = self._member_index_path(svid)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def member_submission(self, svid: str, member_id: str) -> dict | None:
        """Look up a prior submission by game account, across QQ identities."""
        key = self.member_key(svid, member_id)
        return self._member_index(svid).get(key)

    # -- CRUD ----------------------------------------------------------
    def get(self, identity: str) -> SurveyDraft | None:
        draft = self._cache.get(identity)
        if draft is None:
            path = self._draft_path(identity)
            if not os.path.exists(path):
                return None
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    draft = SurveyDraft.from_dict(json.load(handle))
            except (OSError, ValueError, TypeError):
                return None
            self._cache[identity] = draft
        if draft.expired():
            self.delete(identity)
            return None
        return draft

    def save(self, draft: SurveyDraft, ttl: float | None = None) -> SurveyDraft:
        draft.touch(ttl)
        _atomic_write_json(self._draft_path(draft.identity), draft.to_dict())
        self._cache[draft.identity] = draft
        return draft

    def create(self, identity: str, scope: str, group_id: str,
               survey: SurveyDef, *, ttl: float | None = None) -> SurveyDraft:
        draft = SurveyDraft(
            draft_id=secrets.token_hex(3).upper(),
            identity=identity,
            origin_scope=scope,
            origin_group_id=group_id or "",
            svid=survey.svid,
            title=survey.title,
            schema_hash=survey.schema_hash,
        )
        return self.save(draft, ttl)

    def delete(self, identity: str) -> None:
        self._cache.pop(identity, None)
        try:
            os.remove(self._draft_path(identity))
        except OSError:
            pass

    def mark_submitted(self, draft: SurveyDraft, meta: dict | None = None,
                       member_id: str = "") -> None:
        payload = {
            "svid": draft.svid,
            "draft_id": draft.draft_id,
            "submitted_at": time.time(),
            "answers_digest": hashlib.sha256(
                json.dumps(
                    {str(key): answer.__dict__ for key, answer in draft.answers.items()},
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()[:16],
            "meta": meta or {},
        }
        key = self.member_key(draft.svid, member_id) if member_id else ""
        if key:
            payload["member_key"] = key
        _atomic_write_json(
            self._submitted_path(draft.identity, draft.svid, key), payload
        )
        if key:
            index = self._member_index(draft.svid)
            index[key] = {
                "draft_id": draft.draft_id,
                "submitted_at": payload["submitted_at"],
                "identity_digest": self._digest(draft.identity),
            }
            _atomic_write_json(self._member_index_path(draft.svid), index)
        self.delete(draft.identity)

    def submission_record(self, identity: str, svid: str) -> dict | None:
        pattern = os.path.join(
            self.submitted_dir, f"{svid}_{self._digest(identity)}*.json"
        )
        paths = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    return json.load(handle)
            except (OSError, ValueError):
                continue
        return None

    def receipt_path(self, draft: SurveyDraft) -> str:
        os.makedirs(self.receipts_dir, exist_ok=True)
        stamp = int(time.time())
        return os.path.join(
            self.receipts_dir, f"{draft.svid}_{draft.draft_id}_{stamp}.png"
        )

    def prune_receipts(self, days: int) -> int:
        cutoff = time.time() - max(1, int(days)) * 86400
        removed = 0
        try:
            names = os.listdir(self.receipts_dir)
        except OSError:
            return 0
        for name in names:
            path = os.path.join(self.receipts_dir, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
        return removed

    def append_attempt(self, draft: SurveyDraft, outcome: str,
                       duration_ms: int = 0, error: str = "") -> None:
        """Append one submission attempt to a PII-free audit log."""
        record = {
            "ts": time.time(),
            "svid": draft.svid,
            "draft_id": draft.draft_id,
            "identity_hash": self._digest(draft.identity),
            "outcome": str(outcome)[:40],
            "duration_ms": int(duration_ms or 0),
            "error": str(error or "")[:200],
        }
        os.makedirs(self.base_dir, exist_ok=True)
        try:
            with open(os.path.join(self.base_dir, "attempts.jsonl"),
                      "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def prune(self) -> int:
        removed = 0
        try:
            names = os.listdir(self.drafts_dir)
        except OSError:
            return 0
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.drafts_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    draft = SurveyDraft.from_dict(json.load(handle))
            except (OSError, ValueError, TypeError):
                continue
            if not draft.expired():
                continue
            try:
                os.remove(path)
                removed += 1
                self._cache.pop(draft.identity, None)
            except OSError:
                pass
        return removed