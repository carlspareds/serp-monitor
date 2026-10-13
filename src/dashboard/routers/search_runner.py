"""Search execution: the New search modal (/ui/search), single country and
multi-country searches, and live progress (side panel /ui/job/{id}, full page
/job/{id}, JSON GET /job/{id}/status polled by static/app.js).

Progress is ONE status box: a plain sentence of what the search is doing
(dashboard.job_status.activity_sentence), a progress bar, the elapsed time
and a one-line "Countries: … · Engines: …"; the per-step list lives in a
collapsed "Details" (opened by itself only when a step failed).

Background searches: GET /jobs/active lists the running and recently
finished (unseen) jobs of the subjects the viewer may see -- the top-bar
chip, its popover, the "finished" toast and Runs → "Running now" read it;
POST /jobs/{id}/seen hides a finished one for this viewer.

Subject scoping: the subject is captured AT SUBMIT TIME (form field
`subject_id`, validated against the viewer's allowed subjects, default the
active one) and carried into the worker thread explicitly -- a hand-started
thread does not inherit the request's ContextVar, so the worker runs inside
`subject_scope(sid)` with a `db.for_subject(sid)` instance: runs, results,
lifecycle, evidence and alerts are all written with that subject_id even if
the user switches subject while the search runs.
"""
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from apify.actors import ACTORS, CONFIGURED_ENGINES, VALID_DEVICES
from common.money import run_cost_phrase
from config.apify_config import lang_for
from config.positive_domains import merged_with
from dashboard import job_status as js
from dashboard import ui
from dashboard.context import allowed_subjects, is_fragment_request, viewer
from dashboard.deps import db
from dashboard.jobs import _MAX_CONCURRENT_JOBS, step_key
from dashboard.session import audit, is_demo_mode, session_token
from dashboard.ui_helpers import (
    COUNTRIES,
    _clamp_pages,
    _esc,
    _pill,
    _results_table,
    _split_valid_countries,
    _split_valid_engines,
    fragment_or_page,
    ordered_countries,
    overlay_url,
    page,
    plural,
    subject_label,
)
from database.base import subject_scope
from report.multi_country import (
    accepts_kwarg,
    aggregate_summary,
    failed_engine_state,
    persist_engine_run,
    run_multi_country,
)

router = APIRouter()

_NO_STORE = {"Cache-Control": "no-store"}
_DEFAULT_RUN_COST_USD = 0.03

# Step state -> pill colour class (existing .pill variants in style.css).
_STEP_PILL = {
    "pending": "", "launching": "info", "running": "info", "saving": "info",
    "done": "positive", "skipped": "warning", "error": "negative",
}


def _get_main():
    import dashboard.main as main_module
    return main_module


def viewer_key() -> str:
    """Who is looking, for the per-viewer "seen" state of finished jobs:
    the username, or "local" when sign-in is off (one local user)."""
    name = viewer().username
    return f"user:{name}" if name else "local"


def owner_meta(request: Request) -> dict:
    """Who started a job: the user and a short hash of their session."""
    return {"started_by": viewer().username or "local",
            "session": js.session_hash(session_token(request) or request.cookies.get("asst_sid"))}


def visible_subject_ids() -> set:
    """Subjects whose jobs this viewer may see (the allowed subjects; the
    active one when the list is empty, as _search_subject does)."""
    allowed = allowed_subjects()
    if allowed:
        return {s["id"] for s in allowed}
    current = db.current_subject().get("id")
    return {current} if current else set()


def _job_visible(job: dict | None) -> bool:
    """A job is visible to viewers who may see its subject (jobs recorded
    without a subject -- older callers -- stay reachable by their id)."""
    if not job:
        return False
    sid = (job.get("meta") or {}).get("subject_id")
    return sid is None or sid in visible_subject_ids()


def _make_on_step(jobs, job_id: str):
    """on_step(country, engine, state, **info) -> JobManager steps.
    engine=None + state 'error' = every open step of that country."""
    def on_step(country, engine, state, **info):
        if engine is None:
            if state == "error":
                jobs.fail_open_steps(job_id, info.get("message") or "Failed", country=country)
            return
        jobs.set_step(job_id, step_key(country, engine), state=state, **info)
    return on_step


# --------------------------------------------------------------------- #
# Subject of a search
# --------------------------------------------------------------------- #
def _search_subject(subject_id: str = "") -> dict | None:
    """The subject a search runs for: the posted id if the viewer may see it,
    else the active subject. None when nothing is allowed."""
    allowed = allowed_subjects()
    try:
        wanted = int(subject_id) if str(subject_id or "").strip() else None
    except ValueError:
        wanted = None
    if wanted is not None:
        match = next((s for s in allowed if s["id"] == wanted), None)
        if match is not None:
            return match
        if not allowed and wanted == db.current_subject().get("id"):
            return db.current_subject()
        return None
    current = db.current_subject()
    if not allowed or any(s["id"] == current.get("id") for s in allowed):
        return current
    return None


def _avg_run_cost(sdb) -> tuple[float, bool]:
    try:
        costs = [float(r["cost_usd"]) for r in sdb.list_runs(limit=50) if r.get("cost_usd") is not None]
    except Exception:  # noqa: BLE001
        costs = []
    if not costs:
        return _DEFAULT_RUN_COST_USD, False
    return sum(costs) / len(costs), True


