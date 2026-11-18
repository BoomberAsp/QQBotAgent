"""SurveyCake browser submitter (P1).

Runs ``plugins/ruyipage_action.py`` under ``xvfb-run`` in a subprocess. The
plan (URL + answers) is passed through a ``0600`` temp file, never argv, and
is deleted afterwards; the runner reports progress through its result file so
a timeout can be classified as "unknown" instead of "failed".

P1 is opt-in:
    SURVEY_ENABLE_SUBMIT=1
    SURVEY_BROWSER=ruyipage
    SURVEY_BROWSER_TIMEOUT=180
    SURVEY_BROWSER_CONCURRENCY=1
    SURVEY_PROXY=http://...            # optional, falls back to HTTPS_PROXY
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time

from lib import browser_fetch as bf
from lib.survey.surveycake import (
    allowed_hosts,
    ensure_public_host,
    proxy_url,
)

_ACTION_SCRIPT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "plugins", "ruyipage_action.py")
)
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_SEM: asyncio.Semaphore | None = None


class SubmitError(Exception):
    """Configuration/transport error before anything could be submitted."""


def submit_enabled() -> bool:
    return (os.environ.get("SURVEY_ENABLE_SUBMIT", "").strip().lower() in _TRUTHY)


def engine_enabled() -> bool:
    return bf.engine_enabled("SURVEY_BROWSER")


def ready() -> tuple[bool, str]:
    """(ready, reason) — reason is a stable code, not a user-facing message."""
    if not submit_enabled():
        return False, "submit_disabled"
    if not engine_enabled():
        return False, "browser_disabled"
    return True, ""


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _timeout() -> float:
    return float(_env_int("SURVEY_BROWSER_TIMEOUT", 180))


def _sem() -> asyncio.Semaphore:
    global _SEM
    if _SEM is None:
        _SEM = asyncio.Semaphore(_env_int("SURVEY_BROWSER_CONCURRENCY", 1))
    return _SEM


def survey_url(svid: str) -> str:
    return f"https://www.surveycake.com/s/{svid}"


def build_plan(survey, draft, screenshot_path: str = "") -> dict:
    """Pure function: the action plan handed to the ruyipage runner."""
    answers = {}
    for subject_id, answer in (draft.answers or {}).items():
        if answer.is_empty():
            continue
        answers[str(subject_id)] = {
            "kind": answer.kind,
            "text": answer.text or "",
            "option_id": answer.option_id,
        }
    return {
        "url": survey_url(survey.svid),
        "svid": survey.svid,
        "questions": [
            {
                "subject_id": question.subject_id,
                "type": question.type,
                "required": question.required,
            }
            for question in survey.questions
        ],
        "answers": answers,
        "screenshot": screenshot_path,
        "proxy": proxy_url(),
        # The unique substring comes from SurveyCake's thankyoutext; the
        # generic "感谢团长参与" is also in Q5, so it must never be first.
        "success_texts": [
            "3 周年社群纪念问活动",
            "感谢团长参与本次《星陨计划》3 周年社群纪念问活动",
            "已送出", "提交成功", "已提交",
        ],
        "max_pages": 10,
        "step_timeout_ms": 5000,
        "after_click_ms": 900,
        "success_timeout_s": 30,
        "page_load_timeout_s": 60,
    }


def _validate_url(url: str) -> None:
    from urllib.parse import urlsplit

    parts = urlsplit(url or "")
    if parts.scheme != "https":
        raise SubmitError("only https survey links are supported")
    host = (parts.hostname or "").lower()
    if host not in allowed_hosts():
        raise SubmitError(f"survey host not allowed: {host}")
    ensure_public_host(host)


async def _spawn(*args, **kwargs):
    return await asyncio.create_subprocess_exec(*args, **kwargs)


async def _wait_for(coro, timeout):
    return await asyncio.wait_for(coro, timeout)


def _read_result(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


async def submit_survey(survey, draft, *, screenshot_path: str = "",
                        timeout: float | None = None) -> dict:
    """Fill and submit one survey; returns a result dict (never raises for
    runtime failures — only :class:`SubmitError` for configuration gaps).

    Result keys: ok, stage, submitted, screenshot, error, duration_ms,
    exit_code.
    """
    ok, reason = ready()
    if not ok:
        raise SubmitError(reason)
    if not os.path.exists(_ACTION_SCRIPT):
        raise SubmitError(f"browser action runner missing: {_ACTION_SCRIPT}")
    xvfb = shutil.which("xvfb-run")
    if not xvfb:
        raise SubmitError("xvfb-run not found")

    plan = build_plan(survey, draft, screenshot_path)
    _validate_url(plan["url"])
    python = bf.resolve_python()
    total = float(timeout if timeout is not None else _timeout())

    plan_path = ""
    out_path = ""
    try:
        fd, plan_path = tempfile.mkstemp(prefix="survey_plan_", suffix=".json")
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(plan, handle, ensure_ascii=False)
        fd, out_path = tempfile.mkstemp(prefix="survey_result_", suffix=".json")
        os.close(fd)
        os.remove(out_path)                       # runner creates it

        cmd = [xvfb, "-a", python, _ACTION_SCRIPT, plan_path, out_path]
        started = time.time()
        timed_out = False
        stderr = b""
        async with _sem():
            proc = await _spawn(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await _wait_for(
                    proc.communicate(), timeout=total + 30
                )
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    _, stderr = await proc.communicate()
                except Exception:                  # noqa: BLE001
                    stderr = b""
        duration_ms = int((time.time() - started) * 1000)
        exit_code = proc.returncode if proc.returncode is not None else -1

        text = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
        for line in text[-8:]:
            print(f"[SurveyBrowser]   {line}", file=sys.stderr)

        result = _read_result(out_path)
        stage = str(result.get("stage") or "")
        submitted = bool(result.get("submitted")) or stage in (
            "submit_clicked", "unconfirmed"
        )
        success = bool(result.get("ok")) and exit_code == 0

        if timed_out:
            success = False
            if submitted:
                stage = "unconfirmed"
            else:
                stage = stage or "timeout"

        screenshot = str(result.get("screenshot") or "")
        if not success and screenshot and os.path.exists(screenshot):
            # Failure screenshots may contain the member id; never keep them.
            try:
                os.remove(screenshot)
            except OSError:
                pass
            screenshot = ""

        return {
            "ok": success,
            "stage": stage or ("done" if exit_code == 0 else "error"),
            "submitted": submitted,
            "screenshot": screenshot if success else "",
            "error": str(result.get("error") or ("timeout" if timed_out else "")),
            "duration_ms": duration_ms,
            "exit_code": exit_code,
        }
    finally:
        for path in (plan_path, out_path):
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
