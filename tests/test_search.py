"""Tests for item 3.3: PRAGMAs (WAL), indexes, FTS5, and global search.

TDD: these tests are written before touching src/database/models.py.
They use the temporary DB (db_path/seeded_db fixtures from conftest.py),
never data/seo_dashboard.db.
"""
from database.models import Database


# --------------------------------------------------------------------- #
# Opening PRAGMAs
# --------------------------------------------------------------------- #
def test_new_db_opens_in_wal_mode(db_path):
    database = Database(db_path=db_path)
    conn = database._connect()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"


def test_new_db_has_busy_timeout(db_path):
    database = Database(db_path=db_path)
    conn = database._connect()
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    conn.close()
    assert timeout == 5000


def test_new_db_has_synchronous_normal(db_path):
    database = Database(db_path=db_path)
    conn = database._connect()
    # NORMAL == 1 (see SQLite's PRAGMA synchronous documentation)
    sync = conn.execute("PRAGMA synchronous").fetchone()[0]
    conn.close()
    assert sync == 1


# --------------------------------------------------------------------- #
# Indexes
# --------------------------------------------------------------------- #
def _index_names(conn):
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    return {r[0] for r in rows}


def test_expected_indexes_exist(seeded_db):
    conn = seeded_db._connect()
    names = _index_names(conn)
    conn.close()
    expected = {
        "idx_search_results_run_id",
        "idx_search_results_country_scraped",
        "idx_search_results_classification",
        "idx_alerts_created_seen",
    }
    assert expected <= names
    # alerts(dedupe_key) is no longer covered by a column UNIQUE's
    # autoindex: since C21b-2 the UNIQUE is (dedupe_key, subject_id) and
    # lives as its own index, so two subjects can have the same dedupe
    # key without the second one being silently discarded. Since
    # dedupe_key is the composite index's first column, dedupe_key
    # lookups still use it.
    assert "idx_alerts_dedupe_subject" in names


def test_query_plan_uses_index_on_run_id(seeded_db):
    conn = seeded_db._connect()
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM search_results WHERE run_id = ?",
        ("run-test-1",),
    ).fetchall()
    conn.close()
    plan_text = " ".join(str(row["detail"]) for row in plan).lower()
    assert "search_results" in plan_text
    assert "using index" in plan_text or "using covering index" in plan_text


def test_query_plan_uses_index_on_country_and_scraped_at(seeded_db):
    conn = seeded_db._connect()
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM search_results "
        "WHERE country_code = ? AND scraped_at > ?",
        ("us", "2000-01-01"),
    ).fetchall()
    conn.close()
    plan_text = " ".join(str(row["detail"]) for row in plan).lower()
    assert "using index" in plan_text or "using covering index" in plan_text


def test_query_plan_uses_index_on_classification(seeded_db):
    conn = seeded_db._connect()
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM search_results WHERE classification = ?",
        ("negative",),
    ).fetchall()
    conn.close()
    plan_text = " ".join(str(row["detail"]) for row in plan).lower()
    assert "using index" in plan_text or "using covering index" in plan_text


# --------------------------------------------------------------------- #
# FTS5 / search_text
# --------------------------------------------------------------------- #
def test_backfill_populates_fts_for_preexisting_rows(seeded_db, db_path):
    """BACKFILL path (no triggers): a DB with historical rows but no
    populated FTS index (the case of migrating production to 3.3) must
    become searchable when Database is built. Bug regression: COUNT(*) on
    an external-content FTS reflects the content table, not the index --
    the guard must look at the results_fts_data shadow table and rebuild
    with 'rebuild'.
    """
    import sqlite3

    from database.models import Database

    # Simulate a pre-3.3 DB: remove FTS and triggers, leaving the historical rows.
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TRIGGER results_fts_ai")
    conn.execute("DROP TRIGGER results_fts_ad")
    conn.execute("DROP TRIGGER results_fts_au")
    conn.execute("DROP TABLE results_fts")
    conn.commit()
    conn.close()

    migrated = Database(db_path)  # _ensure_fts creates the FTS and must rebuild
    out = migrated.search_text("Fraud")
    assert out["error"] is None
    assert any(r["url"] == "https://scam.example/article" for r in out["results"])

    # Idempotency: rebuilding again doesn't duplicate or break anything.
    again = Database(db_path).search_text("Fraud")
    assert len(again["results"]) == len(out["results"])


