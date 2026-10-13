"""Alerts: the top-bar bell popover and its actions.

GET /ui/alerts        popover fragment: open alerts of the active subject with
                      Acknowledge / Snooze 7 days / Resolve and Mark all read.
GET /alerts           old page: redirects to /?alerts=1 (app.js opens the
                      popover), so links in notifications and bookmarks keep
                      working.
POST /alerts/...      actions. From the popover (fetch, X-SM-Fragment: 1) they
                      answer with the refreshed popover and an X-SM-Toast
                      header; from a plain form they redirect back (PRG).

Routing and the threshold are edited in Settings → Alert routing (#routing)
(#notifications); their POST endpoints stay here and redirect there.
"""
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dashboard.context import (
    is_fragment_request,
    redirect_with_flash,
    safe_next,
    viewer,
)
from dashboard.deps import db
from dashboard.session import audit
from dashboard.ui_helpers import _esc, _pill
from notify.dispatcher import ROUTING_CHANNELS
from processing import alerts as alerts_engine
from report import weekly

router = APIRouter()

_SNOOZE_PRESETS = {"1d": timedelta(days=1), "7d": timedelta(days=7)}
_POPOVER_LIMIT = 30
_SETTINGS_NOTIFY = "/settings#notifications"


def _fmt_when(ts: str | None) -> str:
    text = (ts or "").replace("T", " ")
    try:
        d = datetime.fromisoformat(text[:19])
    except ValueError:
        return _esc(f"{text[:16]} UTC") if text.strip() else ""
    today = datetime.now(UTC).date()
    if d.date() == today:
        return f"Today {d:%H:%M} UTC"
    return f"{d.day} {d:%b} {d:%H:%M} UTC"


def alerts_popover_html() -> str:
    """Bell popover content (also used after every action, in place)."""
    active = db.open_alerts()
    # Bell badge = open (not acknowledged, not snoozed); acknowledged alerts are
    # listed after them in their own group and never counted as open.
    opened = [a for a in active if (a.get("status") or "open") == "open"]
    acked_list = [a for a in active if a.get("status") == "acknowledged"]
    alerts = opened + acked_list
    count = len(opened)
    can_write = viewer().can_write
    items = []
    shown = alerts[:_POPOVER_LIMIT]
    for i, a in enumerate(shown):
        if a.get("status") == "acknowledged" and (i == 0 or shown[i - 1].get("status") != "acknowledged"):
            items.append(f"<div class='alert-grp' data-alerts-acked>Acknowledged ({len(acked_list)})</div>")
        sev = a.get("severity") or "warning"
        unseen = "" if a.get("seen") else " is-unseen"
        acked = a.get("status") == "acknowledged"
        tags = ""
        if a.get("country"):
            tags += f"<span class='tag'>{_esc((a.get('country') or '').upper())}</span>"
        if acked:
            who = f" by {_esc(a['assignee'])}" if a.get("assignee") else ""
            tags += f"<span class='tag'>acknowledged{who}</span>"
        acts = ""
        if can_write:
            hidden = f"<input type='hidden' name='alert_id' value='{a['id']}'>"
            ack = ("" if acked else
                   f"<form class='inline-form' method='post' action='/alerts/ack' data-sm-submit>{hidden}"
                   "<button type='submit' class='btn small'>Acknowledge</button></form>")
            acts = (
                f"<div class='acts'>{ack}"
                f"<form class='inline-form' method='post' action='/alerts/snooze' data-sm-submit>{hidden}"
                "<input type='hidden' name='until' value='7d'>"
                "<button type='submit' class='btn ghost small'>Snooze 7 days</button></form>"
                f"<form class='inline-form' method='post' action='/alerts/resolve' data-sm-submit>{hidden}"
                "<button type='submit' class='btn ghost small'>Resolve</button></form></div>"
            )
        items.append(
            f"<div class='alert-i{unseen}'><div class='li-top'>{_pill(sev)}{tags}"
            f"<span class='when num'>{_fmt_when(a.get('created_at'))}</span></div>"
            f"<div class='msg'>{_esc(a.get('message'))}</div>{acts}</div>"
        )
    items.extend(_deployment_alert_items(alerts))
    more = ""
    if len(alerts) > _POPOVER_LIMIT:
        more = f"<div class='alert-i note'>{len(alerts) - _POPOVER_LIMIT} older alerts not shown.</div>"
    mark_all = ""
    if can_write and any(not a.get("seen") for a in alerts):
        mark_all = ("<form class='inline-form' method='post' action='/alerts/seen' data-sm-submit>"
                    "<button type='submit' class='btn ghost small'>Mark all read</button></form>")
    if items and not opened:
        items.insert(0, "<div class='empty'>No open alerts.</div>")
    body = "".join(items) + more if items else "<div class='empty'>No open alerts.</div>"
    return (
        f"<div data-sm-alerts-count='{count}'>"
        f"<div class='ahd'><h2>Alerts</h2>{mark_all}</div>{body}"
        f"<div class='ahd afoot'><a href='{_SETTINGS_NOTIFY}'>Alert routing and thresholds</a></div>"
        "</div>"
    )


