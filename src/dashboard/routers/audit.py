"""Immutable audit log (C24): the Settings → Audit log card, and the CSV
export (GET /audit/export.csv).

Admin only: the log lists every user's actions and their IP addresses.
dashboard.main._admin_only covers /audit/* (403 for operators and viewers)
and the Settings page renders this card for admins only. GET /audit is kept
for old links and redirects to /settings#audit (keeping its filters).

Read-only -- doesn't call audit(). Known historical chain breaks (FIX 3,
setting `audit_chain_known_breaks_before_id`) are reported as known, new
ones as needing investigation; hashes are never rewritten.
"""
import csv
import io
import json
from urllib.parse import urlencode

from fastapi import APIRouter
from fastapi.responses import RedirectResponse, StreamingResponse

from dashboard.deps import db
from dashboard.ui_helpers import _esc

router = APIRouter()

_CSV_COLUMNS = ("id", "at", "actor_type", "actor_id", "action", "resource_type",
                "resource_id", "params_json", "ip", "row_hash")

_KNOWN_BREAKS_SETTING = "audit_chain_known_breaks_before_id"


def _pill(cls: str, text: str) -> str:
    return f"<span class='pill {cls}'><span class='dot' aria-hidden='true'></span>{_esc(text)}</span>"


# Action -> plain words ("what happened"). Unknown actions are humanised.
ACTION_LABELS = {
    "alert_ack": "Acknowledged an alert", "alert_resolve": "Resolved an alert",
    "alert_snooze": "Snoozed an alert", "alert_routing_save": "Changed alert routing",
    "alert_thresholds_save": "Changed the alert threshold",
    "api_alert_status_change": "Changed an alert through the API",
    "domain_add": "Added a domain", "domain_remove": "Removed a domain",
    "domains_reclassify": "Relabelled results by domain",
    "domains_reclassify_ai": "Relabelled results with AI",
    "evidence_capture": "Saved a copy of a page", "export_excel": "Downloaded an Excel report",
    "export_pdf": "Downloaded a PDF report", "login_failed": "Failed sign-in",
    "login_locked": "Sign-in locked after failures", "login_ok": "Signed in", "logout": "Signed out",
    "note_save": "Saved a note", "note_status_change": "Changed a note's status",
    "project_add": "Created a project", "project_archive": "Archived a project",
    "project_rename": "Renamed a project", "project_restore": "Restored a project",
    "review_same_person": "Reviewed a same-name result", "review_verdict": "Reviewed a result",
    "run_delete": "Deleted a search run", "search_launch": "Started a search",
    "search_multi_launch": "Started a multi-country search",
    "settings_ai_save": "Changed the AI provider", "settings_ai_test": "Tested the AI provider",
    "settings_apify_budget_save": "Changed the Apify budget",
    "settings_apikey_create": "Created an API key", "settings_apikey_revoke": "Revoked an API key",
    "settings_apikey_save": "Changed the Apify token", "settings_apify_test": "Tested Apify",
    "settings_cron_save": "Changed a schedule", "settings_cron_toggle": "Paused or resumed a job",
    "settings_email_save": "Changed email settings", "settings_email_test": "Sent a test email",
    "settings_gone_after_runs_save": "Changed when a result counts as gone",
    "settings_notify_save": "Changed a notification channel",
    "settings_notify_test": "Tested a notification channel",
    "settings_report_save": "Changed the tracked name",
    "settings_review_threshold_save": "Changed the review threshold",
    "settings_secret_remove": "Removed a saved key or password",
    "subject_activate": "Reactivated a subject", "subject_add": "Added a subject",
    "subject_deactivate": "Deactivated a subject", "subject_edit": "Edited a subject",
    "timeline_event_add": "Added a timeline event", "timeline_event_delete": "Deleted a timeline event",
    "user_add": "Added a user", "user_set_active": "Activated or deactivated a user",
    "user_set_password": "Set a user's password", "wizard_apify_save": "Saved the Apify token (setup)",
    "wizard_auth_create": "Created the first admin (setup)",
    "wizard_first_search_save": "Chose the first subject (setup)",
    "wizard_integrations_save": "Finished the integrations step (setup)",
    "wizard_skip": "Skipped a setup step",
}


def action_label(action: str) -> str:
    action = action or ""
    if action in ACTION_LABELS:
        return ACTION_LABELS[action]
    if action.startswith("job_"):
        return "Scheduled job: " + action[4:].replace("_", " ")
    return action.replace("_", " ").capitalize() or "Unknown action"


def _who(r: dict) -> str:
    actor = r.get("actor_id") or ""
    kind = r.get("actor_type") or ""
    if kind == "user":
        return actor or "unknown user"
    if kind == "system" and actor == "anonymous":
        return "Someone (sign-in off)"
    if kind in ("system", "scheduler", "job"):
        return f"System ({actor})" if actor else "System"
    if kind == "api_key":
        return f"API key {actor}"
    return f"{kind}: {actor}" if actor else (kind or "—")


