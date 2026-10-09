"""
Severity 1-5 + topic taxonomy for negatives (2.2)
================================
annotate_severity(db, run_id): for each NEGATIVE result of this run that
isn't a discarded namesake (same_person != 'no') and doesn't yet have a
computed severity (severity IS NULL), calls
ai_classifier.analyze_negative (cached per URL, see 2.5 -- PROMPT_ANALYZE_
NEG_V2, severity rubric + closed topic taxonomy) and persists the result in
`search_results`.

Negatives only (deep dive §2.2, token savings): a positive/neutral never
enters `get_negatives_pending_severity`.

TOTAL best-effort (same spirit as processing/enrich.py::
annotate_same_person): without AI configured, or if analyze_negative
doesn't return a valid verdict (Gemini down, non-JSON response...), the row
simply stays with severity/topic as NULL -- it NEVER breaks the run. There
is NO mass backfill on startup: historical negatives without severity fill
themselves in as they reappear in a new run (the very `severity IS NULL`
condition in get_negatives_pending_severity guarantees it).

Chokepoint (main.py::run_search and report/multi_country.py): called right
AFTER db.save_results and BEFORE update_url_lifecycle -- it needs result_id
(search_results.id), which only exists after the INSERT; unlike
annotate_same_person (2.3, which annotates the dict IN MEMORY before
saving), this annotation is a later UPDATE by id.

annotate_severity_for_result(db, result_id) (F6, review 2.2): same core,
for ONE specific row -- used by dashboard/routers/review.py after a HUMAN
correction that sets classification='negative', because that row does NOT
go through any run chokepoint and would stay with severity NULL forever if
no one sees it again in a new run.

F12 (review 2.2, consistency note): the persisted `severity`/`topic`
columns of `search_results` are the source of truth for alerts
(processing/alerts.py) and for the /results UI. A direct analyze_negative
call re-derives them from the AI cache (ai_verdict_cache) and can diverge
if the cache is cleared or PROMPT_VERSIONS["analyze_negative"] bumps.
"""
from processing import ai_classifier


def _apply_severity(db, row: dict) -> None:
    """Shared core: `row` is already assumed eligible (negative, not a
    namesake, severity IS NULL -- see get_negatives_pending_severity/
    get_result_pending_severity). Calls analyze_negative and only persists
    if the verdict is valid. Best-effort: any exception (AI down, a network
    error not caught by ai_classifier._call_gemini, etc.) is swallowed --
    it must never bring down the caller (a run chokepoint, or the /review
    POST)."""
    try:
        info = ai_classifier.analyze_negative(row)
    except Exception:  # noqa: BLE001 -- best-effort, never breaks the caller
        return
    severity = info.get("severity")
    topic = info.get("topic")
    if not isinstance(severity, int) or not topic:
        # AI failure (not configured, non-JSON response, a verdict cached
        # from a prior failure...) -- the row stays with severity/topic as
        # NULL, retried the next time someone asks for this row again
        # (same criterion: severity IS NULL).
        return
    db.set_result_severity(row["id"], severity, topic)


def annotate_severity(db, run_id: str) -> None:
    """See the module docstring. Returns nothing -- mutates the DB
    in-place (same style as update_url_lifecycle/capture_new_negatives,
    this chokepoint's other post-save steps)."""
    for r in db.get_negatives_pending_severity(run_id):
        _apply_severity(db, r)


def annotate_severity_for_result(db, result_id: int) -> None:
    """F6 (review 2.2): see the module docstring. No-op if `result_id`
    doesn't exist or doesn't qualify (not negative, a confirmed 'no'
    namesake, or already has severity -- IMMUTABLE, what's already computed
    is never recomputed)."""
    row = db.get_result_pending_severity(result_id)
    if row is None:
        return
    _apply_severity(db, row)