# --------------------------------------------------------------------- #
# New search modal
# --------------------------------------------------------------------- #
def search_form_html(subject: dict | None = None, error: str = "",
                     countries_sel: list[str] | None = None, engines_sel: list[str] | None = None) -> str:
    """The New search dialog (ui.overlay). Countries/engines default to the
    subject's tracked countries and Google; only configured engines are shown."""
    v = viewer()
    subject = subject or db.current_subject()
    sid = subject.get("id")
    sdb = db.for_subject(sid) if sid else db
    subjects = allowed_subjects() or [subject]
    projects = {c["id"]: c.get("name") or "" for c in db.list_clients()}
    opts = "".join(
        f"<option value='{s['id']}'{' selected' if s['id'] == sid else ''}>"
        f"{_esc(subject_label(s))}{(' (' + _esc(projects.get(s.get('client_id'), '')) + ')') if projects.get(s.get('client_id')) else ''}"
        "</option>"
        for s in subjects)
    new_url = "/ui/subject/new?" + urlencode({"next": overlay_url("/ui/search?subject=__new_subject__", "modal")})
    if v.can_write:
        opts += "<option value='__new__'>New subject…</option>"
    tracked = [c.lower() for c in (subject.get("countries") or [])] or ["us"]
    chosen = countries_sel if countries_sel is not None else tracked
    universe = ordered_countries(list(dict.fromkeys(tracked + list(COUNTRIES))))
    country_boxes = "".join(
        f"<label><input type='checkbox' name='countries' value='{_esc(c)}'"
        f"{' checked' if c in chosen else ''}> {_esc(c.upper())}</label>"
        for c in universe)
    engines = [e for e in ACTORS if e in CONFIGURED_ENGINES]
    chosen_eng = engines_sel if engines_sel is not None else (["google"] if "google" in engines else engines[:1])
    engine_field = ""
    if len(engines) > 1:
        boxes = "".join(
            f"<label><input type='checkbox' name='engines' value='{_esc(e)}'"
            f"{' checked' if e in chosen_eng else ''}> {_esc(e.title())}</label>" for e in engines)
        engine_field = f"<div class='field'><span class='label'>Engines</span><div class='checks'>{boxes}</div></div>"
    else:
        engine_field = "".join(f"<input type='hidden' name='engines' value='{_esc(e)}'>" for e in engines)
    devices = "".join(f"<option value='{d}'>{d.title()}</option>" for d in VALID_DEVICES)
    per_run, measured = _avg_run_cost(sdb)
    engine_name = engines[0].title() if len(engines) == 1 else "one engine"
    cost = (f"One country with {engine_name} takes about 20 to 90 seconds and costs "
            f"{run_cost_phrase(per_run)} on your Apify plan{'' if measured else ' (rough estimate)'}. "
            "Each extra country or engine adds the same.")
    query = (subject.get("query") or "").strip()
    blockers = []
    if is_demo_mode():
        link = ("<a href='/settings#search'>add it in Settings → Search</a>" if v.is_admin
                else "ask an admin to add it in Settings → Search")
        blockers.append(f"<div class='hint hint-warn' role='status'><b>Needs an Apify key.</b> "
                        f"Searches use your Apify account: {link}.</div>")
    if not v.can_write:
        blockers.append("<div class='hint' role='status'>Your account is read-only, so it can't "
                        "start searches.</div>")
    if not query:
        blockers.append("<div class='hint hint-warn' role='status'>This subject has no tracked name "
                        "yet. Add one in <a href='/settings#projects'>Settings → Projects and "
                        "subjects</a>.</div>")
    err = f"<div class='hint hint-error' role='alert'>{_esc(error)}</div>" if error else ""
    disabled = " disabled aria-disabled='true'" if blockers else ""
    body = (
        f"{err}{''.join(blockers)}"
        "<div class='field'><label for='sq-subject'>Subject</label>"
        f"<select id='sq-subject' name='subject_id' data-sm-refetch='/ui/search?subject='"
        f" data-sm-new='{_esc(new_url)}'>{opts}</select>"
        + (f"<span class='help'>Searches for “{_esc(query)}”.</span>" if query else "")
        + "</div>"
        f"<div class='field'><span class='label'>Countries</span><div class='checks'>{country_boxes}</div></div>"
        f"{engine_field}"
        "<details class='more-options'><summary>More options</summary><div class='row'>"
        "<div class='field'><label for='sq-pages'>Result pages per search</label>"
        "<select id='sq-pages' name='pages'><option value='1'>1 (top 10)</option>"
        "<option value='2'>2 (top 20)</option><option value='3'>3 (top 30)</option></select></div>"
        f"<div class='field'><label for='sq-device'>Device</label><select id='sq-device' name='device'>"
        f"{devices}</select></div></div></details>"
        f"<p class='note'>{_esc(cost)}</p>"
    )
    foot = ("<button type='button' class='btn ghost' data-sm-close>Cancel</button>"
            f"<button type='submit' class='btn primary' data-busy-label='Starting…'{disabled}>Run search</button>")
    form = (f"<form class='overlay-form search-form' action='/search_multi' method='post' data-sm-submit>"
            f"{ui.overlay('New search', body, foot)}</form>")
    return form


@router.get("/ui/search", response_class=HTMLResponse, tags=["ui"])
async def ui_search(subject: str = ""):
    subj = _search_subject(subject) if subject else None
    return fragment_or_page("New search", search_form_html(subj), active="/results")


