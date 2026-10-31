"""
OneBot V11 (NapCat) inbound adapter — stage 2 of the QQ-official plan.

Converts OneBot events into agent.inbound.InboundMessage views:

  - build_view(bot, event)         — cheap sync conversion; computes
    mentioned/text/reply_context up front, attachments stay lazy
  - OneBotAttachmentResolver       — image/file/voice segment walk +
    NapCat download calls, invoked by view.load_attachments()
  - build_reply_context(event)     — reply-segment / [CQ:reply] parsing
    (moved verbatim from agent/turn.py::_build_reply_context); reads
    agent.turn._recent_files so quoting an earlier upload still injects
    its file path / download-failure note

The download helpers and reply parser below were moved VERBATIM out of
agent/turn.py (pure stage-2 sink; no logic changes).

Stage 3 adds the outbound side: OneBotSender implements the
agent.outbound.Sender protocol on top of a NoneBot matcher
(matcher.send() resolves the current bot/event from NoneBot's matcher
context at call time — the same mechanism turn.py relied on before).
OneBot ActionFailed is translated to the platform-agnostic SendFailed
so the kernel's retry loop no longer imports OneBot. This is now the
ONLY module (besides the entry plugin's broadcast task) that constructs
MessageSegment objects for turn replies.
"""
import json
import os
import re
import uuid
from pathlib import Path

import httpx

from nonebot.adapters.onebot.v11 import (
    ActionFailed, Bot, GroupMessageEvent, MessageEvent, MessageSegment,
)

from agent.context import _current_user_workspace
from agent.inbound import Attachment, InboundMessage
from agent.outbound import OutAudio, OutImage, SendFailed
from agent.turn import _AGENT_DIR, _recent_files
from tools.builtin_tools import _ensure_workspace_dirs


# ── Outbound sender (stage 3) ──────────────────────────────────────

class OneBotSender:
    """agent.outbound.Sender for OneBot V11, wrapping one NoneBot matcher.

    The plugin registers one sender per matcher (default/continuous/poke)
    into turn.py's sender slots. Send semantics:
      - send_text:     matcher.send(str)            — as before
      - send_image:    matcher.send(MessageSegment.image(Path|url))
      - send_markdown: plain text (OneBot/QQ has no native markdown)
    ActionFailed → SendFailed (retryable in _safe_send); any other
    exception propagates and is treated as non-retryable there, exactly
    like the pre-stage-3 _safe_send did.
    """

    platform = "onebot"
    # OneBot has no per-turn reply quota; part of the Sender contract.
    max_messages_per_turn = 100
    # NapCat/QQ has no native markdown rendering; the render layer must
    # never emit markdown on this route (buttons-design §6.10.7).
    supports_markdown = False
    # No inline keyboards on the OneBot route either — the render layer
    # gates on this, so send_keyboard below is a pure safety net.
    supports_keyboard = False
    # Streaming output is an official-route c2c capability only
    # (stream_messages API); OneBot always answers buffered.
    supports_stream = False

    def __init__(self, matcher):
        self._matcher = matcher

    async def open_stream(self, first_text: str = ""):
        """OneBot never streams — the kernel falls back to buffered sends."""
        return None

    async def send_text(self, text: str) -> None:
        try:
            await self._matcher.send(text)
        except ActionFailed as e:
            raise SendFailed(str(e), info=getattr(e, "info", None)) from e

    async def send_image(self, img: OutImage) -> None:
        if img.path:
            seg = MessageSegment.image(Path(img.path))
        elif img.url:
            seg = MessageSegment.image(img.url)
        else:
            raise ValueError(f"OutImage has neither path nor url (label={img.label!r})")
        try:
            await self._matcher.send(seg)
        except ActionFailed as e:
            raise SendFailed(str(e), info=getattr(e, "info", None)) from e

    async def send_audio(self, audio: OutAudio) -> None:
        # NapCat transcodes common formats (mp3/wav) to silk itself.
        if audio.path:
            seg = MessageSegment.record(Path(audio.path))
        elif audio.url:
            seg = MessageSegment.record(audio.url)
        else:
            raise ValueError(f"OutAudio has neither path nor url (label={audio.label!r})")
        try:
            await self._matcher.send(seg)
        except ActionFailed as e:
            raise SendFailed(str(e), info=getattr(e, "info", None)) from e

    async def send_markdown(self, text: str) -> None:
        await self.send_text(text)

    async def send_keyboard(self, msg) -> None:
        """Safety-net degradation (supports_keyboard=False gates this).

        The render layer never emits OutKeyboard on the OneBot route;
        if one leaks through anyway (a tool preset _turn_keyboard on a
        shared code path), render body + command list as plain text so
        the content is never lost — with markdown markers stripped,
        because this channel renders them verbatim.
        """
        from lib.button_spec import to_text

        from agent.turn import _clean_md_markers
        body = _clean_md_markers(msg.markdown or "").strip()
        commands = to_text(msg.rows)
        await self.send_text(f"{body}\n\n{commands}" if commands else body)

    async def send_progress(self, text: str, *, immediate: bool = False) -> None:
        # OneBot has no per-turn reply quota — progress lines go out
        # verbatim, exactly like pre-stage-4. The immediate flag only
        # matters on the quota-limited official route; ignored here.
        await self.send_text(text)


