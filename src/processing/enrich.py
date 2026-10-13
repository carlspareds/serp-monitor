"""
Automatic identity annotation (2.3): is_same_person in BATCH over new
results, BEFORE saving them (db.save_results must not call AI -- see
database/models.py). Single entry point: annotate_same_person, called from
the 3 save chokepoints (main.py::run_search, report/multi_country.py::
run_multi_country -- which in turn covers /search_multi and
report/weekly.py).

2.1 spirit (inheritance): a URL with an already-known same_person (from ANY
prior observation, human or AI -- see H2 in save_results) is never asked
again; only genuinely new URLs pay for a Gemini call, and that call is done
in BATCH (up to _BATCH_SIZE results per call) to mind the budget.
"""
from database.models import url_hash
from processing import ai_classifier

# Batch size per Gemini call (2.3, budget): the deep dive asks for "one
# Gemini call with N results"; 10 is a reasonable prompt size
# (title+URL+description truncated) without getting close to token limits.
# DECLINED to lower it (arbitration): pages>1 in /search_page is reachable
# from the UI (30+ results per country), so the chunking is NOT
# speculative.
_BATCH_SIZE = 10


def _known_same_person(db) -> dict[str, str]:
    """url_hash -> already-persisted same_person (not NULL) from any prior
    observation. Source of the inheritance.

    H3 (arbitration): ORDER BY (classified_by='human'), id -- human rows
    are processed LAST, so if conflicting values exist for the same URL
    across observations (e.g. an old AI row said 'unsure' and later a human
    set 'no' via /review, or vice versa by insertion order), the dict
    comprehension keeps the LAST assignment -- the human one ALWAYS wins,
    regardless of whether it's the oldest or newest row by id."""
    conn = db._connect()
    cur = conn.cursor()
    # C21b-2: the inherited same_person verdict has to be for the SAME
    # subject -- otherwise, deciding whether a URL "is the monitored
    # person" would rely on what someone said about a comparable pair from
    # A11.
    subject_clause, subject_params = db._subject_clause()
    rows = cur.execute(
        f"SELECT url, same_person FROM search_results WHERE same_person IS NOT NULL "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
        f"AND {subject_clause} ORDER BY (classified_by = 'human'), "
        "(classified_by = 'human' AND same_person = 'no'), COALESCE(human_at, ''), id",
        subject_params,
    ).fetchall()
    conn.close()
    return {url_hash(row["url"] or ""): row["same_person"] for row in rows}


def _annotate_chunk(chunk: list[dict], context: str) -> None:
    """Annotates same_person on each result in `chunk` (in-place mutation)
    with the real batch. H7/H9 (arbitration): NO per-URL fallback --
    is_same_person_batch is already tolerant of partials (see
    _parse_same_person_batch) and returns a list aligned 1:1 with `chunk`
    (None for what couldn't be resolved). A None gets retried on the next
    observation of that URL instead of re-paying a per-URL call right when
    the batch already failed for that item."""
    verdicts = ai_classifier.is_same_person_batch(chunk, context)
    for r, verdict in zip(chunk, verdicts):
        r["same_person"] = verdict


def annotate_same_person(results: list[dict], db, context: str = "") -> list[dict]:
    """Annotates result['same_person'] ('yes'|'no'|'unsure'|None) on each
    result of `results` (in-place mutation, and also returned for
    convenience). Must be called AFTER classify_batch and BEFORE
    db.save_results.

    - Empty `context` (default) falls back to the active subject's context
      (Database.get_subject_context; settings.subject_context for subject 1), and
      if there's NO saved setting EITHER (absent or explicitly empty --
      H18: saving "" from /settings falls back to the default here, the
      ONLY point that reads the fallback) falls back to
      DEFAULT_SUBJECT_CONTEXT. The 3 callers don't duplicate this lookup.
    - ALREADY known URL (non-NULL same_person from a prior observation):
      inherits that value WITHOUT calling Gemini -- it's data already
      decided (see _known_same_person for human priority on conflicts).
    - NEW URLs: is_same_person_batch already internally gates Gemini
      off/failed by returning None per position -- no duplicating that
      check here (see ai_classifier.is_same_person_batch).
    """
    # Multi-subject phase 1: the ACTIVE subject's context (db.current_subject():
    # for_subject -> request ContextVar -> subject 1, which falls back to the legacy
    # subject_context setting). The same_person cache version hashes the context, so
    # verdicts are cached per subject automatically.
    context = context or db.get_subject_context() or ai_classifier.default_subject_context()
    known = _known_same_person(db)

    new_results = []
    for r in results:
        h = url_hash(r.get("url") or "")
        if h in known:
            r["same_person"] = known[h]
        else:
            r.setdefault("same_person", None)
            new_results.append(r)

    for i in range(0, len(new_results), _BATCH_SIZE):
        _annotate_chunk(new_results[i : i + _BATCH_SIZE], context)

    return results
