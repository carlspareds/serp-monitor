"""Tests for item 4.1: time series aggregated by day
(Database.series_stats) and the N+1 fix for _filtered_run_counts (batch
vs per-row).

TDD, no network: direct Database against a temporary SQLite (tmp_path),
same discipline as tests/test_diff.py and tests/test_lifecycle.py. Dates
are set relative to "now" (not fixed 2020 dates) because
Database.series_stats() filters by a range relative to today (same as
the /analysis router will do, see tests/test_analysis.py) -- a fixed
date from years ago would always fall outside any 30/90/180/365-day range.
"""
from conftest import _iso_day
from conftest import make_db as _db


def _result(url, domain, position, classification, country_code="us", same_person=None):
    r = {
        "query": "q", "url": url, "title": f"title {domain}", "description": "d",
        "displayed_url": domain, "position": position, "country_code": country_code,
        "domain": domain, "classification": classification,
    }
    if same_person is not None:
        r["same_person"] = same_person
    return r


def _set_scraped_day(db, run_id, day) -> None:
    conn = db._connect()
    conn.execute("UPDATE search_results SET scraped_at = ? WHERE run_id = ?",
                (f"{day}T00:00:00", run_id))
    conn.commit()
    conn.close()


def _seed_run(db, run_id, day, country, results) -> None:
    db.save_run(apify_run_id=run_id, queries=["q"], country_code=country, status="completed",
                summary={"total": len(results), "positive": 0, "negative": 0, "neutral": 0})
    db.save_results(results, run_id=run_id)
    _set_scraped_day(db, run_id, day)


# --------------------------------------------------------------------- #
# series_stats
# --------------------------------------------------------------------- #
def test_series_stats_percentages_for_a_single_day(tmp_path):
    db = _db(tmp_path)
    day = _iso_day(1)
    _seed_run(db, "r1", day, "us", [
        _result("https://a.example/1", "a.example", 1, "positive"),
        _result("https://b.example/1", "b.example", 2, "positive"),
        _result("https://c.example/1", "c.example", 3, "negative"),
        _result("https://d.example/1", "d.example", 4, "neutral"),
    ])

    series = db.series_stats(country="us")
    assert len(series) == 1
    row = series[0]
    assert row["day"] == day
    assert row["total"] == 4
    assert row["positive"] == 2
    assert row["negative"] == 1
    assert row["neutral"] == 1
    assert row["pct_positive"] == 50.0
    assert row["pct_negative"] == 25.0
    assert row["pct_neutral"] == 25.0


def test_series_stats_excludes_same_person_no(tmp_path):
    """Rule 2.3: obvious namesakes (same_person='no') don't count in any
    client-facing metric -- the series don't either."""
    db = _db(tmp_path)
    day = _iso_day(1)
    _seed_run(db, "r1", day, "us", [
        _result("https://a.example/1", "a.example", 1, "positive"),
        _result("https://homonym.example/1", "homonym.example", 2, "negative", same_person="no"),
    ])

    row = db.series_stats(country="us")[0]
    assert row["total"] == 1
    assert row["positive"] == 1
    assert row["negative"] == 0


def test_series_stats_global_aggregates_all_countries(tmp_path):
    db = _db(tmp_path)
    day = _iso_day(1)
    _seed_run(db, "r-us", day, "us", [_result("https://a.example/1", "a.example", 1, "positive", "us")])
    _seed_run(db, "r-ca", day, "ca", [_result("https://b.example/1", "b.example", 1, "negative", "ca")])

    global_row = db.series_stats()[0]
    assert global_row["total"] == 2
    assert global_row["positive"] == 1
    assert global_row["negative"] == 1

    us_row = db.series_stats(country="us")[0]
    assert us_row["total"] == 1
    assert us_row["positive"] == 1


def test_series_stats_aggregates_two_runs_same_day(tmp_path):
    """If a day has several runs for the same country, they get aggregated (brief 4.1)."""
    db = _db(tmp_path)
    day = _iso_day(1)
    _seed_run(db, "r1", day, "us", [_result("https://a.example/1", "a.example", 1, "positive")])
    _seed_run(db, "r2", day, "us", [_result("https://b.example/1", "b.example", 2, "negative")])

    series = db.series_stats(country="us")
    assert len(series) == 1
    assert series[0]["total"] == 2
    assert series[0]["positive"] == 1
    assert series[0]["negative"] == 1


def test_series_stats_respects_date_range(tmp_path):
    db = _db(tmp_path)
    old_day = _iso_day(120)
    recent_day = _iso_day(5)
    _seed_run(db, "r-old", old_day, "us", [_result("https://a.example/1", "a.example", 1, "positive")])
    _seed_run(db, "r-recent", recent_day, "us", [_result("https://b.example/1", "b.example", 1, "negative")])

    date_from = _iso_day(90)
    series = db.series_stats(country="us", date_from=date_from)
    days = [r["day"] for r in series]
    assert recent_day in days
    assert old_day not in days


