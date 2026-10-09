"""Settings building blocks shared by /settings and the setup wizard.

One implementation of every integration's setup, used everywhere:

- `section_head` / `section_card`: the header pattern of every Settings
  section (title, one plain sentence, a status pill, ONE primary action) with
  details collapsed below.
- `integration_row`: an integration (Search/Apify, AI provider, Email,
  Telegram, Slack, ntfy) as a list item with its status and a Set up / Change
  button. Settings and the wizard's "Integrations" step render the same rows.
- `modal_html(name, ...)`: the guided dialog of each integration (opened from
  those buttons as `/ui/integration/<name>`): only the fields of what was
  chosen, an in-dialog Test of the values typed (before saving), then Save.
- `secret_field`: a saved secret is never an empty input and never echoed:
  "•••• saved (updated <date>)" with Replace (reveals an input) and Remove
  (app confirmation dialog).
- `integration_state`: Working / Failing: <reason> / Not tested / Not set up /
  Optional, from the same "configured" checks the senders use plus the last
  real result (common.integration_health, processing.ai_status).

Every string here is plain and short; verbs are consistent: Set up, Change,
Test, Save, Remove.
"""
from __future__ import annotations

import json
import os
import secrets as _secrets
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from common import integration_health
from common.config_check import ENV_VARS
from dashboard import admin, ui
from dashboard.deps import db
from dashboard.ui_helpers import _env_file, _esc, fmt_day
from notify import channel_status, email_sender
from processing import ai_status, llm

NAMES = ("apify", "ai", "email", "telegram", "slack", "ntfy")
CHANNELS = ("email", "telegram", "slack", "ntfy")

INFO: dict[str, dict] = {
    "apify": {"title": "Search (Apify)", "short": "Apify",
              "purpose": "Runs the Google and Bing searches. Needed for real results."},
    "ai": {"title": "AI provider", "short": "AI provider",
           "purpose": "Spots people with the same name and scores how harmful a result is. "
                      "Optional: without it, simple rules label the results."},
    "email": {"title": "Email", "short": "Email",
              "purpose": "Sends alerts, the daily digest and the weekly report by email."},
    "telegram": {"title": "Telegram", "short": "Telegram",
                 "purpose": "Sends alerts to you or a group through your own Telegram bot."},
    "slack": {"title": "Slack", "short": "Slack",
              "purpose": "Posts alerts to a Slack channel."},
    "ntfy": {"title": "ntfy", "short": "ntfy",
             "purpose": "Push notifications on your phone with the free ntfy app."},
}

SECTION_OF = {"apify": "search", "ai": "ai", "email": "notifications", "telegram": "notifications",
              "slack": "notifications", "ntfy": "notifications"}

PROVIDER_LABELS = {"gemini": "Gemini", "openai": "OpenAI-compatible"}

# OpenAI-compatible presets: base URL, model placeholder, whether a key is
# needed, and the helper texts shown for that choice.
AI_PRESETS: dict[str, dict] = {
    "openai": {"label": "OpenAI", "base": "https://api.openai.com/v1", "model": "gpt-4o-mini",
               "key": "required",
               "key_help": "Create one at platform.openai.com/api-keys. It starts with sk-.",
               "model_help": "Leave empty for gpt-4o-mini, or enter another model, e.g. gpt-4.1-mini.",
               "note": "OpenAI's own API. Billed by OpenAI."},
    "openrouter": {"label": "OpenRouter", "base": "https://openrouter.ai/api/v1",
                   "model": "openai/gpt-4o-mini", "key": "required",
                   "key_help": "Create one at openrouter.ai/keys. It starts with sk-or-.",
                   "model_help": "The model id as OpenRouter shows it, e.g. openai/gpt-4o-mini.",
                   "note": "One key for many models from different companies."},
    "opencode": {"label": "OpenCode Go", "base": "https://opencode.ai/zen/go/v1",
                 "model": "kimi-k2", "key": "required",
                 "key_help": "Your API key from opencode.ai. Needs an active OpenCode Go plan.",
                 "model_help": "A model id included in your plan, e.g. kimi-k2.",
                 "note": "OpenCode's subscription plan for open models."},
    "ollama": {"label": "Ollama", "base": "http://localhost:11434/v1", "model": "llama3.1",
               "key": "none",
               "key_help": "Not needed for Ollama on your own computer.",
               "model_help": "A model you pulled, e.g. llama3.1 (run: ollama pull llama3.1).",
               "note": "Runs on your own computer: results never leave it. Start Ollama first."},
    "lmstudio": {"label": "LM Studio", "base": "http://localhost:1234/v1",
                 "model": "qwen2.5-7b-instruct", "key": "none",
                 "key_help": "Not needed for LM Studio's local server.",
                 "model_help": "The model id LM Studio shows for the loaded model.",
                 "note": "Runs on your own computer. Start LM Studio's local server and load a model."},
    "custom": {"label": "Custom", "base": "", "model": "your-model-name", "key": "optional",
               "key_help": "Only if your server asks for one.",
               "model_help": "Required: the model name your server serves.",
               "note": "Any server with an OpenAI-style chat API. Include the version path, "
                       "e.g. https://ai.example.com/v1."},
}

EMAIL_PRESETS: dict[str, dict] = {
    "gmail": {"label": "Gmail", "host": "smtp.gmail.com", "port": "465",
              "pass_help": "Use an app password, not your normal password: create one at "
                           "myaccount.google.com/apppasswords (needs 2-Step Verification).",
              "user_help": "Your Gmail address, e.g. alerts@gmail.com."},
    "outlook": {"label": "Outlook / Office 365", "host": "smtp.office365.com", "port": "587",
                "pass_help": "Your account password (or an app password if your account uses "
                             "two-step sign-in). Work accounts may need SMTP sign-in turned on "
                             "by an admin.",
                "user_help": "Your full email address, e.g. alerts@yourcompany.com."},
    "custom": {"label": "Other", "host": "", "port": "465",
               "pass_help": "The password for the user above.",
               "user_help": "Usually your full email address."},
}

