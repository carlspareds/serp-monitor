"""Tests for item 4.6 (Phase 5): notes -> management workflow.

Acceptance (§4.6): "A URL goes through the
full cycle with visible history; an overdue note alerts; the board
groups by status."

TDD, no network: direct Database (tmp_path, `conftest.make_db`) for the
schema/migration/helpers and processing.alerts.check_overdue_notes; TestClient
(fixture `client`, conftest.py) for the /notes, /notes/save,
/notes/status routes -- same approach as tests/test_timeline_impact.py.
"""
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

from conftest import make_db as _db

from database.models import NOTE_STATUSES, Database, note_is_overdue
from processing.alert_types import ALERT_TYPES
from processing.alerts import check_overdue_notes
from report.weekly import run_alert_checks


def _iso_day(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


# --------------------------------------------------------------------- #
# Schema / migration
# --------------------------------------------------------------------- #
def test_url_notes_has_workflow_columns(tmp_path):
    db = _db(tmp_path)
    conn = db._connect()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(url_notes)").fetchall()}
    conn.close()
    assert {"status", "assignee", "due_date", "removal_case_id"} <= cols


def test_note_history_table_exists_with_index(tmp_path):
    db = _db(tmp_path)
    conn = db._connect()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(note_history)").fetchall()}
    idx_names = {row[1] for row in conn.execute("PRAGMA index_list(note_history)").fetchall()}
    conn.close()
    assert {"id", "note_id", "from_status", "to_status", "by", "at", "comment"} <= cols
    assert "idx_note_history_note_id" in idx_names


def test_legacy_status_values_migrate_to_open(tmp_path):
    """Simulates a pre-4.6 DB: rows with the old free-text status ('en
    progreso', 'vigilar') or NULL must normalize to 'open' on
    reopen -- no pre-existing note is left off the board. A value that is
    already valid is left untouched."""
    import sqlite3

    db_path = str(tmp_path / "legacy.db")
    Database(db_path=db_path)  # creates the already-migrated schema
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO url_notes (url, status, note, updated_at) VALUES (?, ?, ?, ?)",
        ("https://example.com/a", "en progreso", "n1", "2026-01-01T00:00:00"),
    )
    conn.execute(
        "INSERT INTO url_notes (url, status, note, updated_at) VALUES (?, ?, ?, ?)",
        ("https://example.com/b", None, "n2", "2026-01-01T00:00:00"),
    )
    conn.execute(
        "INSERT INTO url_notes (url, status, note, updated_at) VALUES (?, ?, ?, ?)",
        ("https://example.com/c", "resolved", "n3", "2026-01-01T00:00:00"),
    )
    conn.commit()
    conn.close()

    reopened = Database(db_path=db_path)
    by_url = {n["url"]: n["status"] for n in reopened.list_notes()}
    assert by_url["https://example.com/a"] == "open"
    assert by_url["https://example.com/b"] == "open"
    assert by_url["https://example.com/c"] == "resolved"  # was already valid, untouched


def test_spanish_status_values_migrate_to_english_idempotently(tmp_path):
    """Older DBs persisted the workflow states in Spanish ('pendiente',
    'en_gestion', 'escalado', 'resuelto', 'descartado'). On startup they are
    rewritten to their English equivalent in url_notes AND note_history;
    running the migration again changes nothing."""
    import sqlite3

    db_path = str(tmp_path / "legacy_es.db")
    Database(db_path=db_path)
    legacy = {"pendiente": "open", "en_gestion": "in_progress", "escalado": "escalated",
              "resuelto": "resolved", "descartado": "dismissed"}
    conn = sqlite3.connect(db_path)
    for i, old in enumerate(legacy):
        conn.execute(
            "INSERT INTO url_notes (url, status, note, updated_at) VALUES (?, ?, ?, ?)",
            (f"https://example.com/{i}", old, "n", "2026-01-01T00:00:00"),
        )
    conn.execute(
        "INSERT INTO note_history (note_id, from_status, to_status, by, at, comment) "
        "VALUES (1, 'pendiente', 'escalado', 'human', '2026-01-01T00:00:00', '')"
    )
    conn.commit()
    conn.close()

    Database(db_path=db_path)
    reopened = Database(db_path=db_path)  # second startup: idempotent

    by_url = {n["url"]: n["status"] for n in reopened.list_notes()}
    assert by_url == {f"https://example.com/{i}": new for i, new in enumerate(legacy.values())}
    conn = sqlite3.connect(db_path)
    hist = conn.execute("SELECT from_status, to_status FROM note_history").fetchall()
    conn.close()
    assert hist == [("open", "escalated")]