# ── Reply/quote context (OneBot-specific; entry layer computes it) ──
def build_reply_context(event: MessageEvent) -> str:
    """Extract reply/quote context from a message's reply segment.

    Returns a string for injection into the augmented message, or "" if
    there is no reply segment.
    """
    # ── Diagnostic: log all message segments ───────────────────────
    import sys
    seg_types = [seg.type for seg in event.message]
    print(f"[REPLY_DIAG] msg_id={event.message_id}  seg_types={seg_types}", file=sys.stderr, flush=True)

    for seg in event.message:
        if seg.type != "reply":
            continue
        reply_id = str(seg.data.get("id", ""))
        reply_text = seg.data.get("text", "") or seg.data.get("message", "") or ""

        parts = []

        # ── Diagnostic: log reply lookup ──────────────────────────
        print(f"[REPLY_DIAG] reply_id={reply_id!r}  recent_keys={list(_recent_files.keys())!r}  match={reply_id in _recent_files}", file=sys.stderr, flush=True)

        if reply_text:
            parts.append(f"[用户引用了消息: \"{reply_text}\"]")
        elif reply_id:
            parts.append(f"[用户回复了消息 {reply_id}]")

        # Check if the replied-to message had files
        if reply_id and reply_id in _recent_files:
            files = _recent_files[reply_id]
            for f in files:
                if f.get("error"):
                    parts.append(
                        f"[用户引用了文件 \"{f['name']}\"，"
                        f"但该文件之前下载失败（{f['error']}）。"
                        f"请告知用户文件无法读取，建议重新上传。]"
                    )
                else:
                    parts.append(
                        f"[用户引用了文件 \"{f['name']}\"。"
                        f"请根据用户意图选择合适的工具处理此文件："
                        f"战斗截图测速/行动值分析用 parse_battle_screenshots，"
                        f"一般图片或文档用 read_file，"
                        f"忽略对话历史中关于其他文件的提及。"
                        f"文件路径: {f['path']}]"
                    )

        return "\n".join(parts) if parts else ""

    # ── Fallback: extract reply id from multiple sources ─────────
    import re as _re

    # 1) Try event.reply attribute (OneBot V11 field)
    event_reply = getattr(event, 'reply', None)
    if event_reply is not None:
        # event.reply can be a dict {'message_id': ..., ...} or a Reply object
        if isinstance(event_reply, dict):
            reply_id = str(event_reply.get('message_id', '') or event_reply.get('id', ''))
        else:
            reply_id = str(getattr(event_reply, 'message_id', '') or getattr(event_reply, 'id', ''))
        if reply_id:
            print(f"[REPLY_DIAG] event.reply: reply_id={reply_id!r}  recent_keys={list(_recent_files.keys())!r}  match={reply_id in _recent_files}", file=sys.stderr, flush=True)
            if reply_id in _recent_files:
                files = _recent_files[reply_id]
                parts = []
                for f in files:
                    parts.append(
                        f"[用户引用了文件 \"{f['name']}\"。"
                        f"请根据用户意图选择合适的工具处理此文件："
                        f"战斗截图测速/行动值分析用 parse_battle_screenshots，"
                        f"一般图片或文档用 read_file，"
                        f"忽略对话历史中关于其他文件的提及。"
                        f"文件路径: {f['path']}]"
                    )
                return "\n".join(parts) if parts else ""
            else:
                return f"[用户回复了消息 {reply_id}]"

    # 2) Parse [CQ:reply,id=XXX] or [reply:id=XXX] from raw message
    raw_msg = getattr(event, 'raw_message', '') or str(event.message)
    m = _re.search(r'\[(?:CQ:)?reply[,:]id=(\d+)\]', raw_msg)
    if m:
        reply_id = m.group(1)
        print(f"[REPLY_DIAG] regex fallback: reply_id={reply_id!r}  recent_keys={list(_recent_files.keys())!r}  match={reply_id in _recent_files}", file=sys.stderr, flush=True)
        if reply_id in _recent_files:
            files = _recent_files[reply_id]
            parts = []
            for f in files:
                parts.append(
                    f"[用户引用了文件 \"{f['name']}\"。"
                    f"请根据用户意图选择合适的工具处理此文件："
                    f"战斗截图测速/行动值分析用 parse_battle_screenshots，"
                    f"一般图片或文档用 read_file，"
                    f"忽略对话历史中关于其他文件的提及。"
                    f"文件路径: {f['path']}]"
                )
            return "\n".join(parts) if parts else ""
        else:
            return f"[用户回复了消息 {reply_id}]"

    return ""