# Secrets with a Remove button: what removing one means (several variables
# go together when the integration can't work without the secret).
REMOVABLE: dict[str, dict] = {
    "APIFY_API_KEY": {"label": "the Apify token", "clears": ("APIFY_API_KEY",), "section": "search",
                      "effect": "Searches stop until you add a token again."},
    "GEMINI_API_KEY": {"label": "the Gemini API key", "clears": ("GEMINI_API_KEY",), "section": "ai",
                       "effect": "AI checks stop while Gemini is the provider."},
    "OPENAI_API_KEY": {"label": "the API key", "clears": ("OPENAI_API_KEY",), "section": "ai",
                       "effect": "Servers that need a key stop answering."},
    "SMTP_PASS": {"label": "the email password",
                  "clears": ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM",
                             "ALERT_EMAIL"),
                  "section": "notifications",
                  "effect": "Email turns off: server, user and password are all needed. "
                            "The email settings are cleared."},
    "TELEGRAM_BOT_TOKEN": {"label": "the Telegram bot token", "clears": ("TELEGRAM_BOT_TOKEN",),
                           "chat_id": True, "section": "notifications",
                           "effect": "Telegram turns off and the chat id is cleared."},
    "SLACK_WEBHOOK_URL": {"label": "the Slack webhook", "clears": ("SLACK_WEBHOOK_URL",),
                          "section": "notifications", "effect": "Slack turns off."},
    "NTFY_URL": {"label": "the ntfy server URL", "clears": ("NTFY_URL",),
                 "section": "notifications", "effect": "ntfy stops unless a public topic is set."},
    "NTFY_TOPIC": {"label": "the ntfy topic", "clears": ("NTFY_TOPIC",),
                   "section": "notifications", "effect": "ntfy stops unless a server URL is set."},
}

_SECRET_DATE = "secret_saved_at:"


# --------------------------------------------------------------------- #
# Small components
# --------------------------------------------------------------------- #
_PILL_CLASS = {"ok": "ok", "fail": "negative", "warn": "warning", "neutral": "neutral",
               "info": "info"}


def pill(state: str, label: str) -> str:
    cls = _PILL_CLASS.get(state, "neutral")
    return (f"<span class='pill {cls}' data-status='{_esc(state)}'><span class='dot' "
            f"aria-hidden='true'></span>{_esc(label)}</span>")


def section_head(title: str, purpose: str, pill_html: str = "", action_html: str = "",
                 level: int = 2) -> str:
    """Title + status pill, one plain sentence, ONE primary action."""
    action = f"<div class='sec-action'>{action_html}</div>" if action_html else ""
    return (f"<div class='sec-head'><div class='sec-text'><div class='sec-title'>"
            f"<h{level}>{_esc(title)}</h{level}>{pill_html}</div>"
            f"<p class='sec-purpose'>{purpose}</p></div>{action}</div>")


def details(label: str, inner: str, open_: bool = False, did: str = "") -> str:
    ident = f" id='{_esc(did)}'" if did else ""
    return (f"<details class='sec-more'{ident}{' open' if open_ else ''}><summary>{_esc(label)}"
            f"</summary><div class='sec-more-body'>{inner}</div></details>")


def section_card(title: str, purpose: str, body: str = "", pill_html: str = "",
                 action_html: str = "", more: str = "", more_label: str = "Details",
                 extra_cls: str = "") -> str:
    more_html = details(more_label, more) if more else ""
    return (f"<div class='card sec-card{(' ' + extra_cls) if extra_cls else ''}'>"
            f"{section_head(title, purpose, pill_html, action_html)}{body}{more_html}</div>")


def field(label: str, control: str, help_html: str = "", fid: str = "", cls: str = "",
          label_id: str = "", help_id: str = "") -> str:
    lid = f" id='{label_id}'" if label_id else ""
    hid = f" id='{help_id}'" if help_id else ""
    help_part = f"<span class='help'{hid}>{help_html}</span>" if (help_html or help_id) else ""
    return (f"<div class='field{(' ' + cls) if cls else ''}'><label for='{fid}'{lid}>{label}</label>"
            f"{control}{help_part}</div>")


def text_input(fid: str, name: str, value: str = "", placeholder: str = "", type_: str = "text",
               extra: str = "") -> str:
    ph = f" placeholder='{_esc(placeholder)}'" if placeholder else ""
    return (f"<input id='{fid}' type='{type_}' name='{name}' value='{_esc(value)}'{ph}"
            f" autocomplete='off'{(' ' + extra) if extra else ''}>")


def stepper(steps: list[str], current: int) -> str:
    """Numbered steps at the top of a guided dialog (current is 1-based)."""
    items = []
    for i, label in enumerate(steps, 1):
        cls = "done" if i < current else ("on" if i == current else "")
        aria = " aria-current='step'" if i == current else ""
        items.append(f"<li class='{cls}'{aria}><span class='n'>{i}</span>{_esc(label)}</li>")
    return f"<ol class='stepper' aria-label='Steps'>{''.join(items)}</ol>"


def step_block(n: int, title: str, inner: str) -> str:
    return (f"<div class='gstep'><div class='gstep-n' aria-hidden='true'>{n}</div>"
            f"<div class='gstep-body'><h3>{title}</h3>{inner}</div></div>")


def ext_link(url: str, label: str | None = None) -> str:
    return (f"<a href='{_esc(url)}' target='_blank' rel='noopener noreferrer'>"
            f"{_esc(label or url.split('://', 1)[-1])}</a>")


def result_html(ok: bool, message: str, title: str | None = None) -> str:
    """Outcome of a Test (or of Find my chat id) shown inside the dialog. An
    empty answer from a reasoning model gets a plain hint."""
    head = title or ("Test passed" if ok else "Test failed")
    hint = "" if ok else llm.reasoning_hint(message)
    hint_html = f"<p class='test-hint' data-reasoning-hint>{_esc(hint)}</p>" if hint else ""
    return (f"<div class='test-result {'ok' if ok else 'fail'}' role='status' "
            f"data-test-ok='{1 if ok else 0}'><b>{_esc(head)}</b> {_esc(message)}{hint_html}</div>")


def test_slot(name: str) -> str:
    return f"<div class='test-slot' id='test-{name}' aria-live='polite'></div>"


def test_button(name: str, form_id: str, label: str = "Test") -> str:
    url = f"/settings/integration/{name}/test"
    return (f"<button type='submit' class='btn' form='{form_id}' formaction='{url}' "
            f"data-sm-action='{url}' data-sm-target='#test-{name}' data-busy-label='Testing…'>"
            f"{_esc(label)}</button>")


# --------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------- #
def secret_saved(env: str) -> bool:
    return bool((os.getenv(env) or "").strip())


