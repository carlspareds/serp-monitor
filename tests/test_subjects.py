"""Multi-subject phase 1 (data layer): the request-scoped active subject
(ContextVar), the Subject CRUD/tracking API, the legacy-settings bridge for
subject 1, the per-subject AI context, and the startup migrations on an
old-shape database."""
import json
import sqlite3
import threading

import pytest
from conftest import make_db as _db

from database.models import (
    DEFAULT_SUBJECT_ID,
    Database,
    current_subject_id,
    reset_active_subject,
    set_active_subject,
    subject_scope,
)
from processing import ai_classifier


def _run(db, run_id, query="q", country="us"):
    db.save_run(apify_run_id=run_id, queries=[query], country_code=country,
                status="completed", summary={"total": 0, "positive": 0, "negative": 0,
                                             "neutral": 0})


# --------------------------------------------------------------------- #
# ContextVar resolution: for_subject -> ContextVar -> DEFAULT_SUBJECT_ID
# --------------------------------------------------------------------- #
def test_contextvar_defaults_to_subject_1():
    assert current_subject_id() == DEFAULT_SUBJECT_ID


def test_unscoped_instance_follows_the_contextvar(tmp_path):
    db = _db(tmp_path)
    _run(db.for_subject(1), "r1")
    _run(db.for_subject(2), "r2")
    token = set_active_subject(2)
    try:
        assert current_subject_id() == 2
        assert [r["apify_run_id"] for r in db.list_runs()] == ["r2"]
        _run(db, "r3")  # writes land on the ContextVar subject too
    finally:
        reset_active_subject(token)
    assert current_subject_id() == 1
    assert [r["apify_run_id"] for r in db.list_runs()] == ["r1"]
    assert {r["apify_run_id"] for r in db.for_subject(2).list_runs()} == {"r2", "r3"}


def test_explicit_for_subject_always_wins_over_the_contextvar(tmp_path):
    db = _db(tmp_path)
    _run(db.for_subject(1), "r1")
    _run(db.for_subject(2), "r2")
    with subject_scope(2):
        assert [r["apify_run_id"] for r in db.for_subject(1).list_runs()] == ["r1"]
        assert {r["apify_run_id"] for r in db.for_all_subjects().list_runs()} == {"r1", "r2"}
        assert db.for_subject(1).current_subject()["id"] == 1
        assert db.current_subject()["id"] == 2


def test_subject_scope_restores_on_error():
    with pytest.raises(RuntimeError), subject_scope(5):
        assert current_subject_id() == 5
        raise RuntimeError("boom")
    assert current_subject_id() == DEFAULT_SUBJECT_ID


def _app(db: Database):
    """Tiny app: a pure-ASGI middleware sets the active subject from a header (the
    phase-2 pattern), sync endpoints run in Starlette's threadpool."""
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/whoami")
    def whoami():  # sync -> anyio threadpool
        seen_in_thread = {}

        def worker():
            seen_in_thread["sid"] = current_subject_id()
            seen_in_thread["db"] = db.current_subject()["id"]

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        return {
            "sid": current_subject_id(),
            "subject": db.current_subject()["id"],
            "runs": [r["apify_run_id"] for r in db.list_runs()],
            "explicit": [r["apify_run_id"] for r in db.for_subject(1).list_runs()],
            "manual_thread": seen_in_thread,
        }

    class SubjectMiddleware:
        def __init__(self, inner):
            self.inner = inner

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                return await self.inner(scope, receive, send)
            headers = dict(scope.get("headers") or [])
            raw = headers.get(b"x-subject")
            token = set_active_subject(int(raw)) if raw else None
            try:
                await self.inner(scope, receive, send)
            finally:
                if token is not None:
                    reset_active_subject(token)

    app.add_middleware(SubjectMiddleware)
    return app


def test_contextvar_reaches_sync_endpoints_in_the_threadpool_but_not_manual_threads(tmp_path):
    from fastapi.testclient import TestClient

    db = _db(tmp_path)
    _run(db.for_subject(1), "r1")
    _run(db.for_subject(2), "r2")
    client = TestClient(_app(db))

    body = client.get("/whoami", headers={"x-subject": "2"}).json()
    assert body["sid"] == 2
    assert body["subject"] == 2
    assert body["runs"] == ["r2"]
    assert body["explicit"] == ["r1"]  # for_subject() wins inside the request too
    # A thread started by hand does NOT inherit the context (Python 3.12): it falls
    # back to subject 1 -- background work must use for_subject(sid)/subject_scope.
    assert body["manual_thread"] == {"sid": 1, "db": 1}

    # No header -> default subject; the previous request's value didn't stick.
    body = client.get("/whoami").json()
    assert body["sid"] == 1
    assert body["runs"] == ["r1"]


