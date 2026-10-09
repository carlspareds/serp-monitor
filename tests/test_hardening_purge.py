"""Retention purges: scheduler/jobs.py::purge_checks + the Database.purge_*
helpers it calls. audit_log and search_results are deliberately excluded
(see the docstring of purge_checks) -- they are the historical record, not
transient operational data.

Each test inserts one OLD row (past retention) and one RECENT row (inside
retention) via direct SQL (the public helpers don't let us control the
timestamp) and confirms the purge deletes only the old one.
"""
from datetime import UTC, datetime, timedelta

from conftest import make_db as _db

from database.models import url_hash


def _iso(dt) -> str:
    return dt.isoformat(timespec="seconds")


def _old(days=400):
    return _iso(datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days))


def _recent(days=1):
    return _iso(datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days))


# --------------------------------------------------------------------- #
# notify_log (180d default)
# --------------------------------------------------------------------- #
def test_purge_notify_log_removes_only_old_rows(tmp_path):
    db = _db(tmp_path)
    conn = db._connect()
    cur = conn.cursor()
    cur.execute("INSERT INTO notify_log (channel, ok, at, error) VALUES ('email', 1, ?, '')",
                (_old(200),))
    cur.execute("INSERT INTO notify_log (channel, ok, at, error) VALUES ('email', 1, ?, '')",
                (_recent(),))
    conn.commit()
    conn.close()

    deleted = db.purge_notify_log()

    assert deleted == 1
    conn = db._connect()
    assert conn.execute("SELECT COUNT(*) FROM notify_log").fetchone()[0] == 1
    conn.close()


# --------------------------------------------------------------------- #
# resolved alerts (180d default) -- open/acknowledged are never touched
# --------------------------------------------------------------------- #
def test_purge_resolved_alerts_removes_only_old_resolved(tmp_path):
    db = _db(tmp_path)
    old_id = db.add_alert("threshold", "critical", "us", "old resolved", "dk-old")
    recent_id = db.add_alert("threshold", "critical", "us", "recent resolved", "dk-recent")
    open_id = db.add_alert("threshold", "critical", "us", "still open", "dk-open")
    db.resolve_alert(old_id)
    db.resolve_alert(recent_id)
    conn = db._connect()
    cur = conn.cursor()
    cur.execute("UPDATE alerts SET resolved_at = ? WHERE id = ?", (_old(200), old_id))
    cur.execute("UPDATE alerts SET resolved_at = ? WHERE id = ?", (_recent(), recent_id))
    conn.commit()
    conn.close()

    deleted = db.purge_resolved_alerts()

    assert deleted == 1
    remaining = {a["id"] for a in db.list_alerts(limit=20, status="all")}
    assert remaining == {recent_id, open_id}


# --------------------------------------------------------------------- #
# evidence (365d default) -- rows AND the vault files on disk
# --------------------------------------------------------------------- #
def test_purge_evidence_removes_old_rows_and_deletes_vault_file(tmp_path):
    from processing.evidence import evidence_dir

    db = _db(tmp_path)
    vault = evidence_dir(db)
    vault.mkdir(parents=True, exist_ok=True)
    old_file = vault / "old.html"
    old_file.write_bytes(b"<html>old</html>")
    recent_file = vault / "recent.html"
    recent_file.write_bytes(b"<html>recent</html>")

    old_id = db.add_evidence(url="https://old.example.com/", url_hash=url_hash("https://old.example.com/"),
                             captured_at=_old(400), html_path=str(old_file),
                             sha256_html="a" * 64, trigger="manual")
    recent_id = db.add_evidence(url="https://recent.example.com/",
                                url_hash=url_hash("https://recent.example.com/"),
                                captured_at=_recent(), html_path=str(recent_file),
                                sha256_html="b" * 64, trigger="manual")

    deleted = db.purge_evidence()

    assert deleted == 1
    assert db.get_evidence_by_id(old_id) is None
    assert db.get_evidence_by_id(recent_id) is not None
    assert not old_file.exists()
    assert recent_file.exists()


def test_purge_evidence_missing_vault_file_does_not_block_row_purge(tmp_path):
    db = _db(tmp_path)
    old_id = db.add_evidence(url="https://old2.example.com/",
                             url_hash=url_hash("https://old2.example.com/"),
                             captured_at=_old(400), html_path="/no/such/path/gone.html",
                             sha256_html="c" * 64, trigger="manual")

    deleted = db.purge_evidence()

    assert deleted == 1
    assert db.get_evidence_by_id(old_id) is None


