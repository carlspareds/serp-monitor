"""
Per-URL lifecycle (3.1)
================================
Post-run job `update_url_lifecycle(db, run_id)`: maintains `url_lifecycle`
(first/last seen, best/last position, status in {active, fading, gone}) for
EVERY result of the run (namesakes included -- filtering by same_person is
the READER's business -- Wins, alerts -- not the recorder's, which just
logs that the URL was there).

Called at the same 2 save chokepoints as
processing.enrich.annotate_same_person (same pattern): main.py::run_search
and report/multi_country.py::run_multi_country (which in turn covers
/search_multi and report/weekly.py) -- ALWAYS after db.save_results, so
that get_results_by_run(run_id) already sees this run's rows.

Per-engine identity (1.1, review HIGH #1): `run_id` belongs to ONE engine
("one DB run per engine", see apify.runner.ApifyRunner.search::engine_runs
and report.multi_country.persist_engine_run) -- url_lifecycle.engine
(composite PK url_hash+country+engine) keeps EACH engine's lifecycle
SEPARATE. Without this, a bing pass would treat URLs that only google
observes as "absent" (accelerated fading/gone, false wins) because
everything would collapse into the table's default 'google' rows -- and a
URL present in both engines would have two observations stepping on one
row (best_position/missed_runs mixed between engines).

An `active`/`fading` URL for a country that doesn't appear in that
country's most recent run accumulates a "missed run" (missed_runs), with
two safeguards (arbitration, 3.1 review):

  - Effective depth (H1): an EMPTY run (depth 0) doesn't penalize anyone; a
    PARTIAL run (depth < a URL's historical position) doesn't penalize that
    specific URL either -- if the run didn't go that deep, its absence
    isn't a signal of anything.
  - Per-query cohort (H2): only URLs that were at some point observed under
    ANY of THIS run's queries, in this country, are penalized -- an ad-hoc
    search with another query doesn't degrade the main query's tracking.

Upon reaching settings.gone_after_runs (default 3) a URL moves to `gone`:
if its last known classification was 'negative' and it isn't a discarded
namesake (same_person != 'no'), it generates an automatic event + a
POSITIVE `url_gone` alert (severity info) -- that same_person is PERSISTED
on the row at that moment (H5). If a `gone` URL reappears, it goes back to
`active` and the open alert auto-resolves (processing.auto_resolve, same
as 7.2).

Robustness safeguards:
  - Per-run idempotency (H3): `runs.lifecycle_done` prevents a second call
    on the same run_id from duplicating missed_runs.
  - Self-healing (H4): if the process dies between the url_lifecycle commit
    and the `add_alert` (e.g. a crash), the NEXT call (from any run) sweeps
    the `gone`+`negative` rows without an active url_gone alert and emits
    it -- add_alert is idempotent via dedupe_key, so this sweep is cheap
    and safe on every call.
"""
import json
from datetime import UTC, datetime

from database.models import url_hash
from notify.dispatcher import emit_alert
from processing.auto_resolve import auto_resolve

GONE_AFTER_RUNS_DEFAULT = 3


def _now_iso() -> str:
    """Naive-UTC ISO (H7, arbitration 3.1): same format as the rest of
    models.py (datetime.utcnow().isoformat(), no '+00:00' suffix), but via
    datetime.now(UTC).replace(tzinfo=None) -- utcnow() is deprecated (ruff
    DTZ003) and models.py only has the free pass for being pre-existing
    code (see pyproject.toml per-file-ignores); new code doesn't."""
    return datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def _gone_after_runs(db) -> int:
    """settings.gone_after_runs, defensive parse (same pattern as
    Database.review_threshold): missing/invalid/<=0 falls back to the
    default."""
    raw = db.get_setting("gone_after_runs", str(GONE_AFTER_RUNS_DEFAULT))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return GONE_AFTER_RUNS_DEFAULT
    return value if value > 0 else GONE_AFTER_RUNS_DEFAULT


