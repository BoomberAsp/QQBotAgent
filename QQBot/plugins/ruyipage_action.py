"""SurveyCake action runner (P1) — executed under ``xvfb-run``.

The parent process (``lib/browser_action.py``) writes a plan JSON to a
``0600`` temp file and passes only its path on the command line, so the
member id / answers never appear in ``ps``. The runner writes a result JSON
incrementally so a timeout can be classified correctly:

    {"stage": "started"} -> {"stage": "loaded"} -> {"stage": "filled"}
    -> {"stage": "submit_clicked"} -> {"ok": true, "stage": "done", ...}

Exit codes:
    0  submitted and the success text appeared
    2  usage
    4  browser / page setup failure (nothing submitted)
    10 failure before the submit click (nothing submitted)
    11 submit clicked but success could not be confirmed (UNKNOWN)
"""
import json
import os
import sys
import time
import traceback

MAX_PAGES_DEFAULT = 10
STEP_TIMEOUT_MS_DEFAULT = 5000
AFTER_CLICK_MS_DEFAULT = 900
SUCCESS_TIMEOUT_S_DEFAULT = 30


def log(message):
    print(f"[survey_action] {message}", file=sys.stderr, flush=True)


def write_result(path, payload):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        log(f"cannot write result: {type(exc).__name__}: {exc}")


class StageError(Exception):
    def __init__(self, stage, message):
        super().__init__(message)
        self.stage = stage
        self.message = message


def _is_missing(element):
    return element is None or type(element).__name__ == "NoneElement"


def find(page, locator, timeout_ms):
    # ruyipage's `ele(timeout=...)` is in SECONDS; our plan uses milliseconds.
    timeout_s = max(0.1, float(timeout_ms) / 1000.0)
    try:
        element = page.ele(locator, timeout=timeout_s)
    except Exception as exc:                       # noqa: BLE001 - selector miss
        log(f"ele({locator}) raised {type(exc).__name__}")
        return None
    if _is_missing(element):
        return None
    return element


def bring_into_view(element):
    """Scroll the element into the viewport (ruyipage click needs a real rect)."""
    try:
        element.scroll.to_see(center=True)
    except Exception:                              # noqa: BLE001 - best effort
        pass


_REACT_SET_VALUE_JS = """function(text) {
  const el = this;
  const proto = el.tagName === 'TEXTAREA'
    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(el, text);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
}"""


def type_text(element, text):
    """Type like a user; fall back to a React-safe JS setter if needed."""
    bring_into_view(element)
    try:
        element.input(str(text))
        return
    except Exception as exc:                       # noqa: BLE001
        log(f"input fallback to JS: {type(exc).__name__}")
    try:
        # Native setter + input/change events is what React's value tracker
        # needs; plain `el.value = ...` can be swallowed as "unchanged".
        element.run_js(_REACT_SET_VALUE_JS, str(text))
    except Exception as exc:                       # noqa: BLE001
        log(f"native setter failed: {type(exc).__name__}; plain JS value")
        element.input(str(text), by_js=True)


def click(page, locator, timeout_ms, after_ms):
    element = find(page, locator, timeout_ms)
    if element is None:
        return False
    bring_into_view(element)
    try:
        element.click()
    except Exception as exc:                       # noqa: BLE001
        log(f"click({locator}) raised {type(exc).__name__}; trying JS click")
        try:
            element.click.by_js()
        except Exception:                          # noqa: BLE001
            try:
                element.run_js("function(){ this.click(); }")
            except Exception:                      # noqa: BLE001
                return False
    time.sleep(after_ms / 1000.0)
    return True


def ensure_question(page, subject_id, plan):
    """Make the question visible, clicking 下一頁 as needed."""
    max_pages = int(plan.get("max_pages") or MAX_PAGES_DEFAULT)
    step_ms = int(plan.get("step_timeout_ms") or STEP_TIMEOUT_MS_DEFAULT)
    after_ms = int(plan.get("after_click_ms") or AFTER_CLICK_MS_DEFAULT)
    for _ in range(max_pages):
        element = find(page, f'css:[data-subject-id="{subject_id}"]', step_ms)
        if element is not None:
            return element
        if not click(page, 'css:[data-qa="next"]', 1200, after_ms):
            break
    return None


def fill_answers(page, plan):
    step_ms = int(plan.get("step_timeout_ms") or STEP_TIMEOUT_MS_DEFAULT)
    for question in plan.get("questions") or []:
        subject_id = int(question["subject_id"])
        answer = (plan.get("answers") or {}).get(str(subject_id))
        if not answer:
            continue
        if ensure_question(page, subject_id, plan) is None:
            raise StageError("question_not_found", f"subject {subject_id} not visible")

        kind = str(answer.get("kind") or "text")
        if kind == "choice":
            option_id = answer.get("option_id")
            if not option_id:
                raise StageError("option_not_found", f"subject {subject_id} has no option")
            if not click(page, f'css:[data-subject-option-id="{option_id}"]',
                         step_ms, 400):
                raise StageError("option_not_found",
                                 f"option {option_id} for subject {subject_id}")
            follow = str(answer.get("text") or "").strip()
            if follow:
                box = find(page,
                           f'css:[data-subject-id="{subject_id}"] input[type="text"]',
                           1500)
                if box is None:
                    box = find(page,
                               f'css:[data-subject-id="{subject_id}"] textarea',
                               1500)
                if box is None:
                    raise StageError("follow_up_not_found", f"subject {subject_id}")
                type_text(box, follow)
        else:
            box = find(page, f'css:[data-subject-answer="{subject_id}"]', step_ms)
            if box is None:
                raise StageError("input_not_found", f"subject {subject_id}")
            type_text(box, answer.get("text") or "")
        time.sleep(0.25)
        log(f"filled subject {subject_id}")


