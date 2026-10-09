"""Tests for item 7.1: canonical severities + per-channel routing.

TDD, no network: send_via/email_sender are always stubbed -- no test touches
real telegram/slack/ntfy/SMTP.
"""
import sqlite3

from conftest import make_db as _db

from database.models import Database
from notify import dispatcher
from processing.alert_types import ALERT_TYPES, normalize_severity


# --------------------------------------------------------------------- #
# Catalog + normalization (processing/alert_types.py)
# --------------------------------------------------------------------- #
def test_normalize_severity_maps_legacy_med_to_warning():
    assert normalize_severity("threshold", "med") == "warning"


def test_normalize_severity_maps_legacy_high_to_critical():
    assert normalize_severity("backup_missing", "high") == "critical"


def test_normalize_severity_keeps_canonical_values_unchanged():
    assert normalize_severity("threshold", "critical") == "critical"
    assert normalize_severity("threshold", "info") == "info"


def test_normalize_severity_empty_falls_back_to_catalog():
    assert normalize_severity("url_gone", "") == "info"


def test_normalize_severity_unknown_kind_defaults_to_warning():
    assert normalize_severity("some_future_kind", "") == "warning"


def test_alert_types_catalog_covers_known_kinds():
    for kind in ("threshold", "negative_new", "apify_budget", "job_missed",
                 "url_gone", "note_overdue"):
        assert ALERT_TYPES[kind]["sev"] in ("info", "warning", "critical")


# --------------------------------------------------------------------- #
# Database.add_alert -- normalization on insert
# --------------------------------------------------------------------- #
def test_add_alert_normalizes_med_to_warning(tmp_path):
    db = _db(tmp_path)
    db.add_alert("threshold", "med", "US", "msg", "dk1")

    row = db.list_alerts(limit=1)[0]
    assert row["severity"] == "warning"


def test_add_alert_normalizes_high_to_critical(tmp_path):
    db = _db(tmp_path)
    db.add_alert("backup_missing", "high", "US", "msg", "dk2")

    row = db.list_alerts(limit=1)[0]
    assert row["severity"] == "critical"


def test_add_alert_returns_id_when_inserted_and_none_when_duplicate(tmp_path):
    db = _db(tmp_path)
    first = db.add_alert("threshold", "med", "US", "msg", "dk3")
    second = db.add_alert("threshold", "med", "US", "msg", "dk3")  # dedupe

    assert isinstance(first, int) and first > 0
    assert not second


# --------------------------------------------------------------------- #
# One-shot migration of legacy rows (med/high) on DB reopen
# --------------------------------------------------------------------- #
def test_legacy_severities_are_migrated_on_reopen(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    Database(db_path=db_path)  # creates the schema
    # Insert rows "by hand" with legacy severities, without going through
    # add_alert (simulates rows created by an earlier version of the code).
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO alerts (created_at, kind, severity, country, message, dedupe_key) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-01-01T00:00:00", "threshold", "med", "US", "legacy med", "legacy1"),
    )
    conn.execute(
        "INSERT INTO alerts (created_at, kind, severity, country, message, dedupe_key) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("2026-01-01T00:00:00", "backup_missing", "high", "US", "legacy high", "legacy2"),
    )
    conn.commit()
    conn.close()

    # Reopening the DB triggers _init_db -> one-shot migration.
    reopened = Database(db_path=db_path)
    rows = {r["dedupe_key"]: r["severity"] for r in reopened.list_alerts(limit=10)}

    assert rows["legacy1"] == "warning"
    assert rows["legacy2"] == "critical"


# --------------------------------------------------------------------- #
# alert_routing: seeded defaults + get/set
# --------------------------------------------------------------------- #
def test_routing_defaults_seeded_on_fresh_db(tmp_path):
    db = _db(tmp_path)

    routing = db.get_routing()

    assert routing == {"critical": "telegram", "warning": "email", "info": "digest"}


def test_set_routing_changes_destination_without_restart(tmp_path):
    db = _db(tmp_path)
    db.set_routing("critical", "slack")

    routing = db.get_routing()

    assert routing["critical"] == "slack"
    # The other severities are not affected.
    assert routing["warning"] == "email"


