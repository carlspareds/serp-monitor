"""Settings: ONE page (/settings) with a section nav and these sections:

  #projects       Projects and subjects     (operators and admins edit); the
                  active subject's Domains live inside it (<details id='domains'>)
  #search         Search providers (Apify, Serper.dev), monthly SERP budget, results review
  #ai             AI provider
  #notifications  Channels, alert routing and threshold, recent deliveries
  #scheduling     Scheduled jobs
  #users          Users                      (admin)
  #api            API keys
  #audit          Audit log                  (admin)
  #backup         Backup

Every section starts with the same header (dashboard.integrations.section_head):
title, one plain sentence, a status pill and ONE primary action; details are
collapsed below. Integrations are set up in guided dialogs
(/ui/integration/<name>, dashboard/routers/integrations.py) that show only the
fields of what was chosen; the setup wizard opens the very same dialogs.

Everyone signed in can open the page; the admin-only sections (everything
from #search down, which holds secrets, integrations and accounts) are only
rendered for admins, and their POSTs stay admin-only in
dashboard.main.role_middleware. The old tab URLs (/settings/notifications,
/settings/api, /settings/backup, /users, /audit) redirect to their anchor.

Every POST answers with a redirect back (its `next` field, else its section)
plus a toast (dashboard.context.redirect_with_flash): nothing claims "saved"
when nothing changed, and a rejected value comes back as a readable
"Not saved: ..." message -- inside the dialog when it was sent from one. A
secret field left empty always means "keep the saved value"; secrets are
never shown again, audited or logged.
"""
import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from apify.client import ApifyClient
from common import integration_health
from dashboard import admin, nav, ui
from dashboard import integrations as integ
from dashboard.context import (
    flash,
    is_fragment_request,
    pop_once,
    redirect_with_flash,
    safe_next,
    viewer,
)
from dashboard.deps import db
from dashboard.session import audit
from dashboard.ui_helpers import (
    COUNTRIES,
    DEFAULT_COUNTRIES,
    _esc,
    fmt_utc,
    fragment_or_page,
    page,
    plural,
)
from database.models import (
    API_KEY_SCOPE_DEFAULT,
    API_KEY_SCOPES,
    REVIEW_THRESHOLD_DEFAULT,
)
from notify import VALID_CHANNELS, channel_status, email_sender, send_via
from notify.dispatcher import ROUTING_CHANNELS
from processing import ai_classifier, ai_status, llm
from processing import alerts as alerts_engine
from processing.auto_resolve import parse_utc
from processing.lifecycle import GONE_AFTER_RUNS_DEFAULT, _gone_after_runs
from scheduler import core as scheduler_core

router = APIRouter()

_CHANNEL_LABELS = {"email": "Email", "telegram": "Telegram", "slack": "Slack", "ntfy": "ntfy"}
_PROVIDER_LABELS = integ.PROVIDER_LABELS
# deadman_check is the watchdog for the other jobs: it keeps its fixed
# 15-minute cadence and can't be changed or paused.
_LOCKED_JOBS = ("deadman_check",)

SECTIONS = (
    ("projects", "Projects and subjects", False),
    ("search", "Search", True),
    ("ai", "AI provider", True),
    ("notifications", "Notifications", True),
    ("scheduling", "Scheduling", True),
    ("users", "Users", True),
    ("api", "API keys", True),
    ("audit", "Audit log", True),
    ("backup", "Backup", True),
)

SCOPE_INFO = {
    "read": ("Read only", "Read results, alerts, runs and reports (GET requests)."),
    "write:alerts": ("Read and update alerts", ("Everything in Read only, plus changing an "
                                                 "alert's status (acknowledge, resolve).")),
    "admin": ("Full access", "Everything the API offers. Give it only to tools you fully trust."),
}


def _get_runner():
    import dashboard.main as main_module
    return main_module.get_runner()


def _fmt_utc(value: str | None) -> str:
    """'YYYY-MM-DD HH:MM UTC' for a stored timestamp: next_run_at is aware
    (APScheduler isoformat), last_run_at naive UTC -- parse_utc handles
    both. Anything unparsable is shown as-is."""
    if not value:
        return ""
    try:
        return parse_utc(value).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return value


def _status_pill(configured: bool, yes: str = "Connected", no: str = "Not set up") -> str:
    return (f"<span class='pill ok'><span class='dot' aria-hidden='true'></span>{yes}</span>"
            if configured else
            f"<span class='pill neutral'><span class='dot' aria-hidden='true'></span>{no}</span>")


def _env_value(env: str) -> str:
    return (os.getenv(env) or "").strip()


def _back(section: str) -> str:
    return f"/settings#{section}"


def _next(raw: str | None, section: str) -> str:
    return safe_next(raw, _back(section))


def _not_saved(section: str, exc: Exception):
    return redirect_with_flash(_back(section), f"Not saved: {exc}", "error")


def _reject(name: str, next_url: str, message: str, values: dict | None = None, **kw):
    """A rejected integration save: back inside its dialog (fetch submit,
    status 400) or as an error toast. Typed secrets are never sent back."""
    text = f"Not saved: {message}"
    if is_fragment_request():
        return HTMLResponse(integ.modal_html(name, next_url, error=text, values=values, **kw),
                            status_code=400, headers={"Cache-Control": "no-store"})
    return redirect_with_flash(next_url, text, "error")


def _test_message(label: str, ok: bool, detail: str) -> str:
    return f"{label}: {'OK' if ok else 'Failed'}. {detail}".strip()


def _health_note(name: str, ident: str) -> tuple[str, str]:
    """(sentence, toast kind) about a test of exactly the saved values."""
    last = integration_health.lookup(db, name, ident)
    if last is None:
        return " Use Test to check that it works.", "ok"
    if last["ok"]:
        return " Test passed.", "ok"
    return f" Its last test failed: {last['reason']}", "error"


_SECRET_ENV = ("KEY", "TOKEN", "SECRET", "PASS", "WEBHOOK", "NTFY_URL", "NTFY_TOPIC")


def _audit_test(request: Request, action: str, ok: bool, detail: str, **params) -> None:
    """Audit entry for a Test button: result ok/failed plus the plain reason
    (failures only). Any secret value from the environment is redacted."""
    reason = "" if ok else " ".join(str(detail or "").split())[:300]
    for name, value in os.environ.items():
        if value and len(value) >= 6 and any(k in name.upper() for k in _SECRET_ENV):
            reason = reason.replace(value, "<secret>")
    audit(request, action, result="ok" if ok else "failed", reason=reason, **params)


def _form_row(*fields: str) -> str:
    return f"<div class='row'>{''.join(fields)}</div>"


# --------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------- #
def _budget_alert_note() -> str:
    """This month's deployment-wide SERP budget alert, if one was raised."""
    alert = db.current_budget_alert()
    if alert is None:
        return ""
    state = {"open": "open", "acknowledged": "acknowledged"}.get(alert.get("status") or "",
                                                                 "resolved")
    return (f"<p class='note' role='status' data-budget-alert>"
            f"<b>Budget alert this month</b> ({state}, {_esc(_fmt_utc(alert.get('created_at')))}): "
            f"{_esc(alert.get('message'))}</p>")


def _integration_card(name: str, more: str = "", body_extra: str = "") -> str:
    st = integ.integration_state(name)
    reason = ""
    if st["state"] in ("fail", "warn") and st["reason"]:
        reason = (f"<p class='integ-reason' role='status' data-health='{name}'>"
                  f"<b>{_esc(st['label'])}:</b> {_esc(st['reason'])}</p>")
    elif st["reason"]:
        reason = f"<p class='integ-meta'>{_esc(st['reason'])}</p>"
    test = integ.saved_test_button(name, _back(integ.SECTION_OF[name])) if st["configured"] else ""
    body = (f"<div class='integ-status' id='{name}-status' data-integration='{name}'>"
            f"<p class='integ-summary'>{_esc(st['summary'])}</p>{reason}"
            f"{('<div class=actions-row>' + test + '</div>') if test else ''}</div>{body_extra}")
    return integ.section_card(integ.INFO[name]["title"], _esc(integ.INFO[name]["purpose"]), body,
                              integ.pill(st["state"], st["label"]),
                              integ.setup_button(name, _back(integ.SECTION_OF[name]), st), more)