def _deployment_alert_items(shown: list[dict]) -> list[str]:
    """The deployment-wide Apify budget alert is stored under subject 1 (see
    database.runs.BUDGET_ALERT_SUBJECT_ID) but concerns every subject: while it
    is open, other subjects' popovers show it too, read-only (its actions live
    with subject 1; Settings → Search shows it as well). Not counted in the
    subject's badge."""
    try:
        alert = db.current_budget_alert()
    except Exception:  # noqa: BLE001 -- the popover must render regardless
        return []
    if (alert is None or alert.get("status") not in ("open", "acknowledged")
            or any(a.get("id") == alert.get("id") for a in shown)):
        return []
    sev = alert.get("severity") or "warning"
    item = (
        f"<div class='alert-i'><div class='li-top'>{_pill(sev)}<span class='tag'>All subjects</span>"
        f"<span class='when num'>{_fmt_when(alert.get('created_at'))}</span></div>"
        f"<div class='msg'>{_esc(alert.get('message'))}</div>"
        "<div class='acts'><a class='btn ghost small' href='/settings#search'>Apify budget settings</a>"
        "</div></div>"
    )
    return [item]


def _done(request: Request, message: str, next_url: str = "", kind: str = "ok"):
    """Fragment request -> refreshed popover + toast header; else PRG."""
    if is_fragment_request():
        return HTMLResponse(alerts_popover_html(),
                            headers={"X-SM-Toast": quote(message), "Cache-Control": "no-store"})
    return redirect_with_flash(safe_next(next_url, "/?alerts=1"), message, kind)


@router.get("/ui/alerts", response_class=HTMLResponse, tags=["alerts"])
async def alerts_popover():
    return HTMLResponse(alerts_popover_html(), headers={"Cache-Control": "no-store"})


@router.get("/alerts", tags=["alerts"])
async def alerts_page():
    return RedirectResponse("/?alerts=1", status_code=303)


@router.post("/alerts/check", tags=["alerts"])
async def alerts_check(request: Request, next: str = Form("", max_length=2048)):
    # The same function the daily `alerts_check` job runs (scheduler/jobs.py),
    # so "Check now" and the schedule always run the same checks.
    result = weekly.run_alert_checks(db)
    n_new = len(result["new_alerts"]) + sum(c.get("new", 0) for c in result["checks"].values())
    subject = db.current_subject()
    who = (subject.get("name") or subject.get("query") or "").strip() or "this subject"
    msg = (f"{n_new} new alert(s) detected for {who}." if n_new
           else f"Checked {who}. No new alerts.")
    kind = "ok"
    if result["error"]:
        msg += f" The overdue-notes check failed: {result['error']}"
        kind = "error"
    return redirect_with_flash(safe_next(next, _SETTINGS_NOTIFY), msg, kind)