def quoted_file_paths(event: MessageEvent) -> list[str]:
    """Local paths of the files carried by the message this event quotes.

    OneBot never inlines quoted content into the reply segment (it only
    carries the quoted message id), so quoted attachments are resolved
    through turn.py's recent-file registry — populated whenever this bot
    downloaded that file. Used by /设形象 to accept "引用一张图 + 发命令".
    """
    reply_id = ""
    for seg in event.message:
        if seg.type == "reply":
            reply_id = str(seg.data.get("id", ""))
            break
    if not reply_id:
        reply = getattr(event, "reply", None)
        if isinstance(reply, dict):
            reply_id = str(reply.get("message_id") or reply.get("id") or "")
        elif reply is not None:
            reply_id = str(
                getattr(reply, "message_id", "") or getattr(reply, "id", "") or "")
    if not reply_id:
        return []
    return [f["path"] for f in _recent_files.get(reply_id, []) if f.get("path")]


# ── View construction ──────────────────────────────────────────────

def _is_mentioned(event: MessageEvent) -> bool:
    """@-detection for group messages; private messages always count.

    Manual segment check first (more reliable than to_me() on NapCat),
    event.is_tome() as backup — same union the plugin guards use.
    """
    if not isinstance(event, GroupMessageEvent):
        return True
    bot_qq = str(event.self_id)
    is_at_bot = any(
        seg.type == "at" and seg.data.get("qq") == bot_qq
        for seg in event.message
    )
    return is_at_bot or event.is_tome()