def _provider_card() -> str:
    """Settings → Search → Provider: which SERP provider searches run
    through (serp.registry): Auto picks Apify, else Serper.dev, by key."""
    from serp.registry import LABELS, active_provider_name, requested_provider
    chosen, active = requested_provider(), active_provider_name()
    options = "".join(
        f"<option value='{value}'{' selected' if value == chosen else ''}>{label}</option>"
        for value, label in (("", "Auto (Apify if its key is set, else Serper.dev)"),
                             *((name, LABELS[name]) for name in LABELS)))
    if active:
        status = f"Searches run through <b>{_esc(LABELS[active])}</b>" + \
                 ("." if chosen else " (chosen automatically).")
        pill = integ.pill("ok", LABELS[active])
    elif chosen:
        status = (f"<b>{_esc(LABELS[chosen])}</b> is chosen but its key isn't saved: searches "
                  "can't run. Add the key above or pick another provider.")
        pill = integ.pill("warn", "Key missing")
    else:
        status = "No provider key saved yet: searches can't run (demo data only)."
        pill = integ.pill("warn", "Not set up")
    body = (f"<p class='note' data-serp-provider='{_esc(active)}'>{status}</p>"
            "<form id='provider-form' action='/settings/serp-provider' method='post' class='row'>"
            + integ.field("Search through",
                          f"<select id='st-provider' name='provider'>{options}</select>",
                          "Apify: Google desktop and mobile, Bing. Serper.dev: Google desktop only, "
                          "cheaper and faster. Both keys can stay saved; this picks the one used.",
                          "st-provider", cls="w-lg")
            + "</form>")
    return integ.section_card(
        "Provider", "Which SERP provider runs the searches. Each one has its own key above.",
        body, pill, "<button type='submit' form='provider-form' class='btn small'>Save provider</button>")


def _search_cards() -> str:
    from serp.registry import LABELS
    spend = db.serp_month_cost()
    budget = float(db.get_setting("apify_monthly_budget", "0") or 0)
    review_threshold = db.review_threshold()
    pending = db.count_pending_review()
    gone = _gone_after_runs(db)
    providers = _integration_card("apify") + _integration_card("serper") + _provider_card()
    budget_pill = (integ.pill("info", f"${budget:.2f} a month") if budget
                   else integ.pill("neutral", "Off"))
    by_provider = ", ".join(f"{LABELS.get(name, name)} ${cost:.2f}"
                            for name, cost in sorted(spend.get("by_provider", {}).items()))
    budget_body = (
        f"<p class='note'>Spend this month: <b>${spend['cost_usd']:.2f}</b> "
        f"({plural(spend['runs'], 'run')}"
        + (f", {spend['compute_units']:.2f} compute units" if spend["compute_units"] else "")
        + (f"; {by_provider}" if by_provider else "") + ").</p>"
        "<form id='budget-form' action='/settings/apify-budget' method='post' class='row'>"
        + integ.field("Monthly SERP budget (USD): alert me when this month's spend reaches",
                      f"<input id='st-budget' type='number' name='budget' value='{budget:.2f}' "
                      "min='0' step='0.5' inputmode='decimal'>",
                      "Example: 20 sends one alert when this month's search spend reaches $20, "
                      "every provider counted (Serper.dev spend is an estimate from its credits). "
                      "0 turns the alert off.", "st-budget", cls="w-lg")
        + "</form>" + _budget_alert_note())
    budget_card = integ.section_card(
        "Monthly budget alert",
        "One alert a month when the SERP spend reaches an amount. Every subject shares the "
        "provider accounts, so the budget covers all of them.",
        budget_body, budget_pill,
        "<button type='submit' form='budget-form' class='btn small'>Save budget</button>")
    review_body = (
        "<form id='review-form' action='/settings/review' method='post' class='form-grid'>"
        + _form_row(
            integ.field("Ask me to review results below this confidence",
                        f"<input id='st-review' type='number' name='review_threshold' "
                        f"value='{review_threshold:.2f}' min='0' max='1' step='0.05'>",
                        "From 0 to 1. Example: 0.6 sends every label the classifier is less than "
                        "60% sure about to Review.", "st-review"),
            integ.field("Count a negative result as gone after",
                        f"<div class='with-unit'><input id='st-gone' type='number' "
                        f"name='gone_after_runs' value='{gone}' min='1' step='1'>"
                        "<span>searches in a row</span></div>",
                        "A negative result missing from this many searches in a row counts as a "
                        "Win on the Overview. Example: 2.", "st-gone"))
        + "</form>")
    review_card = integ.section_card(
        "Results review",
        "When labels wait for your review, and when a removed negative result counts as a win.",
        review_body, integ.pill("info" if pending else "neutral",
                                f"{pending} waiting for review" if pending else "Nothing to review"),
        "<button type='submit' form='review-form' class='btn small'>Save</button>")
    return providers + budget_card + review_card


def _ai_card() -> str:
    more = ("<p class='note'>The AI is used for three things: telling the subject apart from "
            "people with the same name, scoring how harmful a negative result is (1 to 5), and "
            "relabelling results on request (Results → More → AI checks). Without it, results "
            "are labelled by simple rules and nothing else changes.</p>"
            "<p class='note'>The short description that helps tell a subject apart from namesakes "
            "is set per subject, under <a href='#projects'>Projects and subjects</a>.</p>")
    return _integration_card("ai", more, "")


def _channel_options(sev: str, current: str, channels: dict) -> str:
    labels = {"email": "Email", "telegram": "Telegram", "slack": "Slack", "ntfy": "ntfy",
              "digest": "Daily digest", "none": "Off"}
    usable = {**channels, "digest": channels.get("email", False), "none": True}
    out = []
    for ch in ("email", "telegram", "slack", "ntfy", "digest", "none"):
        checked = current == ch
        ok = usable.get(ch, False)
        disabled = "" if (ok or checked) else " disabled"
        note = ""
        if not ok and ch not in ("none",):
            note = ("<span class='ropt-note'>needs email</span>" if ch == "digest"
                    else "<span class='ropt-note'>not set up</span>")
        warn = " ropt-warn" if checked and not ok else ""
        out.append(f"<label class='ropt{warn}{' off' if (not ok and not checked) else ''}'>"
                   f"<input type='radio' name='{sev}' value='{ch}'{' checked' if checked else ''}"
                   f"{disabled}><span>{labels[ch]}</span>{note}</label>")
    return "".join(out)


_SEV_ROWS = (
    ("critical", "Critical", "critical",
     "The most urgent alerts. Nothing is raised as critical by default today."),
    ("warning", "Warning", "warning",
     "Example: a new negative result appears for a country, or a scheduled job was missed."),
    ("info", "Info", "info",
     "Example: a negative result dropped out of the results (good news)."),
)


def _routing_card() -> str:
    channels = channel_status(db)
    routing = db.get_routing()
    rows = []
    broken = []
    for sev, label, cls, example in _SEV_ROWS:
        current = routing.get(sev, "none")
        usable = {**channels, "digest": channels.get("email"), "none": True}
        if not usable.get(current, False):
            where = {"digest": "the daily digest (email)"}.get(current, _CHANNEL_LABELS.get(current, current))
            broken.append(f"{label} alerts go to {where}, which isn't set up, so they are not sent. "
                          "Pick another channel below or set it up.")
        rows.append(
            f"<fieldset class='route-row'><legend class='route-sev'>"
            f"<span class='pill {cls}'><span class='dot' aria-hidden='true'></span>{label}</span>"
            f"<span class='note'>{_esc(example)}</span></legend>"
            f"<div class='route-opts'>{_channel_options(sev, current, channels)}</div></fieldset>")
    sev_thr = alerts_engine.severity_threshold(db)
    neg_thr = alerts_engine.thresholds(db)
    subject = db.current_subject()
    check_for = ((subject.get("name") or subject.get("query") or "").strip()
                 or f"subject {subject.get('id') or 1}")
    warn = "".join(f"<p class='integ-reason' role='status'>{_esc(b)}</p>" for b in broken)
    body = (
        f"{warn}<form id='routing-form' action='/settings/alerts' method='post' class='form-grid'>"
        f"<div class='route-grid'>{''.join(rows)}</div>"
        "<p class='note'><b>Daily digest</b>: one email a day (08:00 UTC) with all the alerts of "
        "that level, instead of one message per alert. Only channels you've set up can be "
        "picked.</p>"
        "<div class='threshold'>"
        "<label for='th-sev'>Alert when one country's negative results add up to a harm score of "
        f"at least</label><input id='th-sev' type='number' name='sev' value='{sev_thr}' min='0' "
        "max='100'>"
        "<p class='help'>Each negative result in a country's results scores 1 (mild) to 5 (very "
        "harmful), or 3 if the AI hasn't scored it. Example: 5 means one very harmful result, "
        "or two medium ones, are enough for an alert.</p>"
        f"<input type='hidden' name='neg' value='{neg_thr}'></div></form>")
    more = ("<form class='inline-form' action='/alerts/check' method='post'>"
            "<input type='hidden' name='next' value='/settings#routing'>"
            f"<button type='submit' class='btn small'>Check now for {_esc(check_for)}</button></form>"
            "<p class='note'>Runs the alert checks for the active subject now. The scheduled check "
            "covers every subject.</p>")
    pill = integ.pill("warn", "Some alerts aren't sent") if broken else integ.pill("ok", "Active")
    return ("<div id='routing'>" + integ.section_card(
        "Alert routing", "Which channel receives each kind of alert, and when an alert is raised.",
        body, pill, "<button type='submit' form='routing-form' class='btn primary small'>Save</button>",
        more, "Run the checks now") + "</div>")