# --------------------------------------------------------------------- #
# Dispatcher: routes according to alert_routing, no network (stubs)
# --------------------------------------------------------------------- #
def test_notify_alert_critical_routes_to_send_via(tmp_path, monkeypatch):
    db = _db(tmp_path)
    calls = []
    monkeypatch.setattr(dispatcher, "send_via", lambda *a, **k: calls.append((a, k)) or (True, "ok"))

    alert = {"id": 1, "kind": "backup_missing", "severity": "critical",
              "country": "US", "message": "Backup missing"}
    dispatcher.notify_alert(db, alert)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] is db
    assert args[1] == "telegram"
    assert args[2].startswith("My project ▸ Subject 1: CRITICAL")
    assert "backup_missing" in args[2]
    assert kwargs.get("alert_id") == 1


def test_notify_alert_warning_routes_to_email(tmp_path, monkeypatch):
    db = _db(tmp_path)
    calls = []
    monkeypatch.setattr(dispatcher.email_sender, "is_configured", lambda: True)
    monkeypatch.setattr(
        dispatcher.email_sender, "send_email",
        lambda subject, body, **k: calls.append((subject, body)) or (True, "ok"),
    )

    alert = {"id": 2, "kind": "threshold", "severity": "warning",
              "country": "US", "message": "2 negatives"}
    dispatcher.notify_alert(db, alert)

    assert len(calls) == 1
    subject, body = calls[0]
    # Every alert names its subject: "<Project> ▸ <Subject>: " (subject 1 of a
    # fresh DB is unnamed -> "Subject 1", in the default "My project" project).
    assert subject == "My project ▸ Subject 1: threshold"
    assert "2 negatives" in body


