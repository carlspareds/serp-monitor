"""Tests for item 7.2: alert lifecycle (ack/assign/snooze/
auto-resolution).

Acceptance from deep dive §7.2: "Renewing a certificate auto-resolves its
alert with resolved_by=auto; a snoozed alert does not notify nor count in
the badge until its date."

TDD, no network: monitors with network output (ssl/wordpress/mail) are
stubbed at the function level, same as in 6.6/9.1 -- never raw sqlite3
except to simulate a pre-existing legacy DB (same pattern as
test_alert_routing.py::test_legacy_severities_are_migrated_on_reopen).
"""
import sqlite3
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

from conftest import make_db as _db

from database.models import Database


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# --------------------------------------------------------------------- #
# Migration: new columns + default status
# --------------------------------------------------------------------- #
def test_alerts_table_has_lifecycle_columns(tmp_path):
    db = _db(tmp_path)
    conn = db._connect()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(alerts)").fetchall()}
    conn.close()
    assert {"status", "assignee", "snoozed_until", "resolved_at", "resolved_by"} <= cols


def test_new_alert_defaults_to_open_status(tmp_path):
    db = _db(tmp_path)
    db.add_alert("threshold", "warning", "US", "msg", "dk1")

    row = db.list_alerts(status="all")[0]
    assert row["status"] == "open"
    assert row["assignee"] is None
    assert row["resolved_at"] is None
    assert row["resolved_by"] is None


def test_preexisting_rows_without_status_are_migrated_to_open_on_reopen(tmp_path):
    """Simulates a 7.1 DB (without the 7.2 columns): a row inserted before
    the migration must fall back to status='open' when the DB is reopened."""
    db_path = str(tmp_path / "legacy.db")
    Database(db_path=db_path)  # creates the schema (already with the 7.2 columns)
    conn = sqlite3.connect(db_path)
    # Forces explicit NULL, as a row created by a version prior to the
    # migration would look (the DEFAULT only covers new inserts).
    conn.execute(
        "INSERT INTO alerts (created_at, kind, severity, country, message, dedupe_key, status) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL)",
        ("2026-01-01T00:00:00", "threshold", "warning", "US", "legacy row", "legacydk"),
    )
    conn.commit()
    conn.close()

    reopened = Database(db_path=db_path)
    row = reopened.list_alerts(status="all")[0]
    assert row["status"] == "open"


# --------------------------------------------------------------------- #
# ack / snooze / resolve change the expected fields
# --------------------------------------------------------------------- #
def test_ack_alert_sets_status_and_assignee(tmp_path):
    db = _db(tmp_path)
    alert_id = db.add_alert("threshold", "warning", "US", "msg", "dk1")

    db.ack_alert(alert_id, assignee="alice")

    row = db.list_alerts(status="all")[0]
    assert row["status"] == "acknowledged"
    assert row["assignee"] == "alice"


def test_snooze_alert_sets_snoozed_until_without_changing_status(tmp_path):
    db = _db(tmp_path)
    alert_id = db.add_alert("threshold", "warning", "US", "msg", "dk1")
    until = _iso(_now() + timedelta(days=1))

    db.snooze_alert(alert_id, until)

    row = db.list_alerts(status="all")[0]
    assert row["snoozed_until"] == until
    assert row["status"] == "open"  # snooze does not touch the status


def test_resolve_alert_sets_resolved_fields(tmp_path):
    db = _db(tmp_path)
    alert_id = db.add_alert("threshold", "warning", "US", "msg", "dk1")

    db.resolve_alert(alert_id, by="human")

    row = db.list_alerts(status="all")[0]
    assert row["status"] == "resolved"
    assert row["resolved_by"] == "human"
    assert row["resolved_at"] is not None


# --------------------------------------------------------------------- #
# Cycle-aware dedupe: resolved frees the key, open does not duplicate
# --------------------------------------------------------------------- #
def test_open_condition_does_not_duplicate(tmp_path):
    db = _db(tmp_path)
    first = db.add_alert("threshold", "warning", "US", "msg", "dk1")
    second = db.add_alert("threshold", "warning", "US", "msg (again)", "dk1")

    assert isinstance(first, int)
    assert not second
    assert len(db.list_alerts(status="all")) == 1


