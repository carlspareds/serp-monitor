"""Impact of a timeline event on the series (4.5, deep dive §4.5).

`event_impact_from_rows()` compares the GLOBAL daily series (Database.
series_stats, 4.1 -- events don't have a country, see brief 4.5) before/
after an event's date, for each window in `windows` (7/14/30 days by
default) -- PURE arithmetic over an already-queried `by_day`.
`rows_for_events()` is that query: callers with MANY events (/timeline,
PDF) query series_stats ONCE for the full range and distribute it (review
4.5 #7: with country='' the query is a full scan of search_results; N
events must NOT cost N scans). `impacts_at()` (F3-close top-5 #2) chains
both for the common case of "a single window, several already-eligible
events" -- the PDF's "Actions & impact" section (export.pdf_report)
uses it instead of repeating the query+loop pattern.

Windows (design, chosen and documented -- brief 4.5 asked for a decision):
  - "before" = average of the metric over the W days BEFORE the event,
    EXCLUDING the event day: [event-W, event-1].
  - "after"  = average of the metric over the following W days, INCLUDING
    the event day: [event, event+W-1]. The event day is included because
    an action (publication, statement) can have a measurable effect the
    same day it happens.
  - `delta = after - before`, except for the sign inversion of
    `pct_negative` documented below.

`series_stats` only returns days with real data -- the average is computed
over the days WITH data in each half, gaps are never filled with 0. It's
also an average of DAILY AVERAGES not weighted by volume: a day with 1
result weighs the same as one with 300 (review 4.5 #10 -- documented as a
known limitation; weighting by `total` is the upgrade if some day
distorts).

Partial coverage (review 4.5 #4): each window returns `n_before` and
`n_after` (days WITH data in each half). A window whose "after" span
crosses TODAY is averaged over the days that exist -- the caller must show
the coverage ("(3/30d)") so that 3 days of after aren't read as
confirmation over 30 days.

Metrics (review 4.5 #6): pct_positive, pct_negative and neg_avg_position --
all three volume-normalized. The raw `negative` count was dropped:
series_stats(country='') aggregates all countries for the day, so a new
country or a cadence change (1→3 runs/day) inflated the count ~3x and the
delta reported a made-up worsening.

"positive = improvement" semantics in `delta` (all 3 metrics):
  - pct_positive:      delta = after - before  (natural sign).
  - pct_negative:      delta = before - after  (INVERTED: a lower % of
                        negatives is an improvement). The RENDER must show
                        the change with its natural sign (-delta) and color
                        it with the normalized one (review 4.5 #5).
  - neg_avg_position:  delta = after - before  (natural sign, semantics
                        inverted relative to a "normal" position: the
                        average position of NEGATIVES going up = they sink
                        = improvement).

`insufficient` (per window): True if either half has no day with data, OR
if `event_date` isn't a valid ISO date (review 4.5 #1: a garbage row in
events degraded /timeline and build_pdf to a permanent 500 -- now it
degrades to insufficient, it never raises).

No AI, no network -- pure functions over what series_stats() returns.
"""
from __future__ import annotations

from datetime import date, timedelta

_METRICS = ("pct_positive", "pct_negative", "neg_avg_position")
DEFAULT_WINDOWS = (7, 14, 30)

# F3-close (top-5 #2): single window used by the "Actions & impact"
# section of export.pdf_report.
DEFAULT_PDF_WINDOW = 30


def _avg(rows: list[dict], field: str) -> float | None:
    vals = [r[field] for r in rows if r.get(field) is not None]
    return (sum(vals) / len(vals)) if vals else None


def _insufficient_windows(windows: tuple[int, ...]) -> dict:
    out: dict = {}
    for w in windows:
        window_out: dict = {"insufficient": True, "n_before": 0, "n_after": 0}
        for field in _METRICS:
            window_out[field] = {"before": None, "after": None, "delta": None}
        out[w] = window_out
    return out