def test_contextvar_is_isolated_between_concurrent_threads():
    """Two workers that each set their own subject never see each other's value."""
    seen = {}
    barrier = threading.Barrier(2)

    def worker(sid):
        with subject_scope(sid):
            barrier.wait()
            seen[sid] = current_subject_id()

    threads = [threading.Thread(target=worker, args=(s,)) for s in (2, 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == {2: 2, 3: 3}


# --------------------------------------------------------------------- #
# Subject CRUD
# --------------------------------------------------------------------- #
def test_add_get_list_subject(tmp_path):
    db = _db(tmp_path)
    sid = db.add_subject("  Alex   Rivera ", aliases=["C. Rivera"], type="person",
                         countries="US, mx,us", context="Portuguese guitarist")
    s = db.get_subject(sid)
    assert s["name"] == "Alex Rivera"
    assert s["aliases"] == ["C. Rivera"]
    assert s["countries"] == ["us", "mx"]
    assert s["query"] == "Alex Rivera"
    assert s["context"] == "Portuguese guitarist"
    assert s["is_demo"] is False and s["active"] is True
    assert [x["id"] for x in db.list_subjects()] == [1, sid]


def test_add_subject_validation(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(ValueError):
        db.add_subject("   ")
    with pytest.raises(ValueError):
        db.add_subject("X", type="planet")
    db.add_subject("Alex Rivera")
    with pytest.raises(ValueError, match="already answers"):
        db.add_subject("alex  RIVERA")


def test_find_subject_by_name_matches_aliases_case_and_whitespace(tmp_path):
    db = _db(tmp_path)
    sid = db.add_subject("Jordan Ashworth (demo)", aliases=["Jordan Ashworth"],
                         queries=["Jordan Ashworth"])
    assert db.find_subject_by_name("jordan   ashworth")["id"] == sid
    assert db.find_subject_by_name("JORDAN ASHWORTH (DEMO)")["id"] == sid
    assert db.find_subject_by_name("Someone Else") is None
    assert db.find_subject_by_name("") is None


def test_update_and_deactivate_subject(tmp_path):
    db = _db(tmp_path)
    sid = db.add_subject("Alex Rivera")
    db.update_subject(sid, countries=["pt"], context="guitarist", is_demo=True)
    s = db.get_subject(sid)
    assert (s["countries"], s["context"], s["is_demo"]) == (["pt"], "guitarist", True)
    db.deactivate_subject(sid)
    assert [x["id"] for x in db.list_subjects()] == [1]
    assert [x["id"] for x in db.list_subjects(include_inactive=True)] == [1, sid]
    with pytest.raises(ValueError):
        db.deactivate_subject(DEFAULT_SUBJECT_ID)
    with pytest.raises(ValueError):
        db.update_subject(999, context="x")


# --------------------------------------------------------------------- #
# Tracking config: the subject row is the source of truth; subject 1 bridges
# the legacy settings keys both ways.
# --------------------------------------------------------------------- #
def test_tracked_getters_read_the_active_subject(tmp_path):
    db = _db(tmp_path)
    sid = db.add_subject("Alex Rivera", countries=["pt"], context="guitarist")
    db.set_setting("report_query", "Jordan Ashworth")
    scoped = db.for_subject(sid)
    assert scoped.get_tracked_query() == "Alex Rivera"
    assert scoped.get_tracked_countries() == ["pt"]
    assert scoped.get_subject_context() == "guitarist"
    assert db.get_tracked_query() == "Jordan Ashworth"
    with subject_scope(sid):
        assert db.get_tracked_query() == "Alex Rivera"


def test_subject_1_falls_back_to_legacy_settings_only_when_its_row_is_empty(tmp_path):
    db = _db(tmp_path)
    conn = db._connect()
    conn.execute("INSERT INTO settings (key, value) VALUES ('report_query', 'Raw Setting')")
    conn.commit()
    conn.close()
    # Row still empty (the raw INSERT bypassed the mirror) -> falls back.
    assert db.get_tracked_query() == "Raw Setting"
    assert db.get_tracked_countries() == ["us", "ca", "gb"]
    # A non-1 subject never falls back to the global settings.
    sid = db.add_subject("Other")
    assert db.for_subject(sid).get_tracked_countries() == []
    assert db.for_subject(sid).get_subject_context() == ""


def test_legacy_setting_writes_mirror_into_subject_1(tmp_path):
    db = _db(tmp_path)
    db.set_setting("report_query", "Jane Doe")
    db.set_setting("report_countries", "ES, fr")
    db.set_setting("subject_context", "CEO of Acme")
    s = db.get_subject(1)
    assert (s["name"], s["query"], s["countries"], s["context"]) == (
        "Jane Doe", "Jane Doe", ["es", "fr"], "CEO of Acme")
    # The name follows the query while it was equal to it...
    db.set_setting("report_query", "Jane A. Doe")
    assert db.get_subject(1)["name"] == "Jane A. Doe"
    # ...but a customized name is left alone.
    db.update_subject(1, name="Jane (client)")
    db.set_setting("report_query", "Jane Doe")
    assert db.get_subject(1)["name"] == "Jane (client)"
    assert db.get_subject(1)["query"] == "Jane Doe"


def test_subject_1_updates_mirror_into_legacy_settings(tmp_path):
    db = _db(tmp_path)
    db.set_tracked_subject(query="Jane Doe", countries=["de", "AT"], context="bio")
    assert db.get_setting("report_query") == "Jane Doe"
    assert db.get_setting("report_countries") == "de,at"
    assert db.get_setting("subject_context") == "bio"
    assert db.get_subject(1)["name"] == "Jane Doe"
    # Another subject's updates never touch the global settings.
    sid = db.add_subject("Other")
    db.for_subject(sid).set_tracked_subject(query="Other Q", countries=["mx"], context="x")
    assert db.get_setting("report_query") == "Jane Doe"
    assert db.get_subject(sid)["query"] == "Other Q"


# --------------------------------------------------------------------- #
# AI: subject name/context come from the active subject -> per-subject cache
# --------------------------------------------------------------------- #
def test_ai_subject_name_and_cache_version_are_per_subject(tmp_path, monkeypatch):
    db = _db(tmp_path)
    db.set_setting("report_query", "Jordan Ashworth")
    sid = db.add_subject("Alex Rivera")
    monkeypatch.setattr(ai_classifier, "_db", None)
    ai_classifier.set_cache_db(db)
    assert ai_classifier.subject_name() == "Jordan Ashworth"
    v1 = ai_classifier._subject_version("classify")
    with subject_scope(sid):
        assert ai_classifier.subject_name() == "Alex Rivera"
        v2 = ai_classifier._subject_version("classify")
    assert v1 != v2


def test_enrich_uses_the_active_subjects_context(tmp_path, monkeypatch):
    from processing.enrich import annotate_same_person

    db = _db(tmp_path)
    db.set_setting("subject_context", "Jordan bio")
    sid = db.add_subject("Alex Rivera", context="Alex bio")
    seen = []
    monkeypatch.setattr(ai_classifier, "is_same_person_batch",
                        lambda records, context="": seen.append(context) or [None] * len(records))
    annotate_same_person([{"url": "https://x.example/1"}], db)
    annotate_same_person([{"url": "https://x.example/2"}], db.for_subject(sid))
    with subject_scope(sid):
        annotate_same_person([{"url": "https://x.example/3"}], db)
    assert seen == ["Jordan bio", "Alex bio", "Alex bio"]


# --------------------------------------------------------------------- #
# Migrations on an OLD-shape database
# --------------------------------------------------------------------- #
def _old_shape_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE url_lifecycle (
            url_hash TEXT NOT NULL, url TEXT, country TEXT NOT NULL,
            engine TEXT DEFAULT 'google', first_seen TEXT, last_seen TEXT,
            best_position INTEGER, last_position INTEGER, classification TEXT,
            status TEXT DEFAULT 'active', gone_since TEXT, missed_runs INTEGER DEFAULT 0,
            same_person TEXT, PRIMARY KEY (url_hash, country, engine));
        CREATE TABLE positive_domains (domain TEXT PRIMARY KEY,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE negative_domains (domain TEXT PRIMARY KEY,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE subjects (id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL DEFAULT 1, name TEXT NOT NULL, aliases_json TEXT,
            type TEXT NOT NULL DEFAULT 'person', context TEXT, default_queries_json TEXT,
            default_countries_json TEXT, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT);
        CREATE TABLE api_keys (id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_hash TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            scopes TEXT NOT NULL DEFAULT 'read', created_at TEXT, last_used TEXT,
            revoked_at TEXT);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
    """)
    conn.execute("INSERT INTO url_lifecycle (url_hash, url, country, status, missed_runs) "
                 "VALUES ('h1', 'https://a.example/x', 'us', 'gone', 3)")
    conn.execute("INSERT INTO positive_domains (domain) VALUES ('own.example')")
    conn.execute("INSERT INTO negative_domains (domain) VALUES ('bad.example')")
    # Subject 1 seeded BEFORE the operator set the query (the demo DB's real shape).
    conn.execute("INSERT INTO subjects (id, name, aliases_json, context, "
                 "default_queries_json, default_countries_json) "
                 "VALUES (1, '', '[]', '', '[\"\"]', '[\"us\", \"ca\", \"gb\"]')")
    conn.execute("INSERT INTO settings VALUES ('report_query', 'Jordan Ashworth')")
    conn.execute("INSERT INTO settings VALUES ('report_countries', 'us')")
    conn.execute("INSERT INTO settings VALUES ('subject_context', 'Fictional CEO')")
    conn.execute("INSERT INTO api_keys (key_hash, name) VALUES ('k', 'legacy')")
    conn.commit()
    conn.close()


def test_migrations_upgrade_an_old_shape_db(tmp_path):
    path = str(tmp_path / "old.db")
    _old_shape_db(path)
    db = Database(db_path=path)

    conn = sqlite3.connect(path)
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name='url_lifecycle'").fetchone()[0]
    assert "PRIMARY KEY (url_hash, country, engine, subject_id)" in ddl
    for table in ("positive_domains", "negative_domains"):
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (table,)).fetchone()[0]
        assert "PRIMARY KEY (domain, subject_id)" in ddl
    cols = {r[1] for r in conn.execute("PRAGMA table_info(subjects)")}
    assert "is_demo" in cols
    cols = {r[1] for r in conn.execute("PRAGMA table_info(api_keys)")}
    assert "subject_id" in cols
    # Rows survive, owned by subject 1.
    assert conn.execute("SELECT status, missed_runs, subject_id FROM url_lifecycle "
                        "WHERE url_hash='h1'").fetchone() == ("gone", 3, 1)
    assert conn.execute("SELECT subject_id FROM api_keys").fetchone() == (None,)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%_pre_c21b2'"
                        ).fetchone()[0] == 0
    conn.close()
    assert db.list_positive_domains() == ["own.example"]
    assert db.list_negative_domains() == ["bad.example"]
    # Subject 1 backfilled from the legacy settings.
    s = db.get_subject(1)
    assert (s["name"], s["queries"], s["countries"], s["context"], s["is_demo"]) == (
        "Jordan Ashworth", ["Jordan Ashworth"], ["us"], "Fictional CEO", False)


def test_migrations_are_idempotent(tmp_path):
    path = str(tmp_path / "old.db")
    _old_shape_db(path)
    Database(db_path=path)
    db = Database(db_path=path)
    Database(db_path=path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM url_lifecycle").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM positive_domains").fetchone()[0] == 1
    conn.close()
    assert db.get_subject(1)["name"] == "Jordan Ashworth"


def test_subject_1_backfill_never_overwrites_values_someone_set(tmp_path):
    path = str(tmp_path / "t.db")
    db = Database(db_path=path)
    db.update_subject(1, name="Custom", countries=["mx"], context="mine",
                      queries=["Custom Q"])
    conn = sqlite3.connect(path)
    conn.execute("UPDATE settings SET value = 'Other' WHERE key = 'report_query'")
    conn.execute("UPDATE settings SET value = 'de' WHERE key = 'report_countries'")
    conn.commit()
    conn.close()
    db = Database(db_path=path)
    s = db.get_subject(1)
    assert (s["name"], s["query"], s["countries"], s["context"]) == (
        "Custom", "Custom Q", ["mx"], "mine")


def test_fresh_db_seed_has_no_blank_default_query(tmp_path):
    db = _db(tmp_path)
    conn = db._connect()
    raw = conn.execute("SELECT default_queries_json FROM subjects WHERE id = 1").fetchone()[0]
    conn.close()
    assert json.loads(raw) == []