class OneBotAttachmentResolver:
    """Walks message segments and downloads image/file/voice to uploads.

    Segment order is preserved; failures are returned as Attachment
    objects with error set (the turn kernel records them in
    _recent_files and the prompt exactly like before).
    """

    def __init__(self, bot: Bot, event: MessageEvent):
        self._bot = bot
        self._event = event

    async def resolve(self, view: InboundMessage) -> list[Attachment]:
        out: list[Attachment] = []
        message_id = str(self._event.message_id)
        for seg in self._event.message:
            if seg.type == "image":
                url = seg.data.get("url", "")
                file_id = seg.data.get("file", "")
                name = f"image-{file_id}"
                saved_path, error = await _download_and_save_file(
                    url, name, bot=self._bot, file_id=file_id)
                out.append(Attachment(kind="image", path=saved_path or "",
                                      name=name, error=error or ""))
            elif seg.type == "file":
                url = seg.data.get("url", "")
                name = (seg.data.get("name") or seg.data.get("filename")
                        or seg.data.get("file_name") or seg.data.get("title")
                        or seg.data.get("file") or "file")
                file_id = seg.data.get("file_id", "")
                saved_path, error = await _download_and_save_file(
                    url, name, bot=self._bot, file_id=file_id)
                out.append(Attachment(kind="file", path=saved_path or "",
                                      name=name, error=error or ""))
            elif seg.type == "record":
                saved_path, error = await _download_voice(
                    self._bot, seg.data, message_id)
                out.append(Attachment(kind="voice", path=saved_path or "",
                                      name="语音消息", error=error or ""))
        return out


def build_view(bot: Bot, event: MessageEvent) -> InboundMessage:
    """OneBot event -> platform-agnostic view (attachments stay lazy)."""
    is_group = isinstance(event, GroupMessageEvent)
    return InboundMessage(
        user_id=str(event.user_id),
        is_group=is_group,
        group_id=str(event.group_id) if is_group else "",
        text=event.get_plaintext().strip(),
        message_id=str(event.message_id),
        self_id=str(event.self_id),
        mentioned=_is_mentioned(event),
        reply_context=build_reply_context(event),
        resolver=OneBotAttachmentResolver(bot, event),
        platform="onebot",
        raw=event,
    )


# ── File Download Helpers (moved verbatim from agent/turn.py) ────

def _get_uploads_dir() -> str:
    """Get the uploads directory at runtime, respecting user workspace contextvar.

    Unlike the module-level WORKSPACE_UPLOADS constant (frozen at import time),
    this checks _current_user_workspace on every call so files are saved to the
    correct per-user workspace during special sessions.
    """
    user_ws = _current_user_workspace.get()
    if user_ws:
        return os.path.join(user_ws, "uploads")
    # Fallback: shared workspace default
    return os.path.join(_AGENT_DIR, "data", "workspace", "uploads")


