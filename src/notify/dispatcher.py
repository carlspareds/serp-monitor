"""Severity-based alert dispatcher (7.1)
================================
`notify_alert(db, alert_row)` is the single point that decides WHERE an
already-created alert goes out, and sends it NOW. Routing (severity ->
channel, `alert_routing` read from the DB on every send -- never cached in
memory, acceptance criterion of the deep dive §7.1) lives in
`_dispatch_now`, reused both by notify_alert and by callers that need to
bypass it (see scheduler.deadman.check, which dispatches its own
job_missed alert with `_dispatch_now` directly instead of `notify_alert` --
the watchdog cannot depend on the very conduit it watches).

Channels (`_dispatch_now`):
  - telegram/slack/ntfy -> notify.send_via (already records it in
    notify_log, history shared with the Test buttons of /settings).
  - email -> notify.email_sender directly (subject = kind, body = message).
  - digest/none -> nothing is sent here; "digest" is collected once a day
    by send_info_digest() (job info_digest, scheduler/jobs.py).

`send_info_digest(db)` groups the alerts from the last 24h whose severity
is routed to "digest" (by default only "info") and that have not yet been
digested into a single email. "Not digested" = no
row exists in notify_log with channel='digest' for that alert_id --
simple criterion (brief 7.1): the digest itself is recorded there after
sending, so the next run doesn't repeat the same ones.
"""
import html
from datetime import UTC, datetime, timedelta

from common import integration_health
from notify import email_sender, send_via
from processing.alert_types import normalize_severity

# Valid channels for alert_routing (allowlist of the /alerts UI,
# see dashboard/main.py -- same idea as notify.VALID_CHANNELS from 7.4).
ROUTING_CHANNELS = ("telegram", "slack", "ntfy", "email", "digest", "none")

# ponytail: wide window (covers weeks of notify_log) so as not to lose
# track of an alert_id already digested; if notify_log volume grew a lot,
# switch to a dedicated SQL query (SELECT alert_id FROM notify_log WHERE
# channel='digest') instead of filtering in Python over recent_notify_log.
_DIGEST_LOOKBACK_ROWS = 2000


# Alerts about the deployment itself, not about one subject: no "Project ▸
# Subject: " prefix (the budget is deployment-wide, a missed job is the
# scheduler's).
GLOBAL_ALERT_KINDS = frozenset({"apify_budget", "job_missed"})


def subject_prefix(db, alert_row: dict) -> str:
    """'<Project> ▸ <Subject>: ' for an alert of one subject (the alert row's
    subject_id, else the subject `db` is scoped to); '' for GLOBAL_ALERT_KINDS
    or when the label can't be resolved. Never raises."""
    if alert_row.get("kind") in GLOBAL_ALERT_KINDS:
        return ""
    try:
        sid = alert_row.get("subject_id")
        label = db.subject_label(sid if sid is not None else None)
    except Exception:  # noqa: BLE001 -- a label must never block a notification
        return ""
    return f"{label}: " if label else ""


def _format_message(alert_row: dict, prefix: str = "") -> str:
    severity = alert_row.get("severity", "warning")
    # Severity in capitals as the message prefix (telegram/slack/ntfy) --
    # plain text, no emoji -- after the "Project ▸ Subject: " prefix.
    parts = [str(severity).upper(), alert_row.get("kind", "")]
    if alert_row.get("country"):
        parts.append(alert_row["country"])
    parts.append(alert_row.get("message", ""))
    return prefix + " · ".join(p for p in parts if p)


def _dispatch_now(db, alert_row: dict) -> None:
    """Sends `alert_row` (dict with at least id/kind/severity/country/
    message) through the channel configured for its severity, NOW. Does
    not raise -- a broken or unconfigured channel must not bring down the
    flow that creates alerts. Reused by notify_alert and by callers (e.g.
    scheduler.deadman.check) that need to dispatch without going through
    notify_alert."""
    severity = alert_row.get("severity") or "warning"
    channel = db.get_routing().get(severity, "none")
    if channel in ("digest", "none"):
        return
    prefix = subject_prefix(db, alert_row)

    if channel == "email":
        # H13 (A3 review, security): `message`/`kind` may contain text from
        # an external source (a Wikipedia article title, a domain name
        # cited by an AI engine...) -- send_email delivers it as
        # html_body/subject WITHOUT escaping, a message with HTML markup
        # would render literally in the operator's email client.
        #
        # Point 7 (silent-alerts audit, BLOCKING): unlike
        # telegram/slack/ntfy (which go through send_via, and ALWAYS record
        # it in notify_log except for an unconfigured channel), this branch
        # used to send the email and return WITHOUT writing anything to
        # notify_log -- no alert routed to email (warning is the default of
        # alert_routing, and job_missed falls there) ever left any trace,
        # neither of success nor of failure. Same approach as send_via:
        # when unconfigured, no row at all (avoids a Test against an empty
        # channel polluting the "last 10" history).
        if email_sender.is_configured():
            ok, detail = email_sender.send_email(
                html.escape(prefix + alert_row.get("kind", "")),
                html.escape(prefix + alert_row.get("message", "")))
            db.add_notify_log(channel="email", ok=ok, error=("" if ok else detail),
                               alert_id=alert_row.get("id"))
            integration_health.record_current(db, "email", ok, detail)
        return
    if channel in ("telegram", "slack", "ntfy"):
        send_via(db, channel, _format_message(alert_row, prefix), alert_id=alert_row.get("id"))