def parse_event_date(event_date: str):
    """Event date or None if the string isn't valid ISO (never raises)."""
    try:
        return date.fromisoformat((event_date or "")[:10])
    except ValueError:
        return None


def impactable_events(events: list[dict], today: str, limit: int) -> list[dict]:
    """The events that PAY for impact calculation (review 4.5 #2/#3/#9):
    only kind='manual' (lifecycle's automatic url_gone events, dated today,
    were exhausting the slots and filling the PDF with system noise) and
    with a date strictly before `today` (a today/future event has no real
    "after" span). The cap is applied ON TOP of the eligible ones.
    `events` comes DESC by event_date/id (list_events)."""
    eligible = [e for e in events
                if e.get("kind") == "manual"
                and parse_event_date(e.get("event_date") or "") is not None
                and (e.get("event_date") or "")[:10] < today]
    return eligible[:limit]


def event_impact_from_rows(by_day: dict, event_date: str,
                            windows: tuple[int, ...] = DEFAULT_WINDOWS) -> dict:
    """Pure arithmetic: {window: {metric: {before, after, delta},
    insufficient, n_before, n_after}} over an already-queried `by_day`
    (day -> series_stats row). See the module docstring."""
    ev = parse_event_date(event_date)
    if ev is None:
        return _insufficient_windows(windows)

    out: dict = {}
    for w in windows:
        before_rows = [by_day[d] for d in
                        ((ev - timedelta(days=n)).isoformat() for n in range(1, w + 1))
                        if d in by_day]
        after_rows = [by_day[d] for d in
                       ((ev + timedelta(days=n)).isoformat() for n in range(w))
                       if d in by_day]
        insufficient = not before_rows or not after_rows

        window_out: dict = {"insufficient": insufficient,
                            "n_before": len(before_rows),
                            "n_after": len(after_rows)}
        for field in _METRICS:
            if insufficient:
                window_out[field] = {"before": None, "after": None, "delta": None}
                continue
            before = _avg(before_rows, field)
            after = _avg(after_rows, field)
            if before is None or after is None:
                delta = None
            elif field == "pct_negative":
                delta = before - after  # inverted -- see module docstring
            else:
                delta = after - before
            window_out[field] = {"before": before, "after": after, "delta": delta}
        out[w] = window_out
    return out


def rows_for_events(db, event_dates: list[str],
                     windows: tuple[int, ...] = DEFAULT_WINDOWS) -> dict:
    """ONE series_stats query that covers the windows of ALL the given
    events: [min-max_w, max+max_w-1]. Returns by_day."""
    parsed = [d for d in (parse_event_date(x) for x in event_dates) if d is not None]
    if not parsed:
        return {}
    max_w = max(windows)
    lo = (min(parsed) - timedelta(days=max_w)).isoformat()
    hi = (max(parsed) + timedelta(days=max_w - 1)).isoformat()
    rows = db.series_stats(country="", date_from=lo, date_to=hi)
    return {r["day"]: r for r in rows}


def impacts_at(db, events: list[dict], window: int = DEFAULT_PDF_WINDOW) -> list[dict]:
    """[{event, impact}] for `events` (already eligible -- the caller
    applies impactable_events BEFORE passing them here, this function
    doesn't filter) with a SINGLE query (rows_for_events) that covers ALL
    of them -- replaces the `rows_for_events(...) +
    event_impact_from_rows(...)[window]` loop pattern (F3-close top-5 #2).
    `impact` is event_impact_from_rows' arithmetic for the SINGLE `window`
    (not the 3 defaults of DEFAULT_WINDOWS -- the PDF's executive section
    shows a single window, see
    DEFAULT_PDF_WINDOW). Output order: the SAME as `events`."""
    if not events:
        return []
    by_day = rows_for_events(db, [e.get("event_date") or "" for e in events], (window,))
    return [
        {"event": e, "impact": event_impact_from_rows(by_day, e.get("event_date") or "",
                                                        (window,))[window]}
        for e in events
    ]