def _deliveries_card() -> str:
    log_rows = db.recent_notify_log(limit=10)
    if not log_rows:
        return ""
    table = (
        "<div class='table-wrap'><table><tr><th>Channel</th><th>Result</th><th>Error</th>"
        "<th>When (UTC)</th></tr>"
        + "".join(
            f"<tr><td>{_esc(_CHANNEL_LABELS.get(r['channel'], r['channel']))}</td>"
            f"<td>{_status_pill(bool(r['ok']), 'Sent', 'Failed')}</td>"
            f"<td class='wrap-any'>{_esc(r.get('error') or '')}</td>"
            f"<td>{_esc(_fmt_utc(r['at']) or r['at'])}</td></tr>" for r in log_rows)
        + "</table></div>")
    return integ.details("Recent deliveries (last 10)", table, did="deliveries")


def _notify_cards() -> str:
    channels = channel_status(db)
    n = sum(1 for v in channels.values() if v)
    rows = "".join(integ.integration_row(ch, _back("notifications")) for ch in integ.CHANNELS)
    pill = (integ.pill("ok", f"{n} of 4 set up") if n
            else integ.pill("warn", "None set up"))
    purpose = ("Where alerts and reports are sent. Set up one or more; until then alerts only "
               "show in the dashboard.")
    channels_card = ("<div class='card sec-card'>"
                     + integ.section_head("Notification channels", purpose, pill)
                     + f"<div class='integ-list'>{rows}</div>{_deliveries_card()}</div>")
    return channels_card + _routing_card()


_SCHEDULER_OFF = ("The scheduler is turned off (DISABLE_SCHEDULER is set), so no jobs run. Remove "
                  "it from the environment to enable scheduled reports and alert checks.")


def _configured_schedules() -> list[dict]:
    """Schedules as configured (stored rows, else the registry defaults),
    for display while the scheduler is off."""
    try:
        stored = {r["name"]: r for r in db.list_scheduled_jobs()}
    except Exception:  # noqa: BLE001
        stored = {}
    names = list(scheduler_core.JOBS) + [n for n in stored if n not in scheduler_core.JOBS]
    rows = []
    for name in names:
        row = stored.get(name) or {"name": name, "enabled": 1,
                                   "cron_expr": scheduler_core.JOBS[name]["default_cron"]}
        rows.append({**row, "next_run_at": None})
    return rows


def _job_title(name: str) -> str:
    return integ.JOB_INFO.get(name, (name.replace("_", " ").capitalize(), ""))[0]


def _scheduling_card() -> str:
    disabled = bool((os.getenv("DISABLE_SCHEDULER") or "").strip())
    jobs_rows = scheduler_core.list_jobs()
    # Scheduler off: nothing is registered, so show the configured schedules
    # read-only (no Pause/Resume, no Change).
    readonly = disabled and not jobs_rows
    if readonly:
        jobs_rows = _configured_schedules()
    order = list(integ.JOB_INFO)
    jobs_rows = sorted(jobs_rows, key=lambda j: order.index(j["name"]) if j["name"] in order else 99)

    def job_row(j) -> str:
        title, what = integ.JOB_INFO.get(j["name"], (_job_title(j["name"]), ""))
        when = integ.describe_cron(j["cron_expr"])
        status = j.get("last_status")
        icon = {"ok": "<span class='pill ok'>ok</span>",
                "error": "<span class='pill negative'>error</span>"}.get(status, "")
        last = (f"{icon} {_esc(_fmt_utc(j.get('last_run_at')))}" if j.get("last_run_at")
                else "hasn't run yet")
        nxt = "not scheduled" if readonly else (_esc(_fmt_utc(j.get("next_run_at"))) or "—")
        paused = not j.get("enabled")
        state = " <span class='tag'>paused</span>" if paused else ""
        actions = ""
        if j["name"] not in _LOCKED_JOBS and not readonly:
            actions = (
                f"<a class='btn small' href='/ui/settings/schedule/{_esc(j['name'])}' data-modal "
                f"data-title='Change schedule'>Change<span class='sr-only'> {_esc(title)}</span></a>"
                f"<form action='/settings/cron/toggle' method='post' class='inline-form'>"
                f"<input type='hidden' name='name' value='{_esc(j['name'])}'>"
                f"<button class='btn ghost small' type='submit'>{'Resume' if paused else 'Pause'}"
                f"<span class='sr-only'> {_esc(title)}</span></button></form>")
        elif j["name"] in _LOCKED_JOBS:
            actions = "<span class='tag'>fixed</span>"
        sentence = (f"<b>{_esc(title)}</b>: {_esc(when)}" + (f" — {_esc(what)}." if what else "."))
        return (f"<div class='job' data-job='{_esc(j['name'])}'><div class='job-main'>"
                f"<p class='job-line'>{sentence}{state}</p>"
                f"<p class='li-sub'>Last run: {last} · Next: {nxt}</p></div>"
                f"<div class='job-actions'>{actions}</div></div>")

    jobs_html = (f"<div class='jobs'>{''.join(job_row(j) for j in jobs_rows)}</div>"
                 if jobs_rows else "<p class='note'>No jobs registered.</p>")
    off_note = (f"<div class='hint hint-warn' role='status' data-scheduler-off>{_esc(_SCHEDULER_OFF)}</div>"
                if disabled else "")
    pill = integ.pill("warn", "Off") if disabled else integ.pill("ok", "On")
    return integ.section_card(
        "Scheduling", "Jobs that run on their own. All times are UTC.", off_note + jobs_html, pill)


def _api_key_subject_options() -> list[tuple[int, str]]:
    """(id, 'Project ▸ Subject') of every active subject, for API key pinning."""
    return [(s["id"], db.subject_label(s["id"])) for s in db.list_subjects()]


def _api_card(new_key: dict | None) -> str:
    rows = db.list_api_keys()
    labels = dict(_api_key_subject_options())

    def key_subject(k: dict) -> str:
        sid = k.get("subject_id")
        if sid is None:
            return "All subjects"
        return labels.get(sid) or f"Subject {sid} (inactive)"

    def key_row(k: dict) -> str:
        scope = SCOPE_INFO.get(k.get("scopes") or "", (k.get("scopes") or "", ""))[0]
        if k.get("revoked_at"):
            state = f"<span class='tag'>revoked {_esc(fmt_utc(k['revoked_at']))}</span>"
            action = ""
        else:
            state = _status_pill(True, "Active")
            what = (f"Revoke the API key {k.get('name') or ''}? Anything using it stops working "
                    "at once. This can't be undone.")
            action = ("<form action='/settings/api-keys/revoke' method='post' class='inline-form'>"
                      f"<input type='hidden' name='key_id' value='{k['id']}'>"
                      f"<button type='submit' class='btn ghost small danger-text' data-sm-confirm='{_esc(what)}'"
                      " data-confirm-title='Revoke this API key?' data-confirm-label='Revoke key'>"
                      f"Revoke<span class='sr-only'> {_esc(k.get('name'))}</span></button></form>")
        last = _esc(fmt_utc(k.get("last_used"))) if k.get("last_used") else "never"
        return (f"<tr><td><b>{_esc(k.get('name'))}</b></td><td>{_esc(scope)}</td>"
                f"<td>{_esc(key_subject(k))}</td><td class='nowrap'>{_esc(fmt_utc(k.get('created_at')))}</td>"
                f"<td class='nowrap'>{last}</td><td>{state}</td><td class='rt'>{action}</td></tr>")

    box = ""
    if new_key:
        box = _new_key_box(new_key)
    active = sum(1 for k in rows if not k.get("revoked_at"))
    listing = (("<div class='table-wrap'><table class='stable'><tr><th>Name</th><th>Access</th>"
                "<th>Subject</th><th>Created</th><th>Last used</th><th>Status</th><th></th></tr>"
                + "".join(key_row(k) for k in rows) + "</table></div>") if rows else
               "<div class='empty-row'><b>No API keys yet.</b> Create one to connect a tool such as "
               "n8n or a script.</div>")
    more = ("<p class='note'>The API is documented at <a href='/docs'>/docs</a>. Send the key in "
            "the <code>X-API-Key</code> header to <code>/api/v1/…</code>. Access levels: "
            "<b>Read only</b> (GET endpoints), <b>Read and update alerts</b> (also "
            "<code>PATCH /api/v1/alerts/{id}</code> to change an alert's status, scope "
            "<code>write:alerts</code>), <b>Full access</b> (scope <code>admin</code>).</p>"
            "<p class='note'>Every endpoint takes <code>?subject=&lt;id&gt;</code>: a key limited "
            "to one subject always uses that subject (another id is refused with 403); an "
            "\"All subjects\" key reads subject 1 when <code>subject</code> is left out.</p>")
    action = ("<a class='btn primary small' href='/ui/settings/api-key/new' data-modal "
              f"data-title='Create an API key'>{nav.icon('plus')}Create API key</a>")
    return integ.section_card(
        "API keys", "Keys that let other tools (n8n, scripts) read SERP Monitor through its API.",
        box + listing, integ.pill("neutral", f"{active} active"), action, more,
        "How the API works")