# --------------------------------------------------------------------- #
# url_lifecycle -- ONLY status='gone', by gone_since; active/fading rows
# are NEVER touched no matter how old
# --------------------------------------------------------------------- #
def test_purge_gone_url_lifecycle_removes_only_old_gone_rows(tmp_path):
    db = _db(tmp_path)
    conn = db._connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO url_lifecycle (url_hash, url, country, engine, first_seen, last_seen, "
        "status, gone_since) VALUES (?, 'https://gone-old.example.com/', 'us', 'google', ?, ?, "
        "'gone', ?)",
        (url_hash("https://gone-old.example.com/"), _old(400), _old(400), _old(400)),
    )
    cur.execute(
        "INSERT INTO url_lifecycle (url_hash, url, country, engine, first_seen, last_seen, "
        "status, gone_since) VALUES (?, 'https://gone-recent.example.com/', 'us', 'google', ?, ?, "
        "'gone', ?)",
        (url_hash("https://gone-recent.example.com/"), _old(400), _recent(), _recent()),
    )
    cur.execute(
        "INSERT INTO url_lifecycle (url_hash, url, country, engine, first_seen, last_seen, "
        "status, gone_since) VALUES (?, 'https://still-active.example.com/', 'us', 'google', ?, "
        "?, 'active', NULL)",
        (url_hash("https://still-active.example.com/"), _old(400), _old(400)),
    )
    conn.commit()
    conn.close()

    deleted = db.purge_gone_url_lifecycle()

    assert deleted == 1
    conn = db._connect()
    remaining = {r[0] for r in conn.execute("SELECT url FROM url_lifecycle").fetchall()}
    conn.close()
    assert remaining == {"https://gone-recent.example.com/", "https://still-active.example.com/"}


# --------------------------------------------------------------------- #
# audit_log and search_results stay out of the purge
# --------------------------------------------------------------------- #
def test_purge_checks_never_touches_audit_log(tmp_path):
    from scheduler.jobs import purge_checks

    db = _db(tmp_path)
    for i in range(5):
        db.add_audit("system", "test", f"action-{i}")
    before = db.list_audit(limit=100)

    purge_checks(db)

    after = db.list_audit(limit=100)
    assert len(after) == len(before) == 5
    assert db.verify_audit_chain() is True


def test_database_has_no_purge_method_for_search_results(tmp_path):
    """search_results is the historical record -- it is never purged.
    Regression guard: catches an accidental purge_search_results being added."""
    from database.models import Database
    assert not hasattr(Database, "purge_search_results")


# --------------------------------------------------------------------- #
# scheduler/jobs.py::purge_checks -- retention configurable per setting
# --------------------------------------------------------------------- #
def test_purge_checks_honors_custom_retention_setting(tmp_path):
    from scheduler.jobs import purge_checks

    db = _db(tmp_path)
    db.set_setting("PURGE_NOTIFY_LOG_DAYS", "10")  # more aggressive than the default (180)
    conn = db._connect()
    cur = conn.cursor()
    cur.execute("INSERT INTO notify_log (channel, ok, at, error) VALUES ('email', 1, ?, '')",
                (_old(30),))  # 30d: would survive the default (180d) but not the setting (10d)
    conn.commit()
    conn.close()

    result = purge_checks(db)

    assert result["notify_log"] == 1


def test_purge_checks_invalid_setting_falls_back_to_default(tmp_path):
    from scheduler.jobs import purge_checks

    db = _db(tmp_path)
    db.set_setting("PURGE_NOTIFY_LOG_DAYS", "not-a-number")
    conn = db._connect()
    cur = conn.cursor()
    cur.execute("INSERT INTO notify_log (channel, ok, at, error) VALUES ('email', 1, ?, '')",
                (_recent(),))
    conn.commit()
    conn.close()

    result = purge_checks(db)

    assert result["notify_log"] == 0  # recent row survives the default (180d)


def test_purge_checks_returns_counts_for_every_purge(tmp_path):
    from scheduler.jobs import purge_checks

    db = _db(tmp_path)
    result = purge_checks(db)

    expected_keys = {"notify_log", "alerts_resolved", "evidence", "url_lifecycle_gone"}
    assert expected_keys <= set(result)