@router.get("/search_page", tags=["search-pages"])
async def search_page():
    """Old full page: the dialog opens over Results."""
    return RedirectResponse(overlay_url("/ui/search", "modal"), status_code=303)


def _search_error(message: str, subject: dict | None = None, countries=None, engines=None):
    """Validation error: the modal again with the message (status 400, stays
    inside the dialog for a fetch submit; a normal page otherwise)."""
    inner = search_form_html(subject, error=message, countries_sel=countries, engines_sel=engines)
    return fragment_or_page("New search", inner, active="/results", status_code=400)


def _started(job_id: str) -> Response:
    """POST/Redirect/GET: a fetch submit from the modal opens the progress side
    panel (X-SM-Panel); a plain form goes to the job page."""
    if is_fragment_request():
        return Response(status_code=200, headers={"X-SM-Panel": f"/ui/job/{job_id}", **_NO_STORE})
    return RedirectResponse(f"/job/{job_id}", status_code=303)


def _check_capacity(jobs) -> str:
    if jobs.running_count() >= _MAX_CONCURRENT_JOBS:
        return (f"{plural(_MAX_CONCURRENT_JOBS, 'search', 'searches')} are already running. Wait for "
                "one to finish before starting another.")
    return ""


@router.post("/search", response_class=HTMLResponse, tags=["search-pages"])
async def run_search(request: Request, query: str = Form("", max_length=200), country: str = Form(..., max_length=64),
                     pages: int = Form(1), engines: list = Form(default=["google"]),
                     device: str = Form("desktop", max_length=64), subject_id: str = Form("", max_length=64)):
    """One country: launches the search in the background (see module doc)."""
    main = _get_main()
    jobs = main.jobs
    subject = _search_subject(subject_id)
    if subject is None:
        return _search_error("That subject isn't available.")
    sid = subject["id"]
    sdb = db.for_subject(sid)
    query = (query or "").strip() or (subject.get("query") or "").strip()
    pages = _clamp_pages(pages)
    valid_engines, _invalid_engines = _split_valid_engines(engines)
    if not query:
        return _search_error("This subject has no tracked name yet.", subject)
    if not valid_engines:
        return _search_error("Pick at least one search engine.", subject)
    if country not in COUNTRIES:
        return _search_error(f"Unknown country: {country.upper()}.", subject)
    busy = _check_capacity(jobs)
    if busy:
        return _search_error(busy, subject)
    device = device if device in VALID_DEVICES else "desktop"

    valid_engines = list(dict.fromkeys(valid_engines))
    job_id = jobs.create(
        "single",
        {"query": query, "country": country, "countries": [country],
         "engines": valid_engines, "device": device, "subject_id": sid,
         "subject_name": subject_label(subject), **owner_meta(request)},
        steps=[(country, e) for e in valid_engines],
    )
    audit(request, "search_launch", query=query, country=country, pages=pages,
          engines=valid_engines, device=device, subject_id=sid)
    positives = merged_with(sdb.list_positive_domains())
    negatives = sdb.list_negative_domains()

    def work(jid):
        with subject_scope(sid):
            on_step = _make_on_step(jobs, jid)
            jobs.add_progress(jid, f"Searching in {country.upper()}…")
            runner = main.get_runner()
            extra = {}
            if accepts_kwarg(runner.search, "on_engine"):
                extra["on_engine"] = lambda engine, state, **info: on_step(country, engine, state, **info)
            outcome = runner.search(queries=[query], country_code=country,
                                    language_code=lang_for(country), max_pages=pages,
                                    engines=valid_engines, device=device, **extra)
            for w in outcome.get("warnings", []):
                jobs.add_progress(jid, f"{w} (skipped)")
            engine_runs = outcome.get("engine_runs") or []
            for er in engine_runs:
                if er.get("error"):
                    on_step(country, er.get("engine"), failed_engine_state(er), message=er["error"])
            if engine_runs and all(er.get("error") for er in engine_runs):
                # Nothing was saved: report it as a failed job, not "completed, 0 results".
                raise RuntimeError("Every engine failed: " + "; ".join(
                    f"{er.get('engine')}: {er.get('error')}" for er in engine_runs))
            all_results: list = []
            ai_note = ""
            new_negatives = 0
            agg_summary = {"total": 0, "positive": 0, "negative": 0, "neutral": 0}
            for engine_run in outcome["engine_runs"]:
                if engine_run.get("error"):
                    continue
                engine = engine_run.get("engine")
                on_step(country, engine, "saving")
                out = persist_engine_run(
                    sdb, engine_run, positive_domains=positives, negative_domains=negatives,
                    on_stage=lambda name, _e=engine: on_step(country, _e, "saving", stage=name))
                new_negatives += out.get("new_negatives", 0)
                on_step(country, engine, "done",
                        summary={**out["summary"], "new_negatives": out.get("new_negatives", 0)})
                all_results.extend(out["results"])
                for k in agg_summary:
                    agg_summary[k] += out["summary"].get(k, 0)
                jobs.add_progress(
                    jid, f"{engine_run['engine'].title()}: saved {plural(out['summary']['total'], 'result')}."
                )
                if out.get("ai_note"):
                    ai_note = ai_note or out["ai_note"]
                    jobs.add_progress(jid, f"{engine_run['engine'].title()}: {out['ai_note']}")
            return {"kind": "single", "run_id": outcome["run_id"], "country": country,
                    "query": query, "summary": agg_summary, "results": all_results,
                    "subject_id": sid, "ai_note": ai_note, "new_negatives": new_negatives}

    jobs.run_in_thread(job_id, work)
    return _started(job_id)