def _new_key_box(new_key: dict) -> str:
    return (f"<div class='secret-box' role='status'><b>Copy this API key now. It won't be shown "
            f"again.</b><div class='copy-row'><code id='new-api-key'>{_esc(new_key.get('api_key'))}</code>"
            "<button type='button' class='btn small' data-copy='#new-api-key'>Copy</button></div>"
            f"<p class='note'>Key for {_esc(new_key.get('name'))}. Send it in "
            "the <code>X-API-Key</code> header to <code>/api/v1/...</code>.</p></div>")


_BACKUP_CARD = """
    <div class='card sec-card'>{head}
      <div class='dl-list'>
        <div class='dl-item'><div><b>Full backup (.db)</b>
          <p class='note'>Everything SERP Monitor stores: projects, subjects, searches and results,
          domains, notes, alerts, users (passwords hashed), API keys (hashed) and settings. Not
          included: integration keys and passwords, which live in the server's .env file.</p></div>
          <a class='btn small' href='/backup/db' download>Download .db</a></div>
        <div class='dl-item'><div><b>Results of the active subject (.csv)</b>
          <p class='note'>One row per search result: run, country, position, URL, title,
          description, label, same-person check and date. Opens in Excel or Google Sheets.</p></div>
          <a class='btn small' href='/backup/csv' download>Export .csv</a></div>
      </div>
    </div>"""


def _backup_card() -> str:
    head = integ.section_head("Backup", "Download a copy of your data. Keep it somewhere safe: "
                                        "the full backup contains everything.")
    return _BACKUP_CARD.format(head=head)


def _overview(v) -> str:
    """Setup at a glance: one chip per integration, linking to its section."""
    if not v.is_admin:
        return ""
    chips = []
    channels = channel_status(db)
    n = sum(1 for x in channels.values() if x)
    from serp.registry import active_provider_name
    # One Search chip: the active SERP provider (Apify when none is set up yet).
    for name, href in ((active_provider_name() or "apify", "#search"), ("ai", "#ai")):
        st = integ.integration_state(name)
        chips.append(f"<a class='ov-chip' href='{href}'><span>{_esc(integ.INFO[name]['short'])}</span>"
                     f"{integ.pill(st['state'], st['label'])}</a>")
    failing = [c for c in integ.CHANNELS if integ.integration_state(c)["state"] == "fail"]
    if failing:
        ch_pill = integ.pill("fail", f"{_CHANNEL_LABELS[failing[0]]} failing")
    else:
        ch_pill = integ.pill("ok" if n else "warn", f"{n} of 4 set up" if n else "None set up")
    chips.append(f"<a class='ov-chip' href='#notifications'><span>Notifications</span>{ch_pill}</a>")
    sched_off = bool((os.getenv("DISABLE_SCHEDULER") or "").strip())
    chips.append("<a class='ov-chip' href='#scheduling'><span>Scheduling</span>"
                 f"{integ.pill('warn', 'Off') if sched_off else integ.pill('ok', 'On')}</a>")
    return f"<div class='ov-strip' aria-label='Setup at a glance'>{''.join(chips)}</div>"


# --------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------- #
@router.get("/settings", response_class=HTMLResponse, tags=["settings"])
async def show_settings(audit_action: str = "", audit_resource: str = ""):
    from dashboard.routers.audit import audit_card_html
    from dashboard.routers.subjects import projects_card_html
    from dashboard.routers.users import users_card_html

    v = viewer()
    f = flash() or {}
    new_key = pop_once(f.get("once"))

    cards = {
        "projects": projects_card_html,
        "search": _search_cards,
        "ai": _ai_card,
        "notifications": _notify_cards,
        "scheduling": _scheduling_card,
        "users": users_card_html,
        "api": lambda: _api_card(new_key),
        "audit": lambda: audit_card_html(audit_action, audit_resource),
        "backup": _backup_card,
    }
    visible = [(key, label) for key, label, admin_only in SECTIONS if v.is_admin or not admin_only]
    snav = "".join(f"<a href='#{key}'{' class=on' if i == 0 else ''}>{_esc(label)}</a>"
                   for i, (key, label) in enumerate(visible))
    sections = "".join(f"<section id='{key}' aria-label='{_esc(label)}'>{cards[key]()}</section>"
                       for key, label in visible)
    if not v.is_admin:
        sections += ("<p class='note'>Integrations, users and other system settings are managed by "
                     "an administrator.</p>")
    finder = ("<div class='sfind'><label class='sr-only' for='settings-find'>Find a setting</label>"
              "<input id='settings-find' type='search' placeholder='Find a setting' "
              "autocomplete='off' data-settings-find></div>")
    body = (f"{_overview(v)}<div class='settings'><nav class='snav' aria-label='Settings sections'>"
            f"{finder}<div class='snav-links'>{snav}</div></nav>"
            f"<div class='sgroup'>{sections}"
            "<p class='note sfind-empty' hidden data-settings-empty>No setting matches. Try another "
            "word, like email, key or schedule.</p></div></div>")
    return page("Settings", body, active="/settings",
                sub="Everything in one place. Saved keys and passwords are never shown again.")


# Old tab URLs: redirect to their section.
@router.get("/settings/notifications", tags=["settings"])
async def show_settings_notifications():
    return RedirectResponse("/settings#notifications", status_code=303)


@router.get("/settings/api", tags=["settings"])
async def show_settings_api():
    return RedirectResponse("/settings#api", status_code=303)


@router.get("/settings/backup", tags=["settings"])
async def show_settings_backup():
    return RedirectResponse("/settings#backup", status_code=303)


# --------------------------------------------------------------------- #
# Dialogs: schedule, API key
# --------------------------------------------------------------------- #
def _admin_only_fragment(title: str):
    return fragment_or_page(title, ui.overlay(title, "<div class='empty'>Only an administrator "
                                                     "can change this.</div>"),
                            active="/settings", status_code=403)


@router.get("/ui/settings/schedule/{name}", response_class=HTMLResponse, tags=["settings"])
async def ui_schedule(name: str):
    if not viewer().is_admin:
        return _admin_only_fragment("Change schedule")
    row = db.get_scheduled_job(name)
    if name in _LOCKED_JOBS or (row is None and name not in scheduler_core.JOBS):
        return fragment_or_page("Change schedule", ui.overlay(
            "Change schedule", "<div class='empty'>This job runs on a fixed schedule.</div>"),
            active="/settings", status_code=404)
    cron = (row or {}).get("cron_expr") or scheduler_core.JOBS[name]["default_cron"]
    cur = integ.parse_cron(cron) or {"frequency": "weekly", "hour": 8,
                                     "weekday": admin.DEFAULT_WEEKDAY}
    title, what = integ.JOB_INFO.get(name, (_job_title(name), ""))
    freq_opts = "".join(f"<option value='{k}'{' selected' if k == cur['frequency'] else ''}>{v}</option>"
                        for k, v in admin.FREQUENCIES.items())
    hour_opts = "".join(f"<option value='{h}'{' selected' if h == cur['hour'] else ''}>{h:02d}:00</option>"
                        for h in range(24))
    weekday_opts = "".join(
        f"<option value='{d}'{' selected' if d == cur['weekday'] else ''}>{label}</option>"
        for d, label in admin.WEEKDAYS.items())
    body = (f"<p class='note'><b>{_esc(title)}</b> {_esc(what)}. Now: "
            f"{_esc(integ.describe_cron(cron))}.</p>"
            "<form id='schedule-form' method='post' action='/settings/cron' class='form-grid' "
            f"data-sm-submit><input type='hidden' name='name' value='{_esc(name)}'>"
            "<input type='hidden' name='next' value='/settings#scheduling'>"
            + _form_row(
                integ.field("How often", f"<select id='cr-freq' name='frequency'>{freq_opts}</select>",
                            "", "cr-freq"),
                f"<div data-show-when='frequency=weekly' class='field'><label for='cr-day'>Day</label>"
                f"<select id='cr-day' name='weekday'>{weekday_opts}</select></div>",
                integ.field("Time (UTC)", f"<select id='cr-hour' name='hour'>{hour_opts}</select>",
                            "", "cr-hour"))
            + "<p class='note'>Times are in UTC. Example: 08:00 UTC is 09:00 in London in winter "
              "and 10:00 in Madrid in summer.</p></form>")
    foot = ("<button type='button' class='btn ghost' data-sm-close>Cancel</button>"
            "<button type='submit' class='btn primary' form='schedule-form' "
            "data-busy-label='Saving…'>Save</button>")
    return fragment_or_page("Change schedule", ui.overlay(f"Change: {title}", body, foot),
                            active="/settings")


