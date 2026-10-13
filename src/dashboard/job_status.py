"""Plain-language status of a search job (pure functions, no request or DB).

One sentence says what a search is doing right now, derived from its steps
(dashboard.jobs: one step per country x engine, states pending -> launching
-> running -> saving -> done/skipped/error, plus the `stage` of a saving step
reported by report.multi_country.persist_engine_run):

    Starting search…
    Searching Google in United Kingdom (2 of 6)…
    Waiting for Apify (Google, United Kingdom)… 0:35
    Saving results…
    Checking same-name results with AI…
    Done: 7 results in 3 countries, 1 new negative
    Search failed: <plain error>

The same sentence is shown by the job panel (/ui/job/{id}), the top-bar chip,
its popover, Runs → "Running now" and the assistant's job card; it travels
as `activity` in GET /job/{id}/status and GET /jobs/active.
"""
from __future__ import annotations

import hashlib

# Finished jobs stay in GET /jobs/active (chip "Search finished", the toast)
# for this long, until the viewer has seen them.
ACTIVE_FINISHED_SECONDS = 10 * 60

ACTIVE_STATES = ("launching", "running", "saving")
TERMINAL_STATES = ("done", "skipped", "error")

# persist_engine_run on_stage(...) -> sentence.
STAGE_SENTENCES = {
    "classify": "Saving results…",
    "same_person": "Checking same-name results with AI…",
    "save": "Saving results…",
    "severity": "Rating the negatives with AI…",
    "evidence": "Capturing evidence for new negatives…",
}

# Apify run states while the actor still works (anything else: fetching).
_APIFY_WAITING = {"READY", "RUNNING", "", None}

_COUNTRY_NAMES = {"ca": "Canada", "us": "the United States", "hk": "Hong Kong",
                  "gb": "the United Kingdom", "fr": "France", "ch": "Switzerland",
                  "de": "Germany", "it": "Italy", "nl": "the Netherlands", "es": "Spain"}


def fmt_elapsed(seconds) -> str:
    """42 -> '0:42', 125 -> '2:05'."""
    try:
        s = max(0, int(seconds))
    except (TypeError, ValueError):
        return "0:00"
    return f"{s // 60}:{s % 60:02d}"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def place(country: str | None) -> str:
    """'gb' -> 'the United Kingdom'; 'global' -> 'worldwide' (used after 'in')."""
    code = (country or "").lower()
    if code in ("", "global"):
        return "worldwide"
    return _COUNTRY_NAMES.get(code, code.upper())


def _in_place(country: str | None) -> str:
    p = place(country)
    return p if p == "worldwide" else f"in {p}"


def _short_place(country: str | None) -> str:
    p = place(country)
    return p.removeprefix("the ")


def engine_label(engine: str | None) -> str:
    return (engine or "google").capitalize()


def step_counts(steps: list[dict]) -> tuple[int, int]:
    done = sum(1 for s in steps if s.get("state") in TERMINAL_STATES)
    return done, len(steps)


def plain_error(error) -> str:
    """First line of an error, trimmed (no tracebacks in the UI)."""
    text = str(error or "").strip().splitlines()[0:1]
    text = text[0].strip() if text else ""
    if not text:
        return "Something went wrong."
    return text if len(text) <= 200 else text[:199] + "…"


def scope_line(meta: dict) -> str:
    """'Countries: US, GB · Engines: Google' (one line, no per-item pills)."""
    meta = meta or {}
    countries = meta.get("countries") or ([meta["country"]] if meta.get("country") else [])
    parts = []
    if countries:
        parts.append("Countries: " + ", ".join(str(c).upper() for c in countries))
    if meta.get("engines"):
        parts.append("Engines: " + ", ".join(engine_label(e) for e in meta["engines"]))
    if meta.get("device") and meta["device"] != "desktop":
        parts.append(f"Device: {str(meta['device']).capitalize()}")
    return " · ".join(parts)


