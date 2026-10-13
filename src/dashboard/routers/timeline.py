"""Timeline: the Timeline side panel (/ui/timeline), POST /timeline/add and
POST /timeline/delete (PRG + toast; a fetch submit from the panel gets the
refreshed panel back). The old /timeline page redirects to /results with the
panel open.

User events (logged here, deletable) show their measured effect in plain
words ("Two weeks later: positive share +8 points, negative share −5
points"); detected events (computed from consecutive runs, or recorded by the
URL lifecycle) are not deletable.

Migrated from dashboard.main (timeline_page/timeline_add/timeline_delete
and detect_anomalies, used exclusively by this page). Same logic; only
the rendering changes to Jinja2. See brief 9.1B.

4.5 (deep dive §4.5, timeline correlated with the SERP): each MANUAL
event (not the detected anomalies) shows the delta of the 7/14/30-day
series via processing.event_impact.event_impact_from_rows() -- see that
module for the semantics of windows and sign ("positive = improvement"
across the 3 metrics). The event annotation on the /analysis chart
ALREADY exists since 4.1 (dashboard.routers.analysis, events_in_range) --
it's not duplicated here.
"""
import re
from datetime import UTC, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dashboard import ui
from dashboard.context import is_fragment_request, redirect_with_flash, viewer
from dashboard.deps import db
from dashboard.main import _esc, audit
from dashboard.ui_helpers import fmt_day, fragment_or_page, overlay_url, toast_header
from processing.event_impact import (
    event_impact_from_rows,
    impactable_events,
    parse_event_date,
    rows_for_events,
)

router = APIRouter()

# ponytail: _IMPACT_CAP=50 bounds how many ELIGIBLE events (manual, with
# a past date -- see impactable_events, review 4.5 #2) show the deltas
# block. After fix #7 the cost is NO LONGER one query per event: there's
# a SINGLE call to series_stats for the combined range and the per-event
# arithmetic is in memory -- the cap only limits trivial HTML/computation.
# Upgrade trigger: if /timeline feels slow with thousands of events, give
# list_events a real LIMIT.
_IMPACT_CAP = 50


def detect_anomalies():
    """
    Compares consecutive dates per country and returns anomalies:
    [{date, country, sev, kind, text}] -- kind: 'new_negative' |
    'positive_lost' | 'positive_dropped'; sev: 'high' for the first two
    (shown as "major"), 'med' for a positive dropping 3+ places.
    Text already in English (current standard, chore F2-close; main.py's
    ES->EN translation layer was removed in UX-1 task 5).
    """
    out = []
    for c in db.distinct_countries():
        dates = db.dates_for_country(c)  # desc
        for i in range(len(dates) - 1):
            new_d, old_d = dates[i], dates[i + 1]
            cur = {r["url"]: r for r in db.get_results_by_run(db.get_latest_run_id(c, new_d))}
            prev = {r["url"]: r for r in db.get_results_by_run(db.get_latest_run_id(c, old_d))}
            for url, r in cur.items():
                if r.get("classification") == "negative" and prev.get(url, {}).get("classification") != "negative":
                    out.append({"date": new_d, "country": c.upper(), "sev": "high",
                                "kind": "new_negative", "domain": r.get("domain") or "",
                                "text": f"New negative result: {r.get('domain')} at position "
                                        f"{r.get('position')}"})
            for url, r in prev.items():
                if r.get("classification") == "positive" and url not in cur:
                    out.append({"date": new_d, "country": c.upper(), "sev": "high",
                                "kind": "positive_lost", "domain": r.get("domain") or "",
                                "text": f"Positive result {r.get('domain')} dropped out of the results"})
                elif r.get("classification") == "positive" and url in cur:
                    pa, pb = cur[url].get("position"), r.get("position")
                    if isinstance(pa, int) and isinstance(pb, int) and pa - pb >= 3:
                        out.append({"date": new_d, "country": c.upper(), "sev": "med",
                                    "kind": "positive_dropped", "domain": r.get("domain") or "",
                                    "text": f"Positive result {r.get('domain')} fell from position "
                                            f"{pb} to {pa}"})
    out.sort(key=lambda e: e["date"], reverse=True)
    return out