def test_legacy_status_values_still_accepted_on_write_and_read(tmp_path):
    """Cheap backward compatibility: an old form/API client sending a Spanish
    status still transitions (stored in English), and a legacy closed status
    is still treated as closed by note_is_overdue."""
    db = _db(tmp_path)
    db.set_note("https://example.com/x", "n1")
    note_id = db.list_notes()[0]["id"]
    assert db.set_note_status(note_id, "escalado", by="human") is True
    assert db.list_notes()[0]["status"] == "escalated"
    assert note_is_overdue("2020-01-01", "resuelto", "2026-01-01") is False


# --------------------------------------------------------------------- #
# set_note: text/assignee/due_date, without touching status
# --------------------------------------------------------------------- #
def test_set_note_creates_pending_note_with_assignee_due_date(tmp_path):
    db = _db(tmp_path)
    db.set_note("https://example.com/x", "review", assignee="alice", due_date="2026-09-01")

    notes = db.list_notes()
    assert len(notes) == 1
    n = notes[0]
    assert n["status"] == "open"
    assert n["note"] == "review"
    assert n["assignee"] == "alice"
    assert n["due_date"] == "2026-09-01"
    assert isinstance(n["id"], int)


def test_set_note_preserves_status_on_text_edit(tmp_path):
    db = _db(tmp_path)
    db.set_note("https://example.com/x", "n1")
    note_id = db.list_notes()[0]["id"]
    db.set_note_status(note_id, "in_progress", by="human")

    db.set_note("https://example.com/x", "new text", assignee="bob")

    n = db.list_notes()[0]
    assert n["status"] == "in_progress"
    assert n["note"] == "new text"
    assert n["assignee"] == "bob"


def test_set_note_deletes_when_pending_and_all_fields_empty(tmp_path):
    db = _db(tmp_path)
    db.set_note("https://example.com/x", "n1")
    db.set_note("https://example.com/x", "", assignee="", due_date="")
    assert db.list_notes() == []


def test_set_note_does_not_delete_when_status_advanced(tmp_path):
    """A note that is ALREADY being managed (status != open) must not
    disappear just because the text was cleared -- it would lose the
    management log."""
    db = _db(tmp_path)
    db.set_note("https://example.com/x", "n1")
    note_id = db.list_notes()[0]["id"]
    db.set_note_status(note_id, "in_progress", by="human")

    db.set_note("https://example.com/x", "", assignee="", due_date="")

    notes = db.list_notes()
    assert len(notes) == 1
    assert notes[0]["status"] == "in_progress"


# --------------------------------------------------------------------- #
# Acceptance 1: "A URL goes through the full cycle with visible history"
# --------------------------------------------------------------------- #
def test_full_cycle_status_transitions_produce_visible_history(tmp_path):
    db = _db(tmp_path)
    db.set_note("https://example.com/x", "n1")
    note_id = db.list_notes()[0]["id"]

    assert db.set_note_status(note_id, "in_progress", by="human", comment="assigned") is True
    assert db.set_note_status(note_id, "escalated", by="human", comment="needs legal") is True
    assert db.set_note_status(note_id, "resolved", by="human", comment="done") is True

    assert db.list_notes()[0]["status"] == "resolved"

    hist = db.note_history(note_id)
    assert len(hist) == 3
    # most recent first
    assert [h["to_status"] for h in hist] == ["resolved", "escalated", "in_progress"]
    assert hist[0]["from_status"] == "escalated"
    assert hist[0]["comment"] == "done"
    assert hist[-1]["from_status"] == "open"
    assert all(h["by"] == "human" for h in hist)


def test_set_note_status_rejects_invalid_status(tmp_path):
    db = _db(tmp_path)
    db.set_note("https://example.com/x", "n1")
    note_id = db.list_notes()[0]["id"]

    assert db.set_note_status(note_id, "not_a_real_status", by="human") is False
    assert db.list_notes()[0]["status"] == "open"
    assert db.note_history(note_id) == []


