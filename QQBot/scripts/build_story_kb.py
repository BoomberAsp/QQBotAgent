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
)

# ── Paths & constants ──────────────────────────────────────────────

DATA_DIR = os.path.join(_QROOT, "data", "story_kb")
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
ZH_PROMPT_VERSION = 2
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

要求：
1. 全部使用简体中文。人名使用游戏中文译名：露比(Rubi)、团长(player)、伊娥丝(Eos)、挪亚(Noah)、
   阿帕西(Aponia)、璐西玛(Lucia)、伊卡弥勒(Esther 等三大方舟领袖)、萝莎莱(Rosalie) 等；不确定的人名保留英文。
2. 只做概括，不要逐句翻译台词。
3. scenes 数组必须恰好 {n} 个元素，i 依次为 1..{n}，与下面场景一一对应。

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


def _build_zh_body(unit: Unit) -> str:
    """Compact per-scene dialogue digest for the enrichment prompt."""
    parts: List[str] = []
    total = 0
    for i, scene in enumerate(unit.scenes, 1):
        head = f"### 场景 {i}｜{scene.title or '(无标题)'}"
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

    async def one(client, unit: Unit):
        async with sem:
            prompt = ZH_PROMPT.format(
                n=len(unit.scenes),
                title=unit.page_title,
                meta_line=_meta_line(unit.meta),
                body=_build_zh_body(unit),
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


def _zh_blob(title_zh: str, gist: str, keywords: Sequence[str],
             chars_zh: Sequence[str], unit_zh: str) -> str:
    """Bigram-indexed Chinese text for one chunk."""
    parts = [title_zh or "", gist or "", " ".join(keywords or []),
             " ".join(chars_zh or []), unit_zh or ""]
    raw = " 。".join(p for p in parts if p)
    return " ".join(cjk_bigrams(raw))


def build_index(units: Sequence[Unit], zh: dict, db_path: str,
                source_note: str = "arkrecodewiki.miraheze.org") -> dict:
    """Stage 4: write the SQLite database (atomically) and return stats."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    tmp_path = db_path + ".building"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    # Chinese name lookup: char_id → 中文名 (for the Chinese index).
    id_to_zh = _char_id_to_zh()

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
            acct_ch_title_zh = chapter_zh_best.get(ch_key, "") or str(
                zinfo.get("chapter_title_zh", "")).strip()
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
                    zinfo.get("unit_summary", "") or "",
                    "、".join(zinfo.get("unit_keywords") or []),
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
                title_zh = (sz.get("title_zh") or "") if isinstance(sz, dict) else ""
                gist = (sz.get("gist") or "") if isinstance(sz, dict) else ""
                kw = (sz.get("keywords") or []) if isinstance(sz, dict) else []
                char_ids = scene.char_ids
                chars_zh = [id_to_zh.get(c, "") for c in char_ids]
                chars_zh = [c for c in chars_zh if c]

                cur = conn.execute(
                    "INSERT INTO scene (unit_id, ord, scene_title, scene_title_zh,"
                    " n_lines, char_ids, speakers, zh_gist, zh_kw, content_hash)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        unit_id, scene.ord, scene.title, title_zh,
                        len(scene.lines), ",".join(char_ids),
                        ",".join(scene.speakers), gist, "、".join(kw),
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
    ap.add_argument("--export-md", metavar="PATH", help="dump parsed units to markdown")
    ap.add_argument("--limit", type=int, default=0, help="cap pages (smoke test)")
    ap.add_argument("--enrich-limit", type=int, default=0, help="cap enrichment calls")
    ap.add_argument("--refetch", action="store_true", help="ignore the raw cache")
    ap.add_argument("--no-dedupe", action="store_true", help="keep duplicate scenes")
    ap.add_argument("--model", default=DEFAULT_MODEL, help="LLM model for enrichment")
    ap.add_argument("--concurrency", type=int, default=3, help="enrichment concurrency")
    ap.add_argument("--force-enrich", action="store_true", help="re-enrich cached units")
    ap.add_argument("--db", default=DB_PATH, help="output database path")
    args = ap.parse_args(argv)

    do_all = args.all or not any((args.crawl, args.index, args.enrich, args.stats,
                                  args.export_md))
    rc = 0

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