def secret_date(env: str) -> str:
    raw = db.get_setting(_SECRET_DATE + env, "")
    return fmt_day(raw) if raw else ""


def write_env(pairs) -> None:
    """Writes (name, value) pairs to the env file (and os.environ) and
    remembers when each secret was last saved (shown as "updated <date>")."""
    path = _env_file()
    for key, value in pairs:
        admin.set_env(path, key, value)
        if ENV_VARS.get(key):
            db.set_setting(_SECRET_DATE + key,
                           datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S") if value else "")


def remove_form_id(env: str) -> str:
    return f"rm-{env.lower().replace('_', '-')}"


def remove_form(env: str, next_url: str) -> str:
    """The hidden form a secret's Remove button submits (outside the dialog's
    main form: forms can't nest)."""
    return (f"<form id='{remove_form_id(env)}' method='post' action='/settings/secret/remove' "
            "data-sm-submit hidden>"
            f"<input type='hidden' name='key' value='{_esc(env)}'>"
            f"<input type='hidden' name='next' value='{_esc(next_url)}'></form>")


def secret_field(env: str, name: str, label: str, fid: str, help_html: str = "",
                 placeholder: str = "", label_id: str = "", help_id: str = "",
                 removable: bool = True, autofocus: bool = False) -> str:
    """A secret input. Saved: never an empty box and never the value, but
    "•••• saved (updated <date>)" with Replace / Remove. Not saved: the input."""
    lid = f" id='{label_id}'" if label_id else ""
    hid = f" id='{help_id}'" if help_id else ""
    help_part = f"<span class='help'{hid}>{help_html}</span>"
    ph = f" placeholder='{_esc(placeholder)}'" if placeholder else ""
    focus = " autofocus" if autofocus and not secret_saved(env) else ""
    control = (f"<input id='{fid}' type='password' name='{name}' autocomplete='off' "
               f"spellcheck='false'{ph}{focus}>")
    if not secret_saved(env):
        return (f"<div class='field secret' data-secret data-state='empty'>"
                f"<label for='{fid}'{lid}>{label}</label>{control}{help_part}</div>")
    when = secret_date(env)
    updated = f" <span class='muted'>(updated {_esc(when)})</span>" if when else ""
    remove = ""
    if removable and env in REMOVABLE:
        info = REMOVABLE[env]
        remove = (f"<button type='submit' class='btn ghost small danger-text' "
                  f"form='{remove_form_id(env)}' "
                  f"data-sm-confirm='{_esc(info['effect'])}' "
                  f"data-confirm-title='Remove {_esc(info['label'])}?' "
                  f"data-confirm-label='Remove'>Remove</button>")
    return (f"<div class='field secret' data-secret data-state='saved'>"
            f"<label for='{fid}'{lid}>{label}</label>"
            f"<div class='secret-saved'><span class='secret-mask' aria-hidden='true'>••••</span>"
            f"<span>Saved{updated}</span>"
            f"<span class='secret-btns'><button type='button' class='btn small' data-secret-replace "
            f"aria-controls='{fid}'>Replace</button>{remove}</span></div>"
            f"<div class='secret-input'>{control}<button type='button' class='btn ghost small' "
            f"data-secret-keep>Keep the saved one</button></div>{help_part}</div>")


# --------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------- #
def _when(at: str) -> str:
    """'2026-10-09T14:03:00' -> '9 Oct 14:03 UTC' (stored times are naive UTC;
    day only when there's no time)."""
    if not at:
        return ""
    day = fmt_day(at, year=False)
    hhmm = str(at)[11:16]
    return f"{day} {hhmm} UTC" if len(hhmm) == 5 and hhmm[2] == ":" else day


def working_text(at: str) -> str:
    """The ONE wording for a passing integration: "Working · tested <time>"
    (card meta line, and the dialog after a successful test)."""
    when = _when(at)
    return f"Working · tested {when}" if when else "Working"


def _apify_summary() -> str:
    return "Token saved." if secret_saved("APIFY_API_KEY") else "No token yet: searches can't run."


def ai_summary() -> str:
    info = llm.provider_info()
    if not info["provider"]:
        return "Not set up: results are labelled by simple rules."
    label = PROVIDER_LABELS.get(info["provider"], info["provider"])
    if info["provider"] == "openai":
        preset = AI_PRESETS.get(detect_preset(), {}).get("label", "")
        where = f" via {preset}" if preset and preset not in ("Custom",) else \
            (f" on {info['base_url']}" if info["base_url"] else "")
        return f"{label}{where} · model {info['model'] or 'not set'}"
    return f"{label} · model {info['model']}"


def _email_summary() -> str:
    cfg = email_sender.config_summary()
    if not email_sender.is_configured():
        return ""
    to = cfg["to"] or cfg["user"]
    return f"Through {cfg['host']} · alerts to {to}"


def _telegram_summary() -> str:
    chat = db.get_setting("telegram_chat_id", "")
    token = secret_saved("TELEGRAM_BOT_TOKEN")
    if token and chat:
        return f"Bot saved · chat {chat}"
    if token:
        return "Bot saved, but no chat id yet: nothing is sent."
    if chat:
        return f"Chat {chat}, but no bot token: nothing is sent."
    return ""


def _ntfy_summary() -> str:
    url, topic = (os.getenv("NTFY_URL") or "").strip(), (os.getenv("NTFY_TOPIC") or "").strip()
    if url:
        return f"Your server: {urlsplit(url).hostname or 'saved'}"
    if topic:
        return "Public topic on ntfy.sh (name saved)."
    return ""


def configured(name: str) -> bool:
    if name == "apify":
        return secret_saved("APIFY_API_KEY")
    if name == "ai":
        return llm.is_configured()
    return bool(channel_status(db).get(name))


def _incomplete(name: str) -> str:
    """Plain reason when something was saved but the integration can't work."""
    if name == "ai":
        p = llm.provider()
        if p and not llm.is_configured():
            return llm.not_configured_reason().replace(llm.NOT_CONFIGURED, "").strip() or \
                "Some settings are missing."
        return ""
    if name == "telegram":
        token = secret_saved("TELEGRAM_BOT_TOKEN")
        chat = bool(db.get_setting("telegram_chat_id", ""))
        if token != chat:
            return "Needs both the bot token and the chat id."
    return ""


def integration_state(name: str) -> dict:
    """{state: ok|fail|warn|neutral, label, reason, configured, summary}."""
    summaries = {"apify": _apify_summary, "ai": ai_summary, "email": _email_summary,
                 "telegram": _telegram_summary,
                 "slack": lambda: "Webhook saved." if secret_saved("SLACK_WEBHOOK_URL") else "",
                 "ntfy": _ntfy_summary}
    out = {"configured": configured(name), "summary": summaries[name](), "reason": ""}
    if not out["configured"]:
        incomplete = _incomplete(name)
        if incomplete:
            out.update(state="warn", label="Incomplete", reason=incomplete)
        elif name == "apify":
            out.update(state="warn", label="Not set up")
        else:
            out.update(state="neutral", label="Optional")
        return out
    if name == "ai":
        last = ai_status.status(db)
    else:
        last = integration_health.status(db, name)
    if last is None:
        out.update(state="neutral", label="Not tested yet")
    elif last["ok"]:
        out.update(state="ok", label="Working", reason=working_text(last["at"]))
    else:
        reason = last["reason"]
        if name == "ai" and llm.reasoning_hint(reason):
            reason = f"{reason} {llm.REASONING_HINT}"
        out.update(state="fail", label="Failing", reason=reason)
    return out


def state_pill(name: str, st: dict | None = None) -> str:
    st = st or integration_state(name)
    return pill(st["state"], st["label"])


def modal_url(name: str, next_url: str, **params) -> str:
    q = "&".join(f"{k}={quote(str(v))}" for k, v in params.items() if v)
    return f"/ui/integration/{name}?next={quote(next_url, safe='/#')}" + (f"&{q}" if q else "")


def setup_button(name: str, next_url: str, st: dict | None = None, primary: bool | None = None) -> str:
    st = st or integration_state(name)
    is_set = st["configured"] or (st["state"] == "warn" and name != "apify")
    label = "Change" if is_set else "Set up"
    primary = (not is_set) if primary is None else primary
    cls = "btn primary small" if primary else "btn small"
    return (f"<a class='{cls}' href='{_esc(modal_url(name, next_url))}' data-modal "
            f"data-title='{_esc(INFO[name]['title'])}'>{label}<span class='sr-only'> "
            f"{_esc(INFO[name]['short'])}</span></a>")


def saved_test_button(name: str, next_url: str) -> str:
    """Test of the SAVED configuration (the row's quick check)."""
    if name == "ai":
        action, hidden = "/settings/ai-test", ""
    elif name == "apify":
        action, hidden = "/settings/apify-test", ""
    elif name == "email":
        action, hidden = "/settings/email-test", ""
    else:
        action, hidden = "/settings/notify-test", f"<input type='hidden' name='channel' value='{name}'>"
    return (f"<form class='inline-form' action='{action}' method='post'>{hidden}"
            f"<input type='hidden' name='next' value='{_esc(next_url)}'>"
            f"<button type='submit' class='btn small' data-busy-label='Testing…'>Test"
            f"<span class='sr-only'> {_esc(INFO[name]['short'])}</span></button></form>")


def integration_row(name: str, next_url: str, show_purpose: bool = True) -> str:
    st = integration_state(name)
    reason = ""
    if st["state"] in ("fail", "warn") and st["reason"]:
        reason = f"<p class='integ-reason' role='status'>{_esc(st['reason'])}</p>"
    elif st["reason"]:
        reason = f"<p class='integ-meta'>{_esc(st['reason'])}</p>"
    purpose = f"<p class='integ-purpose'>{_esc(INFO[name]['purpose'])}</p>" if show_purpose else ""
    test = saved_test_button(name, next_url) if st["configured"] else ""
    return (f"<div class='integ-item' id='{name}' data-integration='{name}'>"
            f"<div class='integ-main'><div class='integ-title'><b>{_esc(INFO[name]['title'])}</b>"
            f"{pill(st['state'], st['label'])}</div>{purpose}"
            + (f"<p class='integ-summary'>{_esc(st['summary'])}</p>" if st["summary"] else "")
            + f"{reason}</div>"
            f"<div class='integ-actions'>{test}{setup_button(name, next_url, st, primary=False)}"
            "</div></div>")


# --------------------------------------------------------------------- #
# AI helpers
# --------------------------------------------------------------------- #
def detect_preset(base_url: str | None = None) -> str:
    """Which OpenAI-compatible preset a base URL belongs to ('' if none set)."""
    base = (base_url if base_url is not None else (os.getenv("OPENAI_BASE_URL") or "")).strip()
    if not base:
        return "openai" if (os.getenv("OPENAI_API_KEY") or "").strip() else ""
    norm = base.rstrip("/")
    for key, p in AI_PRESETS.items():
        if p["base"] and norm == p["base"].rstrip("/"):
            return key
    host = (urlsplit(norm).hostname or "").lower()
    port = urlsplit(norm).port
    if host == "api.openai.com":
        return "openai"
    if host.endswith("openrouter.ai"):
        return "openrouter"
    if llm.is_opencode(norm) and "/go/" in norm + "/":
        return "opencode"
    if port == 11434:
        return "ollama"
    if port == 1234:
        return "lmstudio"
    return "custom"


def ai_candidate(form: dict) -> dict:
    """Env values of the AI dialog's submission (empty secret = keep saved)."""
    provider = (form.get("provider") or "").strip().lower()
    if provider == "gemini":
        return {"LLM_PROVIDER": "gemini",
                "GEMINI_API_KEY": (form.get("apikey") or "").strip() or (os.getenv("GEMINI_API_KEY") or "").strip(),
                "GEMINI_MODEL": (form.get("model") or "").strip()}
    preset = (form.get("preset") or "").strip()
    base = (form.get("openai_base_url") or "").strip() or AI_PRESETS.get(preset, {}).get("base", "")
    effort = (form.get("reasoning_effort") or "").strip().lower() if "reasoning_effort" in form \
        else saved_reasoning_effort()
    if effort == "low" and not (os.getenv("AI_REASONING_EFFORT") or "").strip():
        effort = ""      # the default: keep the variable unset
    return {"LLM_PROVIDER": "openai", "OPENAI_BASE_URL": base,
            "OPENAI_API_KEY": (form.get("openai_key") or "").strip() or (os.getenv("OPENAI_API_KEY") or "").strip(),
            "OPENAI_MODEL": (form.get("openai_model") or "").strip(),
            "AI_REASONING_EFFORT": effort}


REASONING_EFFORT_LABELS = {"low": "Low (default, fastest)", "medium": "Medium", "high": "High",
                           "off": "Off (don't send it)"}


def saved_reasoning_effort() -> str:
    """The saved AI_REASONING_EFFORT as shown in the dialog ('' = default low)."""
    raw = (os.getenv("AI_REASONING_EFFORT") or "").strip().lower()
    if raw in ("none", "0", "false", "no"):
        return "off"
    return raw if raw in REASONING_EFFORT_LABELS else ""


def ai_problem(values: dict) -> str:
    """Plain reason the AI values can't work, '' when they can."""
    provider = values.get("LLM_PROVIDER")
    if provider not in llm.PROVIDERS:
        return "Choose Gemini or OpenAI-compatible."
    effort = values.get("AI_REASONING_EFFORT") or ""
    if effort and effort not in REASONING_EFFORT_LABELS:
        return "Reasoning effort must be low, medium, high or off."
    if provider == "gemini":
        return "" if values.get("GEMINI_API_KEY") else "Paste your Gemini API key."
    base = values.get("OPENAI_BASE_URL") or ""
    if base and urlsplit(base).scheme not in ("http", "https"):
        return "The base URL must start with http:// or https://, e.g. http://localhost:11434/v1."
    with llm.config_override(values):
        reason = llm.not_configured_reason()
    if not reason:
        return ""
    if "needs OPENAI_API_KEY" in reason:
        return "OpenAI needs an API key. Paste it, or choose a local server."
    if "OPENAI_MODEL" in reason:
        return "Enter a model name: this server has no default model."
    return reason.replace(llm.NOT_CONFIGURED, "").strip() or "Some settings are missing."


def ai_ident(values: dict) -> str:
    """Fingerprint of an AI configuration (keys included, never stored raw)."""
    with llm.config_override(values):
        info = llm.provider_info()
        key = llm._env("GEMINI_API_KEY") if info["provider"] == "gemini" else llm._env("OPENAI_API_KEY")
        base = llm.openai_base_url() if info["provider"] == "openai" else ""
    return integration_health.fingerprint("ai", info["provider"], info["model"], base, key)


def ai_saved_values() -> dict:
    return {k: (os.getenv(k) or "").strip() for k in
            ("LLM_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY",
             "OPENAI_MODEL")}


# --------------------------------------------------------------------- #
# Dialogs
# --------------------------------------------------------------------- #
def _hidden(name: str, value: str) -> str:
    return f"<input type='hidden' name='{name}' value='{_esc(value)}'>"


def _foot(name: str, form_id: str, test_label: str = "Test") -> str:
    return ("<button type='button' class='btn ghost' data-sm-close>Cancel</button>"
            + test_button(name, form_id, test_label)
            + f"<button type='submit' class='btn primary' form='{form_id}' "
              "data-busy-label='Saving…'>Save</button>")


def _error_box(error: str) -> str:
    return f"<div class='flash flash-error' role='alert'>{_esc(error)}</div>" if error else ""


def _form_open(form_id: str, action: str, next_url: str) -> str:
    # The first submit button inside the form is Save, so Enter in a field
    # saves (the footer's Test button comes first in the page otherwise).
    return (f"<form id='{form_id}' method='post' action='{action}' class='form-grid' "
            f"data-sm-submit>{_hidden('next', next_url)}"
            "<button type='submit' class='sr-only' tabindex='-1' aria-hidden='true'>Save</button>")


def _radio_cards(name: str, options: list[tuple[str, str, str]], selected: str,
                 attrs: dict | None = None) -> str:
    """Radio choices drawn as small cards: (value, title, description)."""
    attrs = attrs or {}
    out = []
    for value, title, desc in options:
        extra = attrs.get(value, "")
        checked = " checked" if value == selected else ""
        desc_html = f"<span class='rc-desc'>{_esc(desc)}</span>" if desc else ""
        out.append(f"<label class='rcard'><input type='radio' name='{name}' value='{_esc(value)}'"
                   f"{checked}{extra}><span class='rc-body'><b>{_esc(title)}</b>{desc_html}</span>"
                   "</label>")
    return f"<div class='rcards' role='radiogroup'>{''.join(out)}</div>"


def _data_attrs(fill: dict | None = None, placeholder: dict | None = None,
                text: dict | None = None) -> str:
    parts = []
    for attr, val in (("data-fill", fill), ("data-placeholder", placeholder), ("data-text", text)):
        if val is not None:
            parts.append(f" {attr}='{_esc(json.dumps(val))}'")
    return "".join(parts)


def _apify_modal(next_url: str, error: str = "", values: dict | None = None) -> tuple[str, str, str]:
    st = integration_state("apify")
    body = (stepper(["Get your token", "Paste it", "Test and save"], 2 if st["configured"] else 1)
            + _error_box(error)
            + _form_open("f-apify", "/settings/apikey", next_url)
            + step_block(1, "Get your Apify token",
                         "<p class='note'>Sign in to Apify (the free plan works) and open "
                         f"{ext_link('https://console.apify.com/account/integrations')}. Copy the "
                         "<b>Personal API token</b>.</p>")
            + step_block(2, "Paste it here", secret_field(
                "APIFY_API_KEY", "apikey", "Apify API token", "ap-token",
                "It starts with apify_api_, e.g. apify_api_AbC123…", "apify_api_…", autofocus=True))
            + step_block(3, "Test, then save",
                         "<p class='note'>Test checks the token with Apify. Nothing is searched and "
                         "nothing is charged.</p>" + test_slot("apify"))
            + "</form>" + remove_form("APIFY_API_KEY", next_url)
            + "<p class='note'>The monthly budget alert is set in Settings → Search, next to this.</p>")
    title = "Change the Apify token" if st["configured"] else "Set up Search (Apify)"
    return title, body, _foot("apify", "f-apify")


def _ai_choose(next_url: str, error: str = "") -> tuple[str, str, str]:
    # The provider in use: the selected one, else the one whose settings are
    # saved (an unknown LLM_PROVIDER never marks an unconfigured provider).
    current = llm.provider() if llm.provider() in llm.PROVIDERS else ""
    if not current and any(llm._env(k) for k in ("GEMINI_API_KEY", "OPENAI_BASE_URL",
                                                  "OPENAI_API_KEY", "OPENAI_MODEL")):
        current = llm.form_provider()
    cards = []
    for key, title, desc in (
        ("gemini", "Gemini", ("Google's AI. One API key from Google AI Studio. Quick to set "
                              "up, with a free tier.")),
        ("openai", "OpenAI-compatible", ("OpenAI, OpenRouter, OpenCode Go, or a model on your "
                                         "own computer with Ollama or LM Studio.")),
    ):
        tag = "<span class='tag'>In use</span>" if key == current else ""
        url = modal_url("ai", next_url, provider=key)
        cards.append(f"<a class='choice' href='{_esc(url)}' data-sm-nav data-provider='{key}'>"
                     f"<span class='choice-title'><b>{title}</b>{tag}</span>"
                     f"<span class='choice-desc'>{desc}</span>"
                     "<span class='choice-go' aria-hidden='true'>Choose ›</span></a>")
    body = (stepper(["Choose a provider", "Fill in its details", "Test and save"], 1)
            + _error_box(error)
            + "<p class='note'>Pick where the AI runs. You only fill in the details of the one "
              "you choose.</p>"
            + f"<div class='choice-grid'>{''.join(cards)}</div>")
    return "Choose an AI provider", body, "<button type='button' class='btn ghost' data-sm-close>Cancel</button>"


def _ai_back(next_url: str) -> str:
    return (f"<a class='back-link' href='{_esc(modal_url('ai', next_url, step='choose'))}' "
            "data-sm-nav>‹ Choose another provider</a>")


def _ai_gemini(next_url: str, error: str = "", values: dict | None = None) -> tuple[str, str, str]:
    values = values or {}
    current_model = values.get("model") if "model" in values else (
        llm.gemini_model() if (os.getenv("GEMINI_MODEL") or "").strip() else "")
    body = (stepper(["Choose a provider", "Fill in its details", "Test and save"], 2)
            + _ai_back(next_url) + _error_box(error)
            + _form_open("f-ai", "/settings/ai", next_url) + _hidden("provider", "gemini")
            + secret_field("GEMINI_API_KEY", "apikey", "Gemini API key", "ai-gkey",
                           "Create one at " + ext_link("https://aistudio.google.com/apikey")
                           + ". It looks like AIzaSy…", "AIzaSy…", autofocus=True)
            + field("Model (optional)", text_input("ai-gmodel", "model", current_model or "",
                                                   llm.GEMINI_DEFAULT_MODEL),
                    f"Leave empty to use {llm.GEMINI_DEFAULT_MODEL} (fast and cheap).", "ai-gmodel")
            + test_slot("ai") + "</form>" + remove_form("GEMINI_API_KEY", next_url))
    return "Gemini", body, _foot("ai", "f-ai")


def _effort_field(current: str) -> str:
    """AI_REASONING_EFFORT (OpenAI-compatible only): how much a reasoning
    model thinks before answering. Saved through the same validated path."""
    current = current if current in REASONING_EFFORT_LABELS else "low"
    opts = "".join(f"<option value='{k}'{' selected' if k == current else ''}>{_esc(v)}</option>"
                   for k, v in REASONING_EFFORT_LABELS.items())
    return field("Reasoning effort", f"<select id='ai-effort' name='reasoning_effort'>{opts}</select>",
                 "For reasoning models (o-series, DeepSeek R1…): how long they think before "
                 "answering. Low keeps answers quick and cheap. Servers that don't support it "
                 "simply ignore it.", "ai-effort")


def _ai_openai(next_url: str, preset: str = "", error: str = "",
               values: dict | None = None) -> tuple[str, str, str]:
    values = values or {}
    saved_is_openai = llm.form_provider() == "openai"
    saved_preset = detect_preset() if saved_is_openai else ""
    preset = preset if preset in AI_PRESETS else (saved_preset or "openai")
    p = AI_PRESETS[preset]
    saved_base = (os.getenv("OPENAI_BASE_URL") or "").strip() if saved_is_openai else ""
    base = values.get("openai_base_url") if "openai_base_url" in values else (
        saved_base if preset == saved_preset and saved_base else p["base"])
    model = values.get("openai_model") if "openai_model" in values else (
        (os.getenv("OPENAI_MODEL") or "").strip() if preset == saved_preset else "")
    key_label = {"required": "API key", "optional": "API key (optional)",
                 "none": "API key (not needed)"}
    attrs = {}
    for k, pr in AI_PRESETS.items():
        attrs[k] = _data_attrs(
            fill={"#ai-base": pr["base"]},
            placeholder={"#ai-omodel": f"e.g. {pr['model']}", "#ai-base": pr["base"] or "https://…/v1"},
            text={"#ai-okey-label": key_label[pr["key"]], "#ai-okey-help": pr["key_help"],
                  "#ai-omodel-help": pr["model_help"], "#ai-preset-note": pr["note"]})
    attrs[preset] += " autofocus"
    options = [(k, pr["label"], "") for k, pr in AI_PRESETS.items()]
    body = (stepper(["Choose a provider", "Fill in its details", "Test and save"], 2)
            + _ai_back(next_url) + _error_box(error)
            + _form_open("f-ai", "/settings/ai", next_url) + _hidden("provider", "openai")
            + "<fieldset class='field'><legend class='label'>Service</legend>"
            + _radio_cards("preset", options, preset, attrs).replace("class='rcards'", "class='rcards chips'")
            + f"<p class='note' id='ai-preset-note'>{_esc(p['note'])}</p></fieldset>"
            + field("Base URL", text_input("ai-base", "openai_base_url", base or "",
                                           p["base"] or "https://…/v1", "url"),
                    "Filled in for the service you picked. Change it only if your server uses "
                    "another address (keep the /v1 part).", "ai-base")
            + secret_field("OPENAI_API_KEY", "openai_key", key_label[p["key"]], "ai-okey",
                           _esc(p["key_help"]), "sk-…", label_id="ai-okey-label",
                           help_id="ai-okey-help")
            + field("Model", text_input("ai-omodel", "openai_model", model or "", f"e.g. {p['model']}"),
                    _esc(p["model_help"]), "ai-omodel", help_id="ai-omodel-help")
            + _effort_field(values.get("reasoning_effort", saved_reasoning_effort()))
            + test_slot("ai") + "</form>" + remove_form("OPENAI_API_KEY", next_url))
    return "OpenAI-compatible", body, _foot("ai", "f-ai")


def _email_modal(next_url: str, error: str = "", values: dict | None = None) -> tuple[str, str, str]:
    values = values or {}
    cfg = email_sender.config_summary()

    def v(key: str, saved: str) -> str:
        return values.get(key, saved)

    host = v("host", cfg["host"] if email_sender.is_configured() or cfg["host"] else "")
    preset = values.get("preset") or next((k for k, p in EMAIL_PRESETS.items()
                                           if p["host"] and p["host"] == host), "") \
        or ("custom" if host else "gmail")
    p = EMAIL_PRESETS[preset]
    if not host and preset != "custom":
        host = p["host"]
    port = v("port", cfg["port"] if cfg["host"] else p["port"]) or p["port"]
    attrs = {k: _data_attrs(fill={"#em-host": pr["host"], "#em-port": pr["port"]},
                            text={"#em-pass-help": pr["pass_help"], "#em-user-help": pr["user_help"]})
             for k, pr in EMAIL_PRESETS.items()}
    options = [(k, pr["label"], "") for k, pr in EMAIL_PRESETS.items()]
    body = (stepper(["Pick your email service", "Sign-in details", "Send a test and save"],
                    3 if email_sender.is_configured() else 1)
            + _error_box(error) + _form_open("f-email", "/settings/email", next_url)
            + "<fieldset class='field'><legend class='label'>Email service</legend>"
            + _radio_cards("preset", options, preset, attrs).replace("class='rcards'", "class='rcards chips'")
            + "</fieldset>"
            + "<div class='row'>"
            + field("Mail server (SMTP)", text_input("em-host", "host", host, "smtp.example.com"),
                    "Filled in for Gmail and Outlook.", "em-host")
            + field("Port", text_input("em-port", "port", port, "465", "number", "min='1' max='65535'"),
                    "465 (SSL) or 587 (STARTTLS).", "em-port", cls="narrow")
            + "</div>"
            + field("User", text_input("em-user", "user", v("user", cfg["user"]), "alerts@example.com"),
                    _esc(p["user_help"]), "em-user", help_id="em-user-help")
            + secret_field("SMTP_PASS", "password", "Password", "em-pass", _esc(p["pass_help"]),
                           help_id="em-pass-help")
            + "<div class='row'>"
            + field("Send alerts to", text_input("em-to", "alert_to", v("alert_to", cfg["to"]),
                                                 "you@example.com", "text"),
                    "Who receives alerts and reports. Several people? Separate with commas.", "em-to")
            + field("From (optional)", text_input("em-from", "sender",
                                                  v("sender", (os.getenv("SMTP_FROM") or "")),
                                                  "Same as the user"),
                    "Leave empty to send from the user above.", "em-from")
            + "</div>"
            + "<p class='note'>Test sends a short email to “Send alerts to” with the values above, "
              "before anything is saved.</p>"
            + test_slot("email") + "</form>" + remove_form("SMTP_PASS", next_url))
    title = "Change email" if email_sender.is_configured() else "Set up email"
    return title, body, _foot("email", "f-email", "Send test")


def _telegram_modal(next_url: str, error: str = "", values: dict | None = None) -> tuple[str, str, str]:
    values = values or {}
    chat = values.get("telegram_chat_id", db.get_setting("telegram_chat_id", ""))
    find_url = "/settings/integration/telegram/chats"
    find_btn = (f"<button type='submit' class='btn small' form='f-telegram' formaction='{find_url}' "
                f"data-sm-action='{find_url}' data-sm-target='#tg-chats' "
                "data-busy-label='Looking…'>Find my chat id</button>")
    body = (stepper(["Create a bot", "Find your chat", "Send a test and save"],
                    3 if configured("telegram") else 1)
            + _error_box(error) + _form_open("f-telegram", "/settings/notify", next_url)
            + _hidden("channel", "telegram")
            + step_block(1, "Create a bot and paste its token",
                         "<p class='note'>Open " + ext_link("https://t.me/BotFather", "@BotFather")
                         + " in Telegram, send <code>/newbot</code> and follow the questions. "
                           "It replies with a token: copy it.</p>"
                         + secret_field("TELEGRAM_BOT_TOKEN", "telegram_token", "Bot token",
                                        "tg-token", "It looks like 123456789:AAH4…", "123456789:AA…",
                                        autofocus=True))
            + step_block(2, "Find your chat",
                         "<p class='note'>Open your new bot in Telegram and press <b>Start</b> (for "
                         "a group: add the bot to the group and send a message there). Then:</p>"
                         f"<div class='actions-row'>{find_btn}</div>"
                         "<div class='test-slot' id='tg-chats' aria-live='polite'></div>"
                         + field("Chat id", text_input("tg-chat", "telegram_chat_id", chat,
                                                       "e.g. 123456789"),
                                 "Filled in when you pick a chat above. A person's id is a number "
                                 "like 123456789; a group's starts with -100.", "tg-chat"))
            + step_block(3, "Send a test, then save",
                         "<p class='note'>The test sends a short message to that chat.</p>"
                         + test_slot("telegram"))
            + "</form>" + remove_form("TELEGRAM_BOT_TOKEN", next_url))
    title = "Change Telegram" if configured("telegram") else "Set up Telegram"
    return title, body, _foot("telegram", "f-telegram", "Send test")


def _slack_modal(next_url: str, error: str = "", values: dict | None = None) -> tuple[str, str, str]:
    body = (stepper(["Create a webhook", "Paste it", "Send a test and save"],
                    3 if configured("slack") else 1)
            + _error_box(error) + _form_open("f-slack", "/settings/notify", next_url)
            + _hidden("channel", "slack")
            + step_block(1, "Create an Incoming Webhook",
                         "<p class='note'>Open " + ext_link("https://api.slack.com/messaging/webhooks")
                         + ", create a Slack app for your workspace, turn on <b>Incoming Webhooks</b>, "
                           "click <b>Add New Webhook</b> and pick the channel for alerts. Copy the "
                           "webhook URL.</p>")
            + step_block(2, "Paste the webhook URL",
                         secret_field("SLACK_WEBHOOK_URL", "slack_webhook", "Webhook URL", "sl-hook",
                                      "It starts with https://hooks.slack.com/services/…",
                                      "https://hooks.slack.com/services/…", autofocus=True))
            + step_block(3, "Send a test, then save",
                         "<p class='note'>The test posts a short message to that channel.</p>"
                         + test_slot("slack"))
            + "</form>" + remove_form("SLACK_WEBHOOK_URL", next_url))
    title = "Change Slack" if configured("slack") else "Set up Slack"
    return title, body, _foot("slack", "f-slack", "Send test")


def suggested_topic() -> str:
    return "serp-" + _secrets.token_urlsafe(12).replace("_", "").replace("-", "").lower()[:14]


def _ntfy_modal(next_url: str, error: str = "", values: dict | None = None) -> tuple[str, str, str]:
    values = values or {}
    saved_mode = "url" if (os.getenv("NTFY_URL") or "").strip() else "topic"
    mode = values.get("ntfy_mode") or saved_mode
    topic_suggest = suggested_topic()
    fill_btn = (f"<button type='button' class='btn ghost small' data-sm-fill='#nt-topic' "
                f"data-value='{_esc(topic_suggest)}'>Suggest a random name</button>")
    options = [("topic", "Public ntfy.sh", "Free, nothing to install on a server."),
               ("url", "Your own ntfy server", "Messages stay on your server.")]
    body = (stepper(["Choose where", "Topic", "Send a test and save"], 3 if configured("ntfy") else 1)
            + _error_box(error) + _form_open("f-ntfy", "/settings/notify", next_url)
            + _hidden("channel", "ntfy")
            + "<fieldset class='field'><legend class='label'>Where should messages go?</legend>"
            + _radio_cards("ntfy_mode", options, mode) + "</fieldset>"
            + "<div data-show-when='ntfy_mode=topic'>"
            + secret_field("NTFY_TOPIC", "ntfy_topic", "Topic name", "nt-topic",
                           "Anyone who knows a public topic name can read its messages: use a long, "
                           "random name. " + fill_btn, "serp-k3v9q2…")
            + "</div><div data-show-when='ntfy_mode=url'>"
            + secret_field("NTFY_URL", "ntfy_url", "Topic URL on your server", "nt-url",
                           "The full topic URL, e.g. https://ntfy.example.com/serp-alerts",
                           "https://ntfy.example.com/serp-alerts")
            + "</div>"
            + "<div class='hint'><b>On your phone:</b> install the ntfy app (Android or iOS), tap "
              "<b>+</b> and subscribe to the same topic (and server, if you use your own).</div>"
            + test_slot("ntfy") + "</form>"
            + remove_form("NTFY_TOPIC", next_url) + remove_form("NTFY_URL", next_url))
    title = "Change ntfy" if configured("ntfy") else "Set up ntfy"
    return title, body, _foot("ntfy", "f-ntfy", "Send test")


def modal_parts(name: str, next_url: str, *, step: str = "", provider: str = "", preset: str = "",
                error: str = "", values: dict | None = None) -> tuple[str, str, str]:
    """(title, body, foot) of the guided dialog of `name`."""
    if name == "apify":
        return _apify_modal(next_url, error, values)
    if name == "ai":
        # Always starts at "Choose a provider" (the one in use is marked).
        chosen = provider if provider in llm.PROVIDERS else ""
        if chosen == "gemini":
            return _ai_gemini(next_url, error, values)
        if chosen == "openai":
            return _ai_openai(next_url, preset, error, values)
        return _ai_choose(next_url, error)
    if name == "email":
        return _email_modal(next_url, error, values)
    if name == "telegram":
        return _telegram_modal(next_url, error, values)
    if name == "slack":
        return _slack_modal(next_url, error, values)
    return _ntfy_modal(next_url, error, values)


def modal_html(name: str, next_url: str, **kw) -> str:
    title, body, foot = modal_parts(name, next_url, **kw)
    return ui.overlay(title, body, foot)


# --------------------------------------------------------------------- #
# Scheduling wording
# --------------------------------------------------------------------- #
JOB_INFO: dict[str, tuple[str, str]] = {
    "weekly_report": ("Weekly report", ("runs a search for each tracked subject (not demo) "
                                        "and emails the workbook")),
    "alerts_check": ("Alert checks", ("checks each tracked subject (not demo) for new alerts "
                                      "and sends them as routed above")),
    "info_digest": ("Daily digest", ("emails one summary of the day's info alerts, a section "
                                     "per subject")),
    "purge_checks": ("Clean-up", "removes old delivery logs, resolved alerts and old evidence"),
    "deadman_check": ("Watchdog", ("checks that the other jobs ran, and raises an alert if "
                                   "one was missed")),
}

_DAYS = {0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday", 5: "Friday",
         6: "Saturday", 7: "Sunday"}


def parse_cron(expr: str) -> dict | None:
    """{frequency, hour, weekday} for the expressions admin.build_cron_expr
    writes, else None."""
    parts = (expr or "").split()
    if len(parts) != 5 or parts[0] != "0" or not parts[1].isdigit():
        return None
    hour = int(parts[1])
    dom, mon, dow = parts[2], parts[3], parts[4]
    table = {("*", "*"): "daily", ("1", "*"): "monthly", ("1", "1,4,7,10"): "quarterly",
             ("1", "1,7"): "biannual", ("1", "1"): "annual"}
    if dom == "*" and mon == "*" and dow.isdigit():
        return {"frequency": "weekly", "hour": hour, "weekday": int(dow) % 7}
    freq = table.get((dom, mon)) if dow == "*" else None
    return {"frequency": freq, "hour": hour, "weekday": admin.DEFAULT_WEEKDAY} if freq else None


def describe_cron(expr: str) -> str:
    """'every Monday at 08:00 UTC' for a cron expression."""
    if (expr or "").startswith("*/") and expr.split()[1:] == ["*", "*", "*", "*"]:
        return f"every {expr.split()[0][2:]} minutes"
    p = parse_cron(expr)
    if not p:
        return f"on the schedule {expr} (UTC)"
    at = f"at {p['hour']:02d}:00 UTC"
    return {
        "daily": f"every day {at}",
        "weekly": f"every {_DAYS.get(p['weekday'], 'Monday')} {at}",
        "monthly": f"on the 1st of every month {at}",
        "quarterly": f"on the 1st of January, April, July and October {at}",
        "biannual": f"on 1 January and 1 July {at}",
        "annual": f"every year on 1 January {at}",
    }[p["frequency"]]
