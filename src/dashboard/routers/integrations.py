"""Integration dialogs shared by Settings and the setup wizard.

  GET  /ui/integration/{name}               guided dialog (admin only)
  POST /settings/integration/{name}/test    test the values typed in the dialog
  POST /settings/integration/telegram/chats "Find my chat id" (Bot API getUpdates)
  POST /settings/secret/remove              remove a saved secret (and what needs it)

The dialogs post their Save to the usual Settings endpoints (/settings/apikey,
/settings/ai, /settings/email, /settings/notify) with a `next` URL, so Settings
and /setup share one implementation. Tests run on the values typed (an empty
secret field means "the saved one"), before anything is saved; each result is
recorded with a fingerprint of those values (common.integration_health), so it
becomes the integration's status once exactly those values are saved.

Writes are admin-only (dashboard.main.role_middleware covers /settings/*);
secrets are never echoed back, logged or audited.
"""
import html
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from apify.client import ApifyClient
from common import integration_health
from dashboard import admin, ui
from dashboard import integrations as integ
from dashboard.context import (
    is_fragment_request,
    redirect_with_flash,
    safe_next,
    viewer,
)
from dashboard.deps import db
from dashboard.session import audit
from dashboard.ui_helpers import _esc, fragment_or_page
from notify import email_sender, ntfy, slack, telegram
from processing import ai_classifier, llm

router = APIRouter()

_TEST_TEXT = "Test from SERP Monitor: notifications work."


def default_next(name: str) -> str:
    return f"/settings#{integ.SECTION_OF.get(name, 'notifications')}"


def _next(name: str, raw: str | None) -> str:
    return safe_next(raw, default_next(name))


@router.get("/ui/integration/{name}", response_class=HTMLResponse, tags=["settings"])
async def ui_integration(name: str, next: str = "", step: str = "", provider: str = "",
                         preset: str = ""):
    if name not in integ.NAMES:
        return fragment_or_page("Not found", ui.overlay("Not found", "<div class='empty'>This "
                                "integration doesn't exist.</div>"), active="/settings",
                                status_code=404)
    if not viewer().is_admin:
        return fragment_or_page("Administrators only", ui.overlay(
            "Administrators only", "<div class='empty'>Only an administrator can change "
            "integrations.</div>"), active="/settings", status_code=403)
    inner = integ.modal_html(name, _next(name, next), step=step, provider=provider, preset=preset)
    return fragment_or_page(integ.INFO[name]["title"], inner, active="/settings")


# --------------------------------------------------------------------- #
# Tests of the typed values
# --------------------------------------------------------------------- #
def _answer(name: str, next_url: str, ok: bool, message: str, title: str | None = None):
    """A fragment for the dialog's result area, or (no JS) a redirect with a toast."""
    if is_fragment_request():
        return HTMLResponse(integ.result_html(ok, message, title),
                            headers={"Cache-Control": "no-store"})
    head = title or ("Test passed." if ok else "Test failed.")
    hint = "" if ok else llm.reasoning_hint(message)
    return redirect_with_flash(next_url, f"{head} {message}" + (f" {hint}" if hint else ""),
                               "ok" if ok else "error")


def _audit_test(request: Request, action: str, ok: bool, detail: str, secrets=(), **params):
    reason = "" if ok else " ".join(str(detail or "").split())[:300]
    for value in (*secrets, *(os.environ.get(k) or "" for k in os.environ
                              if any(t in k.upper() for t in ("KEY", "TOKEN", "PASS", "WEBHOOK", "NTFY")))):
        if value and len(value) >= 6:
            reason = reason.replace(value, "<secret>")
    audit(request, action, result="ok" if ok else "failed", reason=reason, draft=True, **params)


def _typed(form, field: str, env: str) -> str:
    return (form.get(field) or "").strip() or (os.getenv(env) or "").strip()


async def _test_apify(request, form):
    key = _typed(form, "apikey", "APIFY_API_KEY")
    if not key:
        return False, "Paste your Apify token first.", ()
    try:
        res = await run_in_threadpool(ApifyClient(api_key=key).check_account)
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "detail": str(exc)}
    ok = bool(res.get("ok"))
    msg = (f"Apify accepted the token (account {res.get('username') or 'found'})." if ok
           else admin.apify_check_reason(res.get("detail") or "") + ". Copy the token again and retry.")
    integration_health.record(db, "apify", integration_health.ident_for("apify", {"APIFY_API_KEY": key}),
                              ok, msg, (key,))
    _audit_test(request, "settings_apify_test", ok, msg, (key,))
    return ok, msg, (key,)


def _ai_ping(values: dict) -> tuple[bool, str]:
    with llm.config_override(values), llm.session_scope():
        return ai_classifier._call_llm("Reply with only: ok", max_tokens=llm.SHORT_OUTPUT_TOKENS)