def test_resolved_condition_allows_new_episode(tmp_path):
    db = _db(tmp_path)
    first = db.add_alert("threshold", "warning", "US", "first episode", "dk1")
    db.resolve_alert(first, by="human")

    second = db.add_alert("threshold", "warning", "US", "second episode", "dk1")

    assert isinstance(second, int)
    assert second != first
    all_rows = db.list_alerts(status="all")
    assert len(all_rows) == 2
    # The resolved row keeps its history with the mutated key (traceable),
    # the new one gets the original "clean" key back.
    by_id = {r["id"]: r for r in all_rows}
    assert by_id[first]["dedupe_key"] == f"dk1#resolved#{first}"
    assert by_id[second]["dedupe_key"] == "dk1"
    assert by_id[second]["status"] == "open"


def test_reopened_episode_can_be_resolved_and_reopen_again(tmp_path):
    """Two full cycles: each resolution mutates its own key without
    colliding with the previous one."""
    db = _db(tmp_path)
    ep1 = db.add_alert("ssl", "warning", "host.com", "e1", "dk1")
    db.resolve_alert(ep1, by="auto")
    ep2 = db.add_alert("ssl", "warning", "host.com", "e2", "dk1")
    db.resolve_alert(ep2, by="human")
    ep3 = db.add_alert("ssl", "warning", "host.com", "e3", "dk1")

    assert len({ep1, ep2, ep3}) == 3
    assert isinstance(ep3, int)
    rows = {r["id"]: r["status"] for r in db.list_alerts(status="all")}
    assert rows[ep1] == "resolved"
    assert rows[ep2] == "resolved"
    assert rows[ep3] == "open"


# --------------------------------------------------------------------- #
# open_alerts / count_active_alerts / list_alerts(status=...)
# --------------------------------------------------------------------- #
def test_open_alerts_excludes_resolved(tmp_path):
    db = _db(tmp_path)
    a1 = db.add_alert("threshold", "warning", "US", "m1", "dk1")
    db.add_alert("threshold", "warning", "GB", "m2", "dk2")
    db.resolve_alert(a1, by="human")

    kinds = [r["country"] for r in db.open_alerts()]
    assert kinds == ["GB"]


def test_open_alerts_excludes_snoozed_by_default_but_include_snoozed_returns_them(tmp_path):
    db = _db(tmp_path)
    alert_id = db.add_alert("ssl", "warning", "host.com", "m", "dk1")
    db.snooze_alert(alert_id, _iso(_now() + timedelta(days=1)))

    assert db.open_alerts() == []
    assert len(db.open_alerts(include_snoozed=True)) == 1


def test_count_active_alerts_excludes_resolved_and_snoozed(tmp_path):
    db = _db(tmp_path)
    a1 = db.add_alert("threshold", "warning", "US", "m1", "dk1")
    a2 = db.add_alert("threshold", "warning", "GB", "m2", "dk2")
    db.add_alert("threshold", "warning", "FR", "m3", "dk3")
    db.resolve_alert(a1, by="human")
    db.snooze_alert(a2, _iso(_now() + timedelta(days=1)))

    assert db.count_active_alerts() == 1  # only a3 remains active and visible


def test_snoozed_alert_counts_active_again_after_its_date_passes(tmp_path):
    db = _db(tmp_path)
    alert_id = db.add_alert("threshold", "warning", "US", "m", "dk1")
    # snoozed_until in the past -> already "expired", counts again.
    db.snooze_alert(alert_id, _iso(_now() - timedelta(minutes=1)))

    assert db.count_active_alerts() == 1
    assert len(db.open_alerts()) == 1


def test_list_alerts_status_filter_open_acknowledged_resolved_all(tmp_path):
    db = _db(tmp_path)
    a_open = db.add_alert("threshold", "warning", "US", "m1", "dk1")
    a_ack = db.add_alert("threshold", "warning", "GB", "m2", "dk2")
    a_res = db.add_alert("threshold", "warning", "FR", "m3", "dk3")
    db.ack_alert(a_ack, assignee="bob")
    db.resolve_alert(a_res, by="human")

    assert {r["id"] for r in db.list_alerts(status="open")} == {a_open}
    assert {r["id"] for r in db.list_alerts(status="acknowledged")} == {a_ack}
    assert {r["id"] for r in db.list_alerts(status="resolved")} == {a_res}
    assert {r["id"] for r in db.list_alerts(status="active")} == {a_open, a_ack}
    assert {r["id"] for r in db.list_alerts(status="all")} == {a_open, a_ack, a_res}
    # default without passing status == 'active'
    assert {r["id"] for r in db.list_alerts()} == {a_open, a_ack}