def test_search_text_finds_by_title_word(seeded_db):
    out = seeded_db.search_text("Fraud")
    assert out["error"] is None
    urls = {r["url"] for r in out["results"]}
    assert "https://scam.example/article" in urls


def test_search_text_finds_by_description_word(seeded_db):
    out = seeded_db.search_text("personal")
    assert out["error"] is None
    urls = {r["url"] for r in out["results"]}
    assert "https://jordan-ashworth.example/blog" in urls


def test_search_text_no_match_returns_empty(seeded_db):
    out = seeded_db.search_text("zzz_nonexistent_zzz")
    assert out["error"] is None
    assert out["results"] == []


def test_search_text_empty_query_returns_empty(seeded_db):
    out = seeded_db.search_text("")
    assert out["results"] == []


def test_search_text_insert_trigger_makes_new_row_searchable(seeded_db):
    seeded_db.save_results(
        [{
            "query": "q", "url": "https://new.example/x",
            "title": "Ferrari testarossa unique token", "description": "desc",
            "displayed_url": "new.example", "position": 1,
            "country_code": "us", "domain": "new.example",
            "classification": "neutral",
        }],
        run_id="run-test-1",
    )
    out = seeded_db.search_text("Ferrari")
    urls = {r["url"] for r in out["results"]}
    assert "https://new.example/x" in urls


def test_search_text_update_trigger_reflects_title_change(seeded_db):
    conn = seeded_db._connect()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT id FROM search_results WHERE url = ?",
        ("https://directory.example/entry",),
    ).fetchone()
    row_id = row[0]
    cur.execute(
        "UPDATE search_results SET title = ? WHERE id = ?",
        ("Zebrawood unique renamed title", row_id),
    )
    conn.commit()
    conn.close()

    out_new = seeded_db.search_text("Zebrawood")
    assert any(r["id"] == row_id for r in out_new["results"])

    out_old = seeded_db.search_text("profile")
    assert all(r["id"] != row_id for r in out_old["results"])


def test_search_text_delete_trigger_removes_from_index(seeded_db):
    conn = seeded_db._connect()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT id FROM search_results WHERE url = ?",
        ("https://scam.example/article",),
    ).fetchone()
    row_id = row[0]
    cur.execute("DELETE FROM search_results WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()

    out = seeded_db.search_text("Fraud")
    assert all(r["id"] != row_id for r in out["results"])


def test_search_text_broken_fts_syntax_does_not_raise(seeded_db):
    # Stray quote / broken operator: must not raise sqlite3.OperationalError.
    out = seeded_db.search_text('"unterminated')
    assert isinstance(out["results"], list)

    out2 = seeded_db.search_text("AND OR (")
    assert isinstance(out2["results"], list)


# --------------------------------------------------------------------- #
# /search-text page
# --------------------------------------------------------------------- #
def test_search_page_smoke(client):
    response = client.get("/search-text")
    assert response.status_code == 200


def test_search_page_groups_results_by_classification(client):
    response = client.get("/search-text", params={"q": "Jordan"})
    assert response.status_code == 200
    assert "jordanashworth.example" in response.text


# --------------------------------------------------------------------- #
# /job/{id} error state
# --------------------------------------------------------------------- #
def test_failed_search_page_drops_in_flight_progress(client):
    import dashboard.main as main_module
    jobs = main_module.jobs
    jid = jobs.create("single", {"query": "Jordan Ashworth", "country": "us"})
    jobs.add_progress(jid, "Searching in us…")
    jobs.add_progress(jid, "bing: not configured (skipped)")
    jobs.fail(jid, "No Apify API key configured.")

    text = client.get(f"/job/{jid}").text
    assert "The search failed" in text and "No Apify API key configured." in text
    assert "Searching in" not in text
    assert "bing: not configured (skipped)" in text