def test_set_note_status_returns_false_for_missing_note_id(tmp_path):
    db = _db(tmp_path)
    assert db.set_note_status(99999, "in_progress", by="human") is False


def test_set_note_status_allows_discard_from_any_state(tmp_path):
    """'dismissed' is a legitimate exit from any state (brief: it's not a
    strict state machine, any whitelisted jump is valid)."""
    db = _db(tmp_path)
    db.set_note("https://example.com/x", "n1")
    note_id = db.list_notes()[0]["id"]
    db.set_note_status(note_id, "escalated", by="human")
    assert db.set_note_status(note_id, "dismissed", by="human") is True
    assert db.list_notes()[0]["status"] == "dismissed"


# --------------------------------------------------------------------- #
# Acceptance 3: "the board groups by status"
# --------------------------------------------------------------------- #
def test_list_notes_by_status_groups_correctly(tmp_path):
    db = _db(tmp_path)
    db.set_note("https://example.com/a", "a")
    db.set_note("https://example.com/b", "b")
    note_b_id = next(n["id"] for n in db.list_notes() if n["url"] == "https://example.com/b")
    db.set_note_status(note_b_id, "escalated", by="human")

    grouped = db.list_notes_by_status()
    assert list(grouped.keys()) == list(NOTE_STATUSES)
    assert [n["url"] for n in grouped["open"]] == ["https://example.com/a"]
    assert [n["url"] for n in grouped["escalated"]] == ["https://example.com/b"]
    assert grouped["resolved"] == []


# --------------------------------------------------------------------- #
# Acceptance 2: "an overdue note alerts"
# --------------------------------------------------------------------- #
def test_note_is_overdue_true_for_past_due_open_note():
    assert note_is_overdue("2020-01-01", "open", "2026-01-01") is True


def test_note_is_overdue_false_for_future_due_date():
    assert note_is_overdue("2027-01-01", "open", "2026-01-01") is False


def test_note_is_overdue_false_for_closed_note():
    assert note_is_overdue("2020-01-01", "resolved", "2026-01-01") is False
    assert note_is_overdue("2020-01-01", "dismissed", "2026-01-01") is False


def test_note_is_overdue_false_for_empty_or_malformed_date_never_raises():
    assert note_is_overdue("", "open", "2026-01-01") is False
    assert note_is_overdue("not-a-date", "open", "2026-01-01") is False
    assert note_is_overdue("2026-99-99", "open", "2026-01-01") is False


def test_overdue_notes_excludes_resolved_and_discarded(tmp_path):
    db = _db(tmp_path)
    db.set_note("https://example.com/open", "n", due_date=_iso_day(5))
    db.set_note("https://example.com/resolved", "n", due_date=_iso_day(5))
    resolved_id = next(n["id"] for n in db.list_notes() if n["url"] == "https://example.com/resolved")
    db.set_note_status(resolved_id, "resolved", by="human")

    today = _iso_day(0)
    overdue = db.overdue_notes(today)
    assert [n["url"] for n in overdue] == ["https://example.com/open"]


# --------------------------------------------------------------------- #
# note_overdue Alert: catalog + emit_alert + dedupe
# --------------------------------------------------------------------- #
def test_note_overdue_is_in_alert_types_catalog():
    assert ALERT_TYPES["note_overdue"]["sev"] == "warning"


def test_check_overdue_notes_emits_alert_for_overdue_note(tmp_path):
    db = _db(tmp_path)
    db.set_note("https://example.com/x", "n", assignee="alice", due_date=_iso_day(3))
    note_id = db.list_notes()[0]["id"]

    emitted = check_overdue_notes(db, today=_iso_day(0))

    assert emitted == 1
    alerts = db.list_alerts(status="all")
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "note_overdue"
    assert alerts[0]["severity"] == "warning"
    assert alerts[0]["dedupe_key"] == f"noteoverdue:{note_id}:{_iso_day(3)}"


def test_check_overdue_notes_skips_not_yet_due(tmp_path):
    db = _db(tmp_path)
    db.set_note("https://example.com/x", "n", due_date=_iso_day(-5))  # future
    assert check_overdue_notes(db, today=_iso_day(0)) == 0
    assert db.list_alerts(status="all") == []


