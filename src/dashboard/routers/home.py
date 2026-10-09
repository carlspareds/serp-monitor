"""Overview (/): the state of the ACTIVE subject at a glance.

Layout follows the approved mockup: header (subject · countries · latest run),
a review nudge, four KPIs (positive and negative
share with deltas vs the previous run, open alerts, Wins in 90 days), the
trend of positive/negative share per run (inline SVG, theme tokens), share of
voice + Wins, and "What changed since <previous run>" built on
processing.diff.run_diff (the same diff as Compare).

Everything is scoped to the active subject (the request ContextVar set by
dashboard.context.SubjectMiddleware) and to ONE country: the latest run of the
selected country (a selector appears when the subject has more than one).
The former /analysis page folded into this one (it redirects here).

One home per feature: New search, Review, Compare and Reports live on Results.
The Overview only carries single shortcuts that open the same dialogs: the
review nudge is a plain link to the Review dialog, "What changed" has one
"Open compare" button, and a subject with no runs gets one "Run first search"
button (the New search dialog). No Reports / New search in the header.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from dashboard import nav
from dashboard.deps import db
from dashboard.ui_helpers import (
    _esc,
    active_project_and_subject,
    ai_note_hint,
    ordered_countries,
    page,
    subject_label,
)
from processing.diff import run_diff
from processing.lifecycle import _gone_after_runs

router = APIRouter()

_TREND_RUNS = 12
_CHANGES_SHOWN = 8


# --------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------- #
def _parse(ts) -> datetime | None:
    text = str(ts or "").strip().replace("Z", "")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:19])
    except ValueError:
        return None


def _day(ts) -> str:
    """'6 Oct' (no year)."""
    d = _parse(ts)
    return f"{d.day} {d.strftime('%b')}" if d else "—"


def _when(ts) -> str:
    """'6 Oct 2026, 06:00 UTC'."""
    d = _parse(ts)
    return f"{d.day} {d.strftime('%b %Y')}, {d.strftime('%H:%M')} UTC" if d else "—"


def _domain(r: dict) -> str:
    return r.get("domain") or urlsplit(r.get("url") or "").netloc or (r.get("url") or "")


def _pct(n: int, total: int) -> int:
    return round(100 * n / total) if total else 0


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


# --------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------- #
def _country_runs(country: str) -> list[dict]:
    """Runs of `country` for the active subject, OLDEST first, production
    engine (google) when present, only runs that stored results."""
    runs = [r for r in db.list_runs(limit=500)
            if (r.get("country_code") or "").lower() == country.lower()
            and int(r.get("total_results") or 0) > 0]
    google = [r for r in runs if (r.get("engine") or "google") == "google"]
    runs = google or runs
    runs.sort(key=lambda r: r.get("id") or 0)
    return runs


def _fresh_install() -> bool:
    """No run in ANY subject and nothing tracked yet: the setup isn't done."""
    try:
        any_run = bool(db.for_all_subjects().list_runs(limit=1))
    except Exception:  # noqa: BLE001
        any_run = False
    return not any_run and not db.get_tracked_query()