@router.post("/alerts/routing", tags=["alerts"])
async def alerts_routing(request: Request, critical: str = Form("", max_length=64), warning: str = Form("", max_length=64),
                         info: str = Form("", max_length=64)):
    routing = (("critical", critical), ("warning", warning), ("info", info))
    bad = [ch for _s, ch in routing if ch not in ROUTING_CHANNELS]
    if bad:
        return redirect_with_flash(_SETTINGS_NOTIFY, f"Not saved: unknown channel {bad[0]!r}.", "error")
    current = db.get_routing()
    if all(current.get(sev) == ch for sev, ch in routing):
        return redirect_with_flash(_SETTINGS_NOTIFY, "No changes to the alert routing.", "info")
    for severity, channel in routing:
        db.set_routing(severity, channel)
    audit(request, "alert_routing_save", critical=critical, warning=warning, info=info)
    return redirect_with_flash(_SETTINGS_NOTIFY, "Alert routing saved.")


@router.post("/alerts/seen", tags=["alerts"])
async def alerts_seen(request: Request, next: str = Form("", max_length=2048)):
    db.mark_alerts_seen()
    return _done(request, "All alerts marked as read.", next)


@router.post("/alerts/thresholds", tags=["alerts"])
async def alerts_thresholds(request: Request, sev: str = Form("", max_length=64), neg: str = Form("", max_length=64)):
    try:
        sev_n = int(sev) if sev.strip() else alerts_engine.SEVERITY_THRESHOLD_DEFAULT
        neg_n = int(neg) if neg.strip() else 1
    except ValueError:
        return redirect_with_flash(_SETTINGS_NOTIFY, "Not saved: thresholds must be whole numbers.",
                                   "error")
    sev_n, neg_n = max(0, min(sev_n, 100)), max(0, min(neg_n, 50))
    if (str(sev_n) == db.get_setting("ALERT_SEVERITY_THRESHOLD", "")
            and str(neg_n) == db.get_setting("ALERT_NEG_THRESHOLD", "")):
        return redirect_with_flash(_SETTINGS_NOTIFY, "No changes to the thresholds.", "info")
    db.set_setting("ALERT_SEVERITY_THRESHOLD", str(sev_n))
    db.set_setting("ALERT_NEG_THRESHOLD", str(neg_n))
    audit(request, "alert_thresholds_save", sev=sev_n, neg=neg_n)
    return redirect_with_flash(_SETTINGS_NOTIFY, "Thresholds saved.")


def _alert_id(raw: str) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


@router.post("/alerts/ack", tags=["alerts"])
async def alerts_ack(request: Request, alert_id: str = Form("", max_length=64), assignee: str = Form("", max_length=200),
                     next: str = Form("", max_length=2048)):
    aid = _alert_id(alert_id)
    if aid is None or db.get_alert(aid) is None:
        return _done(request, "That alert no longer exists.", next, "error")
    assignee = assignee.strip() or viewer().username
    db.ack_alert(aid, assignee=assignee)
    audit(request, "alert_ack", resource_type="alert", resource_id=aid, assignee=assignee)
    return _done(request, "Alert acknowledged.", next)


@router.post("/alerts/snooze", tags=["alerts"])
async def alerts_snooze(request: Request, alert_id: str = Form("", max_length=64), until: str = Form("7d", max_length=64),
                        next: str = Form("", max_length=2048)):
    aid = _alert_id(alert_id)
    if aid is None or db.get_alert(aid) is None:
        return _done(request, "That alert no longer exists.", next, "error")
    now = datetime.now(UTC).replace(tzinfo=None)
    delta = _SNOOZE_PRESETS.get(until, timedelta(days=7))
    until_iso = (now + delta).isoformat(timespec="seconds")
    db.snooze_alert(aid, until_iso)
    audit(request, "alert_snooze", resource_type="alert", resource_id=aid, until=until_iso)
    days = delta.days
    return _done(request, f"Alert snoozed for {days} day{'s' if days != 1 else ''}.", next)


@router.post("/alerts/resolve", tags=["alerts"])
async def alerts_resolve(request: Request, alert_id: str = Form("", max_length=64), next: str = Form("", max_length=2048)):
    aid = _alert_id(alert_id)
    if aid is None or db.get_alert(aid) is None:
        return _done(request, "That alert no longer exists.", next, "error")
    db.resolve_alert(aid, by="human")
    audit(request, "alert_resolve", resource_type="alert", resource_id=aid, resolved_by="human")
    return _done(request, "Alert resolved.", next)