def test_check_overdue_notes_dedupes_repeated_calls(tmp_path):
    db = _db(tmp_path)
    db.set_note("https://example.com/x", "n", due_date=_iso_day(3))

    check_overdue_notes(db, today=_iso_day(0))
    emitted_again = check_overdue_notes(db, today=_iso_day(0))

    assert emitted_again == 0
    assert len(db.list_alerts(status="all")) == 1


def test_check_overdue_notes_re_alerts_when_due_date_moves(tmp_path):
    db = _db(tmp_path)
    db.set_note("https://example.com/x", "n", due_date=_iso_day(3))
    check_overdue_notes(db, today=_iso_day(0))

    db.set_note("https://example.com/x", "n", due_date=_iso_day(10))
    emitted = check_overdue_notes(db, today=_iso_day(0))

    assert emitted == 1
    assert len(db.list_alerts(status="all")) == 2


def test_check_overdue_notes_skips_closed_notes(tmp_path):
    db = _db(tmp_path)
    db.set_note("https://example.com/x", "n", due_date=_iso_day(3))
    note_id = db.list_notes()[0]["id"]
    db.set_note_status(note_id, "dismissed", by="human")

    assert check_overdue_notes(db, today=_iso_day(0)) == 0
    assert db.list_alerts(status="all") == []


def test_run_alert_checks_includes_notes_overdue(tmp_path):
    """Real hookup (not a new cron): report.weekly.run_alert_checks --
    the daily `alerts_check` job -- includes the overdue-notes check."""
    db = _db(tmp_path)
    db.set_note("https://example.com/x", "n", due_date=_iso_day(3))

    result = run_alert_checks(db)

    assert result["error"] is None
    assert result["checks"]["notes_overdue"] == {"new": 1}
    alerts = [a for a in db.list_alerts(status="all") if a["kind"] == "note_overdue"]
    assert len(alerts) == 1


# --------------------------------------------------------------------- #
# Router: Note panel (/ui/note), Notes panel (/ui/notes), /notes/save,
# /notes/status, old /notes redirect
# --------------------------------------------------------------------- #
_FRAG = {"X-SM-Fragment": "1"}


def _notes(client, **params):
    return client.get("/ui/notes", params=params, headers=_FRAG)