def _api_key_form(error: str = "", values: dict | None = None) -> str:
    values = values or {}
    scope = values.get("scope") or API_KEY_SCOPE_DEFAULT
    options = [(s, SCOPE_INFO[s][0], SCOPE_INFO[s][1]) for s in ("read", "write:alerts", "admin")
               if s in API_KEY_SCOPES]
    sel = str(values.get("subject_id") or "")
    subj_opts = f"<option value=''{' selected' if not sel else ''}>All subjects</option>" + "".join(
        f"<option value='{sid}'{' selected' if str(sid) == sel else ''}>{_esc(label)}</option>"
        for sid, label in _api_key_subject_options())
    err = f"<div class='flash flash-error' role='alert'>{_esc(error)}</div>" if error else ""
    return (err + "<form id='apikey-form' method='post' action='/settings/api-keys' "
            "class='form-grid' data-sm-submit>"
            + integ.field("Name", integ.text_input("ak-name", "name", values.get("name", ""),
                                                    "e.g. n8n-prod", extra="required"),
                          "Something that tells you where the key is used.", "ak-name")
            + "<fieldset class='field'><legend class='label'>Access</legend>"
            + integ._radio_cards("scope", options, scope) + "</fieldset>"
            + integ.field("Subject", f"<select id='ak-subject' name='subject_id'>{subj_opts}</select>",
                          "Limit the key to one subject, or let it read all of them.", "ak-subject")
            + "</form>")


@router.get("/ui/settings/api-key/new", response_class=HTMLResponse, tags=["settings"])
async def ui_api_key_new():
    if not viewer().is_admin:
        return _admin_only_fragment("Create an API key")
    foot = ("<button type='button' class='btn ghost' data-sm-close>Cancel</button>"
            "<button type='submit' class='btn primary' form='apikey-form' "
            "data-busy-label='Creating…'>Create key</button>")
    return fragment_or_page("Create an API key",
                            ui.overlay("Create an API key", _api_key_form(), foot),
                            active="/settings")


# --------------------------------------------------------------------- #
# Writes (PRG)
# --------------------------------------------------------------------- #
@router.post("/settings", tags=["settings"])
async def save_settings(request: Request, query: str = Form("", max_length=200), countries: list[str] = Form(default=[])):
    """Legacy form: tracked query + countries of the ACTIVE subject."""
    query = " ".join(query.split())
    if not query:
        return redirect_with_flash(_back("projects"), "Not saved: the tracked name is required.", "error")
    valid = [c for c in countries if c in COUNTRIES] or DEFAULT_COUNTRIES.split(",")
    if query == db.get_tracked_query() and valid == db.get_tracked_countries():
        return redirect_with_flash(_back("projects"), "No changes to save.", "info")
    db.set_tracked_subject(query=query, countries=valid)
    audit(request, "settings_report_save", query=query, countries=valid)
    return redirect_with_flash(_back("projects"), "Tracked name and countries saved.")


@router.post("/settings/apify-budget", tags=["settings"])
async def save_apify_budget(request: Request, budget: str = Form("0", max_length=64)):
    try:
        value = max(float(budget or 0), 0.0)
    except ValueError:
        return redirect_with_flash(_back("search"), "Not saved: the budget must be a number, "
                                                    "e.g. 20.", "error")
    if value == float(db.get_setting("apify_monthly_budget", "0") or 0):
        return redirect_with_flash(_back("search"), "No changes to the budget.", "info")
    db.set_setting("apify_monthly_budget", str(value))
    audit(request, "settings_apify_budget_save", budget=value)
    return redirect_with_flash(_back("search"), f"Budget saved: one alert when this month's spend "
                                                f"reaches ${value:.2f}." if value else
                               "Budget alert turned off.")


def _parse_review(review_threshold: str) -> float:
    return min(max(float(review_threshold), 0.0), 1.0)


@router.post("/settings/review-threshold", tags=["settings"])
async def save_review_threshold(request: Request, review_threshold: str = Form(str(REVIEW_THRESHOLD_DEFAULT), max_length=64)):
    try:
        value = _parse_review(review_threshold)
    except ValueError:
        return redirect_with_flash(_back("search"), "Not saved: enter a number between 0 and 1, "
                                                    "e.g. 0.6.", "error")
    if abs(value - db.review_threshold()) < 1e-9:
        return redirect_with_flash(_back("search"), "No changes to the review threshold.", "info")
    db.set_setting("review_threshold", str(value))
    audit(request, "settings_review_threshold_save", review_threshold=value)
    return redirect_with_flash(_back("search"), "Review threshold saved.")


@router.post("/settings/gone-after-runs", tags=["settings"])
async def save_gone_after_runs(request: Request, gone_after_runs: str = Form(str(GONE_AFTER_RUNS_DEFAULT), max_length=64)):
    try:
        value = int(gone_after_runs)
    except ValueError:
        return redirect_with_flash(_back("search"), "Not saved: enter a whole number, e.g. 2.", "error")
    value = value if value > 0 else GONE_AFTER_RUNS_DEFAULT
    if value == _gone_after_runs(db):
        return redirect_with_flash(_back("search"), "No changes.", "info")
    db.set_setting("gone_after_runs", str(value))
    audit(request, "settings_gone_after_runs_save", gone_after_runs=value)
    return redirect_with_flash(_back("search"), "Saved. Wins now need that many runs in a row.")


@router.post("/settings/review", tags=["settings"])
async def save_review(request: Request, review_threshold: str = Form("", max_length=64),
                      gone_after_runs: str = Form("", max_length=64)):
    """Results review card: both values in one Save."""
    try:
        thr = _parse_review(review_threshold) if review_threshold.strip() else db.review_threshold()
    except ValueError:
        return redirect_with_flash(_back("search"), "Not saved: the review threshold must be a "
                                                    "number between 0 and 1, e.g. 0.6.", "error")
    try:
        gone = int(gone_after_runs) if gone_after_runs.strip() else _gone_after_runs(db)
    except ValueError:
        return redirect_with_flash(_back("search"), "Not saved: “searches in a row” must be a whole "
                                                    "number, e.g. 2.", "error")
    gone = gone if gone > 0 else GONE_AFTER_RUNS_DEFAULT
    changed = []
    if abs(thr - db.review_threshold()) >= 1e-9:
        db.set_setting("review_threshold", str(thr))
        audit(request, "settings_review_threshold_save", review_threshold=thr)
        changed.append(f"review below {thr:.2f} confidence")
    if gone != _gone_after_runs(db):
        db.set_setting("gone_after_runs", str(gone))
        audit(request, "settings_gone_after_runs_save", gone_after_runs=gone)
        changed.append(f"gone after {plural(gone, 'search', 'searches')} in a row")
    if not changed:
        return redirect_with_flash(_back("search"), "No changes to the review settings.", "info")
    return redirect_with_flash(_back("search"), "Saved: " + " and ".join(changed) + ".")


def _verify_apify(apikey: str) -> tuple[bool, str]:
    from serp.apify_provider import ApifyProvider
    ok, detail = ApifyProvider(client=ApifyClient(api_key=apikey)).check_credentials()
    # Plain reason only (same wording as /setup): never Apify's raw response body.
    return ok, detail if ok else f"{detail}; it was saved anyway"


def _verify_serper(key: str) -> tuple[bool, str]:
    from serp.serper_provider import SerperProvider
    ok, detail = SerperProvider(api_key=key).check_credentials()
    return ok, detail if ok else f"{detail}; it was saved anyway"


