"""Partial C21 (golden rule F2): subject_id INTEGER DEFAULT 1 across
every subject data table, so features are born multi-subject even
though the multi-client UI arrives in F7 (deep dive §C21)."""
import sqlite3

from conftest import make_db as _db

from database.models import Database


def _columns(db, table):
    con = sqlite3.connect(db.db_path)
    cols = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    con.close()
    return cols


def test_all_subject_tables_have_subject_id(tmp_path):
    db = _db(tmp_path)
    for table in Database._SUBJECT_TABLES:
        assert "subject_id" in _columns(db, table), table


def test_new_rows_default_to_subject_1(tmp_path):
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us",
                status="completed",
                summary={"total": 0, "positive": 0, "negative": 0, "neutral": 0})
    con = sqlite3.connect(db.db_path)
    assert con.execute("SELECT subject_id FROM runs WHERE apify_run_id='r1'").fetchone()[0] == 1
    con.close()


def test_migration_backfills_existing_rows_to_subject_1(tmp_path):
    # Simulates a pre-C21 DB: table without the column, with rows.
    path = str(tmp_path / "legacy.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "event_date TEXT, kind TEXT, description TEXT, created_at TEXT)")
    con.execute("INSERT INTO events (event_date, kind, description, created_at) "
                "VALUES ('2026-01-01', 'note', 'legacy row', '2026-01-01T00:00:00')")
    con.commit()
    con.close()

    db = Database(db_path=path)  # boot migrates it (ALTER with a constant DEFAULT)
    con = sqlite3.connect(db.db_path)
    assert con.execute("SELECT subject_id FROM events").fetchone()[0] == 1
    con.close()
