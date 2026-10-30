#!/usr/bin/env python3
"""Build the Ark Re:Code **story knowledge base**.

Pipeline stages (each resumable, all offline except ``crawl`` / ``enrich``)::

    crawl   MediaWiki allpages + revisions  -> data/story_kb/raw/pages/*.txt
    parse   wikitext -> structured scenes    (in memory, validated by index)
    index   units/scenes/chunks -> SQLite + FTS5  -> data/story_kb/story.db
    enrich  LLM Chinese summaries           -> data/story_kb/zh/units.json

Why split raw / zh / db
-----------------------
The wiki is the source of truth but re-hitting it is slow and rate-limited;
the LLM enrichment is the expensive, rate-limited step; the SQLite file is
fully derived. Keeping three separate artifacts means re-indexing (e.g. after
tweaking chunk sizes) never re-crawls and never re-pays for translations.

Source pages
------------
``Main_Story`` and ``Events/Event_Story`` are index pages. The story text
lives on the pages they link to, in two template families:

* main story: ``{{MainStory|...|Story={{Chat|Stage 1-1: Intro||
  {{SceneChatAudio|line|speaker|H076|voice}} ...}}}}``
* event story: ``{{EventStory|...|Story={{Chat|Scene 1: Part 1||
  {{SceneChat|line|speaker|H613|sound}} ...}}}}``

Line template params are positional: ``text | speaker | char_id | voice``.
``speaker`` is a character name, or one of the markers ``player`` (the
player-character / 团长), ``no`` (narration), ``title`` (section header) and
``scene`` (location caption).

Sibling pages such as ``Main Story/Episode 1/1-1`` and
``Event Story/<event>/A.3`` carry only ``{{Stage}}`` battle data — no story —
so they are filtered out by title shape before fetching.

Usage::

    cd QQBot
    python scripts/build_story_kb.py --all                 # full build
    python scripts/build_story_kb.py --crawl --limit 20    # smoke test
    python scripts/build_story_kb.py --index --stats       # rebuild + report
    python scripts/build_story_kb.py --enrich --concurrency 4
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

_THIS = os.path.dirname(os.path.abspath(__file__))
_QROOT = os.path.dirname(_THIS)                      # .../QQBot
sys.path.insert(0, _QROOT)

from lib.story_kb import (                            # noqa: E402
    SCHEMA_VERSION,
    cjk_bigrams,
    preferred_story_kb_dir,
)


def _resolve_data_dir() -> str:
    """Directory that holds raw/ , zh/ and story.db on this host."""
    override = (os.environ.get("STORY_KB_DATA_DIR") or "").strip()
    if override:
        return override
    return preferred_story_kb_dir()

# ── Paths & constants ──────────────────────────────────────────────

def _load_dotenv_early() -> None:
    """Read QQBot/.env without overriding what is already in the environment.

    The bot does this via python-dotenv at turn.py import; the build script must
    do the same *before* resolving paths, otherwise a build on the server would
    ignore USER_DATA_ROOT and write onto the system disk.
    """
    env_path = os.path.join(_QROOT, ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.split(" #")[0].strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_dotenv_early()

# Where the artifacts live. The DB (and its raw/ + zh/ inputs) belong on the
# deployment data disk, not in the git checkout — same resolution rule as
# lib/story_kb.py so the bot and the builder never disagree.
DATA_DIR = _resolve_data_dir()
RAW_DIR = os.path.join(DATA_DIR, "raw")
RAW_PAGES = os.path.join(RAW_DIR, "pages")
INDEX_DIR = os.path.join(RAW_DIR, "index")
MANIFEST_PATH = os.path.join(RAW_DIR, "manifest.json")
ZH_DIR = os.path.join(DATA_DIR, "zh")
ZH_PATH = os.path.join(ZH_DIR, "units.json")
DB_PATH = os.path.join(DATA_DIR, "story.db")

WIKI_API = "https://arkrecodewiki.miraheze.org/w/api.php"
WIKI_PAGE = "https://arkrecodewiki.miraheze.org/wiki/"
# Miraheze blocks some datacenter IPs at the Cloudflare layer; the bot routes
# through a host-side sing-box inbound. Empty string forces a direct connection.
WIKI_PROXY = os.environ.get("WIKI_PROXY", "http://127.0.0.1:1081")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"
)
BATCH = 50                      # MediaWiki max titles per revisions query
REQUEST_PAUSE = 0.4             # polite delay between revisions batches

DEFAULT_MODEL = "deepseek-chat"
ZH_PROMPT_VERSION = 4
ZH_MAX_SCENE_CHARS = 900        # per-scene cap fed to the LLM
ZH_MAX_TOTAL_CHARS = 24000      # per-page cap fed to the LLM

# Scene-splitting knobs: a "scene" is one {{Chat}} block.
CHUNK_LINES = 22                # lines per retrieval chunk
CHUNK_OVERLAP = 4               # overlap between chunks of the same scene
SCENE_SOFT_MAX = 28             # scenes longer than this are chunked

# Dedupe preference: lower rank wins when two pages carry identical dialogue.
WORK_PRIORITY = {"prologue": 0, "main": 1, "event": 2, "hard": 3, "urgent": 4}

SPOILER = "<!-- story-kb -->"

# ── HTTP ───────────────────────────────────────────────────────────
#
# Cloudflare fingerprints TLS, so which client works depends on the host:
#   * dev box: system `curl` gets 200, python clients get a 403 JS challenge
#   * prod box: curl_cffi with chrome116 impersonation + proxy works
# Both are tried (proxy first when configured) and the winner is memoised.

_http_backend: Optional[str] = None


def _curl_cffi_get(params: dict, proxy: Optional[str]) -> Optional[bytes]:
    from curl_cffi import requests as cffi

    resp = cffi.get(
        WIKI_API, params=params, impersonate="chrome116", timeout=60,
        proxy=proxy or None, headers={"User-Agent": USER_AGENT},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    body = resp.content
    if not body.lstrip().startswith(b"{"):
        raise RuntimeError("non-JSON body (Cloudflare challenge?)")
    return body


def _curl_cli_get(params: dict, proxy: Optional[str]) -> Optional[bytes]:
    cmd = ["curl", "-sS", "-m", "90", "-A", USER_AGENT]
    if proxy:
        cmd += ["-x", proxy]
    cmd += ["-G", WIKI_API]
    for k, v in params.items():
        cmd += ["--data-urlencode", f"{k}={v}"]
    proc = subprocess.run(cmd, capture_output=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"curl exit {proc.returncode}: {proc.stderr[:200]!r}")
    body = proc.stdout
    if not body.lstrip().startswith(b"{"):
        raise RuntimeError("non-JSON body (Cloudflare challenge?)")
    return body


_BACKENDS = [
    ("curl_cffi+proxy", lambda p: _curl_cffi_get(p, WIKI_PROXY)),
    ("curl_cffi", lambda p: _curl_cffi_get(p, None)),
    ("curl+proxy", lambda p: _curl_cli_get(p, WIKI_PROXY)),
    ("curl", lambda p: _curl_cli_get(p, None)),
]


def api_get(params: dict, retries: int = 3) -> dict:
    """GET the MediaWiki API and return parsed JSON, trying each backend once."""
    global _http_backend
    order = _BACKENDS
    if _http_backend:
        order = [b for b in _BACKENDS if b[0] == _http_backend] + [
            b for b in _BACKENDS if b[0] != _http_backend
        ]
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        for name, fn_ in order:
            try:
                body = fn_(params)
                _http_backend = name
                return json.loads(body)
            except Exception as exc:  # noqa: BLE001 — try the next backend
                last_err = exc
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"wiki API unreachable: {last_err}")


# ── Wikitext parsing primitives ────────────────────────────────────

def _find_template_end(text: str, start: int) -> int:
    """Index just past the ``}}`` that closes the template opening at ``start``."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        two = text[i:i + 2]
        if two == "{{":
            depth += 1
            i += 2
            continue
        if two == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return i
            continue
        i += 1
    return -1


def _split_params(inner: str) -> List[str]:
    """Split template innards on top-level ``|`` (respecting nested {{ }} / [[ ]])."""
    parts: List[str] = []
    buf: List[str] = []
    depth_curly = 0
    depth_square = 0
    i = 0
    n = len(inner)
    while i < n:
        two = inner[i:i + 2]
        if two == "{{":
            depth_curly += 1
            buf.append(two)
            i += 2
            continue
        if two == "}}":
            depth_curly -= 1
            buf.append(two)
            i += 2
            continue
        if two == "[[":
            depth_square += 1
            buf.append(two)
            i += 2
            continue
        if two == "]]":
            depth_square -= 1
            buf.append(two)
            i += 2
            continue
        ch = inner[i]
        if ch == "|" and depth_curly <= 0 and depth_square <= 0:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts]


@dataclass
class Template:
    name: str
    params: List[str]
    start: int
    end: int


def scan_templates(text: str) -> List[Template]:
    """All outmost-level templates in ``text`` (nested ones stay inside params)."""
    out: List[Template] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i:i + 2] == "{{":
            end = _find_template_end(text, i)
            if end < 0:
                break
            inner = text[i + 2:end - 2]
            params = _split_params(inner)
            name = params[0].strip() if params else ""
            # Recurse-free: keep only top-level ones; nested live inside params.
            out.append(Template(name, params[1:], i, end))
            i = end
            continue
        i += 1
    return out


def walk_templates(text: str, max_depth: int = 4) -> Iterable[Tuple[Template, int]]:
    """Yield every template at any depth (depth-first) with its nesting depth."""
    for tpl in scan_templates(text):
        yield tpl, 1
        if max_depth > 1:
            for p in tpl.params:
                if "{{" in p:
                    for sub, d in walk_templates(p, max_depth - 1):
                        yield sub, d + 1