def test_old_notes_board_redirects_to_the_notes_panel(client):
    response = client.get("/notes", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/results?panel=/ui/notes"
    one = client.get("/notes", params={"url": "https://example.com/x"}, follow_redirects=False)
    assert one.headers["location"] == "/results?panel=/ui/note%3Furl%3Dhttps%253A%252F%252Fexample.com%252Fx"


def test_notes_panel_loads_ok(client):
    response = _notes(client)
    assert response.status_code == 200 and "<html" not in response.text
    assert "No notes yet." in response.text


def test_note_panel_has_text_status_and_due_date(client):
    text = client.get("/ui/note", params={"url": "https://scam.example/article"}, headers=_FRAG).text
    assert "name='note'" in text and "name='status'" in text and "type='date' name='due_date'" in text
    assert "In progress" in text and "Save note" in text and "data-sm-submit" in text


def test_notes_save_creates_open_note_visible_in_the_list(client, seeded_db):
    response = client.post("/notes/save", data={
        "url": "https://example.com/tracked", "note": "check this",
        "assignee": "alice", "due_date": "2026-09-01",
    }, follow_redirects=False)
    assert response.status_code == 303   # PRG: back to Results with the note open
    assert response.headers["location"].startswith("/results?panel=/ui/note")
    note = seeded_db.list_notes()[0]
    assert note["status"] == "open" and note["assignee"] == "alice"
    text = _notes(client).text
    assert "https://example.com/tracked" in text and "check this" in text


def test_notes_save_from_the_panel_answers_with_the_panel_and_a_toast(client, seeded_db):
    response = client.post("/notes/save", headers=_FRAG, data={
        "url": "https://scam.example/article", "note": "Asked for removal", "status": "in_progress",
        "due_date": "2026-09-01", "comment": "emailed the owner"})
    assert response.status_code == 200
    assert unquote(response.headers["X-SM-Toast"]) == "Note saved."
    assert "Asked for removal" in response.text and "data-sm-reload-on-close" in response.text
    note = seeded_db.list_notes()[0]
    assert note["status"] == "in_progress"
    hist = seeded_db.note_history(note["id"])
    assert hist[0]["to_status"] == "in_progress" and hist[0]["by"] == "local admin"
    assert "by local admin" in response.text and "emailed the owner" in response.text
    again = client.post("/notes/save", headers=_FRAG, data={
        "url": "https://scam.example/article", "note": "Asked for removal", "status": "in_progress",
        "due_date": "2026-09-01"})
    assert unquote(again.headers["X-SM-Toast"]) == "No changes."


def test_notes_save_rejects_a_bad_due_date_inside_the_panel(client, seeded_db):
    response = client.post("/notes/save", headers=_FRAG, data={
        "url": "https://scam.example/article", "note": "x", "due_date": "31/12/2026"})
    assert response.status_code == 400 and "pick a valid due date" in response.text
    assert seeded_db.list_notes() == []


def test_notes_status_transition_records_history_and_audit(client, seeded_db):
    client.post("/notes/save", data={"url": "https://example.com/tracked", "note": "x"})
    note_id = seeded_db.list_notes()[0]["id"]

    response = client.post("/notes/status", data={
        "note_id": str(note_id), "to_status": "in_progress", "comment": "picked up",
    })
    assert response.status_code == 200
    text = client.get("/ui/note", params={"url": "https://example.com/tracked"}, headers=_FRAG).text
    assert "Open → In progress" in text and "picked up" in text

    hist = seeded_db.note_history(note_id)
    assert len(hist) == 1
    assert hist[0]["from_status"] == "open"
    assert hist[0]["to_status"] == "in_progress"

    audit_rows = seeded_db.list_audit(action="note_status_change", resource_id=str(note_id))
    assert len(audit_rows) == 1


def test_notes_status_invalid_status_is_silently_ignored(client, seeded_db):
    client.post("/notes/save", data={"url": "https://example.com/tracked", "note": "x"})
    note_id = seeded_db.list_notes()[0]["id"]

    response = client.post("/notes/status", data={
        "note_id": str(note_id), "to_status": "bogus_status",
    })
    assert response.status_code == 200
    assert seeded_db.list_notes()[0]["status"] == "open"
    assert seeded_db.note_history(note_id) == []


def test_notes_status_non_numeric_note_id_never_500s(client, seeded_db):
    response = client.post("/notes/status", data={
        "note_id": "not-a-number", "to_status": "in_progress",
    })
    assert response.status_code == 200


def test_notes_overdue_filter_shows_only_overdue(client, seeded_db):
    client.post("/notes/save", data={
        "url": "https://example.com/overdue", "note": "n", "due_date": _iso_day(3),
    })
    client.post("/notes/save", data={
        "url": "https://example.com/future", "note": "n", "due_date": _iso_day(-30),
    })

    response = _notes(client, overdue="1")
    assert response.status_code == 200
    assert "https://example.com/overdue" in response.text
    assert "https://example.com/future" not in response.text

    response_all = _notes(client)
    assert "https://example.com/overdue" in response_all.text
    assert "https://example.com/future" in response_all.text


def test_notes_overdue_badge_shown_for_overdue_open_note(client, seeded_db):
    client.post("/notes/save", data={
        "url": "https://example.com/overdue", "note": "n", "due_date": _iso_day(3),
    })
    assert "overdue" in _notes(client).text.lower()
    assert "overdue" in client.get("/ui/note", params={"url": "https://example.com/overdue"},
                                   headers=_FRAG).text


def test_notes_panels_escape_html_in_note_fields(client, seeded_db):
    client.post("/notes/save", data={
        "url": "https://example.com/xss",
        "note": "<script>alert(1)</script>",
        "assignee": "<b>bob</b>",
    })
    for response in (_notes(client), client.get("/ui/note", params={"url": "https://example.com/xss"},
                                                headers=_FRAG)):
        assert "<script>alert(1)</script>" not in response.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
        assert "<b>bob</b>" not in response.text


def test_notes_history_shows_who_changed_it(client, seeded_db):
    client.post("/notes/save", data={"url": "https://example.com/tracked", "note": "x"})
    note_id = seeded_db.list_notes()[0]["id"]
    client.post("/notes/status", data={
        "note_id": str(note_id), "to_status": "in_progress", "comment": "picked up",
    })

    response = client.get("/ui/note", params={"url": "https://example.com/tracked"}, headers=_FRAG)
    assert response.status_code == 200
    assert "History" in response.text
    assert "picked up" in response.text
    assert "by local admin" in response.text and "human" not in response.text
