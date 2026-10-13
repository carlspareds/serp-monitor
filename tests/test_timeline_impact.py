"""Tests for the Timeline panel (/ui/timeline, 4.5, deep dive §4.5): per-event impact deltas
+ expected_effect. Uses the TestClient (fixture `client`, conftest.py), a
temporary DB seeded by `seeded_db` (5 TODAY search_results, see
conftest.SAMPLE_RESULTS).

event_impact_from_rows()'s arithmetic is already covered thoroughly in
tests/test_event_impact.py -- this only covers the form's round-trip
(expected_effect), rendering deltas/"too recent" in /timeline, and the
cap on events that pay for the impact calculation (_IMPACT_CAP).
"""
import re
from datetime import UTC, datetime
from urllib.parse import unquote

from conftest import _iso_day

_FRAG = {"X-SM-Fragment": "1"}


def _add(client, **data):
    """POST from the panel (fetch): the answer is the refreshed panel."""
    return client.post("/timeline/add", data=data, headers=_FRAG)


def _panel(client):
    return client.get("/ui/timeline", headers=_FRAG)


def _result(url, domain, position, classification):
    return {"query": "q", "url": url, "title": f"t {domain}", "description": "d",
            "displayed_url": domain, "position": position, "country_code": "us",
            "domain": domain, "classification": classification}


def test_timeline_add_persists_and_shows_expected_effect(client, seeded_db):
    response = _add(client, event_date=_iso_day(0), description="Press release published", expected_effect="Expect % positive to rise")
    assert response.status_code == 200
    assert "Press release published" in response.text
    assert "Expected: Expect % positive to rise" in response.text


def test_timeline_add_without_expected_effect_omits_expected_line(client, seeded_db):
    response = _add(client, event_date=_iso_day(0), description="No expectation event")
    assert response.status_code == 200
    assert "No expectation event" in response.text
    assert "Expected:" not in response.text


def test_timeline_future_event_shows_too_recent(client, seeded_db):
    response = _add(client, event_date=_iso_day(-10),  # within 10 days -- future
                    description="Planned campaign launch")
    assert response.status_code == 200
    assert "Planned campaign launch" in response.text
    assert "Too recent to measure the effect yet." in response.text


def test_timeline_today_event_shows_too_recent(client, seeded_db):
    """Brief 4.5: 'future OR TODAY events' -- an event dated today also
    counts as too recent (no real 'after' window yet beyond the event's
    own day)."""
    response = _add(client, event_date=_iso_day(0), description="Today event")
    assert response.status_code == 200
    assert "Too recent to measure the effect yet." in response.text


def test_timeline_event_with_insufficient_history_shows_dash(client, seeded_db):
    """Event well in the past, with no data BEFORE it (seeded_db only
    carries TODAY's data) -- the 7d window's 'before' half is empty ->
    insufficient -> '7d: —'."""
    response = _add(client, event_date=_iso_day(3), description="Old event without prior history")
    assert response.status_code == 200
    assert "Not enough search data around this date" in response.text


def test_timeline_event_with_before_and_after_data_shows_real_deltas(client, seeded_db):
    """Event with real data before AND after (before: extra run 2 days
    before the event; after: the 5 TODAY-seeded results, within the
    7-day 'after' window) -- must show a real numeric delta, not '—'."""
    event_date = _iso_day(3)
    before_date = _iso_day(5)

    seeded_db.save_run(apify_run_id="r-before", queries=["q"], country_code="us", status="completed",
                       summary={"total": 2, "positive": 1, "negative": 1, "neutral": 0})
    seeded_db.save_results([
        _result("https://before-a.example/1", "before-a.example", 1, "positive"),
        _result("https://before-b.example/1", "before-b.example", 5, "negative"),
    ], run_id="r-before")
    conn = seeded_db._connect()
    conn.execute("UPDATE search_results SET scraped_at = ? WHERE run_id = 'r-before'",
                (f"{before_date}T00:00:00",))
    conn.commit()
    conn.close()

    response = _add(client, event_date=event_date, description="Event with real before/after data")
    assert response.status_code == 200
    assert "Not enough search data" not in response.text
    assert re.search(r"later: positive share [+\u2212±]\d+ points, negative share [+\u2212±]\d+ points",
                     response.text)


def test_timeline_impact_computed_only_for_capped_number_of_events(client, seeded_db):
    """_IMPACT_CAP (H, ponytail documented in timeline.py): with more
    events than the cap, only the _IMPACT_CAP most recent ones
    calculate/show their delta block -- the rest of the row still shows
    (description, delete button), just without the '7d:/14d:/30d:'
    block."""
    # Lazy import (not at module level): dashboard.routers.timeline
    # imports from dashboard.main, which in turn registers this router --
    # a module-level import here would trigger the same import cycle
    # conftest._reload_app resolves by reordering; the `client` fixture
    # already took care of that before this test runs.
    from dashboard.routers.timeline import _IMPACT_CAP

    n_events = _IMPACT_CAP + 1
    for i in range(n_events):
        # Different dates well in the past (no data around them ->
        # insufficient, but they DO get calculated/rendered for the first
        # _IMPACT_CAP): day 100, 101, 102... back, none today/future.
        seeded_db.add_event(_iso_day(100 + i), f"Bulk event {i}", kind="manual")

    response = _panel(client)
    assert response.status_code == 200
    # Every event with a computed impact (but no real before/after) says so
    # -- exactly _IMPACT_CAP occurrences, not n_events.
    assert response.text.count("Not enough search data") == _IMPACT_CAP
    # The n_events still appear in the table (description visible).
    for i in range(n_events):
        assert f"Bulk event {i}" in response.text