def job_summary(snap: dict, result: dict | None = None) -> dict:
    """{results, negatives, new_negatives, countries, failed_countries} of a
    job, from its finished steps (falls back to the result payload for jobs
    without steps)."""
    steps = snap.get("steps") or []
    done = [s for s in steps if s.get("state") == "done"]
    if done or steps:
        by_country: dict[str, list[str]] = {}
        for s in steps:
            by_country.setdefault(s.get("country") or "", []).append(s.get("state") or "")
        failed = [c for c, states in by_country.items()
                  if states and all(st == "error" for st in states)]
        sums = [s.get("summary") or {} for s in done]
        return {
            "results": sum(int(m.get("total") or 0) for m in sums),
            "negatives": sum(int(m.get("negative") or 0) for m in sums),
            "new_negatives": sum(int(m.get("new_negatives") or 0) for m in sums),
            "countries": len({s.get("country") for s in done}),
            "failed_countries": len(failed),
        }
    result = result or {}
    if result.get("kind") == "single":
        sm = result.get("summary") or {}
        return {"results": int(sm.get("total") or 0), "negatives": int(sm.get("negative") or 0),
                "new_negatives": int(result.get("new_negatives") or 0),
                "countries": 1, "failed_countries": 0}
    agg = result.get("agg") or {}
    return {"results": int(agg.get("total") or 0), "negatives": int(agg.get("negative") or 0),
            "new_negatives": sum(int(o.get("new_negatives") or 0)
                                 for o in result.get("outcomes") or []),
            "countries": int(agg.get("countries_ok") or 0),
            "failed_countries": int(agg.get("countries_error") or 0)}


def done_sentence(summary: dict) -> str:
    """'Done: 7 results in 3 countries, 1 new negative'."""
    n, c = summary.get("results", 0), summary.get("countries", 0)
    failed = summary.get("failed_countries", 0)
    if not c and failed:
        return f"Finished, but no country could be searched ({plural(failed, 'country', 'countries')} failed)"
    new = summary.get("new_negatives", 0)
    text = (f"Done: {plural(n, 'result')} in {plural(max(c, 1), 'country', 'countries')}, "
            + (plural(new, "new negative") if new else "no new negatives"))
    if failed:
        text += f" ({plural(failed, 'country', 'countries')} failed)"
    return text


def _pick_active(steps: list[dict]) -> dict | None:
    saving = [s for s in steps if s.get("state") == "saving"]
    if saving:
        return saving[0]
    active = [s for s in steps if s.get("state") in ("launching", "running")]
    return active[0] if active else None


def activity_sentence(snap: dict, result: dict | None = None) -> str:
    """What the job is doing now, in one plain sentence (see module doc)."""
    status = snap.get("status")
    if status == "error":
        return f"Search failed: {plain_error(snap.get('error'))}"
    if status == "done":
        return done_sentence(job_summary(snap, result))
    steps = snap.get("steps") or []
    if not steps:
        return "Starting search…"
    total = len(steps)
    step = _pick_active(steps)
    if step is not None:
        idx = steps.index(step) + 1
        engine, country = engine_label(step.get("engine")), step.get("country")
        if step.get("state") == "saving":
            return STAGE_SENTENCES.get(step.get("stage") or "", "Saving results…")
        apify = step.get("apify_status")
        if step.get("state") == "running" and apify is not None:
            if str(apify).upper() in _APIFY_WAITING:
                return (f"Waiting for Apify ({engine}, {_short_place(country)})… "
                        f"{fmt_elapsed(step.get('apify_elapsed_s'))}")
            return f"Fetching results ({engine}, {_short_place(country)})…"
        return f"Searching {engine} {_in_place(country)} ({idx} of {total})…"
    if all(s.get("state") == "pending" for s in steps):
        return "Starting search…"
    nxt = next((s for s in steps if s.get("state") == "pending"), None)
    if nxt is not None:
        return (f"Searching {engine_label(nxt.get('engine'))} {_in_place(nxt.get('country'))} "
                f"({steps.index(nxt) + 1} of {total})…")
    return "Finishing up…"


def any_step_failed(steps: list[dict]) -> bool:
    return any(s.get("state") == "error" for s in steps or [])


def session_hash(cookie_value: str | None) -> str:
    """Short, non-reversible id of a session cookie (who started a job)."""
    if not cookie_value:
        return ""
    return hashlib.sha256(cookie_value.encode()).hexdigest()[:16]


__all__ = [
    "ACTIVE_FINISHED_SECONDS",
    "STAGE_SENTENCES",
    "activity_sentence",
    "any_step_failed",
    "done_sentence",
    "engine_label",
    "fmt_elapsed",
    "job_summary",
    "place",
    "plain_error",
    "scope_line",
    "session_hash",
    "step_counts",
]