def ensure_submit(page, plan):
    max_pages = int(plan.get("max_pages") or MAX_PAGES_DEFAULT)
    after_ms = int(plan.get("after_click_ms") or AFTER_CLICK_MS_DEFAULT)
    for _ in range(max_pages):
        if find(page, 'css:[data-qa="submit"]', 800) is not None:
            return True
        if not click(page, 'css:[data-qa="next"]', 1200, after_ms):
            break
    return False


def wait_success(page, plan):
    timeout = float(plan.get("success_timeout_s") or SUCCESS_TIMEOUT_S_DEFAULT)
    markers = plan.get("success_texts") or ["感谢团长参与"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            body = page.ele("tag:body").text or ""
        except Exception:                          # noqa: BLE001 - transient
            body = ""
        if any(marker in body for marker in markers):
            return True
        time.sleep(1.0)
    return False


def build_options(plan):
    from ruyipage import FirefoxOptions, resolve_firefox_path

    browser = None
    try:
        browser = resolve_firefox_path()
    except Exception as exc:                       # noqa: BLE001
        log(f"resolve_firefox_path raised {type(exc).__name__}: {exc}")
    if not browser:
        raise RuntimeError("no Firefox runtime found")

    options = FirefoxOptions()
    options.set_browser_path(browser)
    options.enable_marionette(False)
    options.set_window_size(1440, 1200)
    options.set_timeouts(page_load=60000, script=30000)
    options.set_pref("network.http.http3.enabled", False)
    proxy = str(plan.get("proxy") or "").strip()
    if proxy:
        options.set_proxy(proxy)
        log(f"proxy configured: {proxy.split('@')[-1]}")
    return options


def run(plan, out_path):
    from ruyipage import FirefoxPage

    page = None
    submitted = False
    try:
        options = build_options(plan)
        page = FirefoxPage(options)
        write_result(out_path, {"stage": "browser_ready"})

        page.get(plan["url"], timeout=float(plan.get("page_load_timeout_s") or 60))
        write_result(out_path, {"stage": "loaded", "url": page.url})

        if find(page, 'css:button[data-qa="begin"]', 5000) is not None:
            click(page, 'css:button[data-qa="begin"]', 5000,
                  int(plan.get("after_click_ms") or AFTER_CLICK_MS_DEFAULT))

        fill_answers(page, plan)
        write_result(out_path, {"stage": "filled"})

        if not ensure_submit(page, plan):
            write_result(out_path, {
                "ok": False, "stage": "submit_not_found", "submitted": False,
                "error": "submit button not found",
            })
            return 10

        submit = find(page, 'css:[data-qa="submit"]', 2000)
        if submit is None:
            write_result(out_path, {
                "ok": False, "stage": "submit_not_found", "submitted": False,
                "error": "submit button disappeared",
            })
            return 10
        submit.click()
        submitted = True
        write_result(out_path, {"stage": "submit_clicked", "submitted": True})
        log("submit clicked; waiting for success text")

        if not wait_success(page, plan):
            write_result(out_path, {
                "ok": False, "stage": "unconfirmed", "submitted": True,
                "error": "success text not found",
            })
            return 11

        screenshot = str(plan.get("screenshot") or "")
        if screenshot:
            try:
                os.makedirs(os.path.dirname(os.path.abspath(screenshot)),
                            exist_ok=True)
                page.screenshot(path=screenshot, full_page=True)
            except Exception as exc:               # noqa: BLE001 - receipt only
                log(f"screenshot failed: {type(exc).__name__}: {exc}")
                screenshot = ""
        write_result(out_path, {
            "ok": True, "stage": "done", "submitted": True,
            "screenshot": screenshot, "url": page.url, "title": page.title,
        })
        log("submission confirmed")
        return 0
    except StageError as exc:
        write_result(out_path, {
            "ok": False, "stage": exc.stage, "submitted": submitted,
            "error": exc.message,
        })
        log(f"stage error ({exc.stage}): {exc.message}")
        return 11 if submitted else 10
    except Exception as exc:                       # noqa: BLE001
        log(f"fatal: {type(exc).__name__}: {exc}")
        traceback.print_exc(file=sys.stderr)
        write_result(out_path, {
            "ok": False,
            "stage": "unconfirmed" if submitted else "error",
            "submitted": submitted,
            "error": f"{type(exc).__name__}: {exc}",
        })
        return 11 if submitted else 10
    finally:
        if page is not None:
            try:
                page.quit(timeout=5, force=True)
            except Exception:                     # noqa: BLE001
                pass


def main(argv):
    if len(argv) < 2:
        log("usage: ruyipage_action.py <plan.json> <result.json>")
        return 2
    plan_path, out_path = argv[0], argv[1]
    try:
        with open(plan_path, "r", encoding="utf-8") as handle:
            plan = json.load(handle)
    except Exception as exc:                       # noqa: BLE001
        log(f"cannot read plan: {type(exc).__name__}: {exc}")
        write_result(out_path, {"ok": False, "stage": "bad_plan",
                                "submitted": False, "error": "bad plan"})
        return 2

    write_result(out_path, {"stage": "started"})
    try:
        return run(plan, out_path)
    except Exception as exc:                       # noqa: BLE001 - setup failed
        log(f"setup failed: {type(exc).__name__}: {exc}")
        write_result(out_path, {"ok": False, "stage": "browser_error",
                                "submitted": False,
                                "error": f"{type(exc).__name__}: {exc}"})
        return 4


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