# --------------------------------------------------------------------- #
# UI /alerts: status filters + actions + smoke
# --------------------------------------------------------------------- #
def test_old_alerts_page_redirects_to_overview_with_the_popover(client):
    response = client.get("/alerts?status=resolved", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/?alerts=1"


def test_alerts_popover_lists_open_alerts_with_actions(client, seeded_db):
    alert_id = seeded_db.add_alert("threshold", "warning", "US", "popover msg", "dkpop")
    text = client.get("/ui/alerts").text
    assert "popover msg" in text and "data-sm-alerts-count='1'" in text
    for action in ("/alerts/ack", "/alerts/snooze", "/alerts/resolve"):
        assert f"action='{action}'" in text
    assert f"value='{alert_id}'" in text and "Snooze 7 days" in text
    assert "href='/settings#notifications'" in text and "Mark all read" in text


def test_alerts_ack_endpoint_acknowledges_and_redisplays(client, seeded_db):
    alert_id = seeded_db.add_alert("threshold", "warning", "US", "msg", "dkx")

    response = client.post("/alerts/ack", data={"alert_id": alert_id, "assignee": "alice"},
                           follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"] == "/?alerts=1"
    row = seeded_db.list_alerts(status="all")
    row = next(r for r in row if r["id"] == alert_id)
    assert row["status"] == "acknowledged"
    assert row["assignee"] == "alice"


def test_alert_actions_from_the_popover_update_it_in_place(client, seeded_db):
    alert_id = seeded_db.add_alert("threshold", "warning", "US", "in place", "dkin")
    resp = client.post("/alerts/resolve", data={"alert_id": alert_id},
                       headers={"X-SM-Fragment": "1"})
    assert resp.status_code == 200
    assert "<html" not in resp.text and "class='ahd'" in resp.text
    assert "in place" not in resp.text
    assert unquote(resp.headers["X-SM-Toast"]) == "Alert resolved."


def test_alerts_snooze_endpoint_sets_snoozed_until(client, seeded_db):
    alert_id = seeded_db.add_alert("threshold", "warning", "US", "msg", "dky")

    response = client.post("/alerts/snooze", data={"alert_id": alert_id, "until": "7d"})

    assert response.status_code == 200
    row = next(r for r in seeded_db.list_alerts(status="all") if r["id"] == alert_id)
    assert row["snoozed_until"] is not None


def test_alerts_resolve_endpoint_resolves_as_human(client, seeded_db):
    alert_id = seeded_db.add_alert("threshold", "warning", "US", "msg", "dkz")

    response = client.post("/alerts/resolve", data={"alert_id": alert_id})

    assert response.status_code == 200
    row = next(r for r in seeded_db.list_alerts(status="all") if r["id"] == alert_id)
    assert row["status"] == "resolved"
    assert row["resolved_by"] == "human"


def test_sidebar_badge_uses_active_count_not_seen(client, seeded_db):
    """A snoozed alert (active but outside the badge) must not add to the
    sidebar counter, even though it's still unread (seen=0)."""
    alert_id = seeded_db.add_alert("threshold", "warning", "US", "msg", "dkbadge")
    seeded_db.snooze_alert(alert_id, _iso(_now() + timedelta(days=1)))

    response = client.get("/")
    assert response.status_code == 200
    assert seeded_db.count_active_alerts() == 0


# --------------------------------------------------------------------- #
# "Check now" (POST /alerts/check) runs the same checks as the daily job
# --------------------------------------------------------------------- #
def test_check_now_uses_the_daily_job_function():
    import scheduler.jobs  # noqa: F401 -- populates JOBS
    from report import weekly
    from scheduler.core import JOBS

    # The job runs the same function once per scheduled subject.
    assert JOBS["alerts_check"]["func"] is weekly.run_alert_checks_scheduled


def test_check_now_also_raises_overdue_note_alerts(client, seeded_db):
    yesterday = (_now() - timedelta(days=1)).date().isoformat()
    seeded_db.set_note("https://scam.example/article", "Ask for removal", due_date=yesterday)

    resp = client.post("/alerts/check")

    assert resp.status_code == 200
    kinds = [a["kind"] for a in seeded_db.list_alerts(status="all")]
    assert "note_overdue" in kinds
    assert "new alert(s) detected for " in resp.text


def test_check_now_button_names_the_active_subject(client, seeded_db):
    """"Check now" only checks the active subject, so the label says which."""
    seeded_db.update_subject(1, name="Alice Main")
    html = client.get("/settings").text
    assert "Check now for Alice Main" in html
    assert "Check alerts now" not in html