# --------------------------------------------------------------------- #
# Pieces
# --------------------------------------------------------------------- #
def trend_svg(points: list[tuple[str, int, int]], country: str = "") -> str:
    """Positive / negative share per run as an inline SVG line+area chart.
    points: [(label, positive %, negative %)] oldest first."""
    if not points:
        return "<p class='note'>No runs yet.</p>"
    w, h, left, right, top, bottom = 560, 190, 34, 12, 12, 28
    iw, ih = w - left - right, h - top - bottom
    n = len(points)

    def x(i: int) -> float:
        return left + (iw * i / (n - 1) if n > 1 else iw / 2)

    def y(v: float) -> float:
        return top + ih - ih * v / 100

    def line(idx: int) -> str:
        return " ".join(f"{'L' if i else 'M'}{x(i):.1f},{y(p[idx]):.1f}" for i, p in enumerate(points))

    def area(idx: int) -> str:
        return f"{line(idx)} L{x(n - 1):.1f},{y(0):.1f} L{x(0):.1f},{y(0):.1f} Z"

    parts = []
    for v in (0, 25, 50, 75, 100):
        parts.append(f"<line x1='{left}' x2='{w - right}' y1='{y(v):.1f}' y2='{y(v):.1f}'"
                     " stroke='var(--line)' stroke-width='1'/>"
                     f"<text x='{left - 6}' y='{y(v) + 4:.1f}' font-size='10' text-anchor='end'"
                     f" fill='var(--faint)'>{v}%</text>")
    step = max(1, math.ceil(n / 6))
    for i, (label, _p, _n) in enumerate(points):
        if i % step == 0 or i == n - 1:
            if i != n - 1 and n - 1 - i < step:
                continue   # keep the last label clear of the previous one
            anchor = "end" if (i == n - 1 and n > 1) else ("start" if i == 0 and n > 1 else "middle")
            parts.append(f"<text x='{x(i):.1f}' y='{h - 8}' font-size='10.5' text-anchor='{anchor}'"
                         f" fill='var(--faint)'>{_esc(label)}</text>")
    if n > 1:
        parts.append(f"<path d='{area(1)}' fill='var(--pos)' opacity='.08'/>"
                     f"<path d='{line(1)}' fill='none' stroke='var(--pos)' stroke-width='2.2'"
                     " stroke-linejoin='round' stroke-linecap='round'/>"
                     f"<path d='{area(2)}' fill='var(--neg)' opacity='.06'/>"
                     f"<path d='{line(2)}' fill='none' stroke='var(--neg)' stroke-width='2.2'"
                     " stroke-linejoin='round' stroke-linecap='round'/>")
    last = points[-1]
    parts.append(f"<circle cx='{x(n - 1):.1f}' cy='{y(last[1]):.1f}' r='4' fill='var(--pos)'/>"
                 f"<circle cx='{x(n - 1):.1f}' cy='{y(last[2]):.1f}' r='4' fill='var(--neg)'/>")
    where = f" in {country.upper()}" if country else ""
    label = (f"Share of positive and negative results per run{where}, last {_plural(n, 'run')}. "
             f"Latest: {last[1]}% positive, {last[2]}% negative.")
    rows = "".join(f"<tr><th scope='row'>{_esc(p[0])}</th><td>{p[1]}%</td><td>{p[2]}%</td></tr>"
                   for p in points)
    table = ("<table class='sr-only'><caption>Share per run</caption><tr><th>Run</th>"
             f"<th>Positive</th><th>Negative</th></tr>{rows}</table>")
    return (f"<div class='chart'><svg viewBox='0 0 {w} {h}' role='img' aria-label='{_esc(label)}'"
            f" preserveAspectRatio='xMidYMid meet'>{''.join(parts)}</svg>{table}</div>")


def _delta(cur: int, prev: int | None, prev_day: str, good_up: bool) -> str:
    if prev is None:
        return "<div class='delta'>First run</div>"
    d = cur - prev
    if d == 0:
        return f"<div class='delta num'>No change vs {_esc(prev_day)}</div>"
    good = (d > 0) == good_up
    sign = "+" if d > 0 else "−"
    return (f"<div class='delta num {'up' if good else 'down'}'>{sign}{abs(d)} pts vs "
            f"{_esc(prev_day)}</div>")


def _kpi(label: str, value: str, delta_html: str, swatch: str = "") -> str:
    sw = f"<span class='swatch {swatch}' aria-hidden='true'></span>" if swatch else ""
    return (f"<div class='card kpi'><div class='label'>{sw}{_esc(label)}</div>"
            f"<div class='value num'>{value}</div>{delta_html}</div>")


def _change_rows(diff: dict) -> list[str]:
    items: list[tuple[int, str]] = []
    for r in diff["entered"]:
        cls = r.get("classification") or "neutral"
        tone = {"negative": "bad", "positive": "good"}.get(cls, "")
        pending = r.get("review_status") == "pending"
        note = f"{cls}, waiting for review" if pending else cls
        items.append((1000, (f"<div class='change'><span class='chg-ico {tone}' aria-hidden='true'>+</span>"
                            f"<div><b>{_esc(_domain(r))}</b> entered at position {_esc(r.get('position'))}"
                            f" <span class='muted'>· {_esc(note)}</span></div><span></span></div>")))
    for r in diff["exited"]:
        cls = r.get("classification") or "neutral"
        tone = {"negative": "good", "positive": "bad"}.get(cls, "")
        items.append((900, (f"<div class='change'><span class='chg-ico {tone}' aria-hidden='true'>−</span>"
                           f"<div><b>{_esc(_domain(r))}</b> left the results"
                           f" <span class='muted'>· was position {_esc(r.get('position'))}, {_esc(cls)}"
                           "</span></div><span></span></div>")))
    for r, d in diff["moved"]:
        cls = r.get("classification") or "neutral"
        up = d > 0
        tone = ""
        if cls == "positive":
            tone = "good" if up else "bad"
        elif cls == "negative":
            tone = "bad" if up else "good"
        arrow = "↑" if up else "↓"
        word = "up" if up else "down"
        items.append((abs(d), (f"<div class='change'><span class='chg-ico {tone}' aria-hidden='true'>{arrow}</span>"
                              f"<div><b>{_esc(_domain(r))}</b> moved {word} {_plural(abs(d), 'position')}"
                              f" to {_esc(r.get('position'))} <span class='muted'>· {_esc(cls)}</span></div>"
                              "<span></span></div>")))
    items.sort(key=lambda t: -t[0])
    return [html for _k, html in items]