@router.post("/search_multi", response_class=HTMLResponse, tags=["search-pages"])
async def search_multi(request: Request, query: str = Form("", max_length=200), countries: list = Form(default=[]),
                       pages: int = Form(1), engines: list = Form(default=["google"]),
                       device: str = Form("desktop", max_length=64), subject_id: str = Form("", max_length=64)):
    """One or more countries (the New search modal posts here)."""
    main = _get_main()
    jobs = main.jobs
    subject = _search_subject(subject_id)
    if subject is None:
        return _search_error("That subject isn't available.")
    sid = subject["id"]
    sdb = db.for_subject(sid)
    query = (query or "").strip() or (subject.get("query") or "").strip()
    if not countries:
        return _search_error("Pick at least one country.", subject, [], engines)
    if not query:
        return _search_error("This subject has no tracked name yet.", subject, countries, engines)
    pages = _clamp_pages(pages)
    valid_engines, _invalid_engines = _split_valid_engines(engines)
    if not valid_engines:
        return _search_error("Pick at least one search engine.", subject, countries, [])
    countries, _invalid_countries = _split_valid_countries(countries)
    if not countries:
        return _search_error("None of those countries can be searched.", subject, [], valid_engines)
    busy = _check_capacity(jobs)
    if busy:
        return _search_error(busy, subject, countries, valid_engines)
    device = device if device in VALID_DEVICES else "desktop"

    valid_engines = list(dict.fromkeys(valid_engines))
    countries = list(dict.fromkeys(countries))
    job_id = jobs.create(
        "multi",
        {"query": query, "countries": countries, "engines": valid_engines, "device": device,
         "subject_id": sid, "subject_name": subject_label(subject), **owner_meta(request)},
        steps=[(c, e) for c in countries for e in valid_engines],
    )
    audit(request, "search_multi_launch", query=query, countries=countries, pages=pages,
          engines=valid_engines, device=device, subject_id=sid)
    positives = merged_with(sdb.list_positive_domains())
    negatives = sdb.list_negative_domains()

    def work(jid):
        with subject_scope(sid):
            runner = main.get_runner()
            outcomes = run_multi_country(
                runner, sdb, query, countries,
                positive_domains=positives, negative_domains=negatives,
                max_pages=pages,
                on_event=lambda m: jobs.add_progress(jid, m),
                engines=valid_engines, device=device,
                on_step=_make_on_step(jobs, jid),
            )
            agg = aggregate_summary(outcomes)
            jobs.add_progress(jid, "All countries finished.")
            return {"kind": "multi", "query": query, "agg": agg, "outcomes": outcomes,
                    "subject_id": sid}

    jobs.run_in_thread(job_id, work)
    return _started(job_id)


# --------------------------------------------------------------------- #
# Job progress: shared helpers
# --------------------------------------------------------------------- #
def job_redirect_target(job_id: str, status: str, result: dict | None) -> str | None:
    """Where the browser goes once the job is over: single -> its run;
    multi with exactly one country, successful -> that run; any other multi
    (several countries, or errors to show) -> the server-rendered summary
    at /job/{id}. None while running or on error."""
    if status != "done":
        return None
    result = result or {}
    if result.get("kind") == "single" and result.get("run_id"):
        return f"/run/{result['run_id']}"
    outcomes = result.get("outcomes") or []
    ok = [o for o in outcomes if not o.get("error") and o.get("run_id")]
    if len(outcomes) == 1 and len(ok) == 1:
        return f"/run/{ok[0]['run_id']}"
    return f"/job/{job_id}"


def job_view_url(job: dict | None) -> str:
    """"View results" target once a job is done: switch to the job's subject
    and show the Results of the country searched (one country) or the
    subject's Results (several)."""
    job = job or {}
    meta = job.get("meta") or {}
    result = job.get("result") or {}
    sid = result.get("subject_id") or meta.get("subject_id")
    countries = meta.get("countries") or ([meta["country"]] if meta.get("country") else [])
    ok = [o for o in (result.get("outcomes") or []) if not o.get("error") and o.get("run_id")]
    if result.get("kind") == "single" and result.get("country"):
        target = "/results?" + urlencode({"country": result["country"]})
    elif len(ok) == 1:
        target = "/results?" + urlencode({"country": ok[0].get("country") or ""})
    elif len(countries) == 1:
        target = "/results?" + urlencode({"country": countries[0]})
    else:
        target = "/results"
    if sid:
        return "/subject/switch?" + urlencode({"id": sid, "next": target})
    return target


def _ai_note_of(result: dict) -> str:
    if result.get("ai_note"):
        return result["ai_note"]
    return next((o["ai_note"] for o in result.get("outcomes") or [] if o.get("ai_note")), "")