# Tags whose content should be dropped entirely.
_DROP_TAGS = re.compile(
    r"<(nowiki|ref|gallery|noinclude|includeonly|math|tabs|tab)\b[^>]*>.*?</\1>",
    re.I | re.S,
)
_STRIP_TAGS = re.compile(
    r"</?(big|small|span|div|center|font|ruby|rt|rb|sup|sub|b|i|u|s|p|"
    r"br\s*/?|hr\s*/?|blockquote)[^>]*>",
    re.I,
)


def _inline_template(tpl: Template) -> str:
    """Render an inline template that survived inside a dialogue line."""
    name = tpl.name.lower().strip()
    if name in ("!", "pipe"):
        return "|"
    if name in ("status tooltip", "tooltip", "tip", "term", "color", "highlight"):
        return tpl.params[0] if tpl.params else ""
    if name.startswith(("file:", "image:", "category:")):
        return ""
    # Default: most inline templates take the display text as the first arg.
    return tpl.params[0] if tpl.params else ""


def clean_inline(text: str, depth: int = 0) -> str:
    """Flatten one dialogue line to plain text."""
    if not text:
        return ""
    if depth < 3 and "{{" in text:
        out: List[str] = []
        pos = 0
        for tpl in scan_templates(text):
            out.append(text[pos:tpl.start])
            out.append(clean_inline(_inline_template(tpl), depth + 1))
            pos = tpl.end
        out.append(text[pos:])
        text = "".join(out)

    text = _DROP_TAGS.sub("", text)
    text = text.replace("{{!}}", "|").replace("&nbsp;", " ")
    # <br> inside a dialogue line is a soft wrap of one utterance, not a
    # paragraph break — joining with a space reads correctly both for the
    # LLM and for the exported markdown.
    text = _STRIP_TAGS.sub(lambda m: " " if m.group(0).lower().startswith("<br") else "", text)
    text = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", text)   # [[A|B]] → B
    text = re.sub(r"\[(?:https?://\S+)\s+([^\]]*)\]", r"\1", text)  # [url label] → label
    text = text.replace("'''''", "").replace("'''", "").replace("''", "")
    text = re.sub(r"<[^>]{1,80}>", "", text)                        # leftover tags
    text = text.replace("\u200b", "")
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


# ── Page classification ────────────────────────────────────────────

def _page_url(title: str) -> str:
    return WIKI_PAGE + title.replace(" ", "_")


def is_candidate(title: str) -> bool:
    """Title-shape filter — drop index pages, per-stage battle pages, hubs."""
    if title in ("Main Story", "Event Story", "Events/Event Story"):
        return False
    if title.startswith("Main Story/"):
        rest = title[len("Main Story/"):]
        if rest.startswith("Prologue/"):
            return True
        if rest.startswith("Urgent Mission/"):
            return True
        segs = rest.split("/")
        # Episode N/Chapter M[...]  — story;  Episode N/1-1 — battle data only
        if len(segs) >= 2 and segs[1].startswith("Chapter"):
            return True
        return False
    if title.startswith("Event Story/"):
        rest = title[len("Event Story/"):]
        return "/" not in rest          # hub page carries the dialogue
    return False


def classify(title: str) -> dict:
    """Derive work/episode/chapter/event metadata from a page title."""
    meta = {
        "work": "main", "episode": "", "chapter_no": None,
        "event_name": "", "member": "",
    }
    if title.startswith("Event Story/"):
        meta["work"] = "event"
        meta["event_name"] = title[len("Event Story/"):]
        return meta

    rest = title[len("Main Story/"):]
    if rest.startswith("Prologue"):
        meta["work"] = "prologue"
        meta["episode"] = "Prologue"
        m = re.search(r"Chapter\s+(\d+)", rest)
        meta["chapter_no"] = int(m.group(1)) if m else 0
        return meta
    if rest.startswith("Urgent Mission/"):
        meta["work"] = "urgent"
        meta["episode"] = "Urgent Mission"
        return meta

    m = re.match(r"(Episode\s+\d+(?:\s*\(Hard\))?)/(?:Chapter\s+(\d+))?", rest)
    if m:
        meta["episode"] = m.group(1)
        if "(Hard)" in m.group(1):
            meta["work"] = "hard"
        if m.group(2):
            meta["chapter_no"] = int(m.group(2))
    return meta


# ── Dialogue extraction ────────────────────────────────────────────

_SPEAKER_MARKERS = {"player", "no", "title", "scene", ""}
SPEAKER_PLAYER = "团长"
SPEAKER_NARRATION = "旁白"


@dataclass
class Line:
    speaker: str        # display speaker (character name / 团长 / 旁白)
    text: str
    char_id: str = ""
    kind: str = "say"   # say | narration | title | scene | player


@dataclass
class Scene:
    title: str = ""
    lines: List[Line] = field(default_factory=list)
    ord: int = 0
    content_hash: str = ""

    @property
    def char_ids(self) -> List[str]:
        seen: List[str] = []
        for ln in self.lines:
            if ln.char_id and ln.char_id not in seen:
                seen.append(ln.char_id)
        return seen

    @property
    def speakers(self) -> List[str]:
        seen: List[str] = []
        for ln in self.lines:
            if ln.kind in ("say", "player") and ln.speaker and ln.speaker not in seen:
                seen.append(ln.speaker)
        return seen

    def render(self, limit_lines: Optional[int] = None) -> str:
        out: List[str] = []
        for ln in self.lines[:limit_lines] if limit_lines else self.lines:
            if ln.kind == "title":
                out.append(f"— {ln.text} —")
            elif ln.kind == "scene":
                out.append(f"[场景] {ln.text}")
            elif ln.kind == "narration":
                out.append(f"（{ln.text}）")
            else:
                out.append(f"{ln.speaker}: {ln.text}")
        return "\n".join(out)

    def compute_hash(self) -> str:
        payload = "\n".join(f"{ln.kind}|{ln.speaker}|{ln.text}" for ln in self.lines)
        self.content_hash = hashlib.sha1(payload.encode("utf-8")).hexdigest()
        return self.content_hash


def parse_line(tpl: Template) -> Optional[Line]:
    """Turn a ``{{SceneChat…}}`` template into a Line."""
    params = tpl.params
    if not params:
        return None
    raw_text = params[0]
    speaker_raw = params[1].strip() if len(params) > 1 else ""
    char_id = params[2].strip() if len(params) > 2 else ""
    if not re.fullmatch(r"[A-Za-z]\d{3}", char_id or ""):
        char_id = ""

    text = clean_inline(raw_text)
    if not text:
        return None

    kind_marks = speaker_raw.lower()
    if kind_marks == "title":
        return Line("", text, "", "title")
    if kind_marks == "scene":
        return Line("", text, "", "scene")
    if kind_marks == "no":
        return Line(SPEAKER_NARRATION, text, "", "narration")
    if kind_marks == "player":
        return Line(SPEAKER_PLAYER, text, "", "player")
    if not speaker_raw:
        # Untagged lines are section headers ("Part 1 A Long-Awaited Reunion",
        # "Chapter 2: Global Catastrophe") or narration; separate them so the
        # chunker and the LLM see the structure.
        if re.match(r"^(part|chapter|epilogue|prologue|climax|finale)\b",
                    text, re.I):
            return Line("", text, "", "title")
        return Line(SPEAKER_NARRATION, text, "", "narration")
    return Line(clean_inline(speaker_raw) or speaker_raw, text, char_id, "say")


def parse_page(title: str, wikitext: str) -> Tuple[dict, List[Scene]]:
    """Extract unit metadata + scenes from one page's wikitext."""
    meta = classify(title)
    chapter_title_en = ""

    scenes: List[Scene] = []
    for tpl, _depth in walk_templates(wikitext, max_depth=5):
        name = tpl.name.strip().lower()

        if name in ("eventstory", "mainstory"):
            # Named params: "|Name= X" style
            for raw in tpl.params:
                m = re.match(r"\s*([A-Za-z ]+?)\s*=\s*(.*)", raw, re.S)
                if not m:
                    continue
                key = m.group(1).strip().lower()
                val = m.group(2).strip()
                if key == "name" and not meta.get("event_name"):
                    meta["event_name"] = clean_inline(val)
                elif key == "member":
                    meta["member"] = clean_inline(val)
            continue

        if name != "chat":
            continue

        scene_title = clean_inline(tpl.params[0]) if tpl.params else ""
        body = "|".join(tpl.params[2:]) if len(tpl.params) >= 3 else (
            tpl.params[1] if len(tpl.params) == 2 else ""
        )
        lines: List[Line] = []
        for sub in scan_templates(body):
            if not sub.name.strip().lower().startswith("scenechat"):
                continue
            line = parse_line(sub)
            if line:
                lines.append(line)
        if not lines:
            continue
        for ln in lines:
            if ln.kind == "title" and "chapter" in ln.text.lower() and not chapter_title_en:
                m = re.search(r"chapter\s*\d*\s*[:：]\s*(.+)$", ln.text, re.I)
                chapter_title_en = (m.group(1) if m else ln.text).strip()
        scenes.append(Scene(title=scene_title, lines=lines, ord=len(scenes)))

    meta["chapter_title_en"] = chapter_title_en
    return meta, scenes


# ── Crawl ──────────────────────────────────────────────────────────

def _load_manifest() -> dict:
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {"pages": {}}


def _save_manifest(manifest: dict) -> None:
    os.makedirs(RAW_DIR, exist_ok=True)
    tmp = MANIFEST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, MANIFEST_PATH)