@router.post("/settings/apikey", tags=["settings"])
async def save_apikey(request: Request, apikey: str = Form("", max_length=1024), next: str = Form("", max_length=2048)):
    """Saves the Apify token and verifies it like the setup wizard does. An
    empty field keeps the saved token. The check is informational: the token
    is saved even when it fails (Apify can be briefly unreachable)."""
    import dashboard.main as main_module
    next_url = _next(next, "search")
    apikey = (apikey or "").strip()
    if not apikey:
        if os.getenv("APIFY_API_KEY"):
            return redirect_with_flash(next_url, "No changes. The saved Apify token was kept.", "info")
        return _reject("apify", next_url, "paste your Apify token first.")
    if apikey == (os.getenv("APIFY_API_KEY") or ""):
        return redirect_with_flash(next_url, "No changes. That token is already saved.", "info")
    try:
        admin.check_env_values([("APIFY_API_KEY", apikey)])
    except ValueError as exc:
        return _reject("apify", next_url, str(exc))
    integ.write_env([("APIFY_API_KEY", apikey)])
    main_module._runner = None
    audit(request, "settings_apikey_save", field="APIFY_API_KEY")
    ok, detail = await run_in_threadpool(_verify_apify, apikey)
    integration_health.record_current(db, "apify", ok, detail)
    return redirect_with_flash(next_url,
                               "Apify token saved. " + _test_message("Apify check", ok, detail),
                               "ok" if ok else "error")


@router.post("/settings/serper-key", tags=["settings"])
async def save_serper_key(request: Request, serper_key: str = Form("", max_length=1024),
                          usd_per_1k: str = Form("", max_length=32), next: str = Form("", max_length=2048)):
    """Saves the Serper.dev key (and the price used to estimate its spend)
    exactly like the Apify token: written to the env file, verified
    afterwards, kept even when the check fails."""
    import dashboard.main as main_module
    from serp.serper_provider import DEFAULT_USD_PER_1K
    next_url = _next(next, "search")
    key = (serper_key or "").strip()
    rate = (usd_per_1k or "").strip()
    pairs = []
    if rate:
        try:
            if float(rate) < 0:
                raise ValueError
        except ValueError:
            return _reject("serper", next_url, "the price per 1,000 credits must be a number "
                                               "(e.g. 1.00).", {"usd_per_1k": rate})
        if float(rate) != float(os.getenv("SERPER_USD_PER_1K") or DEFAULT_USD_PER_1K):
            pairs.append(("SERPER_USD_PER_1K", rate))
    if not key:
        if not os.getenv("SERPER_API_KEY"):
            return _reject("serper", next_url, "paste your Serper.dev key first.")
        if pairs:
            integ.write_env(pairs)
            main_module._runner = None   # the cached provider holds the old rate
            audit(request, "settings_serper_key_save", field="SERPER_USD_PER_1K")
            return redirect_with_flash(next_url, "Price saved. The saved Serper.dev key was kept.")
        return redirect_with_flash(next_url, "No changes. The saved Serper.dev key was kept.", "info")
    if key == (os.getenv("SERPER_API_KEY") or "") and not pairs:
        return redirect_with_flash(next_url, "No changes. That key is already saved.", "info")
    if key != (os.getenv("SERPER_API_KEY") or ""):
        pairs.append(("SERPER_API_KEY", key))
    try:
        admin.check_env_values(pairs)
    except ValueError as exc:
        return _reject("serper", next_url, str(exc))
    integ.write_env(pairs)
    main_module._runner = None
    audit(request, "settings_serper_key_save", field=",".join(k for k, _v in pairs))
    ok, detail = await run_in_threadpool(_verify_serper, key)
    integration_health.record_current(db, "serper", ok, detail)
    return redirect_with_flash(next_url,
                               "Serper.dev key saved. " + _test_message("Serper.dev check", ok, detail),
                               "ok" if ok else "error")


@router.post("/settings/serp-provider", tags=["settings"])
async def save_serp_provider(request: Request, provider: str = Form("", max_length=32)):
    """Settings → Search → Provider: writes SERP_PROVIDER ('' = auto)."""
    import dashboard.main as main_module
    from serp.registry import LABELS
    value = (provider or "").strip().lower()
    if value and value not in LABELS:
        return redirect_with_flash(_back("search"), "Not saved: Unknown provider.", "error")
    if value == (os.getenv("SERP_PROVIDER") or "").strip().lower():
        return redirect_with_flash(_back("search"), "No changes to the provider.", "info")
    integ.write_env([("SERP_PROVIDER", value)])
    main_module._runner = None
    audit(request, "settings_serp_provider_save", provider=value or "auto")
    return redirect_with_flash(_back("search"), "Provider saved: searches run through "
                               + (LABELS[value] if value else "the first provider with a key (auto)")
                               + ".")


@router.post("/settings/api-keys", tags=["settings"])
async def create_api_key(request: Request, name: str = Form("", max_length=200), scope: str = Form(API_KEY_SCOPE_DEFAULT, max_length=64),
                         subject_id: str = Form("", max_length=64)):
    name = (name or "").strip() or "unnamed"
    scope = scope if scope in API_KEY_SCOPES else API_KEY_SCOPE_DEFAULT
    pinned = None
    if (subject_id or "").strip():
        try:
            pinned = int(subject_id)
        except ValueError:
            pinned = -1
        row = db.get_subject(pinned) if pinned > 0 else None
        if row is None or not row["active"]:
            if is_fragment_request():
                foot = ("<button type='button' class='btn ghost' data-sm-close>Cancel</button>"
                        "<button type='submit' class='btn primary' form='apikey-form'>Create key</button>")
                return HTMLResponse(ui.overlay("Create an API key", _api_key_form(
                    "Not created: that subject isn't available. Pick another one.",
                    {"name": name, "scope": scope}), foot), status_code=400)
            return redirect_with_flash(_back("api"), "Not created: unknown subject.", "error")
    key_id, raw_key = db.add_api_key(name=name, scopes=scope, subject_id=pinned)
    audit(request, "settings_apikey_create", resource_id=key_id, name=name, scope=scope,
          subject_id=pinned)
    if is_fragment_request():
        # Shown ONCE, in this dialog; never stored, logged or put in a cookie.
        body = (_new_key_box({"api_key": raw_key, "name": name})
                + f"<p class='note'>Access: <b>{_esc(SCOPE_INFO.get(scope, (scope,))[0])}</b>. "
                  "If you lose it, revoke it and create a new one.</p>"
                  "<span hidden data-sm-reload-on-close></span>")
        foot = "<button type='button' class='btn primary' data-sm-close>Done</button>"
        return HTMLResponse(ui.overlay("Your new API key", body, foot),
                            headers={"Cache-Control": "no-store"})
    return redirect_with_flash(_back("api"), f"API key {name} created. Copy it now.",
                               once={"api_key": raw_key, "name": name})


@router.post("/settings/api-keys/revoke", tags=["settings"])
async def revoke_api_key_route(request: Request, key_id: str = Form("", max_length=64)):
    try:
        kid = int(key_id)
    except ValueError:
        return redirect_with_flash(_back("api"), "Unknown API key.", "error")
    db.revoke_api_key(kid)
    audit(request, "settings_apikey_revoke", resource_id=kid)
    return redirect_with_flash(_back("api"), "API key revoked. Tools using it stop working now.")


@router.post("/settings/serp-test", tags=["settings"])
@router.post("/settings/apify-test", tags=["settings"])
async def serp_test(request: Request, next: str = Form("", max_length=2048),
                    provider: str = Form("", max_length=32)):
    """Test of a SAVED provider key through its check_credentials(): the
    named `provider` (each row's Test button), else the active one
    (/settings/apify-test, the pre-#3 route, kept for bookmarks)."""
    from serp.registry import LABELS, PROVIDERS
    name = (provider or "").strip().lower()
    try:
        if name in PROVIDERS:
            ok, detail = await run_in_threadpool(PROVIDERS[name]().check_credentials)
        else:
            runner = _get_runner()
            name = runner.provider_name
            ok, detail = await run_in_threadpool(runner.check_credentials)
    except Exception as e:  # noqa: BLE001
        ok, detail = False, admin.apify_check_reason(str(e))
    name = name if name in PROVIDERS else "apify"
    _audit_test(request, f"settings_{name}_test", ok, detail)
    integration_health.record_current(db, name, ok, detail)
    return redirect_with_flash(_next(next, "search"), _test_message(f"{LABELS[name]} check", ok, detail),
                               "ok" if ok else "error")