_PANEL = overlay_url("/ui/timeline", "panel")
_WINDOW_WORDS = {7: "One week later", 14: "Two weeks later", 30: "A month later"}
_GONE_RE = re.compile(r"^Negative URL gone from top-N in (\w+): (.+)$")


def _points(value: float | None) -> str:
    if value is None:
        return "no data"
    sign = "+" if value > 0 else ("−" if value < 0 else "±")
    n = abs(value)
    return f"{sign}{n:.0f} points" if n >= 1 or n == 0 else f"{sign}{n:.1f} points"


def impact_words(impact: dict) -> str:
    """The measured effect of an event in plain words, from the longest window
    with data on both sides (14 days preferred)."""
    for w in (14, 7, 30):
        m = impact.get(w)
        if not m or m["insufficient"]:
            continue
        pos = m["pct_positive"]["delta"]
        neg = m["pct_negative"]["delta"]
        neg_natural = -neg if neg is not None else None   # event_impact inverts pct_negative
        text = (f"{_WINDOW_WORDS.get(w, f'{w} days later')}: positive share {_points(pos)}, "
                f"negative share {_points(neg_natural)}.")
        if m.get("n_after", w) < w:
            text += f" Based on {m['n_after']} of {w} days so far."
        return text
    return "Not enough search data around this date to measure the effect yet."


def _detected_text(e: dict) -> str:
    """Plain wording for events the system recorded (no "top-N" jargon)."""
    desc = e.get("description") or ""
    m = _GONE_RE.match(desc)
    if m:
        return f"Negative result {m.group(2)} dropped out of the results ({m.group(1).upper()})"
    return desc


def _item(title: str, tag: str, day: str, sub: str = "", action: str = "", cls: str = "") -> str:
    return (f"<div class='li {cls}'><div class='li-top'><b>{title}</b><span class='tag'>{_esc(tag)}</span>"
            f"<span class='rt muted num'>{_esc(fmt_day(day, year=False))}</span>{action}</div>"
            + (f"<div class='li-sub'>{sub}</div>" if sub else "") + "</div>")


def timeline_panel_html(error: str = "", domain: str = "", values: dict | None = None) -> str:
    values = values or {}
    events = db.list_events()
    anomalies = detect_anomalies()
    today = datetime.now(UTC).replace(tzinfo=None).date().isoformat()
    eligible = impactable_events(events, today, _IMPACT_CAP)
    impact_ids = {e["id"] for e in eligible}
    by_day = rows_for_events(db, [e["event_date"] for e in eligible]) if eligible else {}
    domain = (domain or "").strip().lower()

    items = []
    for e in events:
        day = (e.get("event_date") or "")[:10]
        if e.get("kind") == "manual":
            if domain:
                continue
            subs = []
            if e.get("expected_effect"):
                subs.append(f"Expected: {_esc(e['expected_effect'])}")
            if day >= today and parse_event_date(day):
                subs.append("Too recent to measure the effect yet.")
            elif e["id"] in impact_ids:
                subs.append(_esc(impact_words(event_impact_from_rows(by_day, e["event_date"]))))
            action = ""
            if viewer().can_write:
                action = ("<form class='inline-form' action='/timeline/delete' method='post' data-sm-submit>"
                          f"<input type='hidden' name='event_id' value='{e['id']}'>"
                          "<button type='submit' class='btn ghost small' aria-label='Delete event'"
                          f" data-sm-confirm='Delete “{_esc(e['description'])}”? This only removes it from"
                          " the timeline.' data-confirm-title='Delete this event?' data-confirm-label='Delete'>"
                          "Delete</button></form>")
            items.append((day, _item(_esc(e["description"]), "your event", day, "<br>".join(subs), action)))
        else:
            text = _detected_text(e)
            if domain and domain not in text.lower():
                continue
            sub = ("Recorded automatically. It counts as a Win." if e.get("kind") == "url_gone"
                   else "Recorded automatically.")
            items.append((day, _item(_esc(text), "detected", day, sub, cls="li-detected")))
    for a in anomalies:
        if domain and a.get("domain", "").lower() != domain:
            continue
        sub = {"new_negative": "Detected by comparing two runs.",
               "positive_lost": "Detected by comparing two runs.",
               "positive_dropped": "Detected by comparing two runs."}.get(a.get("kind"), "")
        items.append((a["date"], _item(f"{_esc(a['text'])} ({_esc(a['country'])})", "detected", a["date"],
                                       sub, cls="li-detected")))
    items.sort(key=lambda x: x[0], reverse=True)

    form = ""
    if viewer().can_write and not domain:
        err = f"<div class='hint hint-error' role='alert'>{_esc(error)}</div>" if error else ""
        open_attr = " open" if error or values else ""
        form = (
            f"<details class='log-event'{open_attr}><summary class='btn'>＋ Log an event</summary>"
            f"<form class='stack' action='/timeline/add' method='post' data-sm-submit>{err}"
            "<div class='row'><div class='field' style='flex:0 0 160px'><label for='ev-date'>Date</label>"
            f"<input id='ev-date' type='date' name='event_date' required value='{_esc(values.get('event_date') or today)}'></div>"
            "<div class='field'><label for='ev-desc'>What happened</label>"
            f"<input id='ev-desc' name='description' required maxlength='300' placeholder='Press release published'"
            f" value='{_esc(values.get('description') or '')}'></div></div>"
            "<div class='field'><label for='ev-exp'>Expected effect (optional)</label>"
            f"<input id='ev-exp' name='expected_effect' maxlength='300' placeholder='More positive results'"
            f" value='{_esc(values.get('expected_effect') or '')}'></div>"
            "<div><button type='submit' class='btn primary'>Save event</button></div></form></details>"
        )
    intro = ("<p class='note'>Things you did (press releases, removal requests…) next to what changed in "
             "the results, with the measured effect two weeks later.</p>")
    if domain:
        intro = (f"<p class='note'>Detected events for <b>{_esc(domain)}</b>. "
                 "<a href='/ui/timeline' data-sm-nav>Show the whole timeline</a></p>")
    listing = (f"<div class='list'>{''.join(html for _d, html in items)}</div>" if items else
               "<div class='empty'>Nothing on the timeline yet.</div>")
    return ui.overlay("Timeline", intro + form + listing)