def _latest_same_person_by_hash(cur, db) -> dict[str, str]:
    """{url_hash: same_person} for ALL of search_results, ALWAYS
    prioritizing the human verdict (same approach as
    processing.enrich._known_same_person) -- keyed by url_hash because
    search_results does NOT have that column (H6, arbitration): two
    equivalent spellings of the same URL (different utm_, casing...) must
    resolve to the same verdict, and an exact-text match on `url` would
    miss it."""
    # C21b-2: the lifecycle is PER SUBJECT. Without the filter, the
    # same_person verdict from a comparable pair in A11 (subject_id 1000+)
    # would decide whether a URL belongs to the monitored subject.
    subject_clause, subject_params = db._subject_clause()
    rows = cur.execute(
        f"SELECT url, same_person FROM search_results WHERE same_person IS NOT NULL "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
        f"AND {subject_clause} ORDER BY (classified_by = 'human'), "
        "(classified_by = 'human' AND same_person = 'no'), COALESCE(human_at, ''), id",
        subject_params,
    ).fetchall()
    return {url_hash(r["url"] or ""): r["same_person"] for r in rows}


def _latest_domain_by_hash(cur, db) -> dict[str, str]:
    """{url_hash: domain} of the MOST RECENT observation of each URL --
    same reason as _latest_same_person_by_hash (H6): keyed by hash, not by
    exact text."""
    subject_clause, subject_params = db._subject_clause()
    rows = cur.execute(
        f"SELECT url, domain FROM search_results WHERE {subject_clause} ORDER BY id",  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
        subject_params,
    ).fetchall()
    out: dict[str, str] = {}
    for r in rows:
        out[url_hash(r["url"] or "")] = r["domain"] or (r["url"] or "")
    return out


def _emit_url_gone(db, h: str, country: str, domain: str, engine: str) -> None:
    """Emits (event + positive `url_gone` alert + notify_alert) for a
    negative URL that moved to 'gone'. Idempotent via dedupe_key: add_alert
    only inserts if that key isn't already active, so calling it extra
    times (e.g. from the H4 self-healing sweep) doesn't duplicate noise --
    the event and notify_alert ONLY fire when add_alert actually inserted
    (truthy alert_id).

    engine (1.1, review HIGH #1): part of the url_lifecycle row's identity
    (PK url_hash+country+engine) -- the dedupe_key includes it so that the
    SAME "gone" URL on two different engines generates (and auto-resolves)
    two independent alerts, not a single one stepping on itself."""
    message = f"Negative URL gone from top-N in {country}: {domain}"
    alert_id = emit_alert(db, "url_gone", "info", country.upper(), message,
                           f"url_gone:{h}:{country}:{engine}")
    if alert_id:
        db.add_event(_now_iso()[:10], message, kind="url_gone")


def _sweep_missing_alerts(db) -> None:
    """H4 (arbitration): self-healing for missed `url_gone` alerts. Sweeps
    ALL `gone`+`negative` rows of url_lifecycle (any country -- doesn't
    depend on the run that triggers this call) whose persisted same_person
    isn't 'no', and re-emits their alert (cheap: add_alert is already a
    no-op if the alert is still open). Runs AT THE START of
    update_url_lifecycle, before touching anything from the current run."""
    conn = db._connect()
    cur = conn.cursor()
    # Multi-subject: only THIS subject's gone rows -- otherwise a run of subject B
    # would re-emit subject A's url_gone alerts under B (add_alert stamps the active
    # subject), duplicating them across subjects.
    subject_clause, subject_params = db._subject_clause()
    rows = cur.execute(
        "SELECT * FROM url_lifecycle WHERE status = 'gone' AND classification = 'negative' "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
        f"AND {subject_clause}",
        subject_params,
    ).fetchall()
    domains = _latest_domain_by_hash(cur, db) if rows else {}
    conn.close()
    for row in rows:
        if row["same_person"] == "no":
            continue
        domain = domains.get(row["url_hash"]) or row["url"]
        _emit_url_gone(db, row["url_hash"], row["country"], domain, row["engine"] or "google")