def discover_titles() -> List[str]:
    """Every page under the two index namespaces (allpages, with continuation)."""
    titles: List[str] = []
    for prefix in ("Main Story", "Event Story"):
        cont: dict = {}
        while True:
            params = {
                "action": "query", "list": "allpages", "apprefix": prefix,
                "aplimit": "500", "format": "json", "formatversion": "2",
            }
            params.update(cont)
            data = api_get(params)
            titles.extend(p["title"] for p in data.get("query", {}).get("allpages", []))
            if "continue" in data:
                cont = data["continue"]
            else:
                break
    return titles


def fetch_wikitext(titles: Sequence[str]) -> Dict[str, str]:
    """Fetch latest revision content for up to ``BATCH`` titles per request."""
    out: Dict[str, str] = {}
    for i in range(0, len(titles), BATCH):
        chunk = list(titles[i:i + BATCH])
        data = api_get({
            "action": "query", "prop": "revisions", "rvprop": "content",
            "rvslots": "main", "titles": "|".join(chunk),
            "format": "json", "formatversion": "2",
        })
        for page in data.get("query", {}).get("pages", []):
            title = page.get("title", "")
            if page.get("missing"):
                continue
            revs = page.get("revisions") or []
            if not revs:
                continue
            content = (revs[0].get("slots", {}).get("main", {}) or {}).get("content", "")
            if content:
                out[title] = content
        time.sleep(REQUEST_PAUSE)
    return out


def crawl(limit: int = 0, refetch: bool = False) -> dict:
    """Stage 1: discover + download every story-bearing page into raw/pages/."""
    os.makedirs(RAW_PAGES, exist_ok=True)
    manifest = _load_manifest()
    pages: Dict[str, dict] = manifest.get("pages", {})

    all_titles = discover_titles()
    candidates = [t for t in all_titles if is_candidate(t)]
    if limit:
        candidates = candidates[:limit]
    print(f"[crawl] discovered {len(all_titles)} pages, {len(candidates)} candidates")

    todo = [t for t in candidates
            if refetch or t not in pages
            or (pages[t].get("file") and not os.path.isfile(
                os.path.join(RAW_PAGES, pages[t]["file"])))]
    print(f"[crawl] {len(todo)} to fetch ({len(candidates) - len(todo)} cached)")
    if not todo:
        return {"pages": pages, "candidates": len(candidates)}

    fetched = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        try:
            contents = fetch_wikitext(batch)
        except Exception as exc:  # noqa: BLE001 — keep what we already have
            print(f"[crawl] batch failed ({exc}); {fetched} pages saved so far")
            break
        for title, text in contents.items():
            if not _looks_like_story(text):
                # Battle-data page: remember it so it is never fetched again.
                pages[title] = {"file": "", "story": False,
                                "fetched_at": int(time.time())}
                continue
            digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
            fname = f"{digest}.txt"
            with open(os.path.join(RAW_PAGES, fname), "w", encoding="utf-8") as f:
                f.write(text)
            pages[title] = {
                "file": fname,
                "story": True,
                "fetched_at": int(time.time()),
                "chars": len(text),
                "url": _page_url(title),
            }
            fetched += 1
        _save_manifest({"pages": pages, "updated_at": int(time.time())})
        print(f"[crawl] {min(i + BATCH, len(todo))}/{len(todo)} titles, "
              f"{fetched} story pages stored")
    return {"pages": pages, "candidates": len(candidates)}


def _looks_like_story(wikitext: str) -> bool:
    return "{{Chat" in wikitext or "{{SceneChat" in wikitext


# ── Index pages (authoritative chapter / event titles) ─────────────
#
# `Main Story` and `Events/Event Story` are thin pages that transclude one
# table per part/episode (`{{:Main Story/Episode 1}}`) plus an EROLABS /
# Nutaku split for events. The tables carry the canonical chapter names and
# the Traditional-Chinese event titles — the only reliable place to get them
# (individual stage pages only repeat the header on their first page).

INDEX_PAGES = ("Main Story", "Events/Event Story")
_TRANSCLUSION_RE = re.compile(r"\{\{:([^}|]+?)\}\}")


def crawl_indexes(refetch: bool = False) -> None:
    """Download the two index pages and every table page they transclude."""
    os.makedirs(INDEX_DIR, exist_ok=True)
    seed: Dict[str, str] = {}
    for title in INDEX_PAGES:
        data = api_get({
            "action": "query", "prop": "revisions", "rvprop": "content",
            "rvslots": "main", "titles": title, "format": "json",
            "formatversion": "2",
        })
        for page in data.get("query", {}).get("pages", []):
            revs = page.get("revisions") or []
            if revs:
                seed[title] = (revs[0].get("slots", {}).get("main", {}) or {}).get(
                    "content", "")
    subs: List[str] = []
    for wt in seed.values():
        for m in _TRANSCLUSION_RE.finditer(wt):
            subs.append(m.group(1).strip())
    contents: Dict[str, str] = dict(seed)
    if subs:
        contents.update(fetch_wikitext(sorted(set(subs))))
    for title, wt in contents.items():
        slug = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")
        with open(os.path.join(INDEX_DIR, slug + ".txt"), "w", encoding="utf-8") as f:
            f.write(f"<!--title:{title}-->\n" + wt)
    print(f"[crawl] index pages stored: {len(contents)}")


def _table_rows(wikitext: str) -> List[List[str]]:
    """Crude wikitable reader → list of rows, each a list of raw cells."""
    rows: List[List[str]] = []
    for block in re.split(r"\|\}", wikitext):
        if "{|" not in block:
            continue
        body = block.split("{|", 1)[1]
        for row in re.split(r"\n\|-+", body):
            cells: List[str] = []
            # "|| a || b" single-line cells, then "| a" line-start cells
            pieces = row.split("\n")
            for piece in pieces:
                piece = piece.strip()
                if not piece or piece.startswith("!") or piece.startswith("|}"):
                    continue
                if piece.startswith("|") and "||" in piece:
                    cells.extend(c.strip() for c in piece.lstrip("|").split("||"))
                elif piece.startswith("|"):
                    cells.append(piece.lstrip("|").strip())
            if cells:
                rows.append(cells)
    return rows


def parse_chapter_index(wikitext: str, episode: str) -> Dict[str, str]:
    """{'1': 'Resonant Awakening', ...} from one part/episode table."""
    out: Dict[str, str] = {}
    for cells in _table_rows(wikitext):
        idx = None
        for i, cell in enumerate(cells):
            # Must *start* with "Chapter N": the picture cell also contains
            # "…/Episode 1/Chapter 1" inside its link target.
            if re.match(r"^\s*Chapter\s+\d+", cell):
                idx = i
                break
        if idx is None or idx + 1 >= len(cells):
            continue
        m = re.match(r"^\s*Chapter\s+(\d+)", cells[idx])
        name_cell = cells[idx + 1]
        if "[[File:" in name_cell or "[[" in name_cell and "|" in name_cell:
            continue          # wrapped in a link → not a plain chapter name
        name = clean_inline(name_cell)
        name = re.sub(r"^\d{4}-\d{2}-\d{2}$", "", name).strip()
        if m and name and not re.fullmatch(r"[\d\-\s]+", name):
            out.setdefault(m.group(1), name)
    return out


def parse_event_index(wikitext: str) -> Dict[str, str]:
    """{'A Budding Heart': '萌芽的心意', ...} (English name → Chinese name)."""
    out: Dict[str, str] = {}
    for cells in _table_rows(wikitext):
        for cell in cells:
            link = re.search(r"\[\[(Event Story/[^|\]]+)\|?([^\]]*)\]\]", cell)
            if link:
                en = link.group(1)[len("Event Story/"):].strip().replace("_", " ")
                label = clean_inline(link.group(2) or "")
                if has_cjk_text(label):
                    out.setdefault(en, label)
                continue
            m = re.match(r"^\s*(\S.*?)<br\s*/?>\s*[（(](.+?)[)）]\s*$", cell, re.S)
            if m and has_cjk_text(m.group(1)):
                zh = clean_inline(m.group(1))
                en = clean_inline(m.group(2))
                if zh and en:
                    out.setdefault(en, zh)
    return out