def test_timeline_add_rejects_malformed_date_without_writing(client, seeded_db):
    """Review 4.5 #1: a garbage date via POST must not enter events
    (it used to leave /timeline and build_pdf broken)."""
    before = len(seeded_db.list_events())
    response = _add(client, event_date="1999-99-99", description="bad date")
    assert response.status_code == 400 and "pick a valid date" in response.text
    assert len(seeded_db.list_events()) == before


def test_timeline_survives_preexisting_malformed_event_date(client, seeded_db):
    """Review 4.5 #1: a garbage row ALREADY in the DB (direct insert,
    future bug) degrades to insufficient -- never a 500."""
    conn = seeded_db._connect()
    conn.execute(
        "INSERT INTO events (event_date, kind, description, created_at) "
        "VALUES ('1999-99-99', 'manual', 'legacy bad row', '2026-01-01T00:00:00')")
    conn.commit()
    conn.close()
    response = _panel(client)
    assert response.status_code == 200
    assert "legacy bad row" in response.text


def test_timeline_url_gone_events_do_not_consume_impact_slots(client, seeded_db):
    """Review 4.5 #2: today's automatic url_gone events do NOT spend cap
    slots -- a run with many disappeared negatives must not turn off the
    manual events' deltas."""
    from dashboard.routers.timeline import _IMPACT_CAP

    today = datetime.now(UTC).replace(tzinfo=None).date().isoformat()
    for i in range(_IMPACT_CAP + 5):
        seeded_db.add_event(today, f"gone {i}", kind="url_gone")
    seeded_db.add_event(_iso_day(60), "Manual action still gets impact", kind="manual")

    response = _panel(client)
    assert response.status_code == 200
    # The manual event still gets its impact line (insufficient here)
    assert "Not enough search data" in response.text
    # Detected events use plain wording and can't be deleted
    assert "top-N" not in response.text


def test_timeline_partial_after_coverage_is_annotated(client, seeded_db):
    """Review 4.5 #4: a window whose after segment crosses TODAY shows the
    real coverage '(n/30d)' -- 3 days of after isn't 30-day confirmation.
    seeded_db carries TODAY's data (after=1 day with data); before is
    seeded 5 days back (same pattern as the real-deltas test)."""
    seeded_db.save_run(apify_run_id="r-cov", queries=["q"], country_code="us", status="completed",
                       summary={"total": 1, "positive": 1, "negative": 0, "neutral": 0})
    seeded_db.save_results([
        _result("https://cov.example/1", "cov.example", 1, "positive"),
    ], run_id="r-cov")
    conn = seeded_db._connect()
    conn.execute("UPDATE search_results SET scraped_at = ? WHERE run_id = 'r-cov'",
                (f"{_iso_day(5)}T00:00:00",))
    conn.commit()
    conn.close()

    response = _add(client, event_date=_iso_day(3), description="Recent action")
    assert response.status_code == 200
    assert re.search(r"Based on \d+ of \d+ days so far", response.text)


def test_timeline_panel_lists_user_and_detected_events_in_plain_words(client, seeded_db):
    seeded_db.add_event(_iso_day(2), "Negative URL gone from top-N in us: scam.example", kind="url_gone")
    seeded_db.add_event(_iso_day(4), "Press release", kind="manual")
    text = _panel(client).text
    assert "<html" not in text and "Log an event" in text
    assert "Negative result scam.example dropped out of the results (US)" in text
    assert "top-N" not in text
    assert text.count("action='/timeline/delete'") == 1        # only the user event
    assert "your event" in text and "detected" in text
    assert "data-sm-confirm=" in text                           # confirm dialog, not confirm()


def test_timeline_add_and_delete_answer_with_a_toast(client, seeded_db):
    resp = _add(client, event_date=_iso_day(1), description="Removal request sent")
    assert unquote(resp.headers["X-SM-Toast"]) == "Event logged."
    eid = seeded_db.list_events()[0]["id"]
    resp = client.post("/timeline/delete", data={"event_id": str(eid)}, headers=_FRAG)
    assert unquote(resp.headers["X-SM-Toast"]) == "Event deleted."
    assert seeded_db.list_events() == []
    plain = client.post("/timeline/add", data={"event_date": _iso_day(1), "description": "x"},
                        follow_redirects=False)
    assert plain.status_code == 303 and plain.headers["location"] == "/results?panel=/ui/timeline"


def test_old_timeline_page_redirects_to_the_panel(client):
    resp = client.get("/timeline", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/results?panel=/ui/timeline"


def test_show_on_timeline_filters_detected_events_by_domain(client, seeded_db):
    seeded_db.add_event(_iso_day(2), "Negative URL gone from top-N in us: scam.example", kind="url_gone")
    seeded_db.add_event(_iso_day(2), "Negative URL gone from top-N in us: other.example", kind="url_gone")
    text = client.get("/ui/timeline", params={"domain": "scam.example"}, headers=_FRAG).text
    assert "scam.example dropped out" in text and "other.example" not in text