def _done_note(job: dict | None) -> str:
    result = (job or {}).get("result") or {}
    ai_note = _ai_note_of(result)
    ai_tail = f" {ai_note.rstrip('.')}." if ai_note else ""
    if result.get("kind") == "single":
        sm = result.get("summary") or {}
        return (f"Done. {plural(sm.get('total', 0), 'result')}: {sm.get('positive', 0)} positive, "
                f"{sm.get('negative', 0)} negative, {sm.get('neutral', 0)} neutral.{ai_tail}")
    agg = result.get("agg") or {}
    bad = agg.get("countries_error") or 0
    text = (f"Done. {plural(agg.get('total', 0), 'result')}: {agg.get('positive', 0)} positive, "
            f"{agg.get('negative', 0)} negative, {agg.get('neutral', 0)} neutral.")
    if bad:
        text += f" {plural(bad, 'country', 'countries')} failed; see Details."
    return text + ai_tail


def _step_counts(steps: list[dict]) -> tuple[int, int]:
    return js.step_counts(steps)


def _step_label(step: dict) -> str:
    country = (step.get("country") or "").upper()
    engine = (step.get("engine") or "").capitalize()
    return f"{country} · {engine}" if country else engine


def _fmt_elapsed(seconds) -> str:
    return js.fmt_elapsed(seconds)


def _step_detail(step: dict) -> str:
    """Secondary text of a step row in Details (mirrored in app.js::stepDetail)."""
    state = step.get("state")
    if state == "done" and step.get("summary"):
        sm = step["summary"]
        return (f"{sm.get('total', 0)} results · {sm.get('positive', 0)} positive · "
                f"{sm.get('negative', 0)} negative · {sm.get('neutral', 0)} neutral")
    if state in ("skipped", "error") and step.get("message"):
        return str(step["message"])
    if state == "saving" and step.get("stage"):
        return js.STAGE_SENTENCES.get(step["stage"], "Classifying and saving…")
    if step.get("apify_status"):
        el = step.get("apify_elapsed_s")
        return f"Apify {step['apify_status']}" + (f" · {el}s" if el is not None else "")
    return {"launching": "Starting the Apify run…", "saving": "Classifying and saving…"}.get(state, "")


def _steps_html(steps: list[dict]) -> str:
    items = []
    for s in steps:
        state = s.get("state") or "pending"
        items.append(
            f"<li class='job-step' data-step-key='{_esc(s.get('key'))}' data-state='{_esc(state)}'>"
            f"<span class='job-step-name'>{_esc(_step_label(s))}</span>"
            f"<span class='pill {_STEP_PILL.get(state, '')}' data-step-state>"
            f"<span class='dot' aria-hidden='true'></span>{_esc(state)}</span>"
            f"<span class='job-step-detail' data-step-detail>{_esc(_step_detail(s))}</span></li>"
        )
    return f"<ol class='job-steps' data-job-steps>{''.join(items)}</ol>" if items else ""


def _details_html(job: dict) -> str:
    """The collapsed "Details" disclosure: per-step list (state + counts) and
    the activity log. Opened by itself only when a step failed."""
    steps = job.get("steps") or []
    opened = " open" if js.any_step_failed(steps) else ""
    log = job.get("progress") or []
    if job.get("status") == "error":
        # "Searching in xx…" lines describe work in flight; after a failure
        # they only contradict the error.
        log = [m for m in log if not m.startswith("Searching in ")]
    log_html = "".join(f"<li>{_esc(m)}</li>" for m in log)
    return (f"<details class='job-details' data-job-details{opened}><summary>Details</summary>"
            f"{_steps_html(steps)}"
            f"<ul class='job-log' data-job-log>{log_html}</ul></details>")


def _job_subtitle(meta: dict) -> str:
    return js.scope_line(meta)


def _job_head(job: dict) -> str:
    meta = job.get("meta") or {}
    sub = _job_subtitle(meta)
    return (f"<h2 class='job-title'>{_esc(meta.get('query') or 'Search')}</h2>"
            + (f"<p class='muted job-meta'>{_esc(sub)}</p>" if sub else ""))


def _status_box(job_id: str, job: dict, scope: bool = True) -> str:
    """ONE status box: what the search is doing (a sentence), elapsed time,
    progress bar, "Countries: … · Engines: …", then Details (collapsed).
    Updated in place by static/app.js (initJob) while running."""
    steps = job.get("steps") or []
    done_steps, total_steps = _step_counts(steps)
    status = job.get("status") or "running"
    pct = 100 if status == "done" else (int(done_steps * 100 / total_steps) if total_steps else 0)
    elapsed = (job.get("finished_at") or time.time()) - job["started_at"]
    activity = js.activity_sentence(job, job.get("result"))
    running = status == "running"
    poll = f" data-job-status-url='/job/{_esc(job_id)}/status'" if running else ""
    tone = {"done": " is-done", "error": " is-error"}.get(status, " is-running")
    sub = js.scope_line(job.get("meta") or {}) if scope else ""
    role = " role='alert'" if status == "error" else ""
    return (
        f"<div class='job-box{tone}' data-job-box{role}>"
        "<div class='job-box-top'>"
        "<span class='job-spinner' aria-hidden='true'></span>"
        f"<p class='job-activity' data-job-activity>{_esc(activity)}</p>"
        f"<span class='job-timer' data-job-elapsed title='Elapsed time'>{_fmt_elapsed(elapsed)}</span>"
        "</div>"
        f"<div class='job-progress{' is-error' if status == 'error' else ''}' role='progressbar' "
        f"aria-label='Search progress' aria-valuemin='0' aria-valuemax='{total_steps}' "
        f"aria-valuenow='{done_steps}' aria-valuetext='{done_steps} of {total_steps} steps finished' "
        f"data-job-progress><div class='job-progress-bar' data-job-bar style='width:{pct}%'></div></div>"
        + (f"<p class='job-scope' data-job-scope>{_esc(sub)}</p>" if sub else "")
        + f"<p class='sr-only' role='status' aria-live='polite' data-job-live>{_esc(activity)}</p>"
        "</div>"
        f"<p class='job-notice' data-job-notice hidden></p>"
    ), poll


