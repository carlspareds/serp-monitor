"""Tests for item 3.1: URL lifecycle (first/last seen + Victories).

Acceptance criteria (§3.1): "When a URL
is removed manually from a test run for 3 cycles, it becomes `gone`,
appears in Victories, and generates an event + alert." TDD, no network:
direct Database + processing.lifecycle.update_url_lifecycle -- no info
alert triggers real network calls (the default routing for 'info' is
'digest', a no-op in notify.dispatcher.notify_alert).

The "miss" runs (URL absent) use a filler at a DEEP position
(50) unless the test specifically exists to test effective
depth (H1, arbitration) -- this way the run's depth doesn't interfere with
tests that only want to test the missed_runs count.
"""
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import pytest

from database.models import Database, url_hash
from processing.lifecycle import GONE_AFTER_RUNS_DEFAULT, update_url_lifecycle

_DEEP = 50  # filler position in "miss" runs -- see docstring


def _recent_iso(days_ago: int = 1) -> str:
    """Naive-UTC ISO timestamp from `days_ago` days ago -- always within
    list_victories' default window (90 days), without depending on the
    date the tests run on (unlike a fixed literal)."""
    now = datetime.now(UTC).replace(tzinfo=None)
    return (now - timedelta(days=days_ago)).isoformat(timespec="seconds")


from conftest import make_db as _db


def _result(url: str, position: int, classification: str, **overrides) -> dict:
    base = {
        "query": "q", "url": url, "title": "t", "description": "d",
        "displayed_url": url, "position": position, "country_code": "us",
        "domain": urlsplit(url).netloc, "classification": classification,
    }
    base.update(overrides)
    return base


def _run(db: Database, run_id: str, results: list[dict], country="us", queries=None,
        engine="google") -> None:
    counts = {"positive": 0, "negative": 0, "neutral": 0}
    for r in results:
        counts[r.get("classification") or "neutral"] = counts.get(r.get("classification") or "neutral", 0) + 1
        r.setdefault("engine", engine)
    db.save_run(apify_run_id=run_id, queries=queries or ["q"], country_code=country,
                status="completed", summary={**counts, "total": len(results)}, engine=engine)
    db.save_results(results, run_id=run_id)


def _miss(db: Database, run_id: str, filler: str, country="us", queries=None,
         engine="google") -> None:
    """A "filler" run that doesn't see the URL under test -- filler at a
    deep position (_DEEP) so the run's depth doesn't exclude it via
    H1 (unless the test itself wants to test that)."""
    _run(db, run_id, [_result(filler, _DEEP, "neutral")], country=country, queries=queries,
        engine=engine)
    update_url_lifecycle(db, run_id)


def _lifecycle_row(db: Database, url: str, country="us", engine: str | None = None) -> dict | None:
    """engine=None (default): no filtering by engine -- same behavior
    as before 1.1 (useful in tests that only use 'google', the only engine
    present). engine='google'/'bing': the specific row for THAT engine (1.1,
    HIGH review #1 -- url_lifecycle distinguishes engine)."""
    conn = db._connect()
    if engine is not None:
        row = conn.execute(
            "SELECT * FROM url_lifecycle WHERE url_hash = ? AND country = ? AND engine = ?",
            (url_hash(url), country, engine),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM url_lifecycle WHERE url_hash = ? AND country = ?",
            (url_hash(url), country),
        ).fetchone()
    conn.close()
    return dict(row) if row else None


