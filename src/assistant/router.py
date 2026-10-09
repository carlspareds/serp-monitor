"""Assistant HTTP API (JSON) + a minimal server-rendered panel fragment.

  GET  /assistant/status        enabled?, provider (no secrets), role, subject, last AI status
  POST /assistant/chat          {messages, context?, turn?} -> {messages, reply, events,
                                suggestions, followups, progress}
  GET  /assistant/progress      ?turn= -> {step, steps, done} while a chat turn runs
  POST /assistant/stop          {turn} -> the running turn stops before its next step
  POST /assistant/confirm       {id, args?} -> executes a pending action once (with the
                                card's edits, re-validated), audited
  POST /assistant/cancel        {id} -> discards it, audited as cancelled
  GET  /assistant/suggestions   proactive chips for the active subject
  GET  /assistant/panel         HTML fragment (greeting, chips, transcript, composer)
  GET  /assistant/reports/{f}   download a report file the assistant created

Auth/roles are the app's: auth_middleware/role_middleware/csrf_middleware in
dashboard.main run before these routes (role_middleware exempts only
/assistant/chat and /assistant/stop so read-only viewers can chat; their tool set excludes
actions, and /assistant/confirm re-checks the role). Everything runs on the
ACTIVE subject (database.base ContextVar). Module references (deps, session,
dashboard.main) are resolved at call time because the test suite reloads
those modules.
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
import secrets
import sys
import threading
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from assistant import cards, harness, suggest, tools
from assistant.pending import STORE
from dashboard import deps
from dashboard import session as session_mod
from database.base import current_subject_id
from processing import ai_status, llm

router = APIRouter(tags=["assistant"])
log = logging.getLogger("assistant")

DISABLED_MESSAGE = ("The assistant is off: set up an AI provider in Settings → AI provider "
                    "(Gemini or any OpenAI-compatible API).")
AUDIT_ACTION = "assistant_action"
AUDIT_CANCEL = "assistant_action_cancelled"
RATE_LIMIT_TURNS = 20
RATE_LIMIT_WINDOW_S = 60
_COOKIE = "asst_sid"
_NO_STORE = {"Cache-Control": "no-store"}
_rate: dict[str, deque] = defaultdict(deque)
REASONING_HINT = llm.REASONING_HINT

# Progress of running chat turns ("thinking" indicator + Stop), keyed by
# session key + the client's turn id. In memory, pruned after TURN_TTL_S.
TURN_TTL_S = 300
_TURN_ID = re.compile(r"^[A-Za-z0-9_-]{6,40}$")
_turns: dict[str, dict] = {}
_turns_lock = threading.Lock()


def _turn_slot(key: str, turn) -> str | None:
    if not isinstance(turn, str) or not _TURN_ID.match(turn):
        return None
    return f"{key}|{turn}"


def _turn_get(slot: str, create: bool = False) -> dict | None:
    now = time.monotonic()
    with _turns_lock:
        for k in [k for k, v in _turns.items() if now - v["t"] > TURN_TTL_S]:
            del _turns[k]
        entry = _turns.get(slot)
        if entry is None and create:
            entry = _turns[slot] = {"steps": [], "cancelled": False, "done": False, "t": now}
        return entry


def reasoning_hint(reason: str) -> str:
    """The hint shown next to an empty-answer failure (reasoning models)."""
    return llm.reasoning_hint(reason)


# --------------------------------------------------------------------- #
# Request context
# --------------------------------------------------------------------- #
def request_role(request: Request) -> str:
    """admin | operator | viewer. Auth disabled = the local single user = admin
    (same fail-open as dashboard.session.is_admin)."""
    if not session_mod.auth_enabled():
        return "admin"
    return {"admin": "admin", "operator": "operator"}.get(session_mod.session_role(request), "viewer")


def request_user(request: Request) -> str:
    return session_mod.session_user(request) or "local"


def session_key(request: Request) -> tuple[str, str | None]:
    """(key, new_cookie_value). Logged in: derived from the session cookie.
    Auth disabled: a per-browser assistant cookie, created on first use."""
    if session_mod.session_user(request):
        sid = request.cookies.get("sid") or ""
        return "sid:" + hashlib.sha256(sid.encode()).hexdigest()[:24], None
    token = request.cookies.get(_COOKIE) or ""
    if len(token) >= 16:
        return "anon:" + hashlib.sha256(token.encode()).hexdigest()[:24], None
    token = secrets.token_urlsafe(24)
    return "anon:" + hashlib.sha256(token.encode()).hexdigest()[:24], token


def build_context(request: Request, subject_id: int | None = None) -> tools.ToolContext:
    sid = subject_id if subject_id is not None else current_subject_id()
    main = sys.modules.get("dashboard.main")
    return tools.ToolContext(
        db=deps.db.for_subject(sid), subject_id=sid, role=request_role(request),
        user=request_user(request), jobs=getattr(main, "jobs", None),
        runner_factory=lambda: sys.modules["dashboard.main"].get_runner(),
        exports_dir=deps.exports_dir,
    )


def _json(payload: dict, status: int = 200, cookie: str | None = None) -> JSONResponse:
    resp = JSONResponse(harness.scrub_obj(payload), status_code=status, headers=_NO_STORE)
    if cookie:
        resp.set_cookie(_COOKIE, cookie, httponly=True, samesite="lax", max_age=7 * 24 * 3600)
    return resp


async def _body(request: Request) -> dict | None:
    try:
        data = await request.json()
    except ValueError:  # includes json.JSONDecodeError / UnicodeDecodeError
        return None
    return data if isinstance(data, dict) else None


def _rate_limited(key: str, now: float | None = None) -> bool:
    now = time.monotonic() if now is None else now
    q = _rate[key]
    while q and now - q[0] > RATE_LIMIT_WINDOW_S:
        q.popleft()
    if len(q) >= RATE_LIMIT_TURNS:
        return True
    q.append(now)
    return False


def _status_payload(request: Request) -> dict:
    info = llm.provider_info()
    reason = llm.not_configured_reason()
    role = request_role(request)
    model = harness.build_model()
    subject = deps.db.for_subject(current_subject_id()).current_subject()
    last = ai_status.status(deps.db) if not reason else None
    health = None
    if last is not None and not last["ok"]:
        health = {"ok": False, "reason": last["reason"], "at": last["at"],
                  "hint": reasoning_hint(last["reason"])}
    return {
        "enabled": not reason,
        "reason": reason,
        "message": "" if not reason else DISABLED_MESSAGE,
        "settings_url": "/settings#ai",
        "health": health,
        "provider": {"provider": info["provider"], "model": info["model"], "host": info["base_url"]},
        "tool_mode": model.tool_mode if model else None,
        "role": role,
        "can_act": tools.role_allows(role, "operator"),
        "subject": {"id": subject["id"], "name": subject["name"]},
    }


# --------------------------------------------------------------------- #
# JSON endpoints
# --------------------------------------------------------------------- #
@router.get("/assistant/status")
async def assistant_status(request: Request):
    return _json(await run_in_threadpool(_status_payload, request))


@router.get("/assistant/suggestions")
async def assistant_suggestions(request: Request):
    ctx = build_context(request)
    chips = await run_in_threadpool(suggest.compute, ctx)
    return _json({"suggestions": chips})


def _chat_sync(request: Request, messages: list, key: str, page_context=None,
               slot: str | None = None) -> dict:
    model = harness.build_model()
    ctx = build_context(request)
    entry = _turn_get(slot, create=True) if slot else None

    def on_step(label: str) -> None:
        if entry is not None:
            entry["steps"].append(label)

    def should_stop() -> bool:
        return bool(entry and entry["cancelled"])

    on_step("Looking at your subject…")
    overview = tools.get_overview(ctx, {})
    result = harness.run_turn(model, ctx, messages, session_key=key, overview=overview,
                              page_context=page_context, on_step=on_step, should_stop=should_stop)
    if entry is not None:
        entry["done"] = True
    transcript = harness.clean_messages(messages)
    if result.stopped == "cancelled":
        return {"messages": transcript, "reply": "", "events": [], "suggestions": [],
                "followups": [], "progress": result.progress, "stopped": True, "error": None}
    transcript.append({"role": "assistant", "content": result.reply})
    try:
        chips = suggest.compute(ctx, overview)
    except Exception:
        log.exception("assistant_suggestions_failed")
        chips = []
    try:
        last_user = next((m["content"] for m in reversed(transcript) if m["role"] == "user"), "")
        followups = suggest.follow_ups(ctx, result.trace, result.events, last_user, overview)
    except Exception:
        log.exception("assistant_followups_failed")
        followups = []
    log.info("assistant_turn", extra={"steps": result.steps, "stopped": result.stopped,
                                      "tools": [t["name"] for t in result.trace]})
    hint = reasoning_hint(result.error or "")
    return {"messages": transcript, "reply": result.reply, "events": result.events,
            "suggestions": chips, "followups": followups, "progress": result.progress,
            "error": result.error, "hint": hint}


@router.post("/assistant/chat")
async def assistant_chat(request: Request):
    if harness.build_model() is None:
        return _json({"enabled": False, "error": "assistant_disabled",
                      "message": DISABLED_MESSAGE, "settings_url": "/settings#ai"}, 503)
    body = await _body(request)
    if body is None or not isinstance(body.get("messages"), list):
        return _json({"error": "bad_request", "message": "Send {messages: [{role, content}]}."}, 400)
    key, cookie = session_key(request)
    if _rate_limited(key):
        return _json({"error": "rate_limited",
                      "message": f"Too many messages: at most {RATE_LIMIT_TURNS} per minute."}, 429)
    slot = _turn_slot(key, body.get("turn"))
    payload = await run_in_threadpool(_chat_sync, request, body["messages"], key,
                                      body.get("context"), slot)
    return _json(payload, cookie=cookie)


@router.get("/assistant/progress")
async def assistant_progress(request: Request, turn: str = ""):
    key, _ = session_key(request)
    slot = _turn_slot(key, turn)
    entry = _turn_get(slot) if slot else None
    if entry is None:
        return _json({"step": "", "steps": [], "done": False})
    steps = list(entry["steps"])
    return _json({"step": steps[-1] if steps else "", "steps": steps, "done": entry["done"]})


@router.post("/assistant/stop")
async def assistant_stop(request: Request):
    body = await _body(request) or {}
    key, _ = session_key(request)
    slot = _turn_slot(key, body.get("turn"))
    entry = _turn_get(slot, create=True) if slot else None
    if entry is None:
        return _json({"ok": False, "error": "bad_request", "message": "Send {turn}."}, 400)
    entry["cancelled"] = True
    return _json({"ok": True})


def _lookup(request: Request, action_id) -> tuple[object | None, JSONResponse | None]:
    action = STORE.get(str(action_id or ""))
    if action is None:
        return None, _json({"error": "not_found", "message": "This proposal doesn't exist or has expired."}, 404)
    key, _ = session_key(request)
    if action.session_key != key:
        return None, _json({"error": "forbidden", "message": "This proposal belongs to another session."}, 403)
    return action, None


def _audit(request: Request, action, name: str, ok: bool, message: str, resource=None) -> None:
    tool = tools.get_tool(action.tool)
    rtype, rid = resource or ("assistant_tool", action.tool)
    extra = {"edited": True} if getattr(action, "edited", False) else {}
    session_mod.audit(request, name, resource_type=rtype, resource_id=rid,
                      tool=action.tool, subject_id=action.subject_id, pending_id=action.id,
                      args=tools.sanitize_args(tool, action.args), ok=ok, message=message[:300],
                      **extra)


def _edits_sync(request: Request, action, edits) -> tuple[bool, dict | str]:
    """The card's edits re-validated like a new proposal: (True, args) or
    (False, plain message)."""
    tool = tools.get_tool(action.tool)
    ctx = build_context(request, subject_id=action.subject_id)
    try:
        return True, tools.apply_edits(ctx, tool, dict(action.args), edits, action.edit)
    except tools.ToolError as e:
        return False, str(e)
    except Exception:
        log.exception("assistant_edit_failed", extra={"tool": action.tool})
        return False, "Those changes couldn't be checked. Try again."


def _execute_sync(request: Request, action) -> tuple[bool, dict]:
    tool = tools.get_tool(action.tool)
    ctx = build_context(request, subject_id=action.subject_id)
    try:
        result = tool.executor(ctx, dict(action.args))
        resource = result.pop("resource", None)
        _audit(request, action, AUDIT_ACTION, True, result.get("message", ""), resource)
        return True, result
    except tools.ToolError as e:
        _audit(request, action, AUDIT_ACTION, False, str(e))
        return False, {"message": f"Couldn't do it: {e}"}
    except Exception:
        log.exception("assistant_action_failed", extra={"tool": action.tool})
        _audit(request, action, AUDIT_ACTION, False, "internal error")
        return False, {"message": "Something went wrong; nothing may have changed. Check the page and try again."}


@router.post("/assistant/confirm")
async def assistant_confirm(request: Request):
    body = await _body(request) or {}
    action, err = _lookup(request, body.get("id"))
    if err:
        return err
    tool = tools.get_tool(action.tool)
    role = request_role(request)
    if tool is None or not tools.role_allows(role, tool.min_role):
        return _json({"error": "forbidden", "message": "Your role can't perform this action."}, 403)
    if action.subject_id != current_subject_id():
        return _json({"error": "subject_changed",
                      "message": "You switched subject since this was proposed. Ask again."}, 409)
    edited_args = None
    edits = body.get("args")
    if edits is not None and action.status == "pending":
        ok, value = await run_in_threadpool(_edits_sync, request, action, edits)
        if not ok:
            return _json({"error": "invalid_args", "message": f"Not done: {value}"}, 400)
        edited_args = value
    state, action = STORE.claim(action.id)
    if state == "expired":
        return _json({"error": "expired", "message": "This proposal expired. Ask again."}, 404)
    if state == "cancelled":
        return _json({"error": "cancelled", "message": "This proposal was cancelled."}, 409)
    if state == "executing":
        return _json({"error": "in_progress", "message": "Already running."}, 409)
    if state in ("done", "failed"):
        res = dict(action.result or {})
        return _json({"ok": state == "done", "already": True, "id": action.id,
                      "message": res.get("message", ""), "result": res,
                      "events": [dict(type="intent", tool=action.tool, **res["intent"])]
                      if res.get("intent") else []})
    if edited_args is not None and edited_args != action.args:
        action.args, action.edited = edited_args, True
    ok, result = await run_in_threadpool(_execute_sync, request, action)
    STORE.finish(action, ok, result)
    events = [dict(type="intent", tool=action.tool, **result["intent"])] if result.get("intent") else []
    card = cards.report_card(result) if ok and action.tool == "create_report" else None
    if card:
        events.append(card)
    return _json({"ok": ok, "already": False, "id": action.id, "message": result.get("message", ""),
                  "result": result, "events": events, "edited": action.edited})


@router.post("/assistant/cancel")
async def assistant_cancel(request: Request):
    body = await _body(request) or {}
    action, err = _lookup(request, body.get("id"))
    if err:
        return err
    state, action = STORE.cancel(action.id)
    if state != "cancelled":
        return _json({"ok": False, "error": state,
                      "message": "Already handled; nothing to cancel."}, 409)
    await run_in_threadpool(_audit, request, action, AUDIT_CANCEL, True, "cancelled by user")
    return _json({"ok": True, "id": action.id, "message": "Cancelled. Nothing was changed."})


@router.get("/assistant/reports/{name}")
async def assistant_report_file(name: str):
    """A report of the ACTIVE subject (export.reports store)."""
    from database.base import current_subject_id
    from export import reports as store

    path = store.find_report(deps.exports_dir, current_subject_id(), name)
    if path is None:
        return _json({"error": "not_found"}, 404)
    media = {"pdf": "application/pdf", "csv": "text/csv",
             "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    return FileResponse(str(path), media_type=media[path.suffix.lstrip(".")], filename=path.name)


# --------------------------------------------------------------------- #
# Panel fragment
# --------------------------------------------------------------------- #
def _e(v) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


def _greeting_html(text: str) -> str:
    lines = text.split("\n")
    out, bullets = [], []
    for line in lines:
        if line.startswith("• "):
            bullets.append(f"<li>{_e(line[2:])}</li>")
            continue
        if bullets:
            out.append(f"<ul>{''.join(bullets)}</ul>")
            bullets = []
        out.append(f"<p>{_e(line)}</p>")
    if bullets:
        out.append(f"<ul>{''.join(bullets)}</ul>")
    return "".join(out)


_CONFIRM_TEMPLATE = (
    "<template id='assistant-confirm-template'>"
    "<div class='assistant-confirm' data-assistant-confirm-card data-confirm-id=''>"
    "<p class='assistant-confirm-title' data-confirm-title></p>"
    "<ul class='assistant-confirm-details' data-confirm-details></ul>"
    "<p class='assistant-confirm-note' data-confirm-note></p>"
    "<div class='assistant-confirm-actions'>"
    "<button type='button' class='assistant-btn assistant-btn-primary' data-assistant-confirm>Confirm</button>"
    "<button type='button' class='assistant-btn assistant-btn-ghost' data-assistant-cancel>Cancel</button>"
    "</div></div></template>"
)


_ICON = ("<svg viewBox='0 0 24 24' width='18' height='18' fill='none' stroke='currentColor' "
         "stroke-width='2' stroke-linecap='round' stroke-linejoin='round' aria-hidden='true'>{}</svg>")
_CLOSE_ICON = _ICON.format("<path d='M6 6l12 12M18 6L6 18'/>")
_MIN_ICON = _ICON.format("<path d='M6 12h12'/>")
_CLEAR_ICON = _ICON.format("<path d='M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3'/>")
_SPARK = ("<svg class='ico' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.8' "
          "stroke-linecap='round' aria-hidden='true'><path d='M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 "
          "2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6'/></svg>")


def _settings_link(role: str, text: str = "Settings → AI provider") -> str:
    return (f"<a href='/settings#ai' data-assistant-setup>{_e(text)}</a>" if role == "admin"
            else f"{_e(text)} (ask an admin)")


def _panel_sync(request: Request) -> str:
    st = _status_payload(request)
    attrs = (f"data-enabled='{str(st['enabled']).lower()}' data-role='{_e(st['role'])}' "
             f"data-can-act='{str(st['can_act']).lower()}' "
             f"data-subject-id='{_e(st['subject']['id'])}' data-subject-name='{_e(st['subject']['name'])}' "
             "data-chat-url='/assistant/chat' data-confirm-url='/assistant/confirm' "
             "data-cancel-url='/assistant/cancel' data-suggestions-url='/assistant/suggestions' "
             "data-progress-url='/assistant/progress' data-stop-url='/assistant/stop' "
             "data-status-url='/assistant/status'")
    # `ov-head` + data-sm-title: static/app.js uses this header as the panel's
    # own. The buttons are the assistant controller's (it is not an SM overlay:
    # docked on desktop, a sheet that minimizes to a bubble on phones).
    tools_html = (
        ("<button type='button' class='iconbtn' data-assistant-clear aria-label='Clear this "
         f"conversation' title='Clear conversation'>{_CLEAR_ICON}</button>" if st["enabled"] else "")
        + "<button type='button' class='iconbtn' data-assistant-minimize aria-label='Minimize the "
        f"assistant' title='Minimize (Esc)'>{_MIN_ICON}</button>"
        "<button type='button' class='iconbtn' data-assistant-close aria-label='Close the assistant' "
        f"title='Close'>{_CLOSE_ICON}</button>")
    head = ("<header class='assistant-head ov-head'><h2 class='assistant-title' id='assistant-title' "
            f"data-sm-title><span class='assistant-spark'>{_SPARK}</span>Assistant</h2>"
            f"<div class='assistant-tools'>{tools_html}</div>"
            + (f"<p class='assistant-provider muted'>{_e(st['provider']['provider'])} · "
               f"{_e(st['provider']['model'])} · <span class='kbd'>/</span> or "
               "<span class='kbd'>Ctrl K</span> to open</p>" if st["enabled"] else "")
            + "</header>")
    if not st["enabled"]:
        body = ("<div class='assistant-transcript'><div class='assistant-card assistant-card-off' "
                "data-assistant-disabled role='status'>"
                "<p class='assistant-card-title'>The assistant is off</p>"
                f"<p>It needs an AI provider. Set one up in {_settings_link(st['role'])}.</p>"
                "<p class='muted'>Gemini or any OpenAI-compatible API works, including a local "
                "Ollama or LM Studio server. Everything else in the app works without it.</p>"
                + ("<p><a class='btn primary small' href='/settings#ai'>Set up an AI provider</a></p>"
                   if st["role"] == "admin" else "")
                + "</div></div>")
        return (f"<section class='assistant-panel' id='assistant-panel' data-assistant {attrs} "
                f"aria-labelledby='assistant-title'>{head}{body}</section>")
    ctx = build_context(request)
    overview = tools.get_overview(ctx, {})
    greet = suggest.greeting(ctx, overview)
    chips = suggest.compute(ctx, overview)
    chips_html = "".join(
        f"<button type='button' class='assistant-chip' data-assistant-chip "
        f"data-chip-id='{_e(c['id'])}' data-prompt='{_e(c['prompt'])}'>{_e(c['label'])}</button>"
        for c in chips)
    health = ""
    if st["health"]:
        h = st["health"]
        hint = f"<p>{_e(h['hint'])}</p>" if h["hint"] else ""
        health = ("<div class='assistant-card assistant-card-warn' data-assistant-health role='status'>"
                  "<p class='assistant-card-title'>The last AI call failed</p>"
                  f"<p>{_e(h['reason'])}</p>{hint}<p>Check it in {_settings_link(st['role'])}.</p></div>")
    return (
        f"<section class='assistant-panel' id='assistant-panel' data-assistant {attrs} "
        f"aria-labelledby='assistant-title'>{head}"
        "<div class='assistant-transcript' id='assistant-transcript' data-assistant-transcript "
        "role='log' aria-live='polite'>"
        f"<div class='assistant-msg assistant-msg-bot' data-msg-role='assistant' data-greeting>"
        f"{_greeting_html(greet)}</div>{health}</div>"
        f"<div class='assistant-chips' id='assistant-chips' data-assistant-chips>{chips_html}</div>"
        "<form class='assistant-composer' id='assistant-composer' data-assistant-composer "
        "action='/assistant/chat' method='post' autocomplete='off'>"
        "<label class='visually-hidden' for='assistant-input'>Message the assistant</label>"
        "<textarea id='assistant-input' name='message' rows='1' maxlength='4000' data-assistant-input "
        "placeholder='Ask about results or next steps'></textarea>"
        "<button type='submit' class='assistant-send' data-assistant-send>Send</button>"
        "<button type='button' class='assistant-stop' data-assistant-stop hidden>Stop</button></form>"
        f"{_CONFIRM_TEMPLATE}</section>"
    )


@router.get("/assistant/panel", response_class=HTMLResponse)
async def assistant_panel(request: Request):
    markup = await run_in_threadpool(_panel_sync, request)
    resp = HTMLResponse(harness.scrub_secrets(markup), headers=_NO_STORE)
    # Auth disabled: give the browser its assistant session now, so the first
    # turn's progress polls and Stop match the chat request.
    _key, cookie = session_key(request)
    if cookie:
        resp.set_cookie(_COOKIE, cookie, httponly=True, samesite="lax", max_age=7 * 24 * 3600)
    return resp