async def _test_ai(request, form):
    values = integ.ai_candidate(dict(form))
    problem = integ.ai_problem(values)
    secrets = (values.get("GEMINI_API_KEY", ""), values.get("OPENAI_API_KEY", ""))
    if problem:
        return False, problem, secrets
    ok, out = await run_in_threadpool(_ai_ping, values)
    with llm.config_override(values):
        info = llm.provider_info()
    where = f" on {info['base_url']}" if info["provider"] == "openai" and info["base_url"] else ""
    msg = (f"The AI answered (model {info['model']}{where})." if ok else out)
    for secret in secrets:
        if secret:
            msg = msg.replace(secret, "<secret>")
    integration_health.record(db, "ai", integ.ai_ident(values), ok, msg, secrets)
    _audit_test(request, "settings_ai_test", ok, msg, secrets, provider=info["provider"],
                model=info["model"], host=info["base_url"])
    return ok, msg, secrets


def _email_config(form) -> dict:
    return {"SMTP_HOST": (form.get("host") or "").strip(),
            "SMTP_PORT": (form.get("port") or "").strip() or "465",
            "SMTP_USER": (form.get("user") or "").strip(),
            "SMTP_PASS": _typed(form, "password", "SMTP_PASS"),
            "SMTP_FROM": (form.get("sender") or "").strip(),
            "ALERT_EMAIL": (form.get("alert_to") or "").strip()}


async def _test_email(request, form):
    cfg = _email_config(form)
    missing = [label for key, label in (("SMTP_HOST", "the mail server"), ("SMTP_USER", "the user"),
                                        ("SMTP_PASS", "the password")) if not cfg[key]]
    if missing:
        return False, f"Fill in {' and '.join(missing)} first.", (cfg["SMTP_PASS"],)
    ok, detail = await run_in_threadpool(
        email_sender.send_email, "Test · SERP Monitor",
        "<h2>Email test</h2><p>SERP Monitor can send you alerts and reports by email.</p>",
        None, None, cfg)
    if ok:
        detail = f"{detail} Check the inbox (and the spam folder)."
    integration_health.record(db, "email", integration_health.ident_for("email", cfg), ok, detail,
                              (cfg["SMTP_PASS"],))
    db.add_notify_log(channel="email", ok=ok, error="" if ok else detail)
    _audit_test(request, "settings_email_test", ok, detail, (cfg["SMTP_PASS"],))
    return ok, detail, (cfg["SMTP_PASS"],)


async def _test_telegram(request, form):
    token = _typed(form, "telegram_token", "TELEGRAM_BOT_TOKEN")
    chat = (form.get("telegram_chat_id") or "").strip()
    if not token:
        return False, "Paste the bot token first (step 1).", ()
    if not chat:
        return False, "Find your chat id first (step 2), or type it.", (token,)
    ok, detail = await run_in_threadpool(telegram.send, _TEST_TEXT, chat, token)
    if ok:
        detail = "Message sent. Check Telegram."
    detail = detail.replace(token, "<secret>")
    integration_health.record(db, "telegram", integration_health.ident_for(
        "telegram", {"TELEGRAM_BOT_TOKEN": token}, chat), ok, detail, (token,))
    db.add_notify_log(channel="telegram", ok=ok, error="" if ok else detail)
    _audit_test(request, "settings_notify_test", ok, detail, (token,), channel="telegram")
    return ok, detail, (token,)


def _is_http(url: str) -> bool:
    return url.startswith(("http://", "https://")) and len(url.split("://", 1)[1]) > 0


async def _test_slack(request, form):
    url = _typed(form, "slack_webhook", "SLACK_WEBHOOK_URL")
    if not url:
        return False, "Paste the webhook URL first.", ()
    if not _is_http(url):
        return False, "The webhook URL must start with https://hooks.slack.com/…", (url,)
    ok, detail = await run_in_threadpool(slack.send, _TEST_TEXT, url)
    detail = "Message posted. Check the Slack channel." if ok else \
        f"Slack refused the message ({detail}). Check the webhook URL."
    integration_health.record(db, "slack", integration_health.ident_for(
        "slack", {"SLACK_WEBHOOK_URL": url}), ok, detail, (url,))
    db.add_notify_log(channel="slack", ok=ok, error="" if ok else detail.replace(url, "<secret>"))
    _audit_test(request, "settings_notify_test", ok, detail, (url,), channel="slack")
    return ok, detail, (url,)


def _ntfy_values(form) -> dict:
    mode = (form.get("ntfy_mode") or "topic").strip()
    if mode == "url":
        return {"NTFY_URL": _typed(form, "ntfy_url", "NTFY_URL"), "NTFY_TOPIC": ""}
    return {"NTFY_URL": "", "NTFY_TOPIC": _typed(form, "ntfy_topic", "NTFY_TOPIC")}