def has_cjk_text(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")


def load_index_meta() -> dict:
    """Read cached index pages → {chapters, prologue, events_zh}."""
    meta = {"chapters": {}, "events_zh": {}}
    if not os.path.isdir(INDEX_DIR):
        return meta
    for fname in sorted(os.listdir(INDEX_DIR)):
        if not fname.endswith(".txt"):
            continue
        path = os.path.join(INDEX_DIR, fname)
        with open(path, "r", encoding="utf-8") as f:
            wt = f.read()
        title = fname[:-4]
        m = re.match(r"<!--title:(.+?)-->", wt)
        if m:
            title = m.group(1)
        body = re.sub(r"^<!--title:.*?-->", "", wt, count=1)

        if title.startswith("Events/"):
            meta["events_zh"].update(parse_event_index(body))
            continue
        if title == "Main Story":
            continue
        if title.endswith("Urgent Mission"):
            continue
        episode = title[len("Main Story/"):] if title.startswith("Main Story/") else title
        for ch, name in parse_chapter_index(body, episode).items():
            meta["chapters"].setdefault(f"{episode}|{ch}", name)
    meta["chapters"].setdefault("Prologue|0", "Prologue")
    return meta


# ── Parse (raw -> units) ───────────────────────────────────────────

@dataclass
class Unit:
    page_title: str
    url: str
    meta: dict
    scenes: List[Scene]
    work_rank: int = 9
    rerun: bool = False


def load_raw_pages(manifest: dict) -> List[Tuple[str, str]]:
    """(title, wikitext) for every cached page, newest manifest first."""
    out: List[Tuple[str, str]] = []
    for title, info in manifest.get("pages", {}).items():
        path = os.path.join(RAW_PAGES, info.get("file", ""))
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            out.append((title, f.read()))
    out.sort(key=lambda kv: kv[0])
    return out


def parse_all(manifest: dict, index_meta: Optional[dict] = None,
              dedupe: bool = True) -> List[Unit]:
    """Parse cached wikitext into units, dropping duplicate dialogue."""
    chapters = (index_meta or {}).get("chapters", {})
    events_zh = (index_meta or {}).get("events_zh", {})
    units: List[Unit] = []
    for title, wikitext in load_raw_pages(manifest):
        meta, scenes = parse_page(title, wikitext)
        if not scenes:
            continue
        total_lines = sum(len(s.lines) for s in scenes)
        if total_lines < 3:
            continue
        work = meta.get("work", "main")
        # Index tables are authoritative for chapter names (stage pages only
        # repeat the header on the first page of a chapter).
        key = f"{meta.get('episode', '')}|{meta.get('chapter_no')}"
        if chapters.get(key):
            meta["chapter_title_en"] = chapters[key]
        if work == "event":
            meta["event_name_zh"] = events_zh.get(meta.get("event_name", ""), "")
        units.append(Unit(
            page_title=title,
            url=_page_url(title),
            meta=meta,
            scenes=scenes,
            work_rank=WORK_PRIORITY.get(work, 9),
            rerun="(rerun" in title.lower(),
        ))

    units.sort(key=lambda u: (u.work_rank, u.rerun, u.page_title))
    if not dedupe:
        return units

    seen: Dict[str, str] = {}
    kept: List[Unit] = []
    for unit in units:
        fresh: List[Scene] = []
        for scene in unit.scenes:
            h = scene.compute_hash()
            if h in seen:
                continue
            seen[h] = unit.page_title
            fresh.append(scene)
        if not fresh:
            continue
        for i, scene in enumerate(fresh):
            scene.ord = i
        unit.scenes = fresh
        kept.append(unit)
    return kept


# ── Chinese enrichment ─────────────────────────────────────────────

ZH_SYSTEM = (
    "你是《星陨计划》(Ark Re:Code) 的剧情资料整理助手，负责把英文剧情对白整理成"
    "供中文检索与角色记忆使用的中文资料。只输出 JSON，不要任何额外文字。"
)

ZH_PROMPT = """下面是一段《星陨计划》(Ark Re:Code) 的剧情页面，包含 {n} 个场景的英文对白。
请整理成中文资料，输出如下 JSON：
{{
  "unit_summary": "本页剧情的中文梗概，120-200字。按时间顺序写清关键事件、冲突与结果，点明出场角色。",
  "unit_keywords": ["8-15个中文关键词：人物名、地点、组织、关键事件/物品"],
  "chapter_title_zh": "本页所属章节或活动的中文标题（如「共鸣觉醒」）；不确定就给空字符串",
  "scenes": [
    {{"i": 1, "title_zh": "场景标题的中文（保留 Stage/Scene 编号）",
      "gist": "30-60字中文概括这一幕发生了什么",
      "keywords": ["3-6个中文关键词"]}}
  ]
}}

### 人名必须用「官方中文包译名」
下表是**官方译名**（English → 官方中文）。写作时**必须**严格使用表中的中文名，
不要用其它音译（例如不要写 阿帕西/崔斯特/玛莎/普瑞玛/盖塔/璐西玛/诺亚/米凯尔）。

【本页出场角色】
{cast}

【常用角色】（全库高频）
{glossary}

【世界观固定译名】（一律照此写，不要自创音译）
方舟(Ark)、方舟α/β/γ(Ark-α/β/γ)、方舟Ω(Ark-Ω)、使徒(Apostle)、星源(Astrogen)、
星源碎片(astrogen shards)、星源力(Astrogen power)、真理教团(The Order of Truth)、
团长(player)、佣兵团(Mercenary Union)、佣兵联盟、总理(Premier)、复制体(Clone)、
无国界医疗组织、变异体(mutant)、共鸣(Resonance)、特工(Agent)。
（注：使徒有时指那位具体存在，有时指整个族群，按上下文理解，都用「使徒」。）

规则：
1. 表里没有的角色：直接保留英文原名，**不要自己音译**，也不要猜。
   地名/组织名一律用上面的「世界观固定译名」，例如写 方舟α 而不是 阿卡-α。
2. 注意区分姐妹二人：Lucia=璐茜娅、Lucima=璐茜玛。两者常同场出现，**不要混用**；
   各场景标题下方标注了「本场出场」，请据此判断该场说的是谁。
3. 全部使用简体中文（人名按上表）。只做概括，不要逐句翻译台词。
4. scenes 数组必须恰好 {n} 个元素，i 依次为 1..{n}，与下面场景一一对应。

===== 页面：{title}（{meta_line}）=====

{body}
"""


def _load_env_value(key: str) -> str:
    """Read a key from QQBot/.env (falling back to the process environment)."""
    if os.environ.get(key):
        return os.environ[key]
    env_path = os.path.join(_QROOT, ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _meta_line(meta: dict) -> str:
    if meta.get("work") == "event":
        return f"活动剧情，主打角色：{meta.get('member') or '未知'}"
    if meta.get("work") == "urgent":
        return "主线·紧急任务"
    if meta.get("work") == "prologue":
        return "主线·序章"
    ep = meta.get("episode") or ""
    ch = meta.get("chapter_no")
    return f"{ep} 第{ch}章" if ch is not None else ep


def _official_cast(unit: Unit, id_to_zh: Dict[str, str]) -> Tuple[List[str], List[str]]:
    """Return (page cast lines, per-scene cast lines) using official names.

    ``page cast``  — "Trist=翠丝特" for every named speaker on the page.
    ``scene cast`` — same, per scene, so scenes where two similar characters
                     (Lucia / Lucima) co-occur can be told apart by context.
    """
    id_to_en: Dict[str, str] = {}
    page_pairs: List[str] = []
    seen: set = set()
    for scene in unit.scenes:
        for ln in scene.lines:
            if ln.kind == "say" and ln.char_id and ln.char_id in id_to_zh:
                pair = f"{ln.speaker}={id_to_zh[ln.char_id]}"
                id_to_en.setdefault(ln.char_id, ln.speaker)
                if pair not in seen:
                    seen.add(pair)
                    page_pairs.append(pair)

    scene_lines: List[str] = []
    for scene in unit.scenes:
        pairs: List[str] = []
        for ln in scene.lines:
            if ln.kind == "say" and ln.char_id and ln.char_id in id_to_zh:
                pair = f"{ln.speaker}={id_to_zh[ln.char_id]}"
                if pair not in pairs:
                    pairs.append(pair)
        scene_lines.append("、".join(pairs[:8]))
    return page_pairs, scene_lines


def _global_glossary(units: Sequence[Unit], id_to_zh: Dict[str, str],
                     limit: int = 40) -> List[str]:
    """Top-N characters by scene count, as "English=官方中文名"."""
    counts: Dict[str, int] = {}
    en_of: Dict[str, str] = {}
    for unit in units:
        for scene in unit.scenes:
            for ln in scene.lines:
                if ln.kind == "say" and ln.char_id and ln.char_id in id_to_zh:
                    counts[ln.char_id] = counts.get(ln.char_id, 0) + 1
                    en_of.setdefault(ln.char_id, ln.speaker)
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    return [f"{en_of[cid]}={id_to_zh[cid]}" for cid, _n in ranked if cid in en_of]


def _build_zh_body(unit: Unit, scene_cast: Sequence[str] = ()) -> str:
    """Compact per-scene dialogue digest for the enrichment prompt."""
    parts: List[str] = []
    total = 0
    for i, scene in enumerate(unit.scenes, 1):
        head = f"### 场景 {i}｜{scene.title or '(无标题)'}"
        if i - 1 < len(scene_cast) and scene_cast[i - 1]:
            head += f"\n（本场出场：{scene_cast[i - 1]}）"
        text = scene.render()
        if len(text) > ZH_MAX_SCENE_CHARS:
            text = text[:ZH_MAX_SCENE_CHARS] + "\n…（后续省略）"
        block = head + "\n" + text
        if total + len(block) > ZH_MAX_TOTAL_CHARS:
            parts.append(f"### 场景 {i}｜{scene.title or '(无标题)'}\n（内容过长，已省略）")
            continue
        total += len(block)
        parts.append(block)
    return "\n\n".join(parts)


async def _llm_json(client, model: str, prompt: str, retries: int = 3) -> Optional[dict]:
    import httpx

    api_key = _load_env_value("DEEPSEEK_API_KEY")
    api_base = (_load_env_value("DEEPSEEK_API_BASE") or "https://api.deepseek.com").rstrip("/")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置（QQBot/.env）")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": ZH_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "stream": False,
    }
    url = f"{api_base}/chat/completions"
    for attempt in range(retries):
        try:
            resp = await client.post(
                url, json=payload, timeout=300,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            content = resp.json()["choices"][0]["message"]["content"]
            return json.loads(content)
        except Exception as exc:  # noqa: BLE001
            if attempt == retries - 1:
                print(f"[enrich] 失败: {type(exc).__name__}: {exc}")
                return None
            await asyncio.sleep(2.0 * (attempt + 1))
    return None


def _load_zh() -> dict:
    try:
        with open(ZH_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_zh(data: dict) -> None:
    os.makedirs(ZH_DIR, exist_ok=True)
    tmp = ZH_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, ZH_PATH)


def enrich(units: Sequence[Unit], model: str, concurrency: int,
           force: bool = False, limit: int = 0) -> dict:
    """Stage 3: LLM Chinese summaries per unit, written to zh/units.json."""
    import httpx

    zh = _load_zh()
    todo = [u for u in units
            if force or str((zh.get(u.page_title) or {}).get("version", 0)) != str(ZH_PROMPT_VERSION)]
    if limit:
        todo = todo[:limit]
    print(f"[enrich] {len(units)} units, {len(zh)} cached, {len(todo)} to enrich "
          f"(model={model}, concurrency={concurrency})")
    if not todo:
        return zh

    sem = asyncio.Semaphore(max(1, concurrency))
    done = {"n": 0, "fail": 0}

    id_to_zh = _char_id_to_zh()
    glossary = "、".join(_global_glossary(units, id_to_zh))

    async def one(client, unit: Unit):
        async with sem:
            page_cast, scene_cast = _official_cast(unit, id_to_zh)
            prompt = ZH_PROMPT.format(
                n=len(unit.scenes),
                title=unit.page_title,
                meta_line=_meta_line(unit.meta),
                cast="、".join(page_cast) or "（本页无名册角色）",
                glossary=glossary or "（无）",
                body=_build_zh_body(unit, scene_cast),
            )
            data = await _llm_json(client, model, prompt)
            done["n"] += 1
            if not data:
                done["fail"] += 1
                return
            scenes = data.get("scenes")
            if not isinstance(scenes, list) or len(scenes) != len(unit.scenes):
                print(f"[enrich] 场景数不匹配，丢弃: {unit.page_title} "
                      f"({len(scenes) if isinstance(scenes, list) else 'n/a'} "
                      f"!= {len(unit.scenes)})")
                done["fail"] += 1
                return
            zh[unit.page_title] = {
                "version": ZH_PROMPT_VERSION,
                "model": model,
                "updated_at": int(time.time()),
                "unit_summary": str(data.get("unit_summary", "")).strip(),
                "unit_keywords": [str(k).strip() for k in (data.get("unit_keywords") or [])][:20],
                "chapter_title_zh": str(data.get("chapter_title_zh", "")).strip(),
                "scenes": [
                    {
                        "title_zh": str(s.get("title_zh", "")).strip(),
                        "gist": str(s.get("gist", "")).strip(),
                        "keywords": [str(k).strip() for k in (s.get("keywords") or [])][:8],
                    }
                    for s in scenes
                ],
            }
            if done["n"] % 5 == 0:
                _save_zh(zh)
                print(f"[enrich] {done['n']}/{len(todo)} 完成（失败 {done['fail']}）")

    async def _run_all():
        async with httpx.AsyncClient() as client:
            await asyncio.gather(*(one(client, u) for u in todo))

    asyncio.run(_run_all())
    _save_zh(zh)
    print(f"[enrich] done: {done['n']} attempted, {done['fail']} failed, "
          f"{len(zh)} cached total")
    return zh


# ── Index (units -> SQLite) ────────────────────────────────────────

DDL = """
PRAGMA journal_mode=DELETE;
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE unit (
    id INTEGER PRIMARY KEY,
    page_title TEXT NOT NULL UNIQUE,
    work TEXT NOT NULL,
    episode TEXT DEFAULT '',
    chapter_no INTEGER,
    chapter_title_en TEXT DEFAULT '',
    chapter_title_zh TEXT DEFAULT '',
    event_name TEXT DEFAULT '',
    event_name_zh TEXT DEFAULT '',
    member TEXT DEFAULT '',
    url TEXT DEFAULT '',
    scene_count INTEGER DEFAULT 0,
    zh_summary TEXT DEFAULT '',
    zh_keywords TEXT DEFAULT '',
    zh_ver INTEGER DEFAULT 0
);
CREATE INDEX idx_unit_work ON unit(work);

CREATE TABLE scene (
    id INTEGER PRIMARY KEY,
    unit_id INTEGER NOT NULL,
    ord INTEGER NOT NULL,
    scene_title TEXT DEFAULT '',
    scene_title_zh TEXT DEFAULT '',
    n_lines INTEGER DEFAULT 0,
    char_ids TEXT DEFAULT '',
    speakers TEXT DEFAULT '',
    zh_gist TEXT DEFAULT '',
    zh_kw TEXT DEFAULT '',
    cast_line TEXT DEFAULT '',
    content_hash TEXT DEFAULT ''
);
CREATE INDEX idx_scene_unit ON scene(unit_id, ord);

CREATE TABLE chunk (
    id INTEGER PRIMARY KEY,
    scene_id INTEGER NOT NULL,
    ord INTEGER NOT NULL,
    text_en TEXT NOT NULL,
    n_lines INTEGER DEFAULT 0
);
CREATE INDEX idx_chunk_scene ON chunk(scene_id);

CREATE VIRTUAL TABLE chunk_fts USING fts5(
    title, text_en, chars, content='', tokenize="unicode61 remove_diacritics 2"
);
CREATE VIRTUAL TABLE chunk_zh_fts USING fts5(
    zh, content='', tokenize="unicode61 remove_diacritics 2"
);
"""


def chunk_scene(scene: Scene) -> List[List[Line]]:
    """Split a long scene into overlapping line windows."""
    lines = scene.lines
    if len(lines) <= SCENE_SOFT_MAX:
        return [lines]
    out: List[List[Line]] = []
    step = max(1, CHUNK_LINES - CHUNK_OVERLAP)
    i = 0
    while i < len(lines):
        out.append(lines[i:i + CHUNK_LINES])
        if i + CHUNK_LINES >= len(lines):
            break
        i += step
    return out


# ── Official name normalisation ────────────────────────────────────
#
# The zh enrichment was written by an LLM reading *English* dialogue, so it
# transliterated names its own way (阿帕西/玛莎/崔斯特…). The official Chinese
# client package uses different spellings (埃帕蒂/玛夏/翠丝特…), which live in
# config/characters/members_info.json `name`. Gists/keywords are rewritten to
# the official spelling at index time; the fan spellings are kept as extra FTS
# aliases so a query with either form still retrieves.

_NAME_VARIANT_FILE = os.path.join(
    _QROOT, "config", "characters", "story_name_variants.json")


_EXCLUSIVE_RULES: List[Tuple[List[str], Dict[str, str]]] = []


def _load_name_variants() -> Tuple[Dict[str, str], Dict[str, Dict[str, str]]]:
    """(unambiguous, ambiguous) name tables, longest key first.

    ``ambiguous`` maps a fan spelling to {character id: official name} — the
    corpus uses some spellings for two different people (姐妹 璐西玛), so those
    are resolved per scene from ``char_ids`` instead of globally.
    """
    try:
        with open(_NAME_VARIANT_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:  # noqa: BLE001
        return {}, {}
    pairs = {}
    for k, v in (raw.get("variants") or {}).items():
        if isinstance(k, str) and isinstance(v, str) and k and k != v:
            pairs[k] = v
    amb = {}
    for k, m in (raw.get("ambiguous") or {}).items():
        if isinstance(k, str) and isinstance(m, dict) and m:
            amb[k] = {str(c): str(v) for c, v in m.items() if v}
    exclusive = []
    for rule in (raw.get("cast_exclusive") or []):
        ids = [str(i) for i in (rule.get("ids") or []) if i]
        names = {str(k): str(v) for k, v in (rule.get("names") or {}).items() if v}
        if len(ids) >= 2 and all(i in names for i in ids):
            exclusive.append((ids, names))
    _EXCLUSIVE_RULES.clear()
    _EXCLUSIVE_RULES.extend(exclusive)
    return (dict(sorted(pairs.items(), key=lambda kv: -len(kv[0]))),
            dict(sorted(amb.items(), key=lambda kv: -len(kv[0]))))


NAME_VARIANTS, AMBIGUOUS_VARIANTS = _load_name_variants()


def normalize_names(text: str, char_ids: Sequence[str] = ()) -> str:
    """Rewrite fan-transliterated names to the official package spelling.

    ``char_ids`` (the scene's cast) resolves genuinely ambiguous spellings; when
    it cannot, the text is left alone rather than guessed.
    """
    if not text:
        return text
    for src, dst in NAME_VARIANTS.items():
        if src in text:
            text = text.replace(src, dst)
    if AMBIGUOUS_VARIANTS:
        present = set(char_ids or ())
        for src, by_char in AMBIGUOUS_VARIANTS.items():
            if src not in text:
                continue
            hits = {by_char[c] for c in present if c in by_char}
            if len(hits) == 1:
                text = text.replace(src, hits.pop())
    # Look-alike pairs (sisters): correct the wrong name only when a single one
    # of them is on stage AND the text does not already name both (a scene can
    # legitimately mention the absent sister).
    if _EXCLUSIVE_RULES and char_ids:
        present = set(char_ids)
        for ids, names in _EXCLUSIVE_RULES:
            on_stage = [i for i in ids if i in present]
            if len(on_stage) != 1:
                continue
            pair_names = [names[i] for i in ids]
            if all(n in text for n in pair_names):
                continue          # both mentioned on purpose → leave it alone
            keep = names[on_stage[0]]
            for other in ids:
                if other != on_stage[0]:
                    wrong = names[other]
                    if wrong != keep and wrong in text:
                        text = text.replace(wrong, keep)

    # Self-duplication safety net, after the correction above.
    if _EXCLUSIVE_RULES:
        for _ids, _names in _EXCLUSIVE_RULES:
            vals = list(dict.fromkeys(_names.values()))
            for a in vals:
                other = next((v for v in vals if v != a), None)
                if not other:
                    continue
                for conj in ("与", "和", "跟", "同"):
                    text = text.replace(f"{a}{conj}{a}", f"{a}{conj}{other}")
    return text


def _zh_blob(title_zh: str, gist: str, keywords: Sequence[str],
             chars_zh: Sequence[str], unit_zh: str,
             aliases: Sequence[str] = ()) -> str:
    """Bigram-indexed Chinese text for one chunk.

    ``aliases`` carries the fan-transliterated spellings of the characters in
    this scene, so both naming systems stay searchable after normalisation.
    """
    parts = [title_zh or "", gist or "", " ".join(keywords or []),
             " ".join(chars_zh or []), " ".join(aliases or []), unit_zh or ""]
    raw = " 。".join(p for p in parts if p)
    return " ".join(cjk_bigrams(raw))


def build_index(units: Sequence[Unit], zh: dict, db_path: str,
                source_note: str = "arkrecodewiki.miraheze.org") -> dict:
    """Stage 4: write the SQLite database (atomically) and return stats."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    tmp_path = db_path + ".building"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    # Chinese name lookup: char_id → 官方中文名 (for the Chinese index).
    id_to_zh = _char_id_to_zh()
    # official → [fan spellings], so they can be added to the searchable blob.
    official_to_alias: Dict[str, List[str]] = {}
    for _fan, _official in NAME_VARIANTS.items():
        official_to_alias.setdefault(_official, []).append(_fan)
    for _fan, _by_char in AMBIGUOUS_VARIANTS.items():
        for _official in _by_char.values():
            official_to_alias.setdefault(_official, []).append(_fan)

    # A chapter spans several pages; the LLM may phrase its Chinese name
    # slightly differently on each. Pick the most frequent non-empty variant
    # so every page of a chapter shares one label.
    chapter_zh_votes: Dict[str, Dict[str, int]] = {}
    for unit in units:
        meta = unit.meta
        if meta.get("work") == "event":
            continue
        zh_title = str((zh.get(unit.page_title) or {}).get("chapter_title_zh", "")).strip()
        if not zh_title:
            continue
        key = f"{meta.get('episode', '')}|{meta.get('chapter_no')}"
        votes = chapter_zh_votes.setdefault(key, {})
        votes[zh_title] = votes.get(zh_title, 0) + 1
    chapter_zh_best = {
        k: max(v.items(), key=lambda kv: kv[1])[0] for k, v in chapter_zh_votes.items()
    }

    conn = sqlite3.connect(tmp_path)
    try:
        conn.executescript(DDL)
        n_units = n_scenes = n_chunks = n_lines = n_zh = 0

        for unit in units:
            meta = unit.meta
            zinfo = zh.get(unit.page_title) or {}
            scenes_zh = zinfo.get("scenes") or []
            zh_ver = int(zinfo.get("version", 0) or 0)
            ch_key = f"{meta.get('episode', '')}|{meta.get('chapter_no')}"
            acct_ch_title_zh = normalize_names(
                chapter_zh_best.get(ch_key, "") or str(
                    zinfo.get("chapter_title_zh", "")).strip())
            # The wiki index only carries Chinese titles for a couple of
            # events, so the LLM's chapter_title_zh doubles as the event's
            # Chinese name when nothing authoritative exists.
            event_zh = (meta.get("event_name_zh", "") or "").strip()
            if not event_zh and meta.get("work") == "event":
                event_zh = str(zinfo.get("chapter_title_zh", "")).strip()

            cur = conn.execute(
                "INSERT INTO unit (page_title, work, episode, chapter_no,"
                " chapter_title_en, chapter_title_zh, event_name, event_name_zh,"
                " member, url, scene_count, zh_summary, zh_keywords, zh_ver)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    unit.page_title,
                    meta.get("work", "main"),
                    meta.get("episode", "") or "",
                    meta.get("chapter_no"),
                    meta.get("chapter_title_en", "") or "",
                    acct_ch_title_zh if meta.get("work") != "event" else "",
                    meta.get("event_name", "") or "",
                    event_zh,
                    meta.get("member", "") or "",
                    unit.url,
                    len(unit.scenes),
                    normalize_names(zinfo.get("unit_summary", "") or ""),
                    "、".join(normalize_names(str(k))
                              for k in (zinfo.get("unit_keywords") or [])),
                    zh_ver,
                ),
            )
            unit_id = int(cur.lastrowid)
            n_units += 1
            if zinfo.get("unit_summary"):
                n_zh += 1

            unit_zh = " ".join(filter(None, [
                acct_ch_title_zh,
                meta.get("event_name_zh", "") or "",
                " ".join(zinfo.get("unit_keywords") or []),
            ]))

            for scene in unit.scenes:
                sz = scenes_zh[scene.ord] if scene.ord < len(scenes_zh) else {}
                raw_title_zh = (sz.get("title_zh") or "") if isinstance(sz, dict) else ""
                raw_gist = (sz.get("gist") or "") if isinstance(sz, dict) else ""
                raw_kw = (sz.get("keywords") or []) if isinstance(sz, dict) else []
                char_ids = scene.char_ids
                title_zh = normalize_names(raw_title_zh, char_ids)
                gist = normalize_names(raw_gist, char_ids)
                kw = [normalize_names(str(k), char_ids) for k in raw_kw]
                chars_zh = [id_to_zh.get(c, "") for c in char_ids]
                chars_zh = [c for c in chars_zh if c]
                # Fan spellings of the same characters, kept searchable only.
                alias_zh = [a for c in chars_zh for a in official_to_alias.get(c, [])]

                # English speaker → official Chinese name, for the cast line.
                cast_pairs: List[str] = []
                for ln in scene.lines:
                    if ln.kind == "say" and ln.char_id and ln.char_id in id_to_zh:
                        pair = f"{ln.speaker}={id_to_zh[ln.char_id]}"
                        if pair not in cast_pairs:
                            cast_pairs.append(pair)
                scene_cast = "、".join(cast_pairs[:6])

                cur = conn.execute(
                    "INSERT INTO scene (unit_id, ord, scene_title, scene_title_zh,"
                    " n_lines, char_ids, speakers, zh_gist, zh_kw, cast_line, content_hash)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        unit_id, scene.ord, scene.title, title_zh,
                        len(scene.lines), ",".join(char_ids),
                        ",".join(scene.speakers), gist, "、".join(kw), scene_cast,
                        scene.content_hash,
                    ),
                )
                scene_id = int(cur.lastrowid)
                n_scenes += 1
                n_lines += len(scene.lines)

                for ci, window in enumerate(chunk_scene(scene)):
                    text_en = Scene(lines=window).render()
                    cur = conn.execute(
                        "INSERT INTO chunk (scene_id, ord, text_en, n_lines)"
                        " VALUES (?,?,?,?)",
                        (scene_id, ci, text_en, len(window)),
                    )
                    chunk_id = int(cur.lastrowid)
                    n_chunks += 1
                    # English index: scene title (once) + dialogue + character names
                    title_for_fts = (scene.title or "") if ci == 0 else ""
                    chars_en = ",".join(sorted({ln.speaker for ln in window
                                                if ln.kind in ("say", "player", "narration")}))
                    conn.execute(
                        "INSERT INTO chunk_fts (rowid, title, text_en, chars)"
                        " VALUES (?,?,?,?)",
                        (chunk_id, title_for_fts, text_en, chars_en),
                    )
                    zh_blob = _zh_blob(
                        title_zh if ci == 0 else "", gist, kw, chars_zh, unit_zh,
                        aliases=alias_zh,
                    )
                    if not zh_blob and text_en:
                        # gamedata 数据源：正文本身就是中文，不需要 LLM 摘要。
                        # 直接按 cjk_bigrams 建索引；把 unit 标题也带上，否则
                        # "按角色名找羁绊剧情" 会检索不到（标题里有角色名）。
                        zh_blob = _zh_blob(
                            scene.title or "", text_en, (), (), unit_zh or unit.page_title,
                            aliases=alias_zh,
                        )
                    if zh_blob:
                        conn.execute(
                            "INSERT INTO chunk_zh_fts (rowid, zh) VALUES (?,?)",
                            (chunk_id, zh_blob),
                        )

        meta_rows = {
            "schema_version": str(SCHEMA_VERSION),
            "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "built_at_ts": str(int(time.time())),
            "source": source_note,
            "zh_prompt_version": str(ZH_PROMPT_VERSION),
            "chunk_lines": str(CHUNK_LINES),
            "chunk_overlap": str(CHUNK_OVERLAP),
        }
        conn.executemany("INSERT INTO meta (key, value) VALUES (?,?)", meta_rows.items())
        conn.commit()
        conn.execute("PRAGMA optimize")
        conn.commit()
    finally:
        conn.close()

    os.replace(tmp_path, db_path)
    stats = {
        "units": n_units, "scenes": n_scenes, "chunks": n_chunks,
        "dialog_lines": n_lines, "units_with_zh": n_zh, "db": db_path,
        "size_mb": round(os.path.getsize(db_path) / 1048576, 2),
    }
    return stats


def _char_id_to_zh() -> Dict[str, str]:
    """{'H076': '露比', ...} from the vendored members_info.json."""
    path = os.path.join(_QROOT, "config", "characters", "members_info.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            members = json.load(f)
    except Exception:  # noqa: BLE001
        return {}
    return {m["id"]: m.get("name", "") for m in members if m.get("id")}


# ── Stats / export ─────────────────────────────────────────────────

def print_stats(db_path: str = DB_PATH) -> None:
    from lib.story_kb import StoryKB

    kb = StoryKB(db_path)
    st = kb.stats()
    if not st.get("available"):
        print("[stats] 未找到可用的 story.db，请先运行 --index")
        return
    print("[stats]")
    for k, v in st.items():
        print(f"  {k:14s} {v}")
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT work, COUNT(*) c FROM unit GROUP BY work ORDER BY c DESC"
        ).fetchall()
        print("  by work:")
        for work, c in rows:
            print(f"    {work:10s} {c}")
        rows = conn.execute(
            "SELECT e.name, COUNT(*) FROM chunk_fts f JOIN chunk c ON c.id=f.rowid "
            "JOIN scene s ON s.id=c.scene_id JOIN unit u ON u.id=s.unit_id "
            "GROUP BY u.work"
        ).fetchall()
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def export_markdown(units: Sequence[Unit], zh: dict, out_path: str) -> None:
    """Human-readable dump (handy for eyeballing parse quality)."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for unit in units:
            zinfo = zh.get(unit.page_title) or {}
            f.write(f"# {unit.page_title}\n\n")
            if zinfo.get("unit_summary"):
                f.write(f"> {zinfo['unit_summary']}\n\n")
            f.write(f"来源：{unit.url}\n\n")
            for i, scene in enumerate(unit.scenes, 1):
                sz = (zinfo.get("scenes") or [{}] * i)[i - 1] if zinfo else {}
                f.write(f"## {i}. {scene.title}\n\n")
                if isinstance(sz, dict) and sz.get("gist"):
                    f.write(f"*{sz['gist']}*\n\n")
                f.write("```\n" + scene.render() + "\n```\n\n")


def migrate() -> dict:
    """Copy the KB artifacts from the repo checkout to the resolved data dir.

    Idempotent: existing files at the destination are kept (the destination is
    authoritative once it has a database), so re-running after a partial copy
    only fills the gaps. Prints the before/after paths so an operator can see
    where the KB actually ends up.
    """
    import shutil

    src_dir = os.path.join(_QROOT, "data", "story_kb")
    dst_dir = DATA_DIR
    print(f"[migrate] source     : {src_dir}")
    print(f"[migrate] destination: {dst_dir}")
    if os.path.abspath(src_dir) == os.path.abspath(dst_dir):
        print("[migrate] 源与目标相同（本机即数据目录），无需迁移")
        return {"moved": 0, "dest": dst_dir}

    moved = 0
    os.makedirs(dst_dir, exist_ok=True)
    for name in ("raw", "zh"):
        src = os.path.join(src_dir, name)
        if not os.path.isdir(src):
            continue
        for root, _dirs, files in os.walk(src):
            rel = os.path.relpath(root, src_dir)
            target_dir = os.path.join(dst_dir, rel)
            os.makedirs(target_dir, exist_ok=True)
            for fname in files:
                s = os.path.join(root, fname)
                d = os.path.join(target_dir, fname)
                if os.path.exists(d):
                    continue
                shutil.copy2(s, d)
                moved += 1
    db_src = os.path.join(src_dir, "story.db")
    db_dst = os.path.join(dst_dir, "story.db")
    if os.path.isfile(db_src) and not os.path.exists(db_dst):
        shutil.copy2(db_src, db_dst)
        moved += 1
    print(f"[migrate] copied {moved} file(s); DB now at {db_dst} "
          f"(exists={os.path.isfile(db_dst)})")
    if moved == 0 and not os.path.isfile(db_dst):
        print("[migrate] 没有可迁移的数据，请先运行 --crawl / --enrich / --index")
    return {"moved": moved, "dest": dst_dir}



# ── gamedata 数据源（P3）：把解包出来的剧情/羁绊写进同一个 story.db ──
# 设计：换数据源、不换检索语义。Unit/Scene/Line 结构完全复用，build_index 不变，
# 因此 lib/story_kb.py 的 FTS 检索、中文 bigram、persona 加成全部原样生效。

def _gd_paths(lines: List[dict]) -> List[List[str]]:
    """把一段 drama 展开成多条完整路径（分支不压平，与 gamedata_source 同算法）。"""
    index = {r.get("id"): r for r in lines}
    succ = {r.get("id"): [n for n in str(r.get("next") or "").split("~") if n in index]
            for r in lines}
    pred: set = set()
    for v in succ.values():
        pred.update(v)
    roots = [r.get("id") for r in lines if r.get("id") not in pred] or [lines[0].get("id")]
    paths: List[List[str]] = []

    def walk(node: str, acc: List[str]) -> None:
        if len(paths) >= 24 or node in acc:
            if node in acc:
                paths.append(acc)
            return
        acc = acc + [node]
        nxt = succ.get(node) or []
        if len(nxt) > 1:
            for n in nxt:
                walk(n, acc)
            return
        if not nxt:
            paths.append(acc)
            return
        walk(nxt[0], acc)

    for r in roots:
        walk(r, [])
    return paths or [[r.get("id") for r in lines]]


def _clean_text(text: str) -> str:
    """剥掉游戏内富文本标签：``<size=85>``/``<br>``/``<color>`` 之类，保留换行语义。"""
    if not text:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", text)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("\u3000", " ")
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def units_from_gamedata(gdata_dir: str, with_outline: bool = True) -> List[Unit]:
    """读 gamedata 产物 → wiki 同构的 Unit 列表（羁绊剧情台词 + 章节场景目录）。"""
    import hashlib

    def load(*parts):
        p = os.path.join(gdata_dir, *parts)
        if not os.path.isfile(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)

    bonds_raw = load("bonds.json") or {}
    # bonds.json 是 {"bonds": {id: {...}}} 包装结构；直接 .values() 会拿到壳 dict
    bonds = bonds_raw.get("bonds") if isinstance(bonds_raw.get("bonds"), dict) else bonds_raw
    roles = load("roles.json") or {}
    chapters = load("story", "chapters.json") or {}
    scenes_idx = load("story", "scenes.json") or {}
    line_dialogs = load("story", "line_dialogs.json") or []
    text_zh = load("story", "text_zh_hans.json") or {}
    text_en = load("story", "text_en.json") or {}

    role_name = {}
    for code, info in (roles.get("roles") or {}).items():
        names = info.get("name") or {}
        role_name[code] = names.get("zh_hans") or names.get("en") or code

    by_drama: Dict[str, List[dict]] = {}
    for r in line_dialogs:
        by_drama.setdefault(r.get("drama"), []).append(r)

    def mk_scene(title: str, ord_: int, entries: List[Tuple[str, str, str]]) -> Optional[Scene]:
        lines = [Line(speaker=spk or "旁白", text=txt, char_id=cid,
                      kind="say" if cid else "narration")
                 for spk, txt, cid in entries if txt]
        if not lines:
            return None
        blob = "\n".join(f"{ln.speaker}: {ln.text}" for ln in lines)
        return Scene(title=title, lines=lines, ord=ord_,
                     content_hash=hashlib.md5(blob.encode("utf-8")).hexdigest())

    units: List[Unit] = []
    seen_titles: set = set()
    seen_drama: set = set()

    # 1) 羁绊剧情：每条路径一个 scene，文本用游戏内原文（中文优先）
    for bond in bonds.values():
        code = bond.get("role")
        if not code:
            continue
        for did in bond.get("drama_ids") or []:
            # 同一角色可能有多条 RoleCollection 记录指向同一 drama → 去重
            if not did or (code, did) in seen_drama:
                continue
            seen_drama.add((code, did))
            rows = by_drama.get(did) or []
            if not rows:
                continue
            index = {r.get("id"): r for r in rows}
            scenes: List[Scene] = []
            for i, path in enumerate(_gd_paths(rows)):
                entries = []
                for pid in path:
                    row = index.get(pid) or {}
                    key = row.get("text_key") or ""
                    txt = text_zh.get(key) or text_en.get(key) or ""
                    spk = row.get("speaker") or ""
                    entries.append((role_name.get(spk, "") if spk else "", txt, spk))
                sc = mk_scene(f"{did} 第{i + 1}段" if i else f"{did} 主线",
                              i, entries)
                if sc:
                    scenes.append(sc)
            if not scenes:
                continue
            title = f"{role_name.get(code, code)} 羁绊剧情 {did}"
            if title in seen_titles:      # unit.page_title 有 UNIQUE 约束
                continue
            seen_titles.add(title)
            units.append(Unit(
                page_title=title,
                url="",
                meta={"work": "bond", "episode": did.split("_")[0],
                      "chapter_no": 0,
                      "chapter_title_en": did,
                      "chapter_title_zh": f"{role_name.get(code, code)} 羁绊",
                      "member": role_name.get(code, code)},
                scenes=scenes,
            ))

    # 2) 主线（逐场景切、unit 挂章节）+ 活动（客户端原文，用于持续补充中文活动库）
    dialog_index = load("story", "dialog_index.json") or {}
    main_scenes = (dialog_index.get("main_scenes") or {})
    events_idx = (dialog_index.get("events") or {})
    # 活动归档优先：out/events/<event>.json 是跨版本累积的中文原文，
    # 客户端表里只有当期活动（下线后行会被删）。
    archive_events: Dict[str, dict] = {}
    ev_dir = os.path.join(gdata_dir, "events")
    if os.path.isdir(ev_dir):
        for fn in sorted(os.listdir(ev_dir)):
            if not fn.endswith(".json") or fn.startswith("_"):
                continue
            try:
                with open(os.path.join(ev_dir, fn), "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("event"):
                    archive_events[data["event"]] = data
            except Exception:  # noqa: BLE001
                continue
    for ev_id, data in archive_events.items():
        events_idx.setdefault(ev_id, {"segment_list": []})
    hash_seen: set = set()

    def push_unit(title: str, meta: dict, scenes: List[Scene]) -> None:
        if not scenes or title in seen_titles:
            return
        seen_titles.add(title)
        units.append(Unit(page_title=title, url="", meta=meta, scenes=scenes))

    if main_scenes:
        by_chapter: Dict[str, List[dict]] = {}
        for sc in scenes_idx.values():
            if sc.get("dialog_refs"):
                by_chapter.setdefault(sc.get("chapter") or "", []).append(sc)
        for ch_id, sc_list in by_chapter.items():
            ch = chapters.get(ch_id) or {}
            ch_name = ((ch.get("name") or {}).get("zh_hans")
                       or (ch.get("name") or {}).get("en") or ch_id)
            sc_list.sort(key=lambda s: (str((s.get("num") or {}).get("zh_hans") or ""), s.get("id")))
            ch_scenes: List[Scene] = []
            for si, sc in enumerate(sc_list):
                entries: List[Tuple[str, str, str]] = []
                for ref in sc.get("dialog_refs") or []:
                    grp = main_scenes.get(ref) or {}
                    for ln in grp.get("lines") or []:
                        key = ln.get("text_key") or ""
                        txt = _clean_text(text_zh.get(key) or text_en.get(key) or "")
                        if not txt:
                            continue
                        spk_key = ln.get("speaker") or ""
                        spk = _clean_text(text_zh.get(spk_key) or "")
                        entries.append((spk, txt, ""))
                if not entries:
                    continue
                num = (sc.get("num") or {}).get("zh_hans") or ""
                nm = (sc.get("name") or {}).get("zh_hans") or \
                     (sc.get("name") or {}).get("en") or ""
                s_ob = mk_scene(f"{num} {nm}".strip() or sc.get("id", ""), si, entries)
                if s_ob and s_ob.content_hash not in hash_seen:
                    hash_seen.add(s_ob.content_hash)
                    ch_scenes.append(s_ob)
            push_unit(f"{ch_id} {ch_name}（剧情）",
                      {"work": "main", "episode": ch.get("episode") or "",
                       "chapter_no": 0, "chapter_title_en": (ch.get("name") or {}).get("en") or "",
                       "chapter_title_zh": ch_name},
                      ch_scenes)

    for ev_id, ev in events_idx.items():
        # 优先用活动归档（out/events/，跨版本累积；活动下线后客户端文本会消失）
        arch = archive_events.get(ev_id)
        if arch and arch.get("segments"):
            segs = [{"segment": s.get("segment"),
                     "lines": [{"text_key": None, "id": l.get("id"),
                                "speaker": None, "text": l.get("text")}
                               for l in s.get("lines") or []]}
                    for s in arch["segments"]]
            ev_title = arch.get("title") or ev_id
        else:
            segs = ev.get("segment_list") or []
            ev_title = ""
        ev_scenes: List[Scene] = []
        for si, seg in enumerate(segs):
            entries = []
            for ln in seg.get("lines") or []:
                if ln.get("text") is not None:          # 归档来源：文本已就绪
                    txt = _clean_text(ln.get("text") or "")
                    spk = ln.get("speaker") or ""
                else:                                    # 客户端表来源：查 locale
                    key = ln.get("text_key") or ""
                    txt = _clean_text(text_zh.get(key) or text_en.get(key) or "")
                    spk = _clean_text(text_zh.get(ln.get("speaker") or "") or "")
                if not txt:
                    continue
                if not ev_title:
                    head = [x.strip() for x in txt.split("\n") if x.strip()]
                    if head:
                        ev_title = " · ".join(head[:2])
                entries.append((spk, txt, ""))
            s_ob = mk_scene(f"{seg.get('segment')}", si, entries)
            if s_ob and s_ob.content_hash not in hash_seen:
                hash_seen.add(s_ob.content_hash)
                ev_scenes.append(s_ob)
        if ev_scenes:
            push_unit(f"{ev_id} {ev_title or ev_id}（活动·客户端原文）",
                      {"work": "event", "episode": ev_id, "chapter_no": 0,
                       "chapter_title_zh": ev_title, "event_name_zh": ev_title},
                      ev_scenes)

    # 3) 章节目录：让"久别重逢"这类场景名也能被检索到（无台词，仅标题行）
    if with_outline:
        # 只为"没有台词、进不了剧情 unit"的章节建目录 unit：
        # 两者都建会让目录文本与剧情 unit 抢排名（实测「猫祭 羁绊」被目录挤掉）。
        by_chapter2: Dict[str, List[dict]] = {}
        for sc in scenes_idx.values():
            by_chapter2.setdefault(sc.get("chapter"), []).append(sc)
        has_story = {ch_id for ch_id, scs in by_chapter2.items()
                     if any(sc.get("dialog_refs") for sc in scs)}
        for ch_id, ch in chapters.items():
            if not ch.get("name") or ch_id in has_story:
                continue
            rows = sorted(by_chapter2.get(ch_id) or [], key=lambda s: str(s.get("id")))
            if not rows:
                continue
            entries = []
            for sc in rows:
                num = (sc.get("num") or {}).get("zh_hans") or ""
                nm = (sc.get("name") or {}).get("zh_hans") or \
                     (sc.get("name") or {}).get("en") or ""
                if nm:
                    entries.append(("", f"{num} {nm}".strip(), ""))
            sc_ob = mk_scene(f"{ch_id} 场景目录", 0, entries)
            if not sc_ob:
                continue
            units.append(Unit(
                page_title=f"{ch_id} {ch['name'].get('zh_hans') or ch['name'].get('en')}（场景目录）",
                url="",
                meta={"work": "main", "episode": ch.get("episode") or "",
                      "chapter_no": 0,
                      "chapter_title_en": ch["name"].get("en") or "",
                      "chapter_title_zh": ch["name"].get("zh_hans") or ""},
                scenes=[sc_ob],
            ))
    return units



# ── CLI ────────────────────────────────────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Build the Ark Re:Code story knowledge base",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--all", action="store_true", help="crawl + index + enrich")
    ap.add_argument("--crawl", action="store_true", help="stage 1: download wikitext")
    ap.add_argument("--index", action="store_true", help="stage 3: build SQLite")
    ap.add_argument("--enrich", action="store_true", help="stage 2: LLM Chinese summaries")
    ap.add_argument("--stats", action="store_true", help="print DB stats")
    ap.add_argument("--migrate", action="store_true",
                    help="copy raw/ + zh/ + story.db from the repo checkout "
                         "to the resolved data directory (e.g. the data disk)")
    ap.add_argument("--export-md", metavar="PATH", help="dump parsed units to markdown")
    ap.add_argument("--limit", type=int, default=0, help="cap pages (smoke test)")
    ap.add_argument("--enrich-limit", type=int, default=0, help="cap enrichment calls")
    ap.add_argument("--refetch", action="store_true", help="ignore the raw cache")
    ap.add_argument("--no-dedupe", action="store_true", help="keep duplicate scenes")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="LLM model for enrichment")
    ap.add_argument("--concurrency", type=int, default=3, help="enrichment concurrency")
    ap.add_argument("--force-enrich", action="store_true", help="re-enrich cached units")
    ap.add_argument("--db", default=DB_PATH, help="output database path")
    ap.add_argument("--gamedata", metavar="DIR",
                    help="用解包产物建库（读 DIR/story/*.json + bonds.json），"
                         "不需要 --crawl；与 wiki 同构写入同一 schema")
    args = ap.parse_args(argv)

    # ── gamedata 数据源模式（独立于 wiki 爬取）──
    if args.gamedata:
        units = units_from_gamedata(args.gamedata)
        if not units:
            print(f"[gamedata] {args.gamedata} 下没有可入库的剧情/羁绊数据")
            return 2
        lines = sum(len(s.lines) for u in units for s in u.scenes)
        print(f"[gamedata] {len(units)} units, "
              f"{sum(len(u.scenes) for u in units)} scenes, {lines} lines")
        stats = build_index(units, {}, args.db, source_note="gamedata:ARCode")
        print(f"[gamedata] {json.dumps(stats, ensure_ascii=False)}")
        return 0

    do_all = args.all or not any((args.crawl, args.index, args.enrich, args.stats,
                                  args.export_md, args.migrate))
    rc = 0

    if args.migrate:
        migrate()

    if args.crawl or do_all:
        try:
            crawl(limit=args.limit, refetch=args.refetch)
        except Exception as exc:  # noqa: BLE001
            print(f"[crawl] FATAL: {type(exc).__name__}: {exc}")
            return 2
        try:
            crawl_indexes(refetch=args.refetch)
        except Exception as exc:  # noqa: BLE001
            print(f"[crawl] 索引页抓取失败（{type(exc).__name__}: {exc}），"
                  f"章节/活动标题将退回页面内标题")

    if args.index or args.enrich or do_all or args.export_md:
        manifest = _load_manifest()
        if not manifest.get("pages"):
            print("[parse] 没有已缓存的页面，请先 --crawl")
            return 2
        index_meta = load_index_meta()
        units = parse_all(manifest, index_meta, dedupe=not args.no_dedupe)
        total_scenes = sum(len(u.scenes) for u in units)
        total_lines = sum(len(s.lines) for u in units for s in u.scenes)
        print(f"[parse] {len(units)} units, {total_scenes} scenes, {total_lines} lines")

        zh = _load_zh()
        if args.enrich or do_all:
            try:
                zh = enrich(units, model=args.model, concurrency=args.concurrency,
                            force=args.force_enrich, limit=args.enrich_limit)
            except Exception as exc:  # noqa: BLE001
                print(f"[enrich] 跳过（{type(exc).__name__}: {exc}）")

        if args.index or do_all:
            stats = build_index(units, zh, args.db)
            print("[index] " + ", ".join(f"{k}={v}" for k, v in stats.items()))
            _write_build_report(units, zh, stats)

        if args.export_md:
            export_markdown(units, zh, args.export_md)
            print(f"[export] {args.export_md}")

    if args.stats:
        print_stats(args.db)
    return rc


def _write_build_report(units: Sequence[Unit], zh: dict, stats: dict) -> None:
    """Persist a small JSON report next to the DB (used by /status + docs)."""
    report = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "stats": stats,
        "zh_coverage": round(len(zh) / max(1, len(units)), 3),
        "works": {},
        "sample_units": [u.page_title for u in units[:5]],
    }
    for u in units:
        w = u.meta.get("work", "main")
        report["works"][w] = report["works"].get(w, 0) + 1
    path = os.path.join(DATA_DIR, "build_report.json")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
