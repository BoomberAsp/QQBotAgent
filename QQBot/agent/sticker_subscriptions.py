"""Periodically refresh administrator-configured sticker candidate sources."""
import asyncio
import hashlib
import json
import logging
import re
import time
from subprocess import TimeoutExpired
from urllib.parse import urljoin, urlsplit

from lib.media_download import Candidates, MediaError, download, read_source_page, validate_url
from lib.sticker_usage import parse_uses
from . import media_ledger as ledger, sticker_intake
from .group_resources import edit_registry, registry
from .media_tools import approved_url

log = logging.getLogger(__name__)
_INTERVAL = 6 * 3600
_MAX_SOURCES = 12
_MAX_IMAGES = 6
_VALID_IMAGES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def save_source(payload):
    if not isinstance(payload, dict):
        raise ValueError("订阅格式无效")
    persona = payload.get("persona", "")
    url = payload.get("url", "")
    if not isinstance(persona, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", persona):
        raise ValueError("请输入有效人格 ID")
    if not isinstance(url, str):
        raise ValueError("请输入 HTTPS 来源页")
    if type(payload.get("enabled", True)) is not bool:
        raise ValueError("订阅开关无效")
    tags = parse_uses(payload.get("uses", []))
    ident = hashlib.sha256(json.dumps([persona, url]).encode()).hexdigest()[:16]
    record = {"id": ident, "persona": persona, "url": url, "uses": tags, "enabled": payload.get("enabled", True)}
    for key, limit in (("author", 120), ("license", 160), ("image_prefix", 350)):
        value = payload.get(key, "")
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError("订阅字段无效或过长")
        record[key] = value.strip()
    with edit_registry() as (data, _):
        validate_url(url, set(data.get("page_hosts", [])))
        if record["image_prefix"]:
            validate_url(record["image_prefix"], set(data.get("image_hosts", [])))
        sources = data.setdefault("sticker_subscriptions", {})
        if ident not in sources and len(sources) >= _MAX_SOURCES:
            raise ValueError("订阅最多 12 个")
        sources[ident] = record
    return record


def remove_source(ident):
    with edit_registry() as (data, _):
        if not data.get("sticker_subscriptions", {}).pop(ident, None):
            raise ValueError("订阅不存在")
    return {"ok": True}


def sources():
    data, _ = registry()
    with ledger.db() as conn:
        sticker_intake.setup(conn)
        status = {ident: (seen, text) for ident, seen, text in conn.execute(
            "SELECT source, checked, outcome FROM sticker_subscriber_runs"
        )}
    return [{**record, "checked": status.get(ident, (0, ""))[0],
             "outcome": status.get(ident, (0, ""))[1]}
            for ident, record in data.get("sticker_subscriptions", {}).items()]


def _claim(ident, force):
    now = time.time()
    with ledger.db() as conn:
        sticker_intake.setup(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT checked, outcome FROM sticker_subscriber_runs WHERE source=?", (ident,)).fetchone()
        if row and ((row[1] == "同步中" and now - row[0] < 300) or
                    now - row[0] < (30 if force else _INTERVAL)):
            return 0
        conn.execute("INSERT OR REPLACE INTO sticker_subscriber_runs VALUES(?,?,'同步中')", (ident, now))
        return now


def _outcome(ident, claim, message):
    with ledger.db() as conn:
        sticker_intake.setup(conn)
        conn.execute("UPDATE sticker_subscriber_runs SET outcome=? WHERE source=? AND checked=?",
                     (message, ident, claim))


def _urls(raw, mime, page, accept):
    if mime == "application/json":
        value = json.loads(raw)
        if isinstance(value, dict):
            value = value.get("items", [])
        if not isinstance(value, list):
            raise ValueError("JSON 图集应提供 items 数组")
        candidates = []
        for item in value[:100]:
            url = item.get("image_url", "") if isinstance(item, dict) else item
            if isinstance(url, str):
                url = urljoin(page, url)
                if accept(url) and all(url != saved for saved, _ in candidates):
                    description = str(item.get("description") or item.get("title") or "") if isinstance(item, dict) else ""
                    candidates.append((url, description[:300]))
        return candidates[:_MAX_IMAGES]
    if mime not in {"text/html", "application/xhtml+xml"}:
        raise ValueError("支持静态 HTML 图集或 JSON 图集")
    parser = Candidates(page, allow_url=accept)
    parser.feed(raw.decode("utf-8", errors="replace"))
    return [(url, parser.descriptions.get(url, "")) for url in parser.urls[:_MAX_IMAGES]]


async def sync_source(ident, *, force=False):
    data, _ = registry()
    record = data.get("sticker_subscriptions", {}).get(ident)
    if not record or not record.get("enabled"):
        return {"ok": False, "message": "订阅已停用或不存在"}
    claim = await asyncio.to_thread(_claim, ident, force)
    if not claim:
        return {"ok": False, "message": "订阅正在同步或尚未到下次同步时间"}

    pages = set(data.get("page_hosts", []))
    hosts = set(data.get("image_hosts", []))

    def active():
        current, _ = registry()
        return (current.get("sticker_subscriptions", {}).get(ident) == record
                and set(current.get("page_hosts", [])) == pages and set(current.get("image_hosts", [])) == hosts)

    try:
        raw, mime, page = await read_source_page(record["url"], pages)
        prefix = record.get("image_prefix", "")
        prefix_host = urlsplit(prefix).hostname if prefix else None

        def accept(url):
            return bool(approved_url(url, hosts)) and (
                not prefix or url.startswith(prefix) and urlsplit(url).hostname == prefix_host
            )

        found = _urls(raw, mime, page, accept)
        imported = duplicates = failed = 0
        for url, description in found:
            if not active():
                break
            try:
                content, image_mime, _ = await download(url, hosts, sticker_intake.CANDIDATE_BYTES)
                if image_mime not in _VALID_IMAGES:
                    failed += 1
                    continue
                candidate = await asyncio.to_thread(
                    sticker_intake.make_candidate, content, persona=record["persona"], scope="",
                    origin="web", source_url=page, author=record.get("author", ""),
                    license=record.get("license", ""), uses=record.get("uses", []),
                    description=description, mime=image_mime,
                )
                if not active():
                    break
                _, fresh = await asyncio.to_thread(sticker_intake.submit, candidate)
                imported += int(fresh)
                duplicates += int(not fresh)
            except (OSError, ValueError, TimeoutError, TimeoutExpired):
                failed += 1
        message = f"发现 {len(found)} 张，新增 {imported} 张，重复 {duplicates} 张，失败 {failed} 张。"
        await asyncio.to_thread(_outcome, ident, claim, message)
        return {"ok": True, "message": message, "imported": imported}
    except (OSError, MediaError, ValueError, TimeoutError):
        message = "来源不可读取，请检查域名白名单、来源页或 robots 规则。"
        await asyncio.to_thread(_outcome, ident, claim, message)
        return {"ok": False, "message": message}


async def watch():
    while True:
        data, _ = registry()
        for ident in data.get("sticker_subscriptions", {}):
            try:
                await sync_source(ident)
            except Exception:
                log.exception("Sticker subscription failed")
        try:
            await asyncio.to_thread(sticker_intake.pending)
        except Exception:
            log.exception("Sticker intake maintenance failed")
        await asyncio.sleep(300)