def _live_card(job_id: str, job: dict, extra_cls: str = "") -> str:
    box, poll = _status_box(job_id, job)
    return (f"<div class='job-card {extra_cls}' data-job-id='{_esc(job_id)}'{poll} "
            f"data-job-state='{_esc(job.get('status'))}' "
            f"data-started-at='{job['started_at']:.3f}' data-server-now='{time.time():.3f}'>"
            f"{box}{_details_html(job)}</div>")


def _mark_seen_if_finished(jobs, job: dict) -> None:
    if job.get("status") != "running":
        jobs.mark_seen(job["id"], viewer_key())


# --------------------------------------------------------------------- #
# GET /job/{id}/status (JSON, polled by app.js)
# --------------------------------------------------------------------- #
def _not_found_json(job_id: str) -> JSONResponse:
    return JSONResponse({"error": "not_found", "id": job_id}, status_code=404, headers=_NO_STORE)


@router.get("/job/{job_id}/status", tags=["search-pages"])
async def job_status(job_id: str):
    jobs = _get_main().jobs
    snap = jobs.snapshot(job_id)
    if snap is None or not _job_visible(snap):
        return _not_found_json(job_id)
    result = None
    full = None
    if snap["status"] == "done":
        full = jobs.get(job_id) or {}
        result = full.get("result")
    now = time.time()
    end = snap["finished_at"] or now
    done_steps, total_steps = _step_counts(snap["steps"])
    meta = snap["meta"] or {}
    payload = {
        "id": snap["id"],
        "kind": "multi" if snap["type"] == "multi" else "single",
        "status": snap["status"],
        "query": meta.get("query"),
        "subject_id": meta.get("subject_id"),
        "subject_name": meta.get("subject_name") or meta.get("query"),
        "activity": js.activity_sentence(snap, result),
        "scope": js.scope_line(meta),
        "started_at": round(snap["started_at"], 3),
        "server_now": round(now, 3),
        "elapsed_s": round(max(0.0, end - snap["started_at"]), 1),
        "steps": [
            {"key": s["key"], "country": s["country"], "engine": s["engine"],
             "state": s["state"], "apify_status": s["apify_status"],
             "apify_elapsed_s": s["apify_elapsed_s"], "summary": s["summary"],
             "message": s["message"], "stage": s.get("stage")}
            for s in snap["steps"]
        ],
        "done_steps": done_steps,
        "total_steps": total_steps,
        "summary": js.job_summary(snap, result) if snap["status"] != "running" else None,
        "log": snap["progress"],
        "error": js.plain_error(snap["error"]) if snap["error"] else None,
        "redirect": job_redirect_target(job_id, snap["status"], result),
        "view_url": job_view_url(full) if full else None,
        "done_note": _done_note(full) if full else None,
    }
    return JSONResponse(payload, headers=_NO_STORE)


# --------------------------------------------------------------------- #
# GET /jobs/active, POST /jobs/{id}/seen (top-bar chip, toasts, Runs)
# --------------------------------------------------------------------- #
def _brief(jobs, snap: dict, now: float, key: str) -> dict:
    """One job as GET /jobs/active lists it."""
    meta = snap.get("meta") or {}
    status = snap["status"]
    full = jobs.get(snap["id"]) if status == "done" else None
    result = (full or {}).get("result")
    done_steps, total_steps = _step_counts(snap["steps"])
    end = snap["finished_at"] or now
    return {
        "id": snap["id"],
        "subject_id": meta.get("subject_id"),
        "subject_name": meta.get("subject_name") or meta.get("query") or "",
        "query": meta.get("query") or "",
        "status": status,
        "activity": js.activity_sentence(snap, result),
        "done_steps": done_steps,
        "total_steps": total_steps,
        "started_at": round(snap["started_at"], 3),
        "elapsed_s": round(max(0.0, end - snap["started_at"]), 1),
        "finished_at": round(snap["finished_at"], 3) if snap["finished_at"] else None,
        "summary": js.job_summary(snap, result) if status != "running" else None,
        "error": js.plain_error(snap["error"]) if status == "error" else None,
        "redirect": job_redirect_target(snap["id"], status, result),
        "view_url": job_view_url(full) if full else None,
        "started_by": meta.get("started_by") or "",
        "seen": key in (snap.get("seen_by") or ()),
    }