@router.post("/settings/email", tags=["settings"])
async def save_email(request: Request, host: str = Form("", max_length=200), port: str = Form("465", max_length=64), user: str = Form("", max_length=200),
                     password: str = Form("", max_length=1024), sender: str = Form("", max_length=200), alert_to: str = Form("", max_length=500),
                     preset: str = Form("", max_length=64), next: str = Form("", max_length=2048)):
    next_url = _next(next, "notifications")
    host, user, sender, alert_to = host.strip(), user.strip(), sender.strip(), alert_to.strip()
    port = (port or "465").strip()
    try:
        for key, value in (("SMTP_HOST", host), ("SMTP_PORT", port), ("SMTP_USER", user),
                           ("SMTP_PASS", password), ("SMTP_FROM", sender), ("ALERT_EMAIL", alert_to)):
            admin.check_env(key, value)
    except ValueError as exc:
        return _reject("email", next_url, str(exc))
    typed = {"host": host, "port": port, "user": user, "sender": sender, "alert_to": alert_to,
             "preset": preset}
    if not host and not user and not password.strip():
        # Everything emptied: email is turned off (nothing partial is left).
        values = [(k, "") for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "SMTP_FROM", "ALERT_EMAIL")]
        changed = [(k, v) for k, v in values if (os.getenv(k) or "")]
        if not changed:
            return redirect_with_flash(next_url, "No changes to the email settings.", "info")
        integ.write_env(changed)
        audit(request, "settings_email_save", host="", user="", password_changed=True, turned_off=True)
        return redirect_with_flash(next_url, "Email turned off.")
    pw = password.strip() or (os.getenv("SMTP_PASS") or "")
    missing = [label for value, label in ((host, "the mail server"), (user, "the user"),
                                          (pw, "the password")) if not value]
    if missing:
        return _reject("email", next_url, f"fill in {' and '.join(missing)}. Email needs the "
                                          "server, the user and the password together.", typed)
    try:
        n = int(port)
    except ValueError:
        n = -1
    if not 1 <= n <= 65535:
        return _reject("email", next_url, "the port must be a number like 465 (SSL) or 587 "
                                          "(STARTTLS).", typed)
    if alert_to and "@" not in alert_to:
        return _reject("email", next_url, "“Send alerts to” must be an email address, e.g. "
                                          "you@example.com.", typed)
    values = [("SMTP_HOST", host), ("SMTP_PORT", port), ("SMTP_USER", user)]
    if password.strip():
        values.append(("SMTP_PASS", password.strip()))
    values += [("SMTP_FROM", sender), ("ALERT_EMAIL", alert_to)]
    changed = [(k, v) for k, v in values if v != (os.getenv(k) or "")]
    if not changed:
        return redirect_with_flash(next_url, "No changes to the email settings.", "info")
    # Same rules as the startup check, so a save never leaves a config the
    # next restart rejects.
    try:
        admin.check_env_values(values)
    except ValueError as exc:
        return _reject("email", next_url, str(exc), typed)
    integ.write_env(changed)
    audit(request, "settings_email_save", host=host, port=port, user=user, sender=sender,
          alert_to=alert_to, password_changed=bool(password.strip()))
    note, kind = _health_note("email", integration_health.current_ident("email"))
    return redirect_with_flash(next_url, f"Email saved: alerts go to {alert_to or user}.{note}"
                               + " Route alerts to it under Alert routing.", kind)


@router.post("/settings/email-test", tags=["settings"])
async def email_test(request: Request, next: str = Form("", max_length=2048)):
    ok, detail = await run_in_threadpool(
        email_sender.send_email, "Test · SERP Monitor",
        "<h2>Email test</h2><p>Dashboard email delivery works.</p>")
    _audit_test(request, "settings_email_test", ok, str(detail))
    if email_sender.is_configured():
        integration_health.record_current(db, "email", ok, str(detail))
    return redirect_with_flash(_next(next, "notifications"), _test_message("Test email", ok, str(detail)),
                               "ok" if ok else "error")


@router.post("/settings/ai", tags=["settings"])
async def save_ai(request: Request):
    """The AI provider dialog: only the chosen provider's fields arrive.
    Empty secret = keep the saved key. Nothing is written unless the result
    can run (llm.not_configured_reason on the candidate values)."""
    form = dict(await request.form())
    next_url = _next(form.get("next"), "ai")
    provider = (form.get("provider") or "").strip().lower()
    typed = {k: (form.get(k) or "").strip() for k in ("model", "openai_base_url", "openai_model",
                                                         "reasoning_effort")
             if k in form}
    if provider not in llm.PROVIDERS:
        return _reject("ai", next_url, "choose Gemini or OpenAI-compatible.")
    values = integ.ai_candidate(form)
    problem = integ.ai_problem(values)
    if problem:
        return _reject("ai", next_url, problem, typed, provider=provider,
                       preset=(form.get("preset") or ""))
    keys = (("LLM_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL") if provider == "gemini"
            else ("LLM_PROVIDER", "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL",
                  "AI_REASONING_EFFORT"))
    pending = [(k, values[k]) for k in keys if values[k] != _env_value(k)]
    if not pending:
        return redirect_with_flash(next_url, "No changes to the AI provider.", "info")
    try:
        admin.check_env_values(pending)
    except ValueError as exc:
        return _reject("ai", next_url, str(exc), typed, provider=provider)
    integ.write_env(pending)
    changed = {k for k, _v in pending}
    # Secrets: only whether they changed, never their values.
    audit(request, "settings_ai_save", provider=provider,
          apikey_changed="GEMINI_API_KEY" in changed, model=values.get("GEMINI_MODEL", ""),
          openai_base_url=llm._host(values.get("OPENAI_BASE_URL", "")),
          openai_key_changed="OPENAI_API_KEY" in changed, openai_model=values.get("OPENAI_MODEL", ""),
          reasoning_effort=values.get("AI_REASONING_EFFORT", ""))
    # A test of exactly these values in the dialog becomes the status.
    last = integration_health.lookup(db, "ai", integ.ai_ident(integ.ai_saved_values()))
    if last is not None:
        ai_status.record(last["ok"], last["reason"], force=True, db=db)
    note = (" Use Test to check that it works." if last is None else
            " Test passed." if last["ok"] else f" Its last test failed: {last['reason']}")
    return redirect_with_flash(next_url, f"AI provider saved: {integ.ai_summary()}.{note}",
                               "error" if last is not None and not last["ok"] else "ok")


@router.post("/settings/ai-test", tags=["settings"])
async def ai_test(request: Request, next: str = Form("", max_length=2048)):
    ok, detail = await run_in_threadpool(ai_classifier.ping)
    info = llm.provider_info()
    _audit_test(request, "settings_ai_test", ok, detail, provider=info["provider"],
                model=info["model"], host=info["base_url"])
    hint = "" if ok else llm.reasoning_hint(detail)
    return redirect_with_flash(_next(next, "ai"),
                               _test_message("AI provider test", ok, detail) + (f" {hint}" if hint else ""),
                               "ok" if ok else "error")


_NOTIFY_FIELDS = {
    # channel -> ((env var, form field), ...); every one is a secret.
    "telegram": (("TELEGRAM_BOT_TOKEN", "telegram_token"),),
    "slack": (("SLACK_WEBHOOK_URL", "slack_webhook"),),
    "ntfy": (("NTFY_URL", "ntfy_url"), ("NTFY_TOPIC", "ntfy_topic")),
}


def _http_url(value: str) -> bool:
    from urllib.parse import urlsplit
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