async def _download_voice(bot, seg_data: dict, message_id: str = "", max_size_mb: int = 50) -> tuple:
    """Download a QQ voice message.

    Args:
        bot: NoneBot2 OneBot V11 Bot instance.
        seg_data: The ``data`` dict from the record message segment.
        message_id: The QQ message ID (some NapCat versions require this).
        max_size_mb: Maximum file size in MB.

    Returns:
        (saved_path, error_message) — one is None, the other is not.
    """
    file_id = seg_data.get("file", "")
    if not file_id:
        return None, "语音文件 ID 为空，无法下载。"

    max_size_bytes = max_size_mb * 1024 * 1024
    _ensure_workspace_dirs()
    uploads_dir = _get_uploads_dir()
    os.makedirs(uploads_dir, exist_ok=True)
    save_path = os.path.join(
        uploads_dir,
        f"{uuid.uuid4().hex[:8]}-{file_id}"
    )

    diag = []  # Collect diagnostics for the final error message

    # ── Strategy 1: read local file directly ──────────────────────
    for field in ("path", "url"):
        local = seg_data.get(field, "")
        if not local:
            diag.append(f"[{field}] 字段为空")
            continue
        try:
            with open(local, "rb") as src:
                data = src.read()
            if len(data) == 0:
                diag.append(f"[{field}] 文件为空: {local}")
                continue
            if len(data) > max_size_bytes:
                return None, f"语音文件过大 ({len(data) / 1024 / 1024:.1f} MB)，无法处理。"
            with open(save_path, "wb") as dst:
                dst.write(data)
            return save_path, None
        except FileNotFoundError:
            diag.append(f"[{field}] 文件不存在: {local[:120]}")
        except PermissionError:
            diag.append(f"[{field}] 无权限读取: {local[:120]}")
        except (IOError, OSError) as e:
            diag.append(f"[{field}] IO错误: {e} — {local[:120]}")

    # ── Strategy 2: OneBot get_record API ──────────────────────────
    # Build base params: file / file_id + optional message_id
    base_params = {}
    if message_id:
        base_params["message_id"] = message_id

    # NapCat may use non-standard action/param names — try combinations
    for action in ("get_record", "get_file", "getRecord", "download_file"):
        for param_name in ("file", "file_id"):
            params = {param_name: file_id, **base_params}
            try:
                result = await bot.call_api(action, **params)
            except Exception as e:
                diag.append(f"[API {action} {param_name}=] 异常: {e}")
                continue

            if isinstance(result, dict):
                url_or_data = result.get("file", "") or result.get("path", "") or ""
            elif isinstance(result, str):
                url_or_data = result
            else:
                diag.append(f"[API {action} {param_name}=] 返回类型异常: {type(result).__name__}")
                continue

            if not url_or_data:
                diag.append(f"[API {action} {param_name}=] 无 file/path 字段: {json.dumps(result, ensure_ascii=False)[:200]}")
                continue

            try:
                if url_or_data.startswith("base64://"):
                    import base64
                    data = base64.b64decode(url_or_data[len("base64://"):])
                elif url_or_data.startswith(("http://", "https://")):
                    async with httpx.AsyncClient() as client:
                        response = await client.get(url_or_data, timeout=120.0, follow_redirects=True)
                        response.raise_for_status()
                        data = response.content
                elif os.path.isfile(url_or_data):
                    with open(url_or_data, "rb") as src:
                        data = src.read()
                else:
                    diag.append(f"[API {action} {param_name}=] 无法识别返回格式: {url_or_data[:120]}")
                    continue

                if len(data) > max_size_bytes:
                    return None, f"语音文件过大 ({len(data) / 1024 / 1024:.1f} MB)，无法处理。"

                with open(save_path, "wb") as dst:
                    dst.write(data)
                return save_path, None

            except (httpx.HTTPStatusError, httpx.TimeoutException) as e:
                diag.append(f"[API {action} {param_name}=] 下载失败: {e}")
                continue
            except Exception as e:
                diag.append(f"[API {action} {param_name}=] 处理返回值出错: {e}")
                continue

    # ── Strategy 3: NapCat HTTP API ────────────────────────────────
    napcat_base = os.environ.get("NAPCAT_HTTP_BASE", "http://127.0.0.1:6099")
    http_endpoints = (
        ("POST", "/api/get_record"),
        ("POST", "/api/getRecord"),
        ("POST", "/api/record"),
        ("POST", "/api/onebot/get_record"),
        ("GET",  "/api/get_record"),
    )
    for method, endpoint in http_endpoints:
        try:
            async with httpx.AsyncClient() as client:
                if method == "GET":
                    resp = await client.get(
                        f"{napcat_base.rstrip('/')}{endpoint}",
                        params={"file": file_id},
                        timeout=10.0,
                    )
                else:
                    resp = await client.post(
                        f"{napcat_base.rstrip('/')}{endpoint}",
                        json={"file": file_id},
                        timeout=10.0,
                    )
                if resp.status_code != 200:
                    diag.append(f"[HTTP {method} {endpoint}] HTTP {resp.status_code}")
                    continue

                # Try to extract file data from various response shapes
                body = resp.text
                result = None
                try:
                    result = resp.json()
                except Exception:
                    pass

                file_data = ""
                if isinstance(result, dict):
                    file_data = (
                        result.get("file", "") or
                        result.get("data", {}).get("file", "") if isinstance(result.get("data"), dict) else "" or
                        result.get("result", {}).get("file", "") if isinstance(result.get("result"), dict) else "" or
                        str(result)
                    )
                elif isinstance(result, str):
                    file_data = result

                if file_data and file_data.startswith("base64://"):
                    import base64
                    data = base64.b64decode(file_data[len("base64://"):])
                elif file_data and os.path.isfile(str(file_data)):
                    with open(str(file_data), "rb") as src:
                        data = src.read()
                elif file_data and len(file_data) > 100:
                    # Might be raw base64 (without the base64:// prefix)
                    try:
                        import base64
                        data = base64.b64decode(file_data)
                    except Exception:
                        # Also try raw binary response
                        if resp.content and len(resp.content) > 10:
                            data = resp.content
                        else:
                            diag.append(f"[HTTP {method} {endpoint}] 无法解析响应: {body[:120]}")
                            continue
                elif resp.content and len(resp.content) > 10:
                    # Response might be raw binary (the audio file itself)
                    data = resp.content
                else:
                    diag.append(f"[HTTP {method} {endpoint}] 无法解析响应: {body[:120]}")
                    continue

                if len(data) <= max_size_bytes:
                    with open(save_path, "wb") as dst:
                        dst.write(data)
                    return save_path, None

        except Exception as e:
            diag.append(f"[HTTP {method} {endpoint}] 异常: {e}")
            continue

    return None, f"语音下载失败。诊断: {'; '.join(diag)}"


