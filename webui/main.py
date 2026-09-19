"""
WebUI main application — FastAPI entry point.

Run (from the repo root, panel binds 127.0.0.1 only):

    {venv}/bin/python -m uvicorn webui.main:app --host 127.0.0.1 --port 8090

Access over SSH tunnel:  ssh -L 8090:127.0.0.1:8090 user@server
"""

import asyncio
import fcntl
import json
import os
import pty
import signal
import struct
import termios
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import (audit, auth, config, config_editor, data_reader,
               hardware_monitor, log_viewer, playground, process_manager)
from .config import HOST, PORT


# ── App & lifespan ────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    watchdog_task = asyncio.create_task(process_manager.watchdog_loop())
    yield
    watchdog_task.cancel()


app = FastAPI(title="QQBotAgent WebUI", docs_url=None, redoc_url=None,
              lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(config.WEBUI_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(config.WEBUI_DIR / "templates"))

# Pages shown in the sidebar: (route, title)
PAGES = [
    ("/", "仪表盘"),
    ("/processes", "进程管理"),
    ("/logs", "日志查看"),
    ("/terminal", "Web 终端"),
    ("/tokens", "Token 消耗"),
    ("/audit", "工具审计"),
    ("/feedback", "用户反馈"),
    ("/sessions", "会话管理"),
    ("/tasks", "任务日志"),
    ("/memory", "记忆与画像"),
    ("/workspace", "工作区磁盘"),
    ("/config", "配置热管理"),
    ("/playground", "Playground"),
    ("/wiki", "Wiki 缓存"),
]

# Pages fully implemented so far (others render the placeholder template)
_IMPLEMENTED = {"/", "/processes", "/logs", "/terminal",
                "/tokens", "/audit", "/feedback", "/sessions",
                "/tasks", "/workspace", "/config", "/memory", "/wiki",
                "/playground"}

_PUBLIC_PREFIXES = ("/static/", "/api/auth/")
_PUBLIC_PATHS = {"/login", "/favicon.ico"}


# ── Auth middleware ───────────────────────────────────────────────

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    if path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)

    # Setup mode: no password configured yet — only the login page is served
    if auth.require_setup() and path != "/":
        if path.startswith("/api/"):
            return JSONResponse({"error": "panel_not_configured"}, status_code=403)
        return RedirectResponse("/", status_code=302)

    # Websockets verify auth inside the endpoint (close before accept),
    # because an HTTP response cannot answer a WS handshake.
    if path.startswith("/ws/"):
        return await call_next(request)

    if not auth.is_authenticated(request):
        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse("/login", status_code=302)

    return await call_next(request)


# ── Audit middleware (mutating requests only) ─────────────────────

_AUDITED_METHODS = {"POST", "PUT", "DELETE", "PATCH"}


@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    response = await call_next(request)
    try:
        if request.method in _AUDITED_METHODS and response.status_code < 400:
            client_ip = request.client.host if request.client else ""
            audit.log_action(
                action=f"{request.method} {request.url.path}",
                ip=client_ip,
            )
    except Exception:
        pass
    return response


# ── Templates ─────────────────────────────────────────────────────

def _render(request: Request, template: str, **ctx):
    ctx.setdefault("pages", PAGES)
    ctx.setdefault("active", request.url.path)
    ctx.setdefault("now", time.strftime("%Y-%m-%d %H:%M:%S"))
    return templates.TemplateResponse(request, template, ctx)


# ── Auth models & endpoints ───────────────────────────────────────

class PasswordPayload(BaseModel):
    password: str


class ChangePasswordPayload(BaseModel):
    old_password: str
    new_password: str


@app.get("/api/auth/status")
async def auth_status():
    lock = auth.lock_remaining()
    return {
        "configured": auth.is_configured(),
        "locked": lock > 0,
        "lock_seconds": int(lock),
    }


@app.post("/api/auth/setup")
async def auth_setup(payload: PasswordPayload, request: Request):
    if auth.is_configured():
        return JSONResponse({"error": "already_configured"}, status_code=409)
    if len(payload.password) < 8:
        return JSONResponse({"error": "密码至少 8 位"}, status_code=400)
    ok = auth.setup_password(payload.password)
    if not ok:
        return JSONResponse({"error": "setup_failed"}, status_code=500)
    response = JSONResponse({"ok": True})
    auth.create_session(response)
    audit.log_action("auth.setup", "管理员密码首次设置",
                     request.client.host if request.client else "")
    return response


