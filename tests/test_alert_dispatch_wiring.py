"""FIX 1 (silent-alerts audit): "the system is mute" -- 14 sites
created alerts with `db.add_alert(...)` and NEVER called notify_alert.
These tests are the positive evidence that, after migrating each producer to
`notify.dispatcher.emit_alert`, a real alert DOES trigger notify_alert
(intercepted here via `notify.dispatcher.notify_alert`, the point that
`emit_alert` calls internally -- see notify/dispatcher.py).

TDD, no network: same stubs as tests/test_monitor_checks.py,
tests/test_severity.py and tests/test_apify_costs.py -- no test in this
file touches real network.
"""
from conftest import make_db as _db

from database.models import Database
from notify import dispatcher
from processing import alerts as alerts_engine

SUMMARY_NEG = {"total": 1, "positive": 0, "negative": 1, "neutral": 0}


def _calls(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatcher, "notify_alert", lambda db_arg, row: calls.append(row))
    return calls


def _neg(url: str, **overrides) -> dict:
    """Same minimal shape as tests/test_severity.py::_neg -- save_results
    requires country_code/query/displayed_url/etc."""
    base = {
        "query": "q", "url": url, "title": "t", "description": "d",
        "displayed_url": url, "position": 1, "country_code": "us",
        "domain": "neg.example", "classification": "negative",
    }
    base.update(overrides)
    return base


def _row_id(db: Database, url: str) -> int:
    conn = db._connect()
    row = conn.execute("SELECT id FROM search_results WHERE url = ?", (url,)).fetchone()
    conn.close()
    return row["id"]


# --------------------------------------------------------------------- #
# processing/alerts.py: threshold, negative_new, gsc_position
# --------------------------------------------------------------------- #
def test_threshold_alert_dispatches(tmp_path, monkeypatch):
    db = _db(tmp_path)
    calls = _calls(monkeypatch)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary=SUMMARY_NEG)
    db.save_results([_neg("https://neg.example/a")], run_id="r1")
    db.set_result_severity(_row_id(db, "https://neg.example/a"), 5, "legal")

    msgs = alerts_engine.generate_alerts(db)

    assert len(msgs) == 1
    assert len(calls) == 1
    assert calls[0]["kind"] == "threshold"
    # FIX 1 point 5: legacy "high" removed -- the catalog decides "warning".
    assert calls[0]["severity"] == "warning"
    persisted = db.list_alerts(limit=1)[0]
    assert persisted["severity"] == calls[0]["severity"]


def test_negative_new_alert_dispatches(tmp_path, monkeypatch):
    db = _db(tmp_path)
    db.save_run(apify_run_id="r0", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 1, "negative": 0, "neutral": 0})
    db.save_results([_neg("https://pos.example/a", domain="pos.example",
                          classification="positive")], run_id="r0")
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary=SUMMARY_NEG)
    db.save_results([_neg("https://neg.example/a")], run_id="r1")
    # generate_alerts only detects "new negative" by comparing 2 different
    # DATES (dates_for_country).
    conn = db._connect()
    conn.execute("UPDATE search_results SET scraped_at = ? WHERE run_id = 'r0'",
                 ("2026-01-01T00:00:00",))
    conn.execute("UPDATE search_results SET scraped_at = ? WHERE run_id = 'r1'",
                 ("2026-01-02T00:00:00",))
    conn.commit()
    conn.close()
    calls = _calls(monkeypatch)

    alerts_engine.generate_alerts(db)

    negative_calls = [c for c in calls if c["kind"] == "negative_new"]
    assert len(negative_calls) == 1
    assert negative_calls[0]["severity"] == "warning"


# --------------------------------------------------------------------- #
# database/models.py::_check_apify_budget -- special case (cannot
# import notify.dispatcher, cycle) -- the CALLER dispatches (FIX 1 point 4).
# --------------------------------------------------------------------- #
def test_apify_budget_alert_dispatches_via_save_run_caller(tmp_path, monkeypatch):
    """save_run returns the alert row -- the caller (here, the test itself
    simulates report.multi_country.persist_engine_run) must dispatch it."""
    db = _db(tmp_path)
    db.set_setting("apify_monthly_budget", "1.00")
    calls = _calls(monkeypatch)

    budget_alert = db.save_run("run-1", ["q"], "us", "completed",
                                {"total": 1, "positive": 0, "negative": 0, "neutral": 1},
                                cost_usd=5.0)

    assert budget_alert is not None
    assert budget_alert["kind"] == "apify_budget"
    assert budget_alert["severity"] == "critical"  # legacy 'high', untouched (outside the FIX's 4)
    assert calls == []  # save_run does NOT dispatch itself -- the caller does

    dispatcher.notify_alert(db, budget_alert)
    assert len(calls) == 1
    assert calls[0] == budget_alert


def test_apify_budget_alert_dispatches_end_to_end_via_persist_engine_run(tmp_path, monkeypatch):
    from report import multi_country

    db = _db(tmp_path)
    db.set_setting("apify_monthly_budget", "0.01")
    calls = []
    # report.multi_country does `from notify.dispatcher import notify_alert`
    # -- its own reference, captured at import time -- so the stub goes
    # on THE MODULE that uses it, not on notify.dispatcher (monkeypatching
    # there wouldn't intercept it).
    monkeypatch.setattr(multi_country, "notify_alert", lambda db_arg, row: calls.append(row))

    engine_run = {
        "run_id": "run-x", "queries": ["q"], "country_code": "us", "status": "completed",
        "results": [], "cost_usd": 5.0, "compute_units": 1.0, "retries": 0,
        "engine": "google", "device": "desktop",
    }
    multi_country.persist_engine_run(db, engine_run)

    budget_calls = [c for c in calls if c["kind"] == "apify_budget"]
    assert len(budget_calls) == 1


def test_apify_budget_under_budget_does_not_dispatch(tmp_path, monkeypatch):
    db = _db(tmp_path)
    db.set_setting("apify_monthly_budget", "100.00")
    calls = _calls(monkeypatch)

    budget_alert = db.save_run("run-1", ["q"], "us", "completed",
                                {"total": 1, "positive": 0, "negative": 0, "neutral": 1},
                                cost_usd=1.0)

    assert budget_alert is None
    assert calls == []