def update_url_lifecycle(db, run_id: str) -> None:
    """Updates url_lifecycle after saving `run_id`'s results (ALWAYS call
    after db.save_results)."""
    _sweep_missing_alerts(db)  # H4: at the start, before anything else

    run_row = db.get_run(run_id)
    if run_row is None:
        return
    if run_row.get("lifecycle_done"):
        return  # H3: idempotency -- this run was already processed

    country = (run_row.get("country_code") or "").lower()
    # engine (1.1, review HIGH #1): url_lifecycle's identity alongside
    # url_hash+country (composite PK) -- without this, a bing run treated
    # google's URLs as "absent" (permanent fading, accelerated gone, false
    # wins) because everything collapsed into the table's default 'google'
    # rows. See the module docstring.
    engine = (run_row.get("engine") or "google").lower()
    results = db.get_results_by_run(run_id)  # raw, namesakes not filtered

    now = _now_iso()
    gone_after = _gone_after_runs(db)

    # H1: effective depth of THIS run -- the deepest position actually
    # returned. depth=0 (empty run, or no valid positions) isn't a signal
    # of anything.
    positions = [r.get("position") for r in results if isinstance(r.get("position"), int)]
    depth = max(positions) if positions else 0

    conn = db._connect()
    cur = conn.cursor()

    # A single row per url_hash for THIS run (the one with the best --
    # lowest -- position, if the URL appears more than once across
    # several queries).
    seen: dict[str, dict] = {}
    for r in results:
        h = url_hash(r.get("url") or "")
        if not h:
            continue
        best = seen.get(h)
        pos = r.get("position")
        if best is None:
            seen[h] = r
            continue
        best_pos = best.get("position")
        if isinstance(pos, int) and (not isinstance(best_pos, int) or pos < best_pos):
            seen[h] = r

    reappeared_keys: set[str] = set()
    # Multi-subject: url_lifecycle's PK is (url_hash, country, engine, subject_id) --
    # every read/update below is pinned to THIS run's subject.
    lc_clause, lc_params = db._subject_clause()

    for h, r in seen.items():
        url = r.get("url") or ""
        position = r.get("position")
        classification = r.get("classification")
        existing = cur.execute(
            "SELECT * FROM url_lifecycle WHERE url_hash = ? AND country = ? AND engine = ? "  # noqa: S608 -- clause is the constant 'subject_id = ?' (values bound)
            f"AND {lc_clause}",
            (h, country, engine, *lc_params),
        ).fetchone()
        if existing is None:
            cur.execute(
                # C21b-2: the row is born with the ACTIVE subject, not with
                # the column's DEFAULT of 1. Without this, every lifecycle
                # entry was born as subject 1's regardless of which run
                # created it, so a second subject would never see its own
                # win -- and subject 1's would get contaminated with URLs
                # that aren't theirs. Silent and systematic: nothing fails,
                # the data just ends up misattributed.
                "INSERT INTO url_lifecycle (url_hash, url, country, engine, first_seen, "
                "last_seen, best_position, last_position, classification, status, "
                "gone_since, missed_runs, subject_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, 0, ?)",
                (h, url, country, engine, now, now, position, position, classification,
                 db._active_subject_id(None)),
            )
            continue

        old_best = existing["best_position"]
        if isinstance(position, int) and (not isinstance(old_best, int) or position < old_best):
            best_position = position
        else:
            best_position = old_best
        cur.execute(
            "UPDATE url_lifecycle SET url = ?, last_seen = ?, last_position = ?, "  # noqa: S608 -- clause is the constant 'subject_id = ?' (values bound)
            "best_position = ?, classification = ?, status = 'active', gone_since = NULL, "
            f"missed_runs = 0 WHERE url_hash = ? AND country = ? AND engine = ? AND {lc_clause}",
            (url, now, position, best_position, classification, h, country, engine,
             *lc_params),
        )
        if existing["status"] == "gone":
            reappeared_keys.add(f"url_gone:{h}:{country}:{engine}")

    run_clause, run_params = db._subject_clause()
    cur.execute(f"UPDATE runs SET lifecycle_done = 1 WHERE apify_run_id = ? AND {run_clause}",  # noqa: S608 -- clause is the constant 'subject_id = ?' (values bound)
                 (run_id, *run_params))

    if depth == 0:  # H1: empty run (or no valid positions) -- doesn't penalize
        conn.commit()
        conn.close()
        if reappeared_keys:
            auto_resolve(db, "url_gone", lambda a: a["dedupe_key"] in reappeared_keys)
        return

    # H2: per-query cohort -- only URLs ever seen under ANY of THIS run's
    # queries, in this country, are penalized. An ad-hoc search with a
    # different query must not degrade URLs from another query.
    queries = json.loads(run_row.get("queries") or "[]")
    cohort: set[str] = set()
    if queries:
        placeholders = ",".join("?" for _ in queries)
        cohort_clause, cohort_params = db._subject_clause()
        cohort_rows = cur.execute(
            f"SELECT DISTINCT url FROM search_results WHERE country_code = ? "  # noqa: S608 -- clause is the constant 'subject_id = ?' (values bound); only '?' placeholders are interpolated
            f"AND query IN ({placeholders}) AND {cohort_clause}",
            (country, *queries, *cohort_params),
        ).fetchall()
        cohort = {url_hash(r["url"] or "") for r in cohort_rows}

    same_person_map = _latest_same_person_by_hash(cur, db)
    domain_map = _latest_domain_by_hash(cur, db)

    # active/fading URLs for this country AND ENGINE that did NOT appear in
    # this run -- filtered by engine (1.1): without this, a bing run saw
    # URLs that only google had observed as "absent".
    missing_rows = cur.execute(
        "SELECT * FROM url_lifecycle WHERE country = ? AND engine = ? "  # noqa: S608 -- clause is the constant 'subject_id = ?' (values bound)
        f"AND status IN ('active', 'fading') AND {lc_clause}",
        (country, engine, *lc_params),
    ).fetchall()
    # Wins eligible for an event+alert -- decided HERE (same cursor, same
    # transaction still open), but the INSERT into events/alerts is
    # deferred until after closing this connection (see below):
    # db.add_event/db.add_alert open their OWN connection, and with `conn`
    # still uncommitted that second connection would block against
    # url_lifecycle's pending write.
    pending_wins: list[tuple[str, str]] = []  # (url_hash, domain)
    for row in missing_rows:
        if row["url_hash"] in seen:
            continue
        if row["url_hash"] not in cohort:  # H2
            continue
        last_pos = row["last_position"]
        if not (isinstance(last_pos, int) and last_pos <= depth):  # H1
            continue
        missed = (row["missed_runs"] or 0) + 1
        if missed >= gone_after:
            same_person = same_person_map.get(row["url_hash"])
            cur.execute(
                "UPDATE url_lifecycle SET status = 'gone', gone_since = ?, missed_runs = ?, "  # noqa: S608 -- clause is the constant 'subject_id = ?' (values bound)
                f"same_person = ? WHERE url_hash = ? AND country = ? AND engine = ? AND {lc_clause}",
                (now, missed, same_person, row["url_hash"], country, engine, *lc_params),
            )
            if row["classification"] == "negative" and same_person != "no":
                domain = domain_map.get(row["url_hash"]) or row["url"]
                pending_wins.append((row["url_hash"], domain))
        else:
            cur.execute(
                "UPDATE url_lifecycle SET status = 'fading', missed_runs = ? "  # noqa: S608 -- clause is the constant 'subject_id = ?' (values bound)
                f"WHERE url_hash = ? AND country = ? AND engine = ? AND {lc_clause}",
                (missed, row["url_hash"], country, engine, *lc_params),
            )

    conn.commit()
    conn.close()

    # Events/alerts for the ones that just moved to gone (outside the
    # url_lifecycle transaction -- see comment above).
    for h, domain in pending_wins:
        _emit_url_gone(db, h, country, domain, engine)

    if reappeared_keys:
        auto_resolve(db, "url_gone", lambda a: a["dedupe_key"] in reappeared_keys)