async def _download_and_save_file(
    url: str, filename: str, max_size_mb: int = 50,
    bot=None, file_id: str = "",
) -> tuple:
    """Download a file from QQ and save to workspace uploads.

    When the direct ``url`` is available it is used first (standard for
    group-chat files).  When ``url`` is empty (common in private chats) the
    function falls back to the OneBot ``get_file`` API if *bot* and *file_id*
    are provided.

    Args:
        url: Download URL from the message segment (may be empty).
        filename: Original filename (used for extension detection).
        max_size_mb: Maximum file size in MB.
        bot: Optional OneBot V11 Bot instance for API fallback.
        file_id: File ID from the message segment for API fallback.

    Returns:
        (saved_path, error_message) — one is None, the other is not.
    """
    # ── Pick uploads dir at runtime (respects per-user workspace) ──
    _ensure_workspace_dirs()
    uploads_dir = _get_uploads_dir()
    os.makedirs(uploads_dir, exist_ok=True)

    # Generate safe filename: uuid8 prefix + sanitized original name
    ext = os.path.splitext(filename)[1] or ""
    safe_name = re.sub(r'[^\w\-_.]', '_', os.path.splitext(filename)[0])
    if not safe_name:
        safe_name = "file"
    unique_name = f"{uuid.uuid4().hex[:8]}-{safe_name}{ext}"
    save_path = os.path.join(uploads_dir, unique_name)

    max_size_bytes = max_size_mb * 1024 * 1024

    # ── Helper: detect file type from magic bytes ─────────────────
    def _magic_ext(data: bytes) -> str:
        if data[:4] == b'%PDF':
            return ".pdf"
        if data[:4] == b'\x89PNG':
            return ".png"
        if data[:3] == b'\xff\xd8\xff':
            return ".jpg"
        if data[:6] in (b'GIF87a', b'GIF89a'):
            return ".gif"
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            return ".webp"
        if data[:2] == b'BM':
            return ".bmp"
        return ""

    # ── Strategy 1: direct URL download ──────────────────────────
    if url:
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            }
            async with httpx.AsyncClient(headers=headers) as client:
                response = await client.get(url, timeout=120.0, follow_redirects=True)
                response.raise_for_status()

                content_length = len(response.content)
                if content_length > max_size_bytes:
                    return None, (
                        f"文件过大 ({content_length / 1024 / 1024:.1f} MB)，"
                        f"超过限制 ({max_size_mb} MB)。请压缩后重试。"
                    )

                # Determine actual extension: Content-Type first, then magic bytes
                content_type = response.headers.get("content-type", "")
                ct_map = {
                    "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                    "image/webp": ".webp", "image/bmp": ".bmp",
                    "application/pdf": ".pdf",
                }
                for ct_prefix, ct_ext in ct_map.items():
                    if ct_prefix in content_type and not save_path.endswith(ct_ext):
                        save_path = save_path + ct_ext
                        break
                else:
                    # Content-Type didn't match — try magic bytes
                    magic_ext = _magic_ext(response.content)
                    if magic_ext and not save_path.endswith(magic_ext):
                        save_path = save_path + magic_ext

                with open(save_path, "wb") as f:
                    f.write(response.content)

                return save_path, None

        except httpx.HTTPStatusError as e:
            # QQ CDN URLs may return 400/403 with default headers — fall back to API
            if e.response.status_code in (400, 403) and bot is not None and file_id:
                pass  # Fall through to Strategy 2 below
            else:
                return None, f"下载失败 (HTTP {e.response.status_code}): {e.response.reason_phrase}"
        except httpx.TimeoutException:
            return None, "下载超时 (120秒)。文件可能过大或网络不稳定。"
        except httpx.RequestError as e:
            return None, f"网络请求失败: {e}"
        except IOError as e:
            return None, f"文件保存失败: {e}"
        except Exception as e:
            return None, f"下载文件时出现意外错误: {e}"

    # ── Strategy 2: OneBot get_file API fallback (private chats) ──
    if bot is not None and file_id:
        diag = []
        for action in ("get_file", "getFile", "download_file"):
            for param_name in ("file", "file_id"):
                try:
                    result = await bot.call_api(action, **{param_name: file_id})
                except Exception as e:
                    diag.append(f"[API {action} {param_name}=] 异常: {e}")
                    continue

                if isinstance(result, dict):
                    file_data = result.get("file", "") or result.get("path", "") or result.get("url", "") or ""
                elif isinstance(result, str):
                    file_data = result
                else:
                    diag.append(f"[API {action} {param_name}=] 返回类型异常: {type(result).__name__}")
                    continue

                if not file_data:
                    diag.append(f"[API {action} {param_name}=] 无 file/path/url 字段: {json.dumps(result, ensure_ascii=False)[:200]}")
                    continue

                try:
                    import base64
                    if file_data.startswith("base64://"):
                        data = base64.b64decode(file_data[len("base64://"):])
                    elif file_data.startswith(("http://", "https://")):
                        async with httpx.AsyncClient() as client:
                            resp = await client.get(file_data, timeout=120.0, follow_redirects=True)
                            resp.raise_for_status()
                            data = resp.content
                    elif os.path.isfile(file_data):
                        with open(file_data, "rb") as src:
                            data = src.read()
                    else:
                        diag.append(f"[API {action} {param_name}=] 无法识别返回格式: {file_data[:120]}")
                        continue

                    if len(data) > max_size_bytes:
                        return None, f"文件过大 ({len(data) / 1024 / 1024:.1f} MB)，超过限制 ({max_size_mb} MB)。"

                    with open(save_path, "wb") as f:
                        f.write(data)

                    # Fix extension from magic bytes if missing
                    magic_ext = _magic_ext(data)
                    if magic_ext and not save_path.endswith(magic_ext):
                        new_path = save_path + magic_ext
                        os.rename(save_path, new_path)
                        save_path = new_path

                    return save_path, None

                except Exception as e:
                    diag.append(f"[API {action} {param_name}=] 处理返回值出错: {e}")
                    continue

        return None, f"文件下载失败 (API fallback 已尝试 {len(diag)} 次)。诊断: {'; '.join(diag)}"

    if not url and not file_id:
        return None, "文件缺少下载地址和文件 ID，可能未成功上传或 QQ 客户端限制了文件传输。"
    if not url:
        return None, "文件下载地址为空（私聊文件可能需通过 OneBot API 下载，但缺少 bot 连接）。"