def active_jobs(now: float | None = None) -> dict:
    """Running jobs, and jobs finished in the last ACTIVE_FINISHED_SECONDS
    that this viewer hasn't seen, of the subjects the viewer may see: the
    active subject's in `jobs`, the others in `other_jobs`."""
    jobs = _get_main().jobs
    now = time.time() if now is None else now
    key = viewer_key()
    visible = visible_subject_ids()
    active_sid = db.current_subject().get("id")
    mine, other = [], []
    for snap in jobs.snapshots():
        sid = (snap.get("meta") or {}).get("subject_id")
        if sid is None or sid not in visible:
            continue
        if snap["status"] != "running":
            finished = snap["finished_at"] or snap["started_at"]
            if key in (snap.get("seen_by") or ()) or now - finished > js.ACTIVE_FINISHED_SECONDS:
                continue
        (mine if sid == active_sid else other).append(_brief(jobs, snap, now, key))
    running = sum(1 for j in mine + other if j["status"] == "running")
    return {"server_now": round(now, 3), "subject_id": active_sid, "jobs": mine,
            "other_jobs": other, "other_count": len(other), "running": running}


@router.get("/jobs/active", tags=["search-pages"])
async def jobs_active():
    return JSONResponse(active_jobs(), headers=_NO_STORE)


@router.post("/jobs/{job_id}/seen", tags=["search-pages"])
async def job_seen(job_id: str):
    """Hides a finished job from this viewer's chip/toasts. Same-site only
    (csrf_middleware); allowed for read-only accounts (role_middleware)."""
    jobs = _get_main().jobs
    snap = jobs.snapshot(job_id)
    if snap is None or not _job_visible(snap):
        return _not_found_json(job_id)
    jobs.mark_seen(job_id, viewer_key())
    return JSONResponse({"ok": True, "id": job_id}, headers=_NO_STORE)


def running_now_html() -> str:
    """Runs panel: "Running now" (the active subject's running searches;
    each opens the job panel). Kept current by app.js (SM.jobs)."""
    jobs = _get_main().jobs
    sid = db.current_subject().get("id")
    if sid not in visible_subject_ids():
        return ""
    items = []
    now = time.time()
    for snap in jobs.snapshots():
        meta = snap.get("meta") or {}
        if snap["status"] != "running" or meta.get("subject_id") != sid:
            continue
        done_steps, total_steps = _step_counts(snap["steps"])
        pct = int(done_steps * 100 / total_steps) if total_steps else 0
        jid = _esc(snap["id"])
        items.append(
            f"<a class='li job-now' href='/job/{jid}' data-panel='/ui/job/{jid}' data-title='Search progress' "
            f"data-active-job='{jid}'>"
            "<span class='li-top'><span class='job-spinner' aria-hidden='true'></span>"
            f"<b>{_esc(meta.get('query') or 'Search')}</b>"
            f"<span class='muted num' data-active-job-elapsed>{_fmt_elapsed(now - snap['started_at'])}</span></span>"
            f"<span class='li-sub' data-active-job-activity>{_esc(js.activity_sentence(snap))}</span>"
            f"<span class='job-progress sm' aria-hidden='true'><span class='job-progress-bar' data-active-job-bar "
            f"style='width:{pct}%'></span></span></a>")
    if not items:
        return ""
    return ("<section class='running-now' aria-labelledby='running-now-h'>"
            "<h3 id='running-now-h' class='section-label'>Running now</h3>"
            f"<div class='list'>{''.join(items)}</div></section>")


# --------------------------------------------------------------------- #
# GET /ui/job/{id}: live progress in a side panel
# --------------------------------------------------------------------- #
def _gone_panel():
    inner = ui.overlay("Search", "<div class='empty'><b>This search is no longer available.</b>"
                       "<br>It finished a while ago or the server restarted. Its results, if any, "
                       "are under Runs.</div>",
                       "<button type='button' class='btn ghost' data-sm-close>Close</button>")
    return fragment_or_page("Search", inner, active="/results", status_code=404)


_NEW_SEARCH_URL = overlay_url("/ui/search", "modal")


@router.get("/ui/job/{job_id}", response_class=HTMLResponse, tags=["ui"])
async def ui_job(job_id: str):
    jobs = _get_main().jobs
    job = jobs.get(job_id)
    if not job or not _job_visible(job):
        return _gone_panel()
    _mark_seen_if_finished(jobs, job)
    meta = job.get("meta") or {}
    name = meta.get("subject_name") or meta.get("query") or "your subject"
    status = job["status"]
    body = _live_card(job_id, job)
    view = job_view_url(job) if status == "done" else "/results"
    if status == "running":
        body += ("<p class='note' data-job-done-note hidden></p>"
                 "<p class='note job-foot'>Run in background keeps it going; the top bar shows how "
                 "it's doing and tells you when it's done.</p>")
    elif status == "done":
        body += f"<p class='note' data-job-done-note>{_esc(_done_note(job))}</p>"
    bg_label = "Run in background" if status == "running" else "Close"
    foot = (f"<button type='button' class='btn ghost' data-sm-close data-job-background>{bg_label}</button>"
            f"<a class='btn primary' href='{_esc(_NEW_SEARCH_URL)}' data-job-retry"
            f"{'' if status == 'error' else ' hidden'}>Back to New search</a>"
            f"<a class='btn primary' href='{_esc(view)}' data-job-view"
            f"{'' if status == 'done' else ' hidden'}>View results</a>")
    title = f"Searching {name}" if status == "running" else f"Search for {name}"
    inner = ui.overlay(title, body, foot)
    return HTMLResponse(inner, headers=_NO_STORE) if is_fragment_request() else \
        fragment_or_page(title, inner, active="/results")


