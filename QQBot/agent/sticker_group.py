"""Group-local sticker submissions and explicit member opt-in."""
import asyncio
import hashlib
import logging
import re
import sqlite3
import tempfile
from dataclasses import replace
from pathlib import Path
from subprocess import TimeoutExpired

from lib.sticker_usage import infer_uses, parse_uses
from . import image_context, media_ledger, sticker_intake
from .context import _current_user_workspace
from .group_state import scope_for

log = logging.getLogger(__name__)
_jobs = set()
_MAX_JOBS = 4
_DESCRIPTION_MARKER = re.compile(r"(?<!\S)描述\s*[:：]")


async def _collect(view, store, state, *, uses=(), description="", automatic=False):
    scope = scope_for(view)
    owner = sticker_intake.owner_key(scope, view.user_id)
    config = state["config"]
    if config["status"] != "active" or not config["features"].get("image", True):
        raise ValueError("当前群未开启图片功能")
    if (view.reply_message_id or view.has_image_quote) and view.reply_author_id != str(view.user_id):
        raise ValueError("请引用你自己发的表情包，或随命令附上一张你有权分享的图")
    if not automatic and not media_ledger.attempt("sticker:" + scope):
        raise ValueError("本群今日图片投稿次数已达上限")
    # Do not retain the original download in the ordinary conversation workspace.
    workspace = store.root / "workspaces" / hashlib.sha256(scope.encode()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="sticker-", dir=media_ledger.root()) as scratch:
        token = _current_user_workspace.set(scratch)
        try:
            incoming = replace(view, attachments=list(view.attachments))
            attachments = image_context.images_only(await incoming.load_attachments())
        finally:
            _current_user_workspace.reset(token)
        uploaded = [att for att in attachments if not att.quoted]
        if not uploaded and str(getattr(view, "reply_author_id", "")) != str(view.user_id):
            raise ValueError("请引用你自己发的表情包，或随命令附上一张你有权分享的图")
        selected = uploaded or attachments
        if len(selected) != 1:
            raise ValueError("请一次投稿一张表情包")
        local = image_context.local_images(selected, scratch) or image_context.local_images(selected, str(workspace))
        if not local:
            raise ValueError("这张图暂时无法读取，请重新发图")
        with Path(local[0].path).open("rb") as image:
            raw = image.read(sticker_intake.CANDIDATE_BYTES + 1)
        candidate = await asyncio.to_thread(
            sticker_intake.make_candidate, raw, persona=config["persona"], scope=scope,
            owner=owner, origin="group", description=description,
            uses=list(uses) or (infer_uses(view.text) if automatic else []),
            author="本群投稿者",
            license="投稿者同意此机器人在本群复用；审核员确认可用范围",
        )
    current = store.group(scope)
    if (current["generation"] != state["generation"] or current["config"]["status"] != "active"
            or not current["config"]["features"].get("image", True)):
        raise ValueError("群设置已变化，请重新投稿")
    return await asyncio.to_thread(sticker_intake.submit, candidate, automatic=automatic)


async def submit(view, store, state, arguments):
    marker = _DESCRIPTION_MARKER.search(arguments)
    description = ""
    if marker:
        description = sticker_intake.validate_description(arguments[marker.end():].strip())
        arguments = arguments[:marker.start()]
    uses = parse_uses(arguments)
    try:
        ident, fresh = await _collect(view, store, state, uses=uses, description=description)
    except (OSError, TimeoutExpired) as error:
        raise ValueError("图片处理失败，请稍后重试") from error
    if not fresh:
        return "这张或相似的图已经收过。" + (f"候选编号 {ident}" if ident else "")
    return f"已收下 {ident}，审核通过后供当前人格在本群使用。撤回可发 /撤回表情 {ident}。"


def maybe_collect(view, store, state):
    """Opted-in OneBot sticker uploads only. Ordinary photos stay outside intake."""
    if not getattr(view, "is_sticker", False) or len(_jobs) >= _MAX_JOBS:
        return
    if not state["config"]["features"].get("image", True):
        return
    scope = scope_for(view)
    owner = sticker_intake.owner_key(scope, view.user_id)
    try:
        if not sticker_intake.automatic_consent(scope, owner) or not media_ledger.attempt("sticker:" + scope):
            return
    except (OSError, sqlite3.Error):
        log.warning("Sticker submission settings unavailable")
        return

    async def save():
        try:
            await _collect(view, store, state, automatic=True)
        except (OSError, ValueError, TimeoutExpired):
            return
        except Exception:
            log.exception("Automatic sticker submission failed")

    task = asyncio.create_task(save())
    _jobs.add(task)
    task.add_done_callback(_jobs.discard)
