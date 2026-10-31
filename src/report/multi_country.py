"""
Multi-country search orchestration (report)
================================
Launches the same query in several countries (one Apify run per country,
since countryCode is per-run), classifies, saves to the DB and returns an
aggregated summary. A country that fails does NOT interrupt the others.

Multi-engine extraction (1.1): `engines` travels all the way to
ApifyRunner.search -- each country additionally produces a DB run PER
ENGINE (see persist_engine_run below and
apify.runner.ApifyRunner.search::engine_runs). An engine that goes down
within a country does NOT bring down the other engines of that country or
the other countries (nested best-effort: per country, and within each
country, per engine).

Mobile/desktop split (1.3): `device` travels the same way to
ApifyRunner.search (a single device per call, like country_code -- see
the docstring of ApifyRunner.search) and from there to each
engine_run/row.
"""

import inspect
import logging
from collections.abc import Callable

from config.apify_config import lang_for
from notify.dispatcher import notify_alert
from processing import ai_status, llm
from processing.classifier import classify_batch, get_summary
from processing.enrich import annotate_same_person
from processing.evidence import capture_new_negatives
from processing.lifecycle import update_url_lifecycle
from processing.severity import annotate_severity
from serp.base import ENGINE_NOT_CONFIGURED_MSG, SerpProviderError
from serp.runner import SerpRunner

log = logging.getLogger("search")

AI_NOTE_PREFIX = "AI checks skipped: "


def ai_skip_note(failures: list[str]) -> str:
    """'AI checks skipped: <first plain reason>' ('' when nothing failed)."""
    return f"{AI_NOTE_PREFIX}{failures[0]}" if failures else ""