async def _test_ntfy(request, form):
    values = _ntfy_values(form)
    url = values["NTFY_URL"] or ntfy.topic_url(values["NTFY_TOPIC"])
    secrets = (values["NTFY_URL"], values["NTFY_TOPIC"])
    if not url:
        return False, "Enter a topic name (or your server's topic URL) first.", secrets
    if values["NTFY_URL"] and not _is_http(values["NTFY_URL"]):
        return False, "The topic URL must start with http:// or https://", secrets
    ok, detail = await run_in_threadpool(ntfy.send, _TEST_TEXT, url)
    detail = "Notification sent. Check the ntfy app." if ok else \
        f"ntfy refused the message ({detail})."
    integration_health.record(db, "ntfy", integration_health.ident_for("ntfy", values), ok, detail,
                              secrets)
    db.add_notify_log(channel="ntfy", ok=ok, error="" if ok else detail)
    _audit_test(request, "settings_notify_test", ok, detail, secrets, channel="ntfy")
    return ok, detail, secrets


_TESTS = {"apify": _test_apify, "ai": _test_ai, "email": _test_email, "telegram": _test_telegram,
          "slack": _test_slack, "ntfy": _test_ntfy}


@router.post("/settings/integration/{name}/test", tags=["settings"])
async def integration_test(request: Request, name: str):
    form = await request.form()
    next_url = _next(name, form.get("next"))
    if name not in _TESTS:
        return _answer(name, next_url, False, "Unknown integration.")
    ok, message, secrets = await _TESTS[name](request, form)
    for secret in secrets:
        if secret and len(secret) >= 4:
            message = message.replace(secret, "<secret>")
    return _answer(name, next_url, ok, message)


# --------------------------------------------------------------------- #
# Telegram: Find my chat id
# --------------------------------------------------------------------- #
_CHAT_TYPES = {"private": "Private chat", "group": "Group", "supergroup": "Group",
               "channel": "Channel"}


@router.post("/settings/integration/telegram/chats", tags=["settings"])
async def telegram_chats(request: Request):
    form = await request.form()
    next_url = _next("telegram", form.get("next"))
    token = _typed(form, "telegram_token", "TELEGRAM_BOT_TOKEN")
    ok, found = await run_in_threadpool(telegram.recent_chats, token)
    if not ok:
        message = str(found).replace(token, "<secret>") if token else str(found)
        return _answer("telegram", next_url, False, message, "Couldn't list chats.")
    if not found:
        return _answer("telegram", next_url, False,
                       "Open your bot in Telegram and press Start (or send it any message), then "
                       "click Find my chat id again.", "No chats yet.")
    if not is_fragment_request():
        names = "; ".join(f"{c['name']} ({c['id']})" for c in found[:5])
        return redirect_with_flash(next_url, f"Chats found: {names}. Type the id in Chat id.", "info")
    rows = "".join(
        "<li class='chat-pick'><div><b>" + _esc(c["name"]) + "</b><span class='muted'> · "
        + _esc(_CHAT_TYPES.get(c["type"], c["type"])) + f" · id {_esc(c['id'])}</span></div>"
        f"<button type='button' class='btn small' data-sm-fill='#tg-chat' data-value='{_esc(c['id'])}'>"
        "Use this chat</button></li>"
        for c in found[:10])
    return HTMLResponse(
        "<div class='test-result ok' role='status'><b>Pick your chat</b> "
        f"{len(found)} recent chat{'s' if len(found) != 1 else ''} with this bot:"
        f"<ul class='chat-list'>{rows}</ul></div>", headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------- #
# Remove a saved secret
# --------------------------------------------------------------------- #
@router.post("/settings/secret/remove", tags=["settings"])
async def remove_secret(request: Request):
    form = await request.form()
    key = (form.get("key") or "").strip()
    info = integ.REMOVABLE.get(key)
    if info is None:
        return redirect_with_flash("/settings", "Not removed: unknown setting.", "error")
    next_url = safe_next(form.get("next"), f"/settings#{info['section']}")
    if not integ.secret_saved(key):
        return redirect_with_flash(next_url, "Nothing to remove: it isn't saved.", "info")
    pairs = [(k, "") for k in info["clears"] if (os.getenv(k) or "")]
    try:
        admin.check_env_values(pairs)
    except ValueError as exc:
        return redirect_with_flash(next_url, f"Not removed: {exc}", "error")
    integ.write_env(pairs)
    if info.get("chat_id"):
        db.set_setting("telegram_chat_id", "")
    if key == "APIFY_API_KEY":
        import dashboard.main as main_module
        main_module._runner = None
    audit(request, "settings_secret_remove", field=key, cleared=",".join(k for k, _v in pairs))
    label = html.unescape(info["label"])
    return redirect_with_flash(next_url, f"Removed {label}. {info['effect']}")