def _details(r: dict) -> str:
    """params_json as short 'key: value' pairs (no secrets are ever stored)."""
    raw = r.get("params_json") or ""
    try:
        params = json.loads(raw) if raw else {}
    except ValueError:
        return _esc(raw)
    if not isinstance(params, dict):
        return _esc(raw)
    parts = []
    for key, value in params.items():
        if value in ("", None, [], {}):
            continue
        if isinstance(value, bool):
            value = "yes" if value else "no"
        elif isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False)
        parts.append(f"{key.replace('_', ' ')}: {value}")
    text = " · ".join(parts)
    res = f"{r.get('resource_type') or ''} {r.get('resource_id') or ''}".strip()
    if res:
        text = f"{res}" + (f" · {text}" if text else "")
    return _esc(text)


def audit_card_html(action: str = "", resource_id: str = "", limit: int = 50) -> str:
    from dashboard import integrations as integ
    from dashboard.ui_helpers import fmt_utc

    limit = max(1, min(int(limit or 50), 1000))
    rows = db.list_audit(limit=limit, action=action, resource_id=resource_id)
    report = db.audit_chain_report()
    known_cutoff = int(db.get_setting(_KNOWN_BREAKS_SETTING, "0") or 0)
    known = [i for i in report["broken_ids"] if i <= known_cutoff]
    new = [i for i in report["broken_ids"] if i > known_cutoff]
    if report["ok"]:
        state = _pill("positive", "Chain intact")
    elif new:
        state = _pill("negative", f"Chain BROKEN: {len(new)} new break(s), first id {new[0]}")
    else:
        state = _pill("warning", f"{len(known)} known historical break(s)")
    notes = ""
    if known:
        notes += (f"<p class='note'>{len(known)} known break(s) before id {known_cutoff}, from a "
                  "concurrency bug fixed since. Not tampering.</p>")
    if new:
        notes += (f"<p class='note'><b>New breaks after id {known_cutoff} need investigation</b>, "
                  f"first at id {new[0]}.</p>")
    table_rows = "".join(
        "<tr>"
        f"<td class='muted nowrap'>{_esc(fmt_utc(r.get('at')))}</td>"
        f"<td class='wrap-any'>{_esc(_who(r))}</td>"
        f"<td>{_esc(action_label(r.get('action')))}</td>"
        f"<td class='muted wrap-any'>{_details(r)}</td>"
        f"<td class='muted'>{_esc(r.get('ip'))}</td></tr>"
        for r in rows
    )
    if rows:
        table = ("<div class='table-wrap'><table class='stable audit-table'><tr><th>When</th><th>Who</th>"
                 f"<th>What</th><th>Details</th><th>IP</th></tr>{table_rows}</table></div>")
    elif action or resource_id:
        table = ("<div class='empty-row'><b>No entries match this filter.</b> "
                 "<a href='/settings#audit'>Show everything</a></div>")
    else:
        table = "<div class='empty-row'><b>Nothing recorded yet.</b> Changes and sign-ins show up here.</div>"
    try:
        actions = db.audit_actions()
    except Exception:  # noqa: BLE001
        actions = []
    if action and action not in actions:
        actions = [action, *actions]
    opts = "<option value=''>All actions</option>" + "".join(
        f"<option value='{_esc(a)}'{' selected' if a == action else ''}>{_esc(action_label(a))}</option>"
        for a in sorted(actions, key=action_label))
    export_q = urlencode({"action": action, "resource_id": resource_id, "limit": 1000})
    keep_res = (f"<input type='hidden' name='audit_resource' value='{_esc(resource_id)}'>"
                if resource_id else "")
    filt = ("<form class='row audit-filter' method='get' action='/settings#audit' data-autosubmit>"
            "<div class='field'><label for='au-action'>Show</label>"
            f"<select id='au-action' name='audit_action'>{opts}</select></div>{keep_res}"
            "<noscript><button type='submit' class='btn small'>Filter</button></noscript></form>")
    shown = (f"<p class='note'>Latest {len(rows)} entries, newest first. Times are UTC.</p>"
             if rows else "")
    more = ("<p class='note'>Each entry is chained to the previous one with a hash, so edits to "
            "old entries are detected (the status above). Keys and passwords are never recorded, "
            "only that they changed.</p>" + notes)
    action_html = f"<a class='btn small' href='/audit/export.csv?{_esc(export_q)}'>Export CSV</a>"
    return integ.section_card("Audit log", "Who changed what, and when. Read only.",
                              filt + shown + table, state, action_html, more, "How it works")


@router.get("/audit")
async def audit_page(action: str = "", resource_id: str = ""):
    q = urlencode({k: v for k, v in (("audit_action", action), ("audit_resource", resource_id)) if v})
    return RedirectResponse(f"/settings{'?' + q if q else ''}#audit", status_code=303)


@router.get("/audit/export.csv")
async def audit_export_csv(action: str = "", resource_id: str = "", limit: int = 1000):
    rows = db.list_audit(limit=limit, action=action, resource_id=resource_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_CSV_COLUMNS)
    for r in rows:
        writer.writerow([r.get(col, "") for col in _CSV_COLUMNS])
    buf.seek(0)
    return StreamingResponse(
        buf, media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit_log.csv"},
    )