@app.post("/api/auth/login")
async def auth_login(payload: PasswordPayload, request: Request):
    lock = auth.lock_remaining()
    if lock > 0:
        return JSONResponse(
            {"error": f"尝试次数过多，请 {int(lock)} 秒后再试"},
            status_code=429,
        )
    if auth.verify_password(payload.password):
        response = JSONResponse({"ok": True})
        auth.create_session(response)
        audit.log_action("auth.login", "登录成功",
                         request.client.host if request.client else "")
        return response
    locked_for = auth.record_failure()
    audit.log_action("auth.login_failed", "登录失败" + ("，已锁定" if locked_for else ""),
                     request.client.host if request.client else "")
    if locked_for:
        return JSONResponse(
            {"error": f"失败次数过多，面板已锁定 {int(locked_for)} 秒"},
            status_code=429,
        )
    return JSONResponse({"error": "密码错误"}, status_code=401)


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    response = JSONResponse({"ok": True})
    auth.destroy_session(request, response)
    return response


@app.post("/api/auth/change-password")
async def auth_change(payload: ChangePasswordPayload, request: Request):
    if len(payload.new_password) < 8:
        return JSONResponse({"error": "新密码至少 8 位"}, status_code=400)
    if auth.change_password(payload.old_password, payload.new_password):
        audit.log_action("auth.change_password", "管理员密码已修改",
                         request.client.host if request.client else "")
        return {"ok": True}
    return JSONResponse({"error": "原密码错误"}, status_code=401)


# ── Pages ─────────────────────────────────────────────────────────

@app.get("/login")
async def page_login(request: Request):
    if auth.is_authenticated(request):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "login.html", {
        "setup_mode": auth.require_setup(),
    })


@app.get("/")
async def page_dashboard(request: Request):
    if auth.require_setup():
        return RedirectResponse("/login", status_code=302)
    return _render(request, "dashboard.html", title="仪表盘")


def _make_page(route: str, template: str, title: str):
    async def _page(request: Request):
        if route in _IMPLEMENTED:
            return _render(request, template, title=title)
        return _render(request, "placeholder.html", title=title, page_title=title)
    _page.__name__ = f"page_{template.replace('.html', '')}"
    return _page


for _route, _title in PAGES[1:]:
    app.add_api_route(
        _route,
        _make_page(_route, _route.strip("/").replace("/", "_") + ".html", _title),
        methods=["GET"],
    )


# ── Dashboard API ─────────────────────────────────────────────────

def _fmt_uptime(seconds) -> str:
    if not seconds or seconds < 0:
        return ""
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}天{h}时"
    if h:
        return f"{h}时{m}分"
    return f"{m}分钟"