# --------------------------------------------------------------------- #
# GET /job/{id} (server-rendered; running state is updated by app.js)
# --------------------------------------------------------------------- #
@router.get("/job/{job_id}", response_class=HTMLResponse, tags=["search-pages"])
async def show_job(job_id: str):
    main = _get_main()
    jobs = main.jobs
    job = jobs.get(job_id)
    if not job or not _job_visible(job):
        return HTMLResponse(page(
            "Job not found",
            "<div class='card'><p>This search is no longer available (it doesn't exist, "
            "or the server restarted).</p><a href='/search_page'>Start a new search</a></div>",
            active="/search_page"), status_code=404, headers=_NO_STORE)
    _mark_seen_if_finished(jobs, job)

    if job["status"] == "running":
        box, poll = _status_box(job_id, job)
        body = (
            f"<div class='card job-card' id='job' data-job-id='{_esc(job_id)}'{poll} "
            f"data-job-state='running' "
            f"data-started-at='{job['started_at']:.3f}' data-server-now='{time.time():.3f}'>"
            f"<h2 class='job-title'>{_esc((job.get('meta') or {}).get('query') or 'Search')}</h2>"
            f"{box}{_details_html(job)}"
            f"<p class='muted job-foot'>The search runs on the server — you can leave this page "
            f"and come back; the top bar shows how it's doing. "
            f"<a href='/job/{_esc(job_id)}' data-job-refresh>Refresh</a></p>"
            f"<noscript><meta http-equiv='refresh' content='5'></noscript>"
            f"</div>"
        )
        return HTMLResponse(page("Search in progress", body, active="/search_page"),
                            headers=_NO_STORE)

    job_link = f"<p class='muted'>Job <a href='/job/{_esc(job_id)}'>{_esc(job_id)}</a></p>"

    if job["status"] == "error":
        # Notes (skipped engines...) are in Details, without the in-flight
        # "Searching in xx…" lines (_details_html).
        box, _poll = _status_box(job_id, job)
        body = (f"<div class='card job-card job-error' data-job-id='{_esc(job_id)}'>"
                f"<h2 class='job-title'>{_esc((job.get('meta') or {}).get('query') or 'Search')}</h2>"
                f"<h3 class='job-error-title'>The search failed</h3>"
                f"{box}{_details_html(job)}"
                f"<p><a class='btn primary' href='/search_page'>Back to New search</a></p>"
                f"{job_link}</div>")
        return page("Search error", body, active="/search_page")

    res = job["result"] or {}
    details = _details_html(job) if job.get("steps") else ""
    done_line = f"<p class='job-activity is-done'>{_esc(js.activity_sentence(job, res))}</p>"
    if res.get("kind") == "single":
        s = res["summary"]
        body = (
            f"<div class='card'><h2>Search completed</h2>{done_line}"
            f"<p class='muted'>{_esc((res.get('country') or '').upper())} · "
            f"Tracked name: {_esc(res['query'])}</p>"
            f"<p>Total: <b>{s['total']}</b> &nbsp; {_pill('positive')} {s['positive']} &nbsp;"
            f"{_pill('negative')} {s['negative']} &nbsp; {_pill('neutral')} {s['neutral']}</p>"
            f"<p><a href='{_esc(job_view_url(job))}'>View results</a> · "
            f"<a href='/ui/reports' data-modal>Reports</a></p>{details}{job_link}</div>"
            + _results_table(res["results"])
        )
        return page("Search result", body, active="/search_page")

    agg = res.get("agg", {})
    rows = ""
    for o in res.get("outcomes", []):
        if o.get("error"):
            rows += (f"<tr class='negative'><td>{_esc(o['country']).upper()}</td>"
                     f"<td colspan='5' style='color:var(--neg-text)'>ERROR: {_esc(o['error'])}</td></tr>")
        else:
            sm = o["summary"]
            rid = _esc(o["run_id"])
            rows += (f"<tr><td>{_esc(o['country']).upper()}</td><td>{_esc(o['status'])}</td>"
                     f"<td>{sm.get('positive',0)}</td><td>{sm.get('negative',0)}</td>"
                     f"<td>{sm.get('neutral',0)}</td><td><a href='/run/{rid}'>view ›</a></td></tr>")
    body = (
        f"<div class='card'><h2>Multi-country report completed</h2>{done_line}"
        f"<p class='muted'>Tracked name: {_esc(res.get('query'))}</p>"
        f"<p>Countries OK: <b>{agg.get('countries_ok',0)}</b> · With error: <b>{agg.get('countries_error',0)}</b></p>"
        f"<p>Aggregate: {_pill('positive')} {agg.get('positive',0)} &nbsp; "
        f"{_pill('negative')} {agg.get('negative',0)} &nbsp; "
        f"{_pill('neutral')} {agg.get('neutral',0)} &nbsp; (total {agg.get('total',0)})</p>"
        f"<p><a href='{_esc(job_view_url(job))}'>View results</a> · "
        f"<a href='/ui/reports' data-modal>Reports</a></p>{job_link}</div>"
        f"<div class='card'><table><tr><th>Country</th><th>Status</th><th>Pos</th><th>Neg</th>"
        f"<th>Neu</th><th>Detail</th></tr>{rows}</table></div>"
        + (f"<div class='card'>{details}</div>" if details else "")
    )
    return page("Multi-country report", body, active="/search_page")