def accepts_kwarg(fn: Callable, name: str) -> bool:
    """True if `fn` can be called with keyword `name` (explicit parameter or
    **kwargs). Lets progress callbacks be passed only to runners that support
    them (test fakes and legacy runners keep their narrow signatures)."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


def failed_engine_state(engine_run: dict) -> str:
    """'skipped' for an engine that never ran (not configured / unknown),
    'error' for one whose Apify run failed -- for the job progress steps."""
    err = engine_run.get("error") or ""
    if ENGINE_NOT_CONFIGURED_MSG in err or err.endswith("unknown engine"):
        return "skipped"
    return "error"


def persist_engine_run(
    db,
    engine_run: dict,
    positive_domains: list[str] | None = None,
    negative_domains: list[str] | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> dict:
    """Persists ONE engine run (1.1): classifies, saves run+results and
    runs the post-save chokepoints (severity, lifecycle, evidence) --
    exactly the pipeline that used to run
    once per outcome (single-engine), now once PER ENGINE ("one DB run
    per engine", see apify.runner.ApifyRunner.search::engine_runs).

    `engine_run` must be a SUCCESSFUL entry of outcome["engine_runs"]
    (error is None) -- the caller skips failed ones BEFORE reaching here
    (best-effort per engine, see run_multi_country/dashboard.main::
    run_search).

    Returns {"summary": dict, "results": list[dict], "ai_note": str,
    "new_negatives": int} (results ALREADY classified) so the caller can
    aggregate them into its own view (combined table, summary summed across
    engines). new_negatives = negatives seen for the first time (evidence
    captured by capture_new_negatives).

    `on_stage(stage)` (optional, job progress UI) is told what the pipeline
    is doing: "classify", "same_person" (AI same-name check, only with an AI
    provider), "save", "severity" (AI, only with a provider), "evidence".
    Callback failures never break the save."""
    def stage(name: str) -> None:
        if on_stage is None:
            return
        try:
            on_stage(name)
        except Exception:
            log.exception("search_stage_callback_failed")

    stage("classify")
    results = classify_batch(
        engine_run["results"],
        positive_domains=positive_domains,
        negative_domains=negative_domains,
    )
    # The AI steps (same-name before saving, severity after) never stop a
    # search: a failing provider leaves those fields empty. Their failures are
    # collected so the run SAYS so ("AI checks skipped: ...") instead of
    # silently returning nothing. One provider routing session per run.
    with llm.session_scope(f"run:{engine_run.get('run_id') or ''}"), \
            ai_status.collect_failures() as ai_failures:
        return _persist_engine_run(db, engine_run, results, ai_failures, stage)


def _ai_on() -> bool:
    try:
        return bool(llm.is_configured())
    except Exception:  # noqa: BLE001
        return False


def _persist_engine_run(db, engine_run: dict, results: list[dict], ai_failures: list[str],
                        stage: Callable[[str], None] = lambda _name: None) -> dict:
    ai_on = _ai_on()
    # 2.3: identity disambiguation BEFORE saving.
    if ai_on:
        stage("same_person")
    results = annotate_same_person(results, db)
    stage("save")
    summary = get_summary(results)
    # FIX 1 item 4 (muted-alerts fix audit): save_run returns the
    # freshly inserted `apify_budget` row (or None) -- Database cannot
    # dispatch it itself (import cycle), so the caller does it.
    budget_alert = db.save_run(
        apify_run_id=engine_run["run_id"],
        queries=engine_run["queries"],
        country_code=engine_run["country_code"],
        status=engine_run["status"],
        summary=summary,
        cost_usd=engine_run.get("cost_usd"),
        compute_units=engine_run.get("compute_units"),
        retries=engine_run.get("retries", 0),
        engine=engine_run.get("engine", "google"),
        device=engine_run.get("device", "desktop"),
        provider=engine_run.get("provider") or "apify",
    )
    if budget_alert:
        notify_alert(db, budget_alert)
    db.save_results(results, run_id=engine_run["run_id"])
    if ai_on:
        stage("severity")
    annotate_severity(db, engine_run["run_id"])  # 2.2, after saving, before lifecycle
    ai_note = ai_skip_note(ai_failures)
    if ai_note:
        log.warning("search_ai_checks_skipped run=%s failures=%d: %s",
                    engine_run["run_id"], len(ai_failures), ai_failures[0])
        try:
            db.set_run_ai_note(engine_run["run_id"], ai_note)
        except Exception:
            log.exception("search_ai_note_failed")
    update_url_lifecycle(db, engine_run["run_id"])  # 3.1, after saving
    stage("evidence")
    captured = capture_new_negatives(db, engine_run["run_id"])  # 3.2, after lifecycle
    return {"summary": summary, "results": results, "ai_note": ai_note,
            "new_negatives": len(captured or [])}


def run_multi_country(
    runner: SerpRunner,
    db,
    query: str,
    countries: list[str],
    positive_domains: list[str] | None = None,
    negative_domains: list[str] | None = None,
    max_pages: int = 1,
    on_event: Callable | None = None,
    engines: list[str] | None = None,
    device: str = "desktop",
    on_step: Callable | None = None,
) -> list[dict]:
    """
    Returns a list of results per country:
        {country, run_id, status, summary, error}

    engines (1.1, defaults to None -> ApifyRunner.search treats them as
    ["google"], IDENTICAL to the behavior before this item): each
    country's summary is the SUM of the summaries of all of its
    successful engines; the country's run_id/status are those of the
    "primary" engine returned by ApifyRunner.search (see its docstring).

    device (1.3, defaults to "desktop" -- IDENTICAL to the behavior
    before this item): a single device for ALL countries of this call
    (like engines/max_pages) -- travels as-is to ApifyRunner.search.

    on_step (optional, job progress UI): `on_step(country, engine, state,
    **info)` with state launching/running (forwarded from the runner's
    on_engine, when the runner supports it), saving, done (info: summary),
    skipped/error (info: message). engine=None with state "error" means
    "every still-open step of this country" (the whole country failed).
    While saving, `stage` (info) says which part of persist_engine_run runs;
    the done step's summary carries `new_negatives`.
    """
    outcomes: list[dict] = []
    forward = on_step is not None and accepts_kwarg(runner.search, "on_engine")
    for country in countries:
        if on_event:
            on_event(f"Searching in {country}...")
        extra = {}
        if forward:
            def _on_engine(engine, state, _country=country, **info):
                on_step(_country, engine, state, **info)
            extra["on_engine"] = _on_engine
        try:
            outcome = runner.search(
                queries=[query], country_code=country,
                language_code=lang_for(country), max_pages=max_pages,
                engines=engines, device=device, **extra,
            )
            # 1.1 (review LOW #11): outcome["warnings"] -- skipped/failed
            # engines, in readable text -- is poured into on_event
            # (dashboard.main connects it to jobs.add_progress, visible
            # in /job) instead of being left as a field with no real
            # consumer.
            if on_event:
                for w in outcome.get("warnings", []):
                    on_event(f"{country}: {w} (skipped)")
            agg_summary = {"total": 0, "positive": 0, "negative": 0, "neutral": 0}
            ai_note = ""
            new_negatives = 0
            for engine_run in outcome["engine_runs"]:
                if engine_run.get("error"):
                    # already reported above via outcome["warnings"]
                    if on_step:
                        on_step(country, engine_run.get("engine"),
                                failed_engine_state(engine_run), message=engine_run["error"])
                    continue
                stage_cb = None
                if on_step:
                    on_step(country, engine_run.get("engine"), "saving")

                    def stage_cb(name, _country=country, _engine=engine_run.get("engine")):
                        on_step(_country, _engine, "saving", stage=name)
                out = persist_engine_run(
                    db, engine_run,
                    positive_domains=positive_domains,
                    negative_domains=negative_domains,
                    on_stage=stage_cb,
                )
                new_negatives += out.get("new_negatives", 0)
                if out.get("ai_note"):
                    ai_note = ai_note or out["ai_note"]
                    if on_event:
                        on_event(f"{country}: {out['ai_note']}")
                if on_step:
                    on_step(country, engine_run.get("engine"), "done",
                            summary={**out["summary"], "new_negatives": out.get("new_negatives", 0)})
                for k in agg_summary:
                    agg_summary[k] += out["summary"].get(k, 0)
            outcomes.append(
                {
                    "country": country,
                    "run_id": outcome["run_id"],
                    "status": outcome["status"],
                    "summary": agg_summary,
                    "error": None,
                    "ai_note": ai_note,
                    "new_negatives": new_negatives,
                }
            )
        except SerpProviderError as e:
            outcomes.append(
                {"country": country, "run_id": None, "status": "ERROR",
                 "summary": {}, "error": str(e)}
            )
            if on_step:
                on_step(country, None, "error", message=str(e))
        except Exception as e:  # noqa: BLE001
            outcomes.append(
                {"country": country, "run_id": None, "status": "ERROR",
                 "summary": {}, "error": f"Unexpected: {e}"}
            )
            if on_step:
                on_step(country, None, "error", message=f"Unexpected: {e}")
    return outcomes


def aggregate_summary(outcomes: list[dict]) -> dict[str, int]:
    """Sums the counts of all countries that finished successfully."""
    agg = {"positive": 0, "negative": 0, "neutral": 0, "total": 0,
           "countries_ok": 0, "countries_error": 0}
    for o in outcomes:
        if o.get("error"):
            agg["countries_error"] += 1
            continue
        agg["countries_ok"] += 1
        s = o.get("summary", {})
        for k in ("positive", "negative", "neutral", "total"):
            agg[k] += s.get(k, 0)
    return agg