@router.get("/ui/timeline", response_class=HTMLResponse, tags=["ui"])
async def ui_timeline(domain: str = ""):
    return fragment_or_page("Timeline", timeline_panel_html(domain=domain), active="/results")


@router.get("/timeline")
async def timeline_page():
    """Old page: the Timeline panel opens over Results."""
    return RedirectResponse(_PANEL, status_code=303)


def _done(message: str, kind: str = "ok"):
    if is_fragment_request():
        return HTMLResponse(timeline_panel_html(), headers=toast_header(message))
    return redirect_with_flash(_PANEL, message, kind)


@router.post("/timeline/add", response_class=HTMLResponse)
async def timeline_add(request: Request, event_date: str = Form("", max_length=64), description: str = Form("", max_length=10000),
                       expected_effect: str = Form(default="", max_length=10000)):
    # Review 4.5 #1: the date is validated BEFORE writing (a garbage date used
    # to degrade the impact/PDF).
    values = {"event_date": event_date, "description": description, "expected_effect": expected_effect}
    problem = ""
    if parse_event_date(event_date.strip()) is None:
        problem = "Not saved: pick a valid date."
    elif not description.strip():
        problem = "Not saved: describe what happened."
    if problem:
        if is_fragment_request():
            return HTMLResponse(timeline_panel_html(problem, values=values), status_code=400)
        return redirect_with_flash(_PANEL, problem, "error")
    db.add_event(event_date.strip(), description.strip(), kind="manual",
                 expected_effect=(expected_effect or "").strip())
    audit(request, "timeline_event_add", resource_type="event",
          event_date=event_date.strip(), description=description.strip(),
          expected_effect=(expected_effect or "").strip())
    return _done("Event logged.")


@router.post("/timeline/delete", response_class=HTMLResponse)
async def timeline_delete(request: Request, event_id: str = Form("", max_length=64)):
    try:
        eid = int(event_id)
    except ValueError:
        return _done("Nothing was deleted.", "info")
    db.delete_event(eid)
    audit(request, "timeline_event_delete", resource_type="event", resource_id=eid)
    return _done("Event deleted.")


__all__ = ["detect_anomalies", "impact_words", "timeline_panel_html"]