def test_series_stats_neg_avg_position_and_neg_urls(tmp_path):
    db = _db(tmp_path)
    day = _iso_day(1)
    _seed_run(db, "r1", day, "us", [
        _result("https://a.example/1", "a.example", 3, "negative"),
        _result("https://b.example/1", "b.example", 7, "negative"),
        _result("https://c.example/1", "c.example", 1, "positive"),
    ])

    row = db.series_stats(country="us")[0]
    assert row["neg_avg_position"] == 5.0
    assert row["neg_urls"] == 2


def test_series_stats_neg_avg_position_none_without_negatives(tmp_path):
    db = _db(tmp_path)
    day = _iso_day(1)
    _seed_run(db, "r1", day, "us", [_result("https://a.example/1", "a.example", 1, "positive")])

    row = db.series_stats(country="us")[0]
    assert row["neg_avg_position"] is None
    assert row["neg_urls"] == 0


def test_series_stats_empty_range_returns_empty_list(tmp_path):
    db = _db(tmp_path)
    assert db.series_stats(country="us") == []


# --------------------------------------------------------------------- #
# N+1 fix: _filtered_run_counts_batch == per-row _filtered_run_counts
# --------------------------------------------------------------------- #
def test_filtered_run_counts_batch_matches_per_row_with_homonyms(tmp_path):
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 2, "positive": 1, "negative": 1, "neutral": 0})
    db.save_results([
        _result("https://a.example/1", "a.example", 1, "positive"),
        _result("https://h.example/1", "h.example", 2, "negative", same_person="no"),
    ], run_id="r1")
    db.save_run(apify_run_id="r2", queries=["q"], country_code="ca", status="completed",
                summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    db.save_results([
        _result("https://n.example/1", "n.example", 1, "neutral"),
    ], run_id="r2")

    conn = db._connect()
    cur = conn.cursor()
    run_ids = ["r1", "r2"]
    batch = db._filtered_run_counts_batch(cur, run_ids)
    per_row = {rid: db._filtered_run_counts(cur, rid) for rid in run_ids}
    conn.close()

    assert batch == per_row


def test_filtered_run_counts_batch_empty_run_for_missing_results(tmp_path):
    """A run with no search_results (or requested but nonexistent) must not
    be missing from the output dict -- it must appear with zeros, same as
    the per-row _filtered_run_counts."""
    db = _db(tmp_path)
    db.save_run(apify_run_id="empty-run", queries=["q"], country_code="us", status="completed",
                summary={"total": 0, "positive": 0, "negative": 0, "neutral": 0})

    conn = db._connect()
    cur = conn.cursor()
    batch = db._filtered_run_counts_batch(cur, ["empty-run"])
    per_row = db._filtered_run_counts(cur, "empty-run")
    conn.close()

    assert batch["empty-run"] == per_row == {
        "positive": 0, "negative": 0, "neutral": 0, "total_results": 0,
    }


def test_list_runs_and_latest_run_per_country_use_batch_counts(tmp_path):
    """Doesn't verify the number of queries -- only that the final result
    (counts filtered without same_person='no') stays correct after
    switching list_runs/latest_run_per_country to the batch version."""
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 2, "positive": 1, "negative": 1, "neutral": 0})
    db.save_results([
        _result("https://a.example/1", "a.example", 1, "positive"),
        _result("https://h.example/1", "h.example", 2, "negative", same_person="no"),
    ], run_id="r1")

    runs = db.list_runs()
    assert runs[0]["negative"] == 0  # the namesake is excluded
    assert runs[0]["total_results"] == 1

    latest = db.latest_run_per_country()
    assert latest[0]["negative"] == 0
    assert latest[0]["total_results"] == 1


def test_series_null_severity_matches_alerts_canon():
    """_SERIES_NULL_SEVERITY (database.base, duplicated to avoid an import
    cycle -- see the comment in that module) must keep matching
    processing.alerts.NULL_SEVERITY, the canonical source."""
    from database.models import _SERIES_NULL_SEVERITY
    from processing.alerts import NULL_SEVERITY
    assert _SERIES_NULL_SEVERITY == NULL_SEVERITY


def test_series_stats_severity_sum_uses_null_severity_fallback(tmp_path):
    from processing.alerts import NULL_SEVERITY
    db = _db(tmp_path)
    _seed_run(db, "r1", _iso_day(1), "us", [
        _result("https://a.example/1", "a.example", 1, "negative"),
        _result("https://b.example/1", "b.example", 2, "negative"),
        _result("https://c.example/1", "c.example", 3, "positive"),
    ])
    # a.example has explicit severity=5; b.example stays NULL -> counts
    # as NULL_SEVERITY (3) in the sum.
    conn = db._connect()
    conn.execute("UPDATE search_results SET severity = 5 WHERE domain = 'a.example'")
    conn.commit()
    conn.close()

    row = db.series_stats(country="us")[0]
    assert row["severity_sum"] == 5 + NULL_SEVERITY