@router.post("/settings/notify", tags=["settings"])
async def save_notify(request: Request, channel: str = Form("telegram", max_length=64),
                      telegram_chat_id: str | None = Form(None, max_length=200), telegram_token: str = Form("", max_length=1024),
                      slack_webhook: str = Form("", max_length=2048), ntfy_url: str = Form("", max_length=2048),
                      ntfy_topic: str = Form("", max_length=200), ntfy_mode: str = Form("", max_length=64), clear: str = Form("", max_length=64),
                      next: str = Form("", max_length=2048)):
    """Saves one channel. Secrets (bot token, webhook URL, ntfy URL/topic) go
    to the env file and are kept when the field is left empty; `clear=1`
    removes them. The Telegram chat id is not a secret and lives in the
    settings table. A channel is saved only when it can send."""
    next_url = _next(next, "notifications")
    if channel not in _NOTIFY_FIELDS:
        return redirect_with_flash(next_url, "Not saved: unknown channel.", "error")
    label = _CHANNEL_LABELS[channel]
    form = {"telegram_token": telegram_token.strip(), "slack_webhook": slack_webhook.strip(),
            "ntfy_url": ntfy_url.strip(), "ntfy_topic": ntfy_topic.strip()}
    typed = {"ntfy_mode": ntfy_mode} if ntfy_mode else {}
    if channel == "telegram" and telegram_chat_id is not None:
        typed["telegram_chat_id"] = telegram_chat_id.strip()
    chat_id = None
    if clear:
        pairs = [(key, "") for key, _f in _NOTIFY_FIELDS[channel]]
        if channel == "telegram":
            chat_id = ""
    elif channel == "ntfy":
        mode = ntfy_mode or ("url" if form["ntfy_url"] else "topic" if form["ntfy_topic"]
                             else ("url" if _env_value("NTFY_URL") else "topic"))
        if mode == "url":
            url = form["ntfy_url"] or _env_value("NTFY_URL")
            if not url:
                return _reject("ntfy", next_url, "enter your server's topic URL.", typed)
            if not _http_url(url):
                return _reject("ntfy", next_url, "the topic URL must be a full http(s) address, "
                                                 "e.g. https://ntfy.example.com/serp-alerts.", typed)
            pairs = [("NTFY_URL", url), ("NTFY_TOPIC", "")]
        else:
            topic = form["ntfy_topic"] or _env_value("NTFY_TOPIC")
            if not topic:
                return _reject("ntfy", next_url, "enter a topic name, e.g. serp-k3v9q2.", typed)
            if "/" in topic or any(c.isspace() for c in topic):
                return _reject("ntfy", next_url, "a topic name can't contain spaces or “/”.", typed)
            pairs = [("NTFY_URL", ""), ("NTFY_TOPIC", topic)]
    elif channel == "slack":
        url = form["slack_webhook"] or _env_value("SLACK_WEBHOOK_URL")
        if not url:
            return _reject("slack", next_url, "paste the webhook URL.", typed)
        if not _http_url(url):
            return _reject("slack", next_url, "the webhook URL must be a full http(s) address, "
                                              "like https://hooks.slack.com/services/….", typed)
        pairs = [("SLACK_WEBHOOK_URL", url)]
    else:
        token = form["telegram_token"] or _env_value("TELEGRAM_BOT_TOKEN")
        chat = (telegram_chat_id if telegram_chat_id is not None
                else db.get_setting("telegram_chat_id", "")).strip()
        if not token:
            return _reject("telegram", next_url, "paste the bot token (step 1).", typed)
        if not chat:
            return _reject("telegram", next_url, "add the chat id: click Find my chat id "
                                                 "(step 2) or type it.", typed)
        if not chat.lstrip("-").isdigit() and not chat.startswith("@"):
            return _reject("telegram", next_url, "the chat id is a number like 123456789 (or "
                                                 "-100… for a group).", typed)
        pairs = [("TELEGRAM_BOT_TOKEN", token)]
        if chat != db.get_setting("telegram_chat_id", ""):
            chat_id = chat
    writes = [(k, v) for k, v in pairs if v != (os.getenv(k) or "")]
    if not writes and chat_id is None:
        return redirect_with_flash(next_url, f"No changes to {label}.", "info")
    try:
        admin.check_env_values(writes)
    except ValueError as exc:
        return _reject(channel, next_url, str(exc), typed)
    integ.write_env(writes)
    params = {"channel": channel, "fields": ",".join(k for k, _v in writes), "cleared": bool(clear)}
    if chat_id is not None:
        db.set_setting("telegram_chat_id", chat_id)
        params["telegram_chat_id"] = chat_id
    audit(request, "settings_notify_save", **params)
    if clear:
        return redirect_with_flash(next_url, f"{label} turned off.")
    note, kind = _health_note(channel, integration_health.current_ident(channel, db))
    return redirect_with_flash(next_url, f"{label} saved.{note} Route alerts to it under "
                                         "Alert routing.", kind)


@router.post("/settings/notify-test", tags=["settings"])
async def notify_test(request: Request, channel: str = Form("", max_length=64), next: str = Form("", max_length=2048)):
    next_url = _next(next, "notifications")
    if channel not in VALID_CHANNELS:
        return redirect_with_flash(next_url, "Unknown channel.", "error")
    ok, detail = send_via(db, channel, "Test from SERP Monitor: notification delivery works.")
    _audit_test(request, "settings_notify_test", ok, str(detail), channel=channel)
    return redirect_with_flash(next_url,
                               _test_message(f"{_CHANNEL_LABELS[channel]} test", ok, str(detail)),
                               "ok" if ok else "error")


_ROUTE_LABELS = {"email": "email", "telegram": "Telegram", "slack": "Slack", "ntfy": "ntfy",
                 "digest": "the daily digest", "none": "nowhere (off)"}


@router.post("/settings/alerts", tags=["settings"])
async def save_alert_settings(request: Request, critical: str = Form("", max_length=64), warning: str = Form("", max_length=64),
                              info: str = Form("", max_length=64), sev: str = Form("", max_length=64), neg: str = Form("", max_length=64)):
    """Alert routing grid + threshold in one Save. Only channels that are set
    up can be chosen (a stored choice that isn't set up may be kept)."""
    back = "/settings#routing"
    current = db.get_routing()
    routing = {"critical": critical or current.get("critical", "none"),
               "warning": warning or current.get("warning", "none"),
               "info": info or current.get("info", "none")}
    bad = [ch for ch in routing.values() if ch not in ROUTING_CHANNELS]
    if bad:
        return redirect_with_flash(back, f"Not saved: unknown channel {bad[0]!r}.", "error")
    usable = {**channel_status(db), "none": True}
    usable["digest"] = usable.get("email", False)
    for sev_name, ch in routing.items():
        if ch != current.get(sev_name) and not usable.get(ch):
            what = "email (the daily digest is an email)" if ch == "digest" else _CHANNEL_LABELS[ch]
            return redirect_with_flash(back, f"Not saved: set up {what} first, or pick another "
                                             "channel.", "error")
    try:
        sev_n = int(sev) if sev.strip() else alerts_engine.severity_threshold(db)
        neg_n = int(neg) if neg.strip() else alerts_engine.thresholds(db)
    except ValueError:
        return redirect_with_flash(back, "Not saved: the harm score must be a whole number, "
                                         "e.g. 5.", "error")
    sev_n, neg_n = max(0, min(sev_n, 100)), max(0, min(neg_n, 50))
    changes = []
    route_changed = [s for s in ("critical", "warning", "info") if routing[s] != current.get(s)]
    if route_changed:
        for s in ("critical", "warning", "info"):
            db.set_routing(s, routing[s])
        audit(request, "alert_routing_save", **routing)
        changes += [f"{s} alerts go to {_ROUTE_LABELS[routing[s]]}" for s in route_changed]
    if sev_n != alerts_engine.severity_threshold(db):
        db.set_setting("ALERT_SEVERITY_THRESHOLD", str(sev_n))
        db.set_setting("ALERT_NEG_THRESHOLD", str(neg_n))
        audit(request, "alert_thresholds_save", sev=sev_n, neg=neg_n)
        changes.append(f"alert at a harm score of {sev_n}")
    if not changes:
        return redirect_with_flash(back, "No changes to the alert settings.", "info")
    text = "; ".join(changes)
    return redirect_with_flash(back, f"Saved: {text[0].upper() + text[1:]}.")


@router.post("/settings/cron", tags=["settings"])
async def save_cron(request: Request, frequency: str = Form("", max_length=64), hour: str = Form("8", max_length=64),
                    name: str = Form("weekly_report", max_length=200),
                    weekday: str = Form(str(admin.DEFAULT_WEEKDAY), max_length=64), next: str = Form("", max_length=2048)):
    next_url = _next(next, "scheduling")
    if name in _LOCKED_JOBS:
        return HTMLResponse(page("Not allowed", f"<div class='card'><p>{_esc(name)} runs on a fixed "
                                 "schedule and can't be changed.</p><p><a href='/settings#scheduling'>"
                                 "Back to Settings</a></p></div>", active="/settings"), status_code=400)
    try:
        expr = admin.build_cron_expr(int(hour), frequency, weekday=int(weekday))
    except (ValueError, KeyError) as exc:
        return redirect_with_flash(next_url, f"Not saved: {exc}", "error")
    current = db.get_scheduled_job(name)
    if current and current["cron_expr"] == expr and current["enabled"]:
        return redirect_with_flash(next_url, "No changes to the schedule.", "info")
    scheduler_core.set_job_cron(name, expr, enabled=True)
    audit(request, "settings_cron_save", resource_type="scheduled_job", resource_id=name,
          frequency=frequency, hour=hour, cron=expr)
    return redirect_with_flash(next_url, f"{_job_title(name)} now runs {integ.describe_cron(expr)}.")


@router.post("/settings/cron/toggle", tags=["settings"])
async def toggle_cron(request: Request, name: str = Form("", max_length=200)):
    if name in _LOCKED_JOBS:
        return HTMLResponse(page("Not allowed", f"<div class='card'><p>{_esc(name)} runs on a fixed "
                                 "schedule and can't be paused.</p><p><a href='/settings#scheduling'>"
                                 "Back to Settings</a></p></div>", active="/settings"), status_code=400)
    row = db.get_scheduled_job(name)
    if not row:
        return redirect_with_flash(_back("scheduling"), "Unknown job.", "error")
    scheduler_core.set_job_cron(name, row["cron_expr"], enabled=not row["enabled"])
    audit(request, "settings_cron_toggle", resource_type="scheduled_job", resource_id=name,
          enabled=not row["enabled"])
    verb = "paused: it won't run until you resume it" if row["enabled"] else "resumed"
    return redirect_with_flash(_back("scheduling"), f"{_job_title(name)} {verb}.")