def _lifecycle_rows(db: Database, url: str, country="us") -> list[dict]:
    """ALL rows for a URL+country, without filtering by engine -- to
    verify that two different engines produce SEPARATE rows (1.1)."""
    conn = db._connect()
    rows = conn.execute(
        "SELECT * FROM url_lifecycle WHERE url_hash = ? AND country = ?",
        (url_hash(url), country),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _url_gone_alerts(db) -> list[dict]:
    return [a for a in db.list_alerts(status="all") if a["kind"] == "url_gone"]


# --------------------------------------------------------------------- #
# Basic upsert: first/last seen + best position
# --------------------------------------------------------------------- #
def test_upsert_tracks_first_last_seen_and_best_position(tmp_path):
    db = _db(tmp_path)
    url = "https://negative.example/a"

    _run(db, "r1", [_result(url, 5, "negative")])
    update_url_lifecycle(db, "r1")
    row1 = _lifecycle_row(db, url)
    assert row1["status"] == "active"
    assert row1["missed_runs"] == 0
    assert row1["best_position"] == 5
    assert row1["last_position"] == 5
    first_seen = row1["first_seen"]

    _run(db, "r2", [_result(url, 3, "negative")])
    update_url_lifecycle(db, "r2")
    row2 = _lifecycle_row(db, url)
    assert row2["first_seen"] == first_seen  # no cambia
    assert row2["last_position"] == 3
    assert row2["best_position"] == 3  # min(5, 3)
    assert row2["missed_runs"] == 0


def test_upsert_new_run_with_a_worse_position_keeps_best(tmp_path):
    db = _db(tmp_path)
    url = "https://negative.example/b"

    _run(db, "r1", [_result(url, 2, "negative")])
    update_url_lifecycle(db, "r1")

    _run(db, "r2", [_result(url, 9, "negative")])
    update_url_lifecycle(db, "r2")

    row = _lifecycle_row(db, url)
    assert row["last_position"] == 9
    assert row["best_position"] == 2  # the historical best doesn't get worse


# --------------------------------------------------------------------- #
# H7: naive-UTC timestamps (no +00:00 suffix), same format as the rest
# of models.py.
# --------------------------------------------------------------------- #
def test_timestamps_are_naive_utc_without_offset_suffix(tmp_path):
    db = _db(tmp_path)
    url = "https://negative.example/naive-ts"

    _run(db, "r1", [_result(url, 1, "negative")])
    update_url_lifecycle(db, "r1")

    row = _lifecycle_row(db, url)
    assert "+00:00" not in row["first_seen"]
    assert "+00:00" not in row["last_seen"]



# --------------------------------------------------------------------- #
# Disappearance: 3 runs without seeing it -> gone + event + url_gone alert
# --------------------------------------------------------------------- #
def test_url_absent_three_runs_becomes_gone_with_event_and_alert(tmp_path):
    db = _db(tmp_path)
    gone_url = "https://negative.example/gone"
    filler_url = "https://neutral.example/filler"

    _run(db, "r1", [_result(gone_url, 4, "negative"), _result(filler_url, 1, "neutral")])
    update_url_lifecycle(db, "r1")

    # 3 consecutive runs from the same country WITHOUT the negative URL.
    for i in range(2, 2 + GONE_AFTER_RUNS_DEFAULT):
        _miss(db, f"r{i}", filler_url)

    row = _lifecycle_row(db, gone_url)
    assert row["status"] == "gone"
    assert row["gone_since"] is not None
    assert row["missed_runs"] == GONE_AFTER_RUNS_DEFAULT
    assert row["same_person"] is None  # H5: persisted as-is when transitioning to gone

    events = db.list_events()
    assert any("negative.example" in e["description"] for e in events)

    url_gone_alerts = _url_gone_alerts(db)
    assert len(url_gone_alerts) == 1
    assert url_gone_alerts[0]["severity"] == "info"
    # engine (1.1): dedupe_key includes the engine -- 'google' by default.
    assert url_gone_alerts[0]["dedupe_key"] == f"url_gone:{url_hash(gone_url)}:us:google"


def test_fading_status_between_first_miss_and_gone_threshold(tmp_path):
    db = _db(tmp_path)
    url = "https://negative.example/fading"
    filler = "https://neutral.example/filler2"

    _run(db, "r1", [_result(url, 4, "negative"), _result(filler, 1, "neutral")])
    update_url_lifecycle(db, "r1")

    _miss(db, "r2", filler)

    row = _lifecycle_row(db, url)
    assert row["status"] == "fading"
    assert row["missed_runs"] == 1
    assert row["gone_since"] is None

    # No alert/event yet -- the threshold wasn't reached.
    assert db.list_alerts(status="all") == []
    assert db.list_events() == []


def test_url_absent_in_a_different_country_does_not_count(tmp_path):
    """gone_after_runs counts RUNS FROM THE SAME COUNTRY -- runs from another
    country don't affect the missed_runs counter."""
    db = _db(tmp_path)
    url = "https://negative.example/us-only"

    _run(db, "r1-us", [_result(url, 4, "negative", country_code="us")], country="us")
    update_url_lifecycle(db, "r1-us")

    for i in range(2, 2 + GONE_AFTER_RUNS_DEFAULT):
        _miss(db, f"r{i}-ca", "https://filler.net/x", country="ca")

    row = _lifecycle_row(db, url, country="us")
    assert row["status"] == "active"
    assert row["missed_runs"] == 0


# --------------------------------------------------------------------- #
# H1: effective depth -- empty/partial run does not penalize
# --------------------------------------------------------------------- #
def test_empty_run_does_not_penalize_anyone(tmp_path):
    db = _db(tmp_path)
    url = "https://negative.example/empty-run-safe"

    _run(db, "r1", [_result(url, 3, "negative")])
    update_url_lifecycle(db, "r1")

    _run(db, "r2", [])  # empty run -- depth 0
    update_url_lifecycle(db, "r2")

    row = _lifecycle_row(db, url)
    assert row["status"] == "active"
    assert row["missed_runs"] == 0


def test_partial_run_does_not_degrade_urls_below_crawled_depth(tmp_path):
    """A PARTIAL run (top-4) didn't see deep_url (historical position 9, outside
    what was tracked) -- it isn't penalized. shallow_url (historical position 2)
    WAS within the run's scope and didn't appear -- it is penalized."""
    db = _db(tmp_path)
    deep_url = "https://negative.example/deep"
    shallow_url = "https://neutral.example/shallow"

    _run(db, "r1", [_result(deep_url, 9, "negative"), _result(shallow_url, 2, "neutral")])
    update_url_lifecycle(db, "r1")

    for i in range(2, 2 + GONE_AFTER_RUNS_DEFAULT):
        _run(db, f"r{i}", [_result("https://neutral.example/top4-filler", 4, "neutral")])
        update_url_lifecycle(db, f"r{i}")

    deep_row = _lifecycle_row(db, deep_url)
    assert deep_row["status"] == "active"
    assert deep_row["missed_runs"] == 0

    shallow_row = _lifecycle_row(db, shallow_url)
    assert shallow_row["missed_runs"] == GONE_AFTER_RUNS_DEFAULT
    assert shallow_row["status"] == "gone"


# --------------------------------------------------------------------- #
# H2: query cohort -- an ad-hoc search with a different query doesn't degrade
# the main query's tracking.
# --------------------------------------------------------------------- #
def test_ad_hoc_search_with_a_different_query_does_not_degrade_main_query_urls(tmp_path):
    db = _db(tmp_path)
    main_url = "https://negative.example/main-query-url"
    other_filler = "https://neutral.example/other-query-filler"

    _run(db, "r1", [_result(main_url, 3, "negative", query="Main Person")],
         queries=["Main Person"])
    update_url_lifecycle(db, "r1")

    # Consecutive runs under a DIFFERENT query (ad-hoc): main_url was never
    # observed under "Other Topic", so it shouldn't be degraded.
    for i in range(2, 3 + GONE_AFTER_RUNS_DEFAULT):
        _run(db, f"r{i}", [_result(other_filler, 1, "neutral", query="Other Topic")],
             queries=["Other Topic"])
        update_url_lifecycle(db, f"r{i}")

    row = _lifecycle_row(db, main_url)
    assert row["status"] == "active"
    assert row["missed_runs"] == 0


# --------------------------------------------------------------------- #
# Identity by engine (1.1, HIGH review #1): url_lifecycle distinguishes
# engine -- a bing pass shouldn't penalize google's URLs, and a
# URL seen by both engines should carry two separate observations.
# --------------------------------------------------------------------- #
def test_bing_pass_does_not_penalize_google_only_url_across_three_cycles(tmp_path):
    """HIGH regression #1: before identity by engine, url_lifecycle
    collapsed everything into the default 'google' rows -- a bing pass
    (which never sees google's top-1 URL) increased its missed_runs
    just as if google had stopped seeing it, sending it to 'gone'
    in 3 cycles even though google NEVER stopped seeing it."""
    db = _db(tmp_path)
    google_top1 = "https://negative.example/google-top1"
    bing_filler = "https://neutral.example/bing-only"

    for i in range(1, 1 + GONE_AFTER_RUNS_DEFAULT):
        _run(db, f"g{i}", [_result(google_top1, 1, "negative")], engine="google")
        update_url_lifecycle(db, f"g{i}")
        _run(db, f"b{i}", [_result(bing_filler, 1, "neutral")], engine="bing")
        update_url_lifecycle(db, f"b{i}")

    row = _lifecycle_row(db, google_top1, engine="google")
    assert row["status"] == "active"
    assert row["missed_runs"] == 0


def test_url_seen_by_both_engines_creates_two_separate_rows(tmp_path):
    """A URL present in BOTH engines shouldn't collapse into a single
    url_lifecycle row overwriting best_position -- each engine carries its own
    observation (PK url_hash+country+engine)."""
    db = _db(tmp_path)
    shared_url = "https://negative.example/shared-both-engines"

    _run(db, "g1", [_result(shared_url, 2, "negative")], engine="google")
    update_url_lifecycle(db, "g1")
    _run(db, "b1", [_result(shared_url, 5, "negative")], engine="bing")
    update_url_lifecycle(db, "b1")

    rows = _lifecycle_rows(db, shared_url)
    assert len(rows) == 2
    assert {r["engine"] for r in rows} == {"google", "bing"}
    google_row = next(r for r in rows if r["engine"] == "google")
    bing_row = next(r for r in rows if r["engine"] == "bing")
    assert google_row["best_position"] == 2
    assert bing_row["best_position"] == 5


# --------------------------------------------------------------------- #
# H3: per-run idempotency -- double call doesn't duplicate misses
# --------------------------------------------------------------------- #
def test_double_call_on_same_run_does_not_double_count_misses(tmp_path):
    db = _db(tmp_path)
    url = "https://negative.example/idempotent"
    filler = "https://neutral.example/idempotent-filler"

    _run(db, "r1", [_result(url, 3, "negative"), _result(filler, _DEEP, "neutral")])
    update_url_lifecycle(db, "r1")

    _run(db, "r2", [_result(filler, _DEEP, "neutral")])
    update_url_lifecycle(db, "r2")
    update_url_lifecycle(db, "r2")  # double call -- shouldn't duplicate the miss

    row = _lifecycle_row(db, url)
    assert row["missed_runs"] == 1
    assert row["status"] == "fading"


# --------------------------------------------------------------------- #
# H4: missed alert (crash between commit and add_alert) -- the next
# call auto-heals it.
# --------------------------------------------------------------------- #
def test_missed_alert_after_crash_is_emitted_by_next_update(tmp_path, monkeypatch):
    db = _db(tmp_path)
    url = "https://negative.example/crash"
    filler = "https://neutral.example/crash-filler"

    _run(db, "r1", [_result(url, 4, "negative"), _result(filler, _DEEP, "neutral")])
    update_url_lifecycle(db, "r1")

    for i in range(2, 1 + GONE_AFTER_RUNS_DEFAULT):
        _miss(db, f"r{i}", filler)

    # The next miss crosses the threshold -- simulates the process dying right
    # before add_alert() completes (e.g. a worker crash).
    orig_add_alert = db.add_alert

    def boom(*a, **kw):
        raise RuntimeError("simulated crash before alert insert")

    monkeypatch.setattr(db, "add_alert", boom)
    last_run = f"r{1 + GONE_AFTER_RUNS_DEFAULT}"
    _run(db, last_run, [_result(filler, _DEEP, "neutral")])
    with pytest.raises(RuntimeError):
        update_url_lifecycle(db, last_run)

    # The status is ALREADY 'gone' -- it was committed BEFORE the alert attempt.
    assert _lifecycle_row(db, url)["status"] == "gone"
    assert _url_gone_alerts(db) == []  # add_alert never got to insert

    monkeypatch.setattr(db, "add_alert", orig_add_alert)

    # Any subsequent call (from any run) sweeps it up and emits it.
    _run(db, "healer-run", [_result(filler, _DEEP, "neutral")])
    update_url_lifecycle(db, "healer-run")

    alerts = _url_gone_alerts(db)
    assert len(alerts) == 1
    assert alerts[0]["status"] == "open"


# --------------------------------------------------------------------- #
# A 'no' homonym/namesake that disappears does NOT generate an alert nor count as a Victory
# (H5)
# --------------------------------------------------------------------- #
def test_homonym_no_disappearing_does_not_alert_or_count_as_victory(tmp_path):
    db = _db(tmp_path)
    url = "https://scam.example/homonym-gone"
    filler = "https://neutral.example/filler3"

    _run(db, "r1", [_result(url, 3, "negative", same_person="no"), _result(filler, _DEEP, "neutral")])
    update_url_lifecycle(db, "r1")

    for i in range(2, 2 + GONE_AFTER_RUNS_DEFAULT):
        _miss(db, f"r{i}", filler)

    row = _lifecycle_row(db, url)
    assert row["status"] == "gone"  # the lifecycle KEEPS tracking it
    assert row["same_person"] == "no"

    assert db.list_events() == []
    assert _url_gone_alerts(db) == []
    assert db.list_victories(days=90) == []  # H5: also doesn't count as a victory


# --------------------------------------------------------------------- #
# H6: same_person/domain are resolved by url_hash, not by exact URL
# text -- two equivalent spellings of the same URL must converge.
# --------------------------------------------------------------------- #
def test_same_person_lookup_uses_url_hash_across_spelling_variants(tmp_path):
    db = _db(tmp_path)
    spelling_a = "https://Scam.EXAMPLE/dup?utm_source=x"
    spelling_b = "https://scam.example/dup"  # same url_hash after normalize_url
    filler = "https://neutral.example/hash-homonym-filler"
    assert url_hash(spelling_a) == url_hash(spelling_b)

    # First observation (spelling A): a human already marked 'no' (homonym).
    _run(db, "r1", [_result(spelling_a, 3, "negative", same_person="no",
                             domain="scam.example")])
    update_url_lifecycle(db, "r1")

    # Second observation, DIFFERENT spelling (B) -- the one that stays as
    # url_lifecycle.url after the upsert -- WITHOUT same_person in this row.
    _run(db, "r2", [_result(spelling_b, 5, "negative", same_person=None,
                             domain="scam.example")])
    update_url_lifecycle(db, "r2")

    for i in range(3, 3 + GONE_AFTER_RUNS_DEFAULT):
        _miss(db, f"r{i}", filler)

    # With an EXACT text lookup on url_lifecycle.url (spelling B), the
    # 'no' from spelling A wouldn't have been found -- the alert would have
    # fired anyway (bug). Via url_hash, it IS found: no
    # alert/event, and it doesn't count as a Victory.
    assert _lifecycle_row(db, spelling_b)["status"] == "gone"
    assert db.list_events() == []
    assert _url_gone_alerts(db) == []
    assert db.list_victories(days=90) == []


# --------------------------------------------------------------------- #
# Reappearance: gone -> active, missed_runs=0, gone_since=NULL, auto-resolves
# --------------------------------------------------------------------- #
def test_reappearance_resets_status_and_auto_resolves_alert(tmp_path):
    db = _db(tmp_path)
    url = "https://negative.example/reappears"
    filler = "https://neutral.example/filler4"

    _run(db, "r1", [_result(url, 4, "negative"), _result(filler, _DEEP, "neutral")])
    update_url_lifecycle(db, "r1")

    for i in range(2, 2 + GONE_AFTER_RUNS_DEFAULT):
        _miss(db, f"r{i}", filler)

    assert _lifecycle_row(db, url)["status"] == "gone"
    alert = next(a for a in _url_gone_alerts(db))
    assert alert["status"] == "open"

    # Reappears.
    reappear_run = f"r{2 + GONE_AFTER_RUNS_DEFAULT}"
    _run(db, reappear_run, [_result(url, 6, "negative"), _result(filler, _DEEP, "neutral")])
    update_url_lifecycle(db, reappear_run)

    row = _lifecycle_row(db, url)
    assert row["status"] == "active"
    assert row["missed_runs"] == 0
    assert row["gone_since"] is None

    resolved = next(a for a in _url_gone_alerts(db))
    assert resolved["status"] == "resolved"
    assert resolved["resolved_by"] == "auto"


# --------------------------------------------------------------------- #
# Initial backfill: idempotent and without alerts/events
# --------------------------------------------------------------------- #
def test_backfill_populates_from_existing_history_without_noise(tmp_path):
    db_path = str(tmp_path / "backfill.db")
    db = Database(db_path=db_path)  # url_lifecycle table freshly created, empty

    url = "https://negative.example/backfill"
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db.save_results([_result(url, 7, "negative")], run_id="r1")
    db.save_run(apify_run_id="r2", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db.save_results([_result(url, 2, "negative")], run_id="r2")

    # New connection (simulates restart) -- backfill runs in _init_db.
    db2 = Database(db_path=db_path)
    row = _lifecycle_row(db2, url)
    assert row is not None
    assert row["status"] == "active"
    assert row["missed_runs"] == 0
    assert row["best_position"] == 2  # min(7, 2)
    assert row["last_position"] == 2  # most recent observation
    assert row["classification"] == "negative"
    assert db2.list_events() == []
    assert db2.list_alerts(status="all") == []

    # Idempotent: a third boot doesn't duplicate rows or touch them.
    conn = db2._connect()
    count_before = conn.execute("SELECT COUNT(*) FROM url_lifecycle").fetchone()[0]
    conn.close()

    db3 = Database(db_path=db_path)
    conn = db3._connect()
    count_after = conn.execute("SELECT COUNT(*) FROM url_lifecycle").fetchone()[0]
    conn.close()
    assert count_after == count_before




# --------------------------------------------------------------------- #
# Victories
# --------------------------------------------------------------------- #
def test_list_victories_returns_gone_negatives_within_window(tmp_path):
    db = _db(tmp_path)
    url = "https://negative.example/victory"
    filler = "https://neutral.example/filler5"

    _run(db, "r1", [_result(url, 4, "negative"), _result(filler, _DEEP, "neutral")])
    update_url_lifecycle(db, "r1")
    for i in range(2, 2 + GONE_AFTER_RUNS_DEFAULT):
        _miss(db, f"r{i}", filler)

    victories = db.list_victories(days=90)
    assert len(victories) == 1
    assert victories[0]["url"] == url
    assert victories[0]["best_position"] == 4


def test_list_victories_excludes_gone_positives_and_old_entries(tmp_path):
    db = _db(tmp_path)
    conn = db._connect()
    conn.execute(
        "INSERT INTO url_lifecycle (url_hash, url, country, first_seen, last_seen, "
        "best_position, last_position, classification, status, gone_since, missed_runs) "
        "VALUES (?, ?, 'us', '2020-01-01T00:00:00', '2020-01-01T00:00:00', 1, 1, "
        "'negative', 'gone', '2020-01-01T00:00:00', 3)",
        (url_hash("https://old.example/x"), "https://old.example/x"),
    )
    conn.execute(
        "INSERT INTO url_lifecycle (url_hash, url, country, first_seen, last_seen, "
        "best_position, last_position, classification, status, gone_since, missed_runs) "
        "VALUES (?, ?, 'us', ?, ?, 1, 1, 'positive', 'gone', ?, 3)",
        (url_hash("https://positive.example/x"), "https://positive.example/x",
         _recent_iso(), _recent_iso(), _recent_iso()),
    )
    conn.commit()
    conn.close()

    assert db.list_victories(days=90) == []


def test_list_victories_respects_country_filter(tmp_path):
    """H8: /analysis passes its selected country -- list_victories respects it."""
    db = _db(tmp_path)
    conn = db._connect()
    conn.execute(
        "INSERT INTO url_lifecycle (url_hash, url, country, first_seen, last_seen, "
        "best_position, last_position, classification, status, gone_since, missed_runs) "
        "VALUES (?, ?, 'ca', ?, ?, 1, 1, 'negative', 'gone', ?, 3)",
        (url_hash("https://ca-only.example/x"), "https://ca-only.example/x",
         _recent_iso(), _recent_iso(), _recent_iso()),
    )
    conn.commit()
    conn.close()

    assert db.list_victories(days=90, country="us") == []
    assert len(db.list_victories(days=90, country="ca")) == 1
    assert len(db.list_victories(days=90)) == 1  # without a filter, it sees them all


# --------------------------------------------------------------------- #
# Smoke: the Overview (formerly /analysis) shows Wins, with a real row
# --------------------------------------------------------------------- #
def test_analysis_page_shows_victorias_section_with_a_real_row(client, seeded_db):
    gone_since = _recent_iso(2)
    conn = seeded_db._connect()
    conn.execute(
        "INSERT INTO url_lifecycle (url_hash, url, country, first_seen, last_seen, "
        "best_position, last_position, classification, status, gone_since, missed_runs) "
        "VALUES (?, ?, 'us', ?, ?, 7, 7, 'negative', 'gone', ?, 3)",
        (url_hash("https://victorious.example/x"), "https://victorious.example/x",
         _recent_iso(10), _recent_iso(10), gone_since),
    )
    conn.commit()
    conn.close()

    response = client.get("/?country=us")
    assert response.status_code == 200
    assert "<h2>Wins</h2>" in response.text
    assert "victorious.example" in response.text
    assert "gone after" in response.text