@app.get("/api/dashboard")
async def api_dashboard():
    procs = await asyncio.to_thread(process_manager.status_all)
    res = await asyncio.to_thread(hardware_monitor.sample)
    today = time.strftime("%Y-%m-%d")
    tok = await asyncio.to_thread(data_reader.tokens_daily, today)
    bucket = tok.get("bucket", {})
    # 今日 agent_loop 命中率（全局口径会被 triage/多模态稀释，Phase 1.2）
    _al = (await asyncio.to_thread(data_reader.tokens_daily_by_purpose, today)).get("agent_loop") or {}
    _al_inp = _al.get("input_tokens", 0)
    hit_rate_agent_loop = (_al.get("cached_input_tokens", 0) / _al_inp) if _al_inp else None
    unread = await asyncio.to_thread(data_reader.feedback_unread_count)
    for name, info in procs.items():
        if info.get("state") == "running":
            sub = f"PID {info.get('pid')}"
            if info.get("uptime") is not None:
                sub += " · " + _fmt_uptime(info["uptime"])
            info["sub"] = sub
        elif info.get("state") == "unavailable":
            info["sub"] = "docker 不可用"
        else:
            if info.get("last_crash"):
                info["sub"] = (f"崩溃 {info.get('crash_count', 0)} 次 · "
                               f"最近 {time.strftime('%m-%d %H:%M', time.localtime(info['last_crash']))}")
            else:
                info["sub"] = "已停止"
    return {
        "panel": {
            "host": HOST,
            "port": PORT,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "processes": procs,
        "resources": {
            "cpu": res.get("cpu"),
            "mem": res.get("mem_percent"),
            "disk": res.get("disk_percent"),
            "mem_used": res.get("mem_used"),
            "mem_total": res.get("mem_total"),
            "disk_used": res.get("disk_used"),
            "disk_total": res.get("disk_total"),
            "load": res.get("load"),
            "uptime": res.get("uptime"),
        },
        "tokens": {
            "today_tokens": bucket.get("input_tokens", 0) + bucket.get("output_tokens", 0),
            "today_input": bucket.get("input_tokens", 0),
            "today_output": bucket.get("output_tokens", 0),
            "hit_rate": tok.get("hit_rate"),
            "hit_rate_agent_loop": hit_rate_agent_loop,
        },
        "feedback": {
            "unread": unread,
            "total": await asyncio.to_thread(data_reader.feedback_total_count),
        },
        "watchdog": {"enabled": process_manager.watchdog_enabled()},
    }


# ── Process management API ────────────────────────────────────────

_VALID_ACTIONS = {"start", "stop", "restart"}


@app.get("/api/processes")
async def api_processes():
    procs = await asyncio.to_thread(process_manager.status_all)
    return {
        "watchdog": {
            "enabled": process_manager.watchdog_enabled(),
            "poll_seconds": process_manager.POLL_INTERVAL,
            "rate_limit": f"{process_manager.MAX_RESTARTS} 次 / "
                          f"{process_manager.RATE_WINDOW // 60} 分钟",
        },
        "processes": procs,
    }


@app.post("/api/processes/{name}/{action}")
async def api_process_action(name: str, action: str, request: Request):
    if name not in config.PROCESSES:
        return JSONResponse({"error": f"未知进程: {name}"}, status_code=404)
    if action not in _VALID_ACTIONS:
        return JSONResponse({"error": f"未知操作: {action}"}, status_code=400)
    fn = getattr(process_manager, action)
    result = await asyncio.to_thread(fn, name)
    if not result.get("ok"):
        return JSONResponse(result, status_code=500)
    audit.log_action(f"process.{action}", f"{action} {name}",
                     request.client.host if request.client else "")
    return result


class WatchdogPayload(BaseModel):
    enabled: bool


@app.post("/api/processes/watchdog")
async def api_watchdog(payload: WatchdogPayload, request: Request):
    process_manager.set_watchdog(payload.enabled)
    audit.log_action("watchdog.toggle",
                     "看门狗" + ("开启" if payload.enabled else "关闭"),
                     request.client.host if request.client else "")
    return {"ok": True, "enabled": payload.enabled}


# ── Log viewer API ────────────────────────────────────────────────

@app.get("/api/logs/{source}")
async def api_logs(source: str, lines: int = 200, before: int = 0, q: str = ""):
    result = await asyncio.to_thread(log_viewer.read, source, lines, before, q)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    return result


@app.post("/api/logs/{source}/clear")
async def api_logs_clear(source: str, request: Request):
    result = await asyncio.to_thread(log_viewer.clear, source)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    audit.log_action("logs.clear", f"清空日志 {source}",
                     request.client.host if request.client else "")
    return result


@app.websocket("/ws/logs/{source}")
async def ws_logs(ws: WebSocket, source: str):
    if not auth.is_authenticated(ws):
        await ws.close(code=1008)
        return
    if source not in config.log_sources():
        await ws.close(code=1008)
        return
    await ws.accept()
    is_docker = config.log_sources()[source].startswith("docker:")
    offset = 0
    sent_buf: list[str] = []
    try:
        if not is_docker:
            # Start from end-of-file so we only push *new* lines
            initial = await asyncio.to_thread(log_viewer.read, source, 1)
            offset = initial.get("size") or 0
        while True:
            if is_docker:
                data = await asyncio.to_thread(log_viewer.read, source, 500)
                tail = data.get("lines", [])
                # find longest suffix of sent_buf that prefixes tail
                k = 0
                if sent_buf and tail:
                    max_k = min(len(sent_buf), len(tail))
                    for k in range(max_k, 0, -1):
                        if sent_buf[-k:] == tail[:k]:
                            break
                    else:
                        k = 0
                new = tail[k:]
                sent_buf = (sent_buf + new)[-1000:]
                if new:
                    await ws.send_text(json.dumps({"lines": new}))
            else:
                data = await asyncio.to_thread(log_viewer.read_new, source, offset)
                offset = data.get("offset", offset)
                if data.get("lines"):
                    await ws.send_text(json.dumps({"lines": data["lines"]}))
            await asyncio.sleep(1.5)
    except (WebSocketDisconnect, RuntimeError):
        pass


# ── Token metering API ────────────────────────────────────────────

@app.get("/api/tokens/summary")
async def api_tokens_summary(days: int = 7):
    return await asyncio.to_thread(data_reader.tokens_summary, min(max(days, 1), 90))


@app.get("/api/tokens/daily")
async def api_tokens_daily(date: str = ""):
    date = date or time.strftime("%Y-%m-%d")
    return await asyncio.to_thread(data_reader.tokens_daily, date)


@app.get("/api/tokens/users")
async def api_tokens_users(days: int = 7, limit: int = 20):
    return await asyncio.to_thread(data_reader.tokens_users,
                                   min(max(days, 1), 90), min(max(limit, 1), 100))


# ── Tool audit API ────────────────────────────────────────────────

@app.get("/api/audit/tool-calls")
async def api_audit_calls(date: str = "", tool: str = "", user: str = "",
                          page: int = 1, size: int = 50):
    return await asyncio.to_thread(data_reader.audit_calls, date, tool, user, page, size)


@app.get("/api/audit/tool-stats")
async def api_audit_stats(days: int = 7):
    return await asyncio.to_thread(data_reader.audit_stats, min(max(days, 1), 90))


# ── Feedback API ──────────────────────────────────────────────────

@app.get("/api/feedback")
async def api_feedback(month: str = "", type: str = Query("", alias="type"),
                       page: int = 1, size: int = 50):
    return await asyncio.to_thread(data_reader.feedback_list, month, type, page, size)


@app.post("/api/feedback/mark-read")
async def api_feedback_mark_read():
    await asyncio.to_thread(data_reader.feedback_mark_read)
    return {"ok": True}


class TagPayload(BaseModel):
    tag: str


@app.post("/api/feedback/{month}/{seq}/tag")
async def api_feedback_tag(month: str, seq: int, payload: TagPayload,
                           request: Request):
    result = await asyncio.to_thread(data_reader.feedback_tag, month, seq, payload.tag)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    audit.log_action("feedback.tag", f"反馈 {month}#{seq} 打标签「{payload.tag}」",
                     request.client.host if request.client else "")
    return result


# ── Session management API ────────────────────────────────────────

@app.get("/api/sessions")
async def api_sessions():
    return await asyncio.to_thread(data_reader.sessions_list)


@app.get("/api/sessions/{uid}/temp")
async def api_session_temp(uid: str):
    result = await asyncio.to_thread(data_reader.session_detail, uid)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    return result


@app.post("/api/sessions/{uid}/clear-temp")
async def api_session_clear(uid: str, request: Request):
    result = await asyncio.to_thread(data_reader.session_clear_temp, uid)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    audit.log_action("session.clear_temp", f"清除临时会话 {uid}",
                     request.client.host if request.client else "")
    return result


@app.get("/api/sessions/{uid}/special")
async def api_session_special(uid: str):
    return await asyncio.to_thread(data_reader.special_list, uid)


@app.delete("/api/sessions/{uid}/special/{name}")
async def api_session_special_delete(uid: str, name: str, request: Request):
    result = await asyncio.to_thread(data_reader.special_delete, uid, name)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    audit.log_action("session.delete_special",
                     f"删除特殊会话 {uid}/{name}（释放 {result.get('freed_bytes', 0)} 字节）",
                     request.client.host if request.client else "")
    return {"ok": True, **result}


# ── Task log API ──────────────────────────────────────────────────

@app.get("/api/tasks")
async def api_tasks(user_id: str = "", page: int = 1, size: int = 50):
    return await asyncio.to_thread(data_reader.tasks_list, user_id, page, size)


@app.get("/api/tasks/{uid}/{task_id}")
async def api_task_detail(uid: str, task_id: str):
    result = await asyncio.to_thread(data_reader.task_detail, uid, task_id)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    return result


# ── Workspace API ─────────────────────────────────────────────────

@app.get("/api/workspace/stats")
async def api_workspace_stats():
    return await asyncio.to_thread(data_reader.workspace_stats)


@app.get("/api/workspace/{uid}")
async def api_workspace_user(uid: str):
    result = await asyncio.to_thread(data_reader.workspace_tree, uid)
    if "error" in result and not result.get("entries"):
        return JSONResponse(result, status_code=404)
    return result


# ── Config hot-management API ─────────────────────────────────────

def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


@app.get("/api/config/prompts")
async def api_config_prompts():
    return await asyncio.to_thread(config_editor.prompts_list)


@app.get("/api/config/prompts/{name:path}")
async def api_config_prompt_get(name: str):
    result = await asyncio.to_thread(config_editor.prompt_read, name)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    return result


class PromptPayload(BaseModel):
    content: str


@app.put("/api/config/prompts/{name:path}")
async def api_config_prompt_put(name: str, payload: PromptPayload, request: Request):
    result = await asyncio.to_thread(config_editor.prompt_write, name, payload.content)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    audit.log_action("config.prompt_write", f"编辑提示词 {name}", _ip(request))
    return result


@app.get("/api/config/permissions")
async def api_config_permissions():
    return await asyncio.to_thread(config_editor.permissions_read)


class PermissionsPayload(BaseModel):
    superusers: str = ""
    vip_users: str = ""


@app.put("/api/config/permissions")
async def api_config_permissions_put(payload: PermissionsPayload, request: Request):
    result = await asyncio.to_thread(config_editor.permissions_write,
                                     payload.superusers, payload.vip_users)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    audit.log_action("config.permissions_write", "修改权限名单", _ip(request))
    return result


@app.get("/api/config/models")
async def api_config_models():
    result = await asyncio.to_thread(config_editor.models_read)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    return result


class RawPayload(BaseModel):
    content: str


@app.put("/api/config/models")
async def api_config_models_put(payload: RawPayload, request: Request):
    result = await asyncio.to_thread(config_editor.models_write, payload.content)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    audit.log_action("config.models_write", "修改模型配置（需重启 NoneBot）", _ip(request))
    return result


@app.get("/api/config/group-features")
async def api_config_group_features():
    return await asyncio.to_thread(config_editor.group_features_read)


@app.put("/api/config/group-features")
async def api_config_group_features_put(payload: RawPayload, request: Request):
    result = await asyncio.to_thread(config_editor.json_config_write,
                                     config_editor.GROUP_FEATURES_FILE, payload.content)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    result["note"] = "已保存 — 下条群消息即生效"
    audit.log_action("config.group_features_write", "修改群功能开关", _ip(request))
    return result


@app.get("/api/config/personality")
async def api_config_personality():
    return await asyncio.to_thread(config_editor.personality_read)


class PersonalityPayload(BaseModel):
    personality: str
    group_personality: str = ""


@app.put("/api/config/personality")
async def api_config_personality_put(payload: PersonalityPayload, request: Request):
    r1 = await asyncio.to_thread(config_editor.json_config_write,
                                 config_editor.PERSONALITY_FILE, payload.personality)
    if "error" in r1:
        return JSONResponse({"error": f"personality_config: {r1['error']}"}, status_code=400)
    if payload.group_personality.strip():
        r2 = await asyncio.to_thread(config_editor.json_config_write,
                                     config_editor.GROUP_PERSONALITY_FILE,
                                     payload.group_personality)
        if "error" in r2:
            return JSONResponse(
                {"error": f"group_personality: {r2['error']}（personality_config 已保存）"},
                status_code=400)
    audit.log_action("config.personality_write", "修改人格配置", _ip(request))
    return {"ok": True, "note": "已保存，新消息即生效"}


# ── Memory & profile API ──────────────────────────────────────────

@app.get("/api/memory")
async def api_memory(mem_type: str = "", user_id: str = "", q: str = ""):
    return await asyncio.to_thread(data_reader.memory_list, mem_type, user_id, q)


class MemoryPayload(BaseModel):
    name: str
    description: str = ""
    type: str
    content: str = ""
    user_id: str = ""


@app.put("/api/memory")
async def api_memory_put(payload: MemoryPayload, request: Request):
    result = await asyncio.to_thread(data_reader.memory_save, payload.name,
                                     payload.description, payload.type,
                                     payload.content, payload.user_id)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    audit.log_action("memory.save", f"写入记忆 {payload.type}/{payload.name}", _ip(request))
    return result


@app.delete("/api/memory")
async def api_memory_delete(name: str, mem_type: str = "", user_id: str = "",
                            request: Request = None):
    result = await asyncio.to_thread(data_reader.memory_delete, name, mem_type, user_id)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    audit.log_action("memory.delete", f"删除记忆 {mem_type or '*'}/{name}", _ip(request))
    return result


@app.get("/api/profiles/{uid}")
async def api_profile_get(uid: str):
    result = await asyncio.to_thread(data_reader.profile_get, uid)
    if "error" in result:
        return JSONResponse(result, status_code=404)
    return result


@app.put("/api/profiles/{uid}")
async def api_profile_put(uid: str, payload: dict, request: Request):
    result = await asyncio.to_thread(data_reader.profile_save, uid, payload)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    audit.log_action("profile.save", f"修改用户画像 {uid}", _ip(request))
    return result


# ── Wiki cache API ────────────────────────────────────────────────

@app.get("/api/wiki/cache-status")
async def api_wiki_cache():
    return await asyncio.to_thread(data_reader.wiki_cache_status)


# ── Playground / system-prompt preview API ────────────────────────

@app.get("/api/playground/options")
async def api_playground_options():
    return {
        "models": await asyncio.to_thread(playground.available_models),
        "personalities": await asyncio.to_thread(playground.available_personalities),
    }


class PlaygroundPayload(BaseModel):
    message: str
    personality: str = ""
    tier: str = "reasoning"
    role: str = "admin"
    history: list = []


@app.post("/api/playground/run")
async def api_playground_run(payload: PlaygroundPayload, request: Request):
    result = await playground.run_playground(
        payload.message, personality=payload.personality, tier=payload.tier,
        role=payload.role, history=payload.history)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    audit.log_action("playground.run",
                     f"Playground 对话 tier={payload.tier} personality={payload.personality or '-'}",
                     _ip(request))
    return result


@app.get("/api/config/system-prompt-preview")
async def api_system_prompt_preview(personality: str = "", role: str = "admin"):
    result = await asyncio.to_thread(playground.preview_system_prompt, personality, role)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


# ── Web terminal (pty over websocket) ─────────────────────────────

@app.websocket("/ws/terminal")
async def ws_terminal(ws: WebSocket):
    if not auth.is_authenticated(ws):
        await ws.close(code=1008)
        return
    await ws.accept()

    shell = os.environ.get("SHELL") or "/bin/bash"
    try:
        pid, fd = pty.fork()
    except OSError as e:
        await ws.send_text(f"\r\n[无法创建终端: {e}]\r\n")
        await ws.close()
        return
    if pid == 0:  # child — becomes the shell
        os.environ.setdefault("TERM", "xterm-256color")
        try:
            os.execvp(shell, [shell])
        except OSError:
            os._exit(127)

    # parent
    async def _reader():
        loop = asyncio.get_running_loop()
        try:
            while True:
                try:
                    data = await asyncio.to_thread(os.read, fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                await ws.send_text(data.decode("utf-8", errors="replace"))
        finally:
            try:
                await ws.send_text("\r\n[会话结束]\r\n")
                await ws.close()
            except Exception:
                pass

    reader_task = asyncio.create_task(_reader())
    try:
        while True:
            msg = await ws.receive_text()
            if msg.startswith("{"):
                try:
                    ctrl = json.loads(msg)
                    if ctrl.get("type") == "resize":
                        winsize = struct.pack("HHHH", int(ctrl.get("rows", 24)),
                                              int(ctrl.get("cols", 80)), 0, 0)
                        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
                        continue
                except (ValueError, OSError):
                    pass
            os.write(fd, msg.encode("utf-8"))
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        reader_task.cancel()
        try:
            os.kill(pid, signal.SIGHUP)
        except (ProcessLookupError, OSError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass


# ── Entrypoint ────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("webui.main:app", host=HOST, port=PORT)
