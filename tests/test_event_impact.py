"""Tests for item 4.5: timeline correlated with the SERP.

Acceptance criteria (§4.5): "An event with a
date within the historical range shows correct deltas in all three windows;
the analysis chart annotates it" (the annotation on /analysis is already
covered by tests/test_analysis.py::test_analysis_page_event_in_range_appears_in_
visible_list_and_json, from item 4.1 -- not duplicated here).

Two levels:
  - Unit, with a fully controlled `series_stats` stub (exact dates and
    values, without going through SQL) -- isolates the arithmetic of
    processing.event_impact.event_impact_from_rows() from the real
    aggregation (rows_for_events() + event_impact_from_rows(), F3-close top-5 #2:
    the convenience wrapper event_impact() was removed -- only used by
    these tests, no production caller; see impacts_at() for the real
    "several events, one window" case).
  - Integration, with a real Database (make_db) seeded with
    search_results on specific dates -- exercises the literal acceptance
    criteria end to end (real series_stats -> event_impact_from_rows).

TDD, no network.
"""
from datetime import date, timedelta

import pytest
from conftest import make_db as _db

from processing.event_impact import event_impact_from_rows, rows_for_events


class _StubDB:
    """Stand-in for Database to isolate event_impact_from_rows() from series_stats():
    stores the calls received and returns only the rows whose 'day' falls
    within [date_from, date_to] (same contract as Database.series_
    stats, ISO string comparison -- valid because all dates are
    10 characters long)."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def series_stats(self, country="", date_from="", date_to=""):
        self.calls.append((country, date_from, date_to))
        return [r for r in self.rows if date_from <= r["day"] <= date_to]


def _row(day, pct_positive, negative, neg_avg_position):
    # `negative` here is the synthetic value of pct_negative (renamed #6)
    return {"day": day, "pct_positive": pct_positive, "pct_negative": negative,
            "neg_avg_position": neg_avg_position}


EVENT = date(2024, 6, 15)


def _constant_rows(before_val, after_val):
    """30 days before (excluding the event) + 30 days after (including
    the event) with the same constant value on each half -- so the delta
    must come out IDENTICAL in the 3 windows (7/14/30), the brief's literal
    acceptance criteria."""
    rows = []
    for n in range(1, 31):
        d = (EVENT - timedelta(days=n)).isoformat()
        rows.append(_row(d, *before_val))
    for n in range(30):
        d = (EVENT + timedelta(days=n)).isoformat()
        rows.append(_row(d, *after_val))
    return rows


# --------------------------------------------------------------------- #
# Unit (stub): arithmetic and sign semantics.
# --------------------------------------------------------------------- #
def test_event_impact_correct_delta_in_all_three_windows():
    """Literal acceptance criteria: before constant (pct_positive=40, pct_negative=5,
    neg_avg_position=3.0), after constant (50, 3, 6.0) -- the delta must
    be the same in 7d/14d/30d because the value doesn't vary within each
    half."""
    stub = _StubDB(_constant_rows((40.0, 5, 3.0), (50.0, 3, 6.0)))

    by_day = rows_for_events(stub, [EVENT.isoformat()], windows=(7, 14, 30))
    result = event_impact_from_rows(by_day, EVENT.isoformat(), windows=(7, 14, 30))

    for w in (7, 14, 30):
        assert result[w]["insufficient"] is False
        assert result[w]["pct_positive"] == {"before": 40.0, "after": 50.0, "delta": pytest.approx(10.0)}


def test_event_impact_negative_count_delta_sign_is_inverted():
    """negative (count): lowering the count is an IMPROVEMENT -- the delta
    must come out POSITIVE when the count drops (5 -> 3), not negative as a
    raw after-before would give. See docstring of processing.event_impact."""
    stub = _StubDB(_constant_rows((40.0, 5, 3.0), (50.0, 3, 6.0)))

    by_day = rows_for_events(stub, [EVENT.isoformat()], windows=(7,))
    result = event_impact_from_rows(by_day, EVENT.isoformat(), windows=(7,))

    neg = result[7]["pct_negative"]
    assert neg["before"] == 5.0
    assert neg["after"] == 3.0
    assert neg["delta"] == pytest.approx(2.0)  # positive = improvement (fewer negatives)


def test_event_impact_neg_avg_position_delta_sign_is_natural_but_means_improvement():
    """neg_avg_position: WITHOUT inverting the sign (after - before), but a
    positive delta (the average position of NEGATIVES RISES in number, e.g.
    #3 -> #6) means the negative sank -- an improvement, even though the
    position number went up (the opposite of normal content)."""
    stub = _StubDB(_constant_rows((40.0, 5, 3.0), (50.0, 3, 6.0)))

    by_day = rows_for_events(stub, [EVENT.isoformat()], windows=(7,))
    result = event_impact_from_rows(by_day, EVENT.isoformat(), windows=(7,))

    pos = result[7]["neg_avg_position"]
    assert pos["before"] == 3.0
    assert pos["after"] == 6.0
    assert pos["delta"] == pytest.approx(3.0)  # positive = improvement (it sinks)


def test_event_impact_single_bounded_series_stats_call_covers_max_window():
    """Sargable (brief 4.5): a SINGLE call to series_stats, bounded to
    [event - max(windows), event + max(windows) - 1] -- not one per
    window."""
    stub = _StubDB(_constant_rows((40.0, 5, 3.0), (50.0, 3, 6.0)))

    rows_for_events(stub, [EVENT.isoformat()], windows=(7, 14, 30))

    assert len(stub.calls) == 1
    country, date_from, date_to = stub.calls[0]
    assert country == ""
    assert date_from == (EVENT - timedelta(days=30)).isoformat()
    assert date_to == (EVENT + timedelta(days=29)).isoformat()


def test_event_impact_insufficient_when_before_half_has_no_data():
    """insufficient=True if EITHER of the two halves has no day with
    data -- before empty, after with data."""
    rows = [_row((EVENT + timedelta(days=n)).isoformat(), 50.0, 3, 6.0) for n in range(10)]
    stub = _StubDB(rows)

    by_day = rows_for_events(stub, [EVENT.isoformat()], windows=(7,))
    result = event_impact_from_rows(by_day, EVENT.isoformat(), windows=(7,))

    assert result[7]["insufficient"] is True
    assert result[7]["pct_positive"] == {"before": None, "after": None, "delta": None}
    assert result[7]["pct_negative"] == {"before": None, "after": None, "delta": None}
    assert result[7]["neg_avg_position"] == {"before": None, "after": None, "delta": None}


def test_event_impact_insufficient_when_after_half_has_no_data():
    """After half empty -- e.g. an event too recent to have any
    observations after it yet (the /timeline router filters out
    'too recent' before it gets here, but event_impact_from_rows() must
    degrade just as safely if called with a future event)."""
    rows = [_row((EVENT - timedelta(days=n)).isoformat(), 40.0, 5, 3.0) for n in range(1, 10)]
    stub = _StubDB(rows)

    future_event = (EVENT + timedelta(days=365)).isoformat()
    by_day = rows_for_events(stub, [future_event], windows=(7, 14, 30))
    result = event_impact_from_rows(by_day, future_event, windows=(7, 14, 30))

    for w in (7, 14, 30):
        assert result[w]["insufficient"] is True


def test_event_impact_neg_avg_position_none_without_marking_whole_window_insufficient():
    """If one half DID have days with data but none with negatives,
    neg_avg_position comes out as None without insufficient triggering for
    pct_positive/negative (which do have a value)."""
    before = [_row((EVENT - timedelta(days=n)).isoformat(), 40.0, 0, None) for n in range(1, 8)]
    after = [_row((EVENT + timedelta(days=n)).isoformat(), 60.0, 2, 4.0) for n in range(7)]
    stub = _StubDB(before + after)

    by_day = rows_for_events(stub, [EVENT.isoformat()], windows=(7,))
    result = event_impact_from_rows(by_day, EVENT.isoformat(), windows=(7,))

    assert result[7]["insufficient"] is False
    assert result[7]["pct_positive"]["delta"] == pytest.approx(20.0)
    # before had no negatives (avg of empty list = None); after did (4.0).
    # One side being None is enough for THIS metric's delta to be None,
    # without dragging insufficient along (which is already False, see above).
    assert result[7]["neg_avg_position"] == {"before": None, "after": 4.0, "delta": None}


def test_event_impact_accepts_datetime_style_event_date_string():
    """event_date can arrive with a time component (e.g. 'YYYY-MM-DDTHH:MM:SS'
    from a legacy row) -- only the first 10 characters are used."""
    stub = _StubDB(_constant_rows((40.0, 5, 3.0), (50.0, 3, 6.0)))

    event_date = EVENT.isoformat() + "T10:30:00"
    by_day = rows_for_events(stub, [event_date], windows=(7,))
    result = event_impact_from_rows(by_day, event_date, windows=(7,))

    assert result[7]["insufficient"] is False


# --------------------------------------------------------------------- #
# Integration: real Database (make_db) + real series_stats.
# --------------------------------------------------------------------- #
def _seed_result(url, domain, position, classification):
    return {"query": "q", "url": url, "title": f"t {domain}", "description": "d",
            "displayed_url": domain, "position": position, "country_code": "us",
            "domain": domain, "classification": classification}


def test_event_impact_end_to_end_with_real_database(tmp_path):
    """Literal end-to-end acceptance criteria: an event with a date within
    the real historical range (real Database.series_stats, no stub) shows
    correct deltas in the 3 windows.

    before_day (5 days before the event): 1 positive + 2 negatives (pos 4
    and 6) -> pct_positive=33.3, pct_negative=66.7, neg_avg_position=5.0.
    after_day (the EVENT DAY -- included in the 'after' window, see
    the module docstring): 3 positives + 1 negative (pos 10) ->
    pct_positive=75.0, pct_negative=25.0, neg_avg_position=10.0.
    Only one day is populated in each half -> same result in 7/14/30d.
    """
    db = _db(tmp_path)
    event_date = "2024-03-15"
    before_day = "2024-03-10"

    db.save_run(apify_run_id="r-before", queries=["q"], country_code="us", status="completed",
                summary={"total": 3, "positive": 1, "negative": 2, "neutral": 0})
    db.save_results([
        _seed_result("https://a.example/1", "a.example", 1, "positive"),
        _seed_result("https://b.example/1", "b.example", 4, "negative"),
        _seed_result("https://c.example/1", "c.example", 6, "negative"),
    ], run_id="r-before")

    db.save_run(apify_run_id="r-after", queries=["q"], country_code="us", status="completed",
                summary={"total": 4, "positive": 3, "negative": 1, "neutral": 0})
    db.save_results([
        _seed_result("https://d.example/1", "d.example", 1, "positive"),
        _seed_result("https://e.example/1", "e.example", 2, "positive"),
        _seed_result("https://f.example/1", "f.example", 3, "positive"),
        _seed_result("https://g.example/1", "g.example", 10, "negative"),
    ], run_id="r-after")

    conn = db._connect()
    conn.execute("UPDATE search_results SET scraped_at = ? WHERE run_id = 'r-before'",
                (f"{before_day}T00:00:00",))
    conn.execute("UPDATE search_results SET scraped_at = ? WHERE run_id = 'r-after'",
                (f"{event_date}T00:00:00",))
    conn.commit()
    conn.close()

    by_day = rows_for_events(db, [event_date], windows=(7, 14, 30))
    result = event_impact_from_rows(by_day, event_date, windows=(7, 14, 30))

    for w in (7, 14, 30):
        assert result[w]["insufficient"] is False
        assert result[w]["pct_positive"]["before"] == pytest.approx(33.3)
        assert result[w]["pct_positive"]["after"] == pytest.approx(75.0)
        assert result[w]["pct_positive"]["delta"] == pytest.approx(41.7)
        assert result[w]["pct_negative"]["before"] == pytest.approx(66.7)
        assert result[w]["pct_negative"]["after"] == pytest.approx(25.0)
        assert result[w]["pct_negative"]["delta"] == pytest.approx(41.7)
        assert result[w]["neg_avg_position"]["before"] == pytest.approx(5.0)
        assert result[w]["neg_avg_position"]["after"] == pytest.approx(10.0)
        assert result[w]["neg_avg_position"]["delta"] == pytest.approx(5.0)


# --------------------------------------------------------------------- #
# Migration (events.expected_effect column) + CRUD.
# --------------------------------------------------------------------- #
def test_migration_adds_expected_effect_column(tmp_path):
    db = _db(tmp_path)
    conn = db._connect()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    conn.close()
    assert "expected_effect" in cols


def test_add_event_persists_expected_effect(tmp_path):
    db = _db(tmp_path)
    db.add_event("2024-03-15", "Press release published", kind="manual",
                 expected_effect="Expect % positive to rise within 2 weeks")

    events = db.list_events()
    assert events[0]["expected_effect"] == "Expect % positive to rise within 2 weeks"


def test_add_event_without_expected_effect_defaults_to_empty_string(tmp_path):
    db = _db(tmp_path)
    db.add_event("2024-03-15", "No expectation set")

    events = db.list_events()
    assert events[0]["expected_effect"] == ""
