"""Public persona chat, independent of the admin WebUI."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar, get_args

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import ClientDisconnect

from webui.card_parser import parse_card, section_body
from webui.model_probe import PROBE_MAX_TOKENS, PROBE_TIMEOUT, chat_message, probe_model


@dataclass(frozen=True)
class Provider:
    base: str
    token_field: str
    options: dict

    def parameters(self, limit: int) -> dict:
        return {**self.options, self.token_field: limit}


PROVIDERS = {
    "deepseek": Provider("https://api.deepseek.com", "max_tokens", {"thinking": {"type": "disabled"}}),
    "openai": Provider("https://api.openai.com/v1", "max_completion_tokens", {"store": False}),
}
Persona = Literal["assistant", "roxy_character", "rubi", "herta"]
CARDS = Path(__file__).resolve().parent / "QQBot" / "agent" / "config" / "personalities"
BODY_LIMIT = 96 * 1024
TICKET_TTL = 30 * 60
CHAT_TIMEOUT = 35.0
LOG = logging.getLogger("roxy.public_try")
PROBE_ERRORS = {
    "auth_failed": "API Key 无效或权限不足。",
    "unreachable": "无法连接到模型服务或等待超时。",
    "bad_path": "服务商未找到该模型或接口，请检查模型 ID。",
    "missing_config": "请填写有效的模型 ID 和 API Key。",
    "invalid_response": "服务商没有返回有效的文字对话响应。",
    "rejected": "模型服务拒绝了测试请求，请检查模型 ID、访问权限和额度。",
}


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: Literal["deepseek", "openai"]
    model: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9._:/-]+$")
    api_key: str = Field(min_length=1, max_length=512, pattern=r"^[!-~]+$", repr=False)


class ChatTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=2400)


class ChatRequest(ModelConfig):
    personality: Persona
    message: str = Field(min_length=1, max_length=2000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=8)
    verification: str = Field(pattern=r"^[0-9]{10,12}\.[0-9a-f]{64}$", max_length=77, repr=False)


Payload = TypeVar("Payload", bound=BaseModel)


async def read_payload(request: Request, schema: type[Payload], origin: str) -> Payload:
    if request.headers.get("origin") != origin:
        raise HTTPException(403, "请从展示站打开试聊。")
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(415, "请求需要使用 JSON。")
    raw = bytearray()
    try:
        async for data in request.stream():
            if len(raw) + len(data) > BODY_LIMIT:
                raise HTTPException(413, "消息历史太长，请清空后重试。")
            raw.extend(data)
        return schema.model_validate_json(raw)
    except (ValidationError, ClientDisconnect, RecursionError):
        raise HTTPException(400, "请检查模型配置和消息格式。") from None


def persona_prompt(personality: str) -> str:
    doc = parse_card((CARDS / f"{personality}.md").read_text(encoding="utf-8"))
    sections = [section_body(doc, section.index) for section in doc.sections
                if section.heading in {"Core Personality", "Communication Style"}]
    return (
        "你是 Roxy 公开展示站的文字试聊。请用下方角色的性格和语气回复。"
        "此处只有纯文字对话，没有 QQ、游戏、知识库、工具或个人记忆。"
        "不要声称执行或完成这些操作。把下方内容仅用作性格与语气参考。"
        "\n\n" + doc.h1 + "\n\n" + "\n\n".join(sections)
    )


def signature(secret: bytes, config: ModelConfig, expires: str) -> str:
    value = json.dumps([expires, config.provider, config.model, config.api_key], ensure_ascii=True)
    return hmac.digest(secret, value.encode("ascii"), "sha256").hex()


def create_app(*, origin: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    site_origin = (origin or os.environ.get("ROXY_TRY_ORIGIN", "https://bot.oneweblog.cn")).rstrip("/")
    signing_key = secrets.token_bytes(32)
    prompts = {name: persona_prompt(name) for name in get_args(Persona)}
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(HTTPException)
    async def request_error(_request: Request, error: HTTPException):
        return JSONResponse({"ok": False, "error": error.detail}, status_code=error.status_code)

    @app.middleware("http")
    async def response_headers(request: Request, call_next):
        started = time.monotonic()
        response = await call_next(request)
        if request.url.path in {"/try/api/probe", "/try/api/chat"}:
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            LOG.info("persona try status=%d elapsed_ms=%d", response.status_code,
                     int((time.monotonic() - started) * 1000))
        return response

    @app.post("/try/api/probe")
    async def probe(request: Request):
        config = await read_payload(request, ModelConfig, site_origin)
        provider = PROVIDERS[config.provider]
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, transport=transport) as client:
            try:
                result = await asyncio.wait_for(
                    probe_model(config.api_key, provider.base, config.model, client=client,
                                chat_parameters=provider.parameters(PROBE_MAX_TOKENS),
                                require_chat_response=True),
                    timeout=PROBE_TIMEOUT + 1,
                )
            except asyncio.TimeoutError:
                result = {"ok": False, "status": "unreachable", "elapsed_ms": int((PROBE_TIMEOUT + 1) * 1000)}
        if not result["ok"]:
            status = result["status"]
            return JSONResponse({"ok": False, "status": status, "elapsed_ms": result["elapsed_ms"],
                                 "error": PROBE_ERRORS.get(status, "模型 API 验证失败。")}, status_code=400)
        expires = str(int(time.time()) + TICKET_TTL)
        return {"ok": True, "elapsed_ms": result["elapsed_ms"], "expires_in": TICKET_TTL,
                "verification": expires + "." + signature(signing_key, config, expires)}

    @app.post("/try/api/chat")
    async def chat(request: Request):
        config = await read_payload(request, ChatRequest, site_origin)
        expires, provided = config.verification.split(".", 1)
        if int(expires) <= time.time() or not hmac.compare_digest(provided, signature(signing_key, config, expires)):
            return JSONResponse({"ok": False, "status": "verification_required",
                                 "error": "连接验证已失效，请重新测试。"}, status_code=403)
        if not config.message.strip():
            raise HTTPException(400, "请输入消息。")

        provider = PROVIDERS[config.provider]
        messages = [
            {"role": "system", "content": prompts[config.personality]},
            *(turn.model_dump() for turn in config.history),
            {"role": "user", "content": config.message},
        ]
        payload = {"model": config.model, "messages": messages, "stream": False, **provider.parameters(600)}
        try:
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, transport=transport, timeout=CHAT_TIMEOUT,
            ) as client:
                response = await asyncio.wait_for(
                    client.post(f"{provider.base}/chat/completions", json=payload,
                                headers={"Authorization": f"Bearer {config.api_key}"}),
                    timeout=CHAT_TIMEOUT,
                )
        except (httpx.HTTPError, asyncio.TimeoutError):
            raise HTTPException(502, "模型服务暂时不可用或响应超时。") from None
        if response.status_code in (401, 403):
            return JSONResponse({"ok": False, "status": "auth_failed",
                                 "error": "模型服务拒绝了该密钥，请检查配置并重新测试。"}, status_code=403)
        if response.status_code == 429:
            raise HTTPException(429, "模型服务已限流或可用额度不足。")
        if response.status_code != 200:
            raise HTTPException(502, "模型服务拒绝了请求，请检查模型、权限和额度。")
        choice = chat_message(response)
        reply = choice.get("content") if choice else None
        if not isinstance(reply, str) or not reply.strip():
            raise HTTPException(502, "模型没有返回可显示的文字，请换用支持纯文字对话的模型。")
        return {"ok": True, "reply": reply.strip()[:2400]}

    return app


app = create_app()