def _empty(title: str, text: str, action: str) -> str:
    return (f"<div class='card empty-cta'><h2>{_esc(title)}</h2><p>{text}</p>"
            f"<div class='actions-row'>{action}</div></div>")


def _first_search_button() -> str:
    """The one New search shortcut on the Overview: only for a subject with no
    runs (operators and admins). Opens the same dialog as Results → New search."""
    from dashboard.context import viewer

    if not viewer().can_write:
        return "<a class='btn' href='/results'>Open Results</a>"
    return (f"<a class='btn primary' href='/ui/search' data-modal data-title='New search'"
            f" data-first-search>{nav.icon('plus')}Run first search</a>")


# --------------------------------------------------------------------- #
# Route
# --------------------------------------------------------------------- #
@router.get("/", response_class=HTMLResponse, tags=["home"])
async def home(request: Request, country: str = ""):
    _project, subject = active_project_and_subject()
    name = subject_label(subject)
    countries = ordered_countries([c.lower() for c in db.distinct_countries()])

    if not countries:
        if _fresh_install():
            body = _empty("Welcome to SERP Monitor",
                          "Add your Apify key and the name you want to track, then run a first "
                          "search. The setup wizard walks you through it in a few minutes.",
                          "<a class='btn primary' href='/setup'>Open the setup wizard</a>"
                          "<a class='btn ghost' href='/onboarding'>Read the getting started guide</a>")
        else:
            tracked = ", ".join(c.upper() for c in subject.get("countries") or []) or "no countries yet"
            body = _empty(f"No searches yet for {name}",
                          f"Run a first search to see where {_esc(name)} ranks and what changes over "
                          f"time. Tracked countries: {_esc(tracked)}.",
                          _first_search_button())
        return page("Overview", body, active="/", sub=_esc(name))

    if country.lower() not in countries:
        preferred = [c for c in (subject.get("countries") or []) if c in countries]
        country = preferred[0] if preferred else countries[0]
    country = country.lower()

    runs = _country_runs(country)
    if not runs:
        body = _empty(f"No complete runs for {country.upper()} yet",
                      "The searches for this country haven't stored any results yet.",
                      _first_search_button())
        return page("Overview", body, active="/", sub=_esc(name))

    latest = runs[-1]
    prev = runs[-2] if len(runs) > 1 else None
    total = int(latest.get("total_results") or 0)
    p, ng, nu = (int(latest.get(k) or 0) for k in ("positive", "negative", "neutral"))
    pos_pct, neg_pct = _pct(p, total), _pct(ng, total)
    prev_day = _day(prev.get("created_at")) if prev else ""
    prev_pos = _pct(int(prev.get("positive") or 0), int(prev.get("total_results") or 0)) if prev else None
    prev_neg = _pct(int(prev.get("negative") or 0), int(prev.get("total_results") or 0)) if prev else None

    # Header sub line: subject · country (selector when several) · latest run.
    if len(countries) > 1:
        opts = "".join(f"<option value='{_esc(c)}'{' selected' if c == country else ''}>{_esc(c.upper())}</option>"
                       for c in countries)
        country_html = (f"<form class='country-form' method='get' action='/'>"
                        f"<label class='sr-only' for='ov-country'>Country</label>"
                        f"<select id='ov-country' name='country' onchange='this.form.submit()'>{opts}</select>"
                        "<noscript><button class='btn small' type='submit'>Show</button></noscript></form>")
    else:
        country_html = _esc(country.upper())
    sub = f"{_esc(name)} · {country_html} · latest run {_esc(_when(latest.get('created_at')))}"

    # Nudge
    pending = db.count_pending_review()
    nudge = ""
    if pending:
        nudge = (f"<div class='nudge'><p><b>{_plural(pending, 'result needs', 'results need')} your review.</b>"
                 " The rules couldn't decide them. It takes about a minute. "
                 "<a href='/ui/review' data-modal data-title='Review'>Start review</a></p></div>")

    # KPIs
    # Open = not acknowledged, not snoozed (same definition as the bell badge).
    open_alerts = [a for a in db.open_alerts() if (a.get("status") or "open") == "open"]
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    new_today = sum(1 for a in open_alerts if (a.get("created_at") or "")[:10] == today)
    victories = db.list_victories(days=90, country=country)
    win_delta = (f"<div class='delta up'>{_esc(urlsplit(victories[0].get('url') or '').netloc or 'A negative')}"
                 " dropped out</div>" if victories else "<div class='delta'>None in the last 90 days</div>")
    kpis = (
        "<div class='kpis'>"
        + _kpi("Positive", f"{pos_pct}%", _delta(pos_pct, prev_pos, prev_day, True), "pos")
        + _kpi("Negative", f"{neg_pct}%", _delta(neg_pct, prev_neg, prev_day, False), "neg")
        + _kpi("Open alerts", str(len(open_alerts)),
               f"<div class='delta'>{'None' if not new_today else new_today} new today</div>")
        + _kpi("Wins (90 days)", str(len(victories)), win_delta)
        + "</div>"
    )

    # Trend
    points = []
    for r in runs[-_TREND_RUNS:]:
        t = int(r.get("total_results") or 0)
        points.append((_day(r.get("created_at")), _pct(int(r.get("positive") or 0), t),
                       _pct(int(r.get("negative") or 0), t)))
    trend = (
        "<div class='card'><div class='card-head'><h2>Trend</h2>"
        "<span class='legend'><span><span class='swatch pos' aria-hidden='true'></span>positive</span>"
        "<span><span class='swatch neg' aria-hidden='true'></span>negative</span></span></div>"
        f"{trend_svg(points, country)}</div>"
    )

    # Share of voice + Wins
    segs = "".join(f"<span style='flex:{n};background:var(--{tok})'></span>"
                   for n, tok in ((p, "pos"), (ng, "neg"), (nu, "neu")) if n)
    gone_n = _gone_after_runs(db)
    wins_html = "".join(
        f"<div class='li' style='border:0;padding:4px 0'><div class='li-top'>"
        f"<b>{_esc(urlsplit(v.get('url') or '').netloc or v.get('url'))}</b>"
        f"<span class='tag'>gone after {_plural(gone_n, 'run')}</span></div>"
        f"<span class='li-sub'>Negative result no longer in the results since "
        f"{_esc(_day(v.get('gone_since')))}.</span></div>"
        for v in victories[:3]
    ) or ("<p class='note'>No wins in the last 90 days. A win is a negative result that stays out "
          f"of the results for {_plural(gone_n, 'run')} in a row.</p>")
    sov = (
        "<div class='card'><div class='card-head'><h2>Share of voice</h2>"
        f"<span class='muted num'>{_plural(total, 'result')}</span></div>"
        f"<div class='sov' role='img' aria-label='{p} positive, {ng} negative, {nu} neutral'>{segs}</div>"
        "<div class='legend'>"
        f"<span><span class='swatch pos' aria-hidden='true'></span>{p} positive</span>"
        f"<span><span class='swatch neg' aria-hidden='true'></span>{ng} negative</span>"
        f"<span><span class='swatch neu' aria-hidden='true'></span>{nu} neutral</span></div>"
        "<hr class='divider'><div class='card-head' style='margin-bottom:6px'><h2>Wins</h2></div>"
        f"{wins_html}</div>"
    )

    # What changed
    if prev:
        a, b = prev.get("apify_run_id"), latest.get("apify_run_id")
        rows = _change_rows(run_diff(db, a, b))
        compare_url = f"/ui/compare?a={quote(str(a))}&b={quote(str(b))}"
        more = ""
        if len(rows) > _CHANGES_SHOWN:
            more = (f"<p class='note' style='margin-top:8px'>{len(rows) - _CHANGES_SHOWN} more: "
                    "open compare to see every change.</p>")
        body_changes = ("<div class='changes'>" + "".join(rows[:_CHANGES_SHOWN]) + "</div>" + more
                        if rows else "<p class='note'>Nothing entered, left or moved between the two latest runs.</p>")
        changes = (
            f"<div class='card'><div class='card-head'><h2>What changed since {_esc(prev_day)}</h2>"
            f"<a class='btn small' href='{_esc(compare_url)}' data-modal data-modal-wide"
            " data-title='Compare runs'>Open compare</a></div>"
            f"{body_changes}</div>"
        )
    else:
        changes = ("<div class='card'><div class='card-head'><h2>What changed</h2></div>"
                   f"<p class='note'>This is the first run for {_esc(country.upper())}. Changes show up "
                   "after the next run.</p></div>")

    ai_hint = ai_note_hint([latest.get("ai_note")])
    body = (f"<div class='ov-grid'>{ai_hint}{nudge}{kpis}<div class='two'>{trend}{sov}</div>{changes}</div>")
    return page("Overview", body, active="/", sub=sub)