def notify_alert(db, alert_row: dict) -> None:
    """Single entry point for every alert: route it by severity and send it now.
    Never raises -- a broken channel must not break the caller."""
    _dispatch_now(db, alert_row)


def _pending_digest_alerts(db) -> list[dict]:
    """Digest-routed alerts of the last 24h not digested yet, from EVERY
    scheduled subject (Database.scheduled_subjects: active, non-demo) -- the
    digest is one email per run for the whole deployment, grouped by subject.
    Alerts of the deployment itself (GLOBAL_ALERT_KINDS) are kept whatever
    subject row they were stored under."""
    # created_at is stored naive-UTC (see Database.add_alert); now(UTC) +
    # stripping tzinfo produces the same format so they can be compared as
    # strings without triggering DTZ003 (which does prohibit calling
    # datetime.utcnow() directly).
    cutoff = (datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=24)).isoformat(
        timespec="seconds"
    )
    already = {
        r["alert_id"] for r in db.recent_notify_log(limit=_DIGEST_LOOKBACK_ROWS)
        if r["channel"] == "digest" and r["alert_id"] is not None
    }
    # Every severity routed to "digest" (the /alerts routing card promises
    # exactly that), read on every run like the rest of the routing.
    digest_severities = {sev for sev, ch in db.get_routing().items() if ch == "digest"}
    scheduled = {s["id"] for s in db.scheduled_subjects()}
    # status="all": the digest summarizes what HAPPENED in 24h -- an alert
    # resolved (by hand or by auto_resolve) before the cutoff still belongs
    # in the day's summary (review 7.2 F1).
    return [
        a for a in db.for_all_subjects().list_alerts(limit=2000, status="all")
        if a["severity"] in digest_severities and a["created_at"] >= cutoff
        and a["id"] not in already
        and (a.get("subject_id") in scheduled or a.get("kind") in GLOBAL_ALERT_KINDS)
    ]


def send_info_digest(db) -> bool:
    """Groups the pending digest alerts (see _pending_digest_alerts) into ONE
    email per run, with a section per subject ("Project ▸ Subject") and a
    "Deployment" section for GLOBAL_ALERT_KINDS. Returns True if it sent,
    False if there was nothing new (or the email failed -- it is retried on
    the next run, the alerts are not marked as digested)."""
    pending = _pending_digest_alerts(db)
    if not pending:
        return False
    groups: dict = {}
    for a in pending:
        key = None if a.get("kind") in GLOBAL_ALERT_KINDS else a.get("subject_id")
        groups.setdefault(key, []).append(a)
    sections = []
    for key in sorted(groups, key=lambda k: (k is None, k or 0)):
        if key is None:
            title = "Deployment"
        else:
            try:
                title = db.subject_label(key)
            except Exception:  # noqa: BLE001
                title = f"Subject {key}"
        # H13 (A3 review, security): same escaping as notify_alert -- the
        # digest concatenates kind/country/message of arbitrary alerts as raw HTML.
        rows = "".join(
            f"<li><b>{html.escape(a['kind'])}</b> · {html.escape(a.get('country', '') or '')} · "
            f"{html.escape(a['message'])}</li>"
            for a in groups[key]
        )
        sections.append(f"<h3>{html.escape(title)} ({len(groups[key])})</h3><ul>{rows}</ul>")
    ok, _detail = email_sender.send_email(f"SEO info digest ({len(pending)})", "".join(sections))
    if ok:
        for a in pending:
            db.add_notify_log(channel="digest", ok=True, alert_id=a["id"])
    return ok


def emit_alert(db, kind: str, severity: str, country: str, message: str,
                dedupe_key: str) -> int | None:
    """Single point of CREATION + dispatch of an alert (FIX 1, fix/alertas-
    mudas audit -- "the system is mute": 14 producers created alerts and
    never called notify_alert). Calls `db.add_alert(...)` and, if it
    actually inserted (new dedupe_key), dispatches with notify_alert using
    EXACTLY the same payload that was just persisted.

    Severity normalized ONCE, right here (processing.alert_types.
    normalize_severity) -- neither add_alert nor notify_alert decide again
    on their own, so the PERSISTED severity and the DISPATCHED one can never
    disagree (unlike the previous pattern, duplicated verbatim 22 times
    across producers: `add_alert` normalized only for saving, and the dict
    passed to `notify_alert` repeated the raw value the caller passed in --
    hence the 4 legacy severities that contradicted ALERT_TYPES, see FIX 1
    point 5).

    Replaces the pattern `alert_id = db.add_alert(...); if alert_id:
    notify_alert(db, {...})` -- every alert producer in src/ must go
    through here (see tests/test_dispatch_guard.py, which fails if a new
    `db.add_alert(` appears outside an explicit whitelist).

    Returns the inserted id (same as Database.add_alert) or None if it was
    a duplicate (dedupe) -- in that case nothing is dispatched, it's the
    same incident already notified."""
    normalized = normalize_severity(kind, severity)
    alert_id = db.add_alert(kind, normalized, country, message, dedupe_key)
    if alert_id:
        alert_row = {"id": alert_id, "kind": kind, "severity": normalized,
                      "country": country, "message": message}
        notify_alert(db, alert_row)
    return alert_id