def test_notify_alert_info_digest_does_not_send_immediately(tmp_path, monkeypatch):
    db = _db(tmp_path)
    called = {"n": 0}
    monkeypatch.setattr(dispatcher, "send_via", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    monkeypatch.setattr(dispatcher.email_sender, "send_email", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    alert = {"id": 3, "kind": "wp_outdated", "severity": "info",
              "country": "US", "message": "WP outdated"}
    dispatcher.notify_alert(db, alert)

    assert called["n"] == 0


def test_changing_routing_changes_next_send_channel(tmp_path, monkeypatch):
    """Acceptance: changing the routing in the DB takes effect without a
    restart -- the dispatcher reads from the DB on every send, it doesn't
    cache the routing in memory."""
    db = _db(tmp_path)
    calls = []
    monkeypatch.setattr(dispatcher, "send_via", lambda *a, **k: calls.append(a[1]) or (True, "ok"))

    alert = {"id": 4, "kind": "ssl_error", "severity": "critical",
              "country": "US", "message": "SSL down"}
    dispatcher.notify_alert(db, alert)
    assert calls == ["telegram"]

    db.set_routing("critical", "slack")
    dispatcher.notify_alert(db, alert)
    assert calls == ["telegram", "slack"]


# --------------------------------------------------------------------- #
# Daily info digest (info_digest job)
# --------------------------------------------------------------------- #
def test_info_digest_groups_three_infos_in_one_email(tmp_path, monkeypatch):
    db = _db(tmp_path)
    db.add_alert("wp_outdated", "info", "US", "WP 1 outdated", "wp1")
    db.add_alert("wp_outdated", "info", "GB", "WP 2 outdated", "wp2")
    db.add_alert("wp_outdated", "info", "FR", "WP 3 outdated", "wp3")

    calls = []
    monkeypatch.setattr(dispatcher.email_sender, "is_configured", lambda: True)
    monkeypatch.setattr(
        dispatcher.email_sender, "send_email",
        lambda subject, body, **k: calls.append((subject, body)) or (True, "ok"),
    )

    sent = dispatcher.send_info_digest(db)

    assert sent is True
    assert len(calls) == 1
    _subject, body = calls[0]
    assert body.count("<li>") == 3


def test_info_digest_does_not_resend_same_day(tmp_path, monkeypatch):
    db = _db(tmp_path)
    db.add_alert("wp_outdated", "info", "US", "WP outdated", "wp1")

    calls = []
    monkeypatch.setattr(dispatcher.email_sender, "is_configured", lambda: True)
    monkeypatch.setattr(
        dispatcher.email_sender, "send_email",
        lambda subject, body, **k: calls.append(1) or (True, "ok"),
    )

    first = dispatcher.send_info_digest(db)
    second = dispatcher.send_info_digest(db)

    assert first is True
    assert second is False
    assert len(calls) == 1


def test_digest_includes_every_severity_routed_to_digest(tmp_path, monkeypatch):
    """The /alerts routing card says routing a severity to "digest" sends
    those alerts in the daily email -- not only "info"."""
    db = _db(tmp_path)
    db.set_routing("warning", "digest")
    db.set_routing("info", "none")
    db.add_alert("wp_outdated", "info", "US", "info not routed", "d-info")
    db.add_alert("threshold", "warning", "US", "warning routed to digest", "d-warn")

    calls = []
    monkeypatch.setattr(dispatcher.email_sender, "is_configured", lambda: True)
    monkeypatch.setattr(
        dispatcher.email_sender, "send_email",
        lambda subject, body, **k: calls.append(body) or (True, "ok"),
    )

    assert dispatcher.send_info_digest(db) is True
    assert "warning routed to digest" in calls[0]
    assert "info not routed" not in calls[0]


def test_alerts_page_explains_digest_routing(client):
    text = client.get("/settings").text
    assert "one email a day" in text and "08:00 UTC" in text


def test_info_digest_no_send_without_pending_info(tmp_path, monkeypatch):
    db = _db(tmp_path)
    calls = []
    monkeypatch.setattr(dispatcher.email_sender, "is_configured", lambda: True)
    monkeypatch.setattr(dispatcher.email_sender, "send_email", lambda *a, **k: calls.append(1) or (True, "ok"))

    sent = dispatcher.send_info_digest(db)

    assert sent is False
    assert calls == []


# --------------------------------------------------------------------- #
# UI /alerts: routing selects + smoke
# --------------------------------------------------------------------- #
def test_routing_selects_live_in_settings_notifications(client):
    response = client.get("/settings")

    assert response.status_code == 200
    assert "action='/settings/alerts'" in response.text
    for sev in ("critical", "warning", "info"):
        assert f"name='{sev}'" in response.text


def test_alerts_routing_save_updates_destination(client):
    response = client.post("/alerts/routing", data={
        "critical": "slack", "warning": "email", "info": "digest",
    })

    assert response.status_code == 200
    assert "slack" in response.text.lower()


# --------------------------------------------------------------------- #
# Fix H13 (A3 review, security): notify_alert/send_info_digest escape
# message/kind/country in the email branch -- both concatenate alert
# text (which can come from external data: a Wikipedia title, a domain
# cited by an AI engine...) into raw HTML without escaping.
# --------------------------------------------------------------------- #
def test_notify_alert_email_escapes_html_in_message(tmp_path, monkeypatch):
    db = _db(tmp_path)
    calls = []
    monkeypatch.setattr(dispatcher.email_sender, "is_configured", lambda: True)
    monkeypatch.setattr(
        dispatcher.email_sender, "send_email",
        lambda subject, body, **k: calls.append((subject, body)) or (True, "ok"),
    )

    alert = {"id": 9, "kind": "wiki_edited", "severity": "warning", "country": "",
              "message": "<script>alert(1)</script> changed the page"}
    dispatcher.notify_alert(db, alert)

    assert len(calls) == 1
    _subject, body = calls[0]
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


# --------------------------------------------------------------------- #
# FIX 1 (silent-alerts audit): emit_alert -- add_alert + dispatch
# in a single point, so that the persisted severity and the dispatched
# one can never diverge.
# --------------------------------------------------------------------- #
def test_emit_alert_creates_and_dispatches_with_normalized_severity(tmp_path, monkeypatch):
    db = _db(tmp_path)
    calls = []
    monkeypatch.setattr(dispatcher, "notify_alert", lambda db_arg, row: calls.append(row))

    alert_id = dispatcher.emit_alert(db, "threshold", "med", "US", "msg", "dk1")

    assert isinstance(alert_id, int) and alert_id > 0
    persisted = db.list_alerts(limit=1)[0]
    assert persisted["severity"] == "warning"
    assert len(calls) == 1
    assert calls[0] == {"id": alert_id, "kind": "threshold", "severity": "warning",
                         "country": "US", "message": "msg"}


def test_emit_alert_dedupe_does_not_create_or_dispatch_twice(tmp_path, monkeypatch):
    db = _db(tmp_path)
    calls = []
    monkeypatch.setattr(dispatcher, "notify_alert", lambda db_arg, row: calls.append(row))

    first = dispatcher.emit_alert(db, "threshold", "warning", "US", "msg", "dk1")
    second = dispatcher.emit_alert(db, "threshold", "warning", "US", "msg (again)", "dk1")

    assert isinstance(first, int)
    assert not second
    assert len(calls) == 1
    assert len(db.list_alerts(status="all")) == 1


def test_emit_alert_persisted_and_dispatched_severity_never_diverge(tmp_path, monkeypatch):
    """Regression test for legacy severities that contradicted the catalog
    (FIX 1, point 5): emit_alert normalizes ONCE and uses the SAME
    value both to persist and to dispatch -- unlike the previous
    pattern (add_alert normalized on its own, notify_alert received the
    raw severity from the caller), here it is structurally
    impossible for them to diverge."""
    db = _db(tmp_path)
    calls = []
    monkeypatch.setattr(dispatcher, "notify_alert", lambda db_arg, row: calls.append(row))

    dispatcher.emit_alert(db, "url_gone", "", "example.com", "outdated", "dk2")

    persisted = db.list_alerts(limit=1)[0]
    assert persisted["severity"] == calls[0]["severity"] == "info"


def test_emit_alert_returns_none_without_dispatching_when_add_alert_fails(tmp_path, monkeypatch):
    """There is no dedupe here -- add_alert simply doesn't insert (DB
    constraint, etc): notify_alert must not be called without a real
    alert_id."""
    db = _db(tmp_path)
    calls = []
    monkeypatch.setattr(dispatcher, "notify_alert", lambda db_arg, row: calls.append(row))
    monkeypatch.setattr(db, "add_alert", lambda *a, **k: None)

    result = dispatcher.emit_alert(db, "threshold", "warning", "US", "msg", "dk1")

    assert result is None
    assert calls == []


# --------------------------------------------------------------------- #
# Point 7 (silent-alerts audit): _dispatch_now/email never left a
# row in notify_log -- unlike telegram/slack/ntfy (via
# send_via), the email branch of _dispatch_now sent (or didn't) and
# returned without touching notify_log. With warning->email as the
# default (including job_missed, whose default severity is "warning"),
# this explains why neither real alerts nor the dead-man's switch itself
# ever left ANY trace in the history even though the dispatch DID run.
# --------------------------------------------------------------------- #
def test_dispatch_now_email_success_logs_to_notify_log(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(dispatcher.email_sender, "is_configured", lambda: True)
    monkeypatch.setattr(dispatcher.email_sender, "send_email", lambda *a, **k: (True, "sent"))

    dispatcher._dispatch_now(db, {"id": 42, "kind": "job_missed", "severity": "warning",
                                   "country": "minutely_job", "message": "did not run"})

    rows = db.recent_notify_log()
    assert len(rows) == 1
    assert rows[0]["channel"] == "email"
    assert rows[0]["ok"] == 1
    assert rows[0]["alert_id"] == 42


def test_dispatch_now_email_failure_logs_error_to_notify_log(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(dispatcher.email_sender, "is_configured", lambda: True)
    monkeypatch.setattr(dispatcher.email_sender, "send_email",
                         lambda *a, **k: (False, "SMTP timeout"))

    dispatcher._dispatch_now(db, {"id": 7, "kind": "threshold", "severity": "warning",
                                   "country": "US", "message": "m"})

    rows = db.recent_notify_log()
    assert len(rows) == 1
    assert rows[0]["ok"] == 0
    assert rows[0]["error"] == "SMTP timeout"


def test_dispatch_now_email_not_configured_does_not_log(tmp_path, monkeypatch):
    """Symmetry with send_via (review 7.4 #3): an unconfigured channel does
    not leave a row -- prevents a Test click against an empty channel from
    pushing real sends out of the 'last 10'."""
    db = _db(tmp_path)
    monkeypatch.setattr(dispatcher.email_sender, "is_configured", lambda: False)

    dispatcher._dispatch_now(db, {"id": 1, "kind": "threshold", "severity": "warning",
                                   "country": "US", "message": "m"})

    assert db.recent_notify_log() == []


def test_info_digest_escapes_html_in_message(tmp_path, monkeypatch):
    db = _db(tmp_path)
    db.add_alert("wp_outdated", "info", "US", "<img src=x onerror=alert(1)>", "wpxss")

    calls = []
    monkeypatch.setattr(dispatcher.email_sender, "is_configured", lambda: True)
    monkeypatch.setattr(
        dispatcher.email_sender, "send_email",
        lambda subject, body, **k: calls.append((subject, body)) or (True, "ok"),
    )

    sent = dispatcher.send_info_digest(db)

    assert sent is True
    _subject, body = calls[0]
    assert "<img src=x onerror=alert(1)>" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body
