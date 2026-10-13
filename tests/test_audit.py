"""Tests for item C24: immutable audit log (INSERT only, hash chain).

Acceptance criteria (§C24):
"every write action from a test session appears with actor and
parameters; the hash chain verifies; there is no UPDATE on the table
in the code; filtering by resource reconstructs the complete history of
a URL."

TDD, no network: Database directly for the hash chain and sqlite3 by hand
ONLY to simulate external tampering with the row (never via the Database
API, which never updates audit_log -- see the static test below).
"""
import re
import sqlite3
from pathlib import Path

_MODELS_SRC = Path(__file__).resolve().parent.parent / "src" / "database" / "models.py"


from conftest import make_db as _db


# --------------------------------------------------------------------- #
# add_audit / hash chain
# --------------------------------------------------------------------- #
def test_audit_log_table_exists(tmp_path):
    db = _db(tmp_path)
    conn = db._connect()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(audit_log)").fetchall()}
    conn.close()
    assert {"id", "at", "actor_type", "actor_id", "action", "resource_type",
            "resource_id", "params_json", "ip", "row_hash"} <= cols


def test_add_audit_inserts_a_row_with_actor_and_params(tmp_path):
    db = _db(tmp_path)
    db.add_audit(actor_type="user", actor_id="alice", action="timeline_event_add",
                 resource_type="event", resource_id="", params={"pos_pct": 60}, ip="127.0.0.1")

    rows = db.list_audit()
    assert len(rows) == 1
    row = rows[0]
    assert row["actor_type"] == "user"
    assert row["actor_id"] == "alice"
    assert row["action"] == "timeline_event_add"
    assert '"pos_pct": 60' in row["params_json"]
    assert row["ip"] == "127.0.0.1"
    assert row["row_hash"]


def test_first_row_chains_from_empty_prev_hash(tmp_path):
    db = _db(tmp_path)
    db.add_audit(actor_type="system", actor_id="anonymous", action="a1")

    row = db.list_audit()[0]
    # The first row uses prev_hash="" -- reproducible with the same canonical repr.
    expected = db._audit_row_canonical(row)
    import hashlib
    assert row["row_hash"] == hashlib.sha256(("" + expected).encode("utf-8")).hexdigest()


def test_add_audit_chains_hashes_across_rows(tmp_path):
    db = _db(tmp_path)
    db.add_audit(actor_type="system", actor_id="anonymous", action="a1")
    db.add_audit(actor_type="system", actor_id="anonymous", action="a2")
    db.add_audit(actor_type="system", actor_id="anonymous", action="a3")

    rows = db.list_audit(limit=10)  # DESC: a3, a2, a1
    rows = list(reversed(rows))  # insertion order: a1, a2, a3
    assert len({r["row_hash"] for r in rows}) == 3  # all distinct
    assert db.verify_audit_chain() is True


def test_add_audit_serializes_concurrent_writes_with_begin_immediate(tmp_path):
    """FIX 1 (audit-race fix): in real use,
    verify_audit_chain() returned False against the real DB -- 30
    'job_run_started'/actor_type='job' rows didn't match their expected hash,
    each one with a SIBLING row from the SAME second (two jobs fired by
    APScheduler at the same instant). Cause: add_audit did a
    SELECT of prev_hash and then INSERT without opening the write
    transaction BEFORE the SELECT -- two threads could read the same prev_hash
    and the second one to commit ended up mis-chained.

    This test reproduces the race with 2 threads x N=20 writes each,
    started at the same time with a barrier. WITHOUT the fix (BEGIN IMMEDIATE
    before the SELECT) this fails reproducibly: verify_audit_chain()
    returns False and/or fewer than 3 distinct row_hash values appear because
    the same prev_hash collided. WITH the fix, BEGIN IMMEDIATE serializes
    read+write: the second thread waits (busy_timeout=5000) and reads the
    row_hash already inserted by the first."""
    import threading

    db = _db(tmp_path)
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker(tag: str) -> None:
        barrier.wait()
        for i in range(20):
            try:
                db.add_audit(actor_type="job", actor_id=tag, action="job_run_started",
                              resource_type="job", resource_id=f"{tag}-{i}")
            except Exception as exc:  # noqa: BLE001 -- collected for assert, not swallowed
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"t{n}",)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    rows = db.list_audit(limit=100)
    assert len(rows) == 40
    assert len({r["row_hash"] for r in rows}) == 40  # no prev_hash collisions
    assert db.verify_audit_chain() is True


def test_verify_audit_chain_true_on_empty_log(tmp_path):
    db = _db(tmp_path)
    assert db.verify_audit_chain() is True


def test_verify_audit_chain_detects_tampering(tmp_path):
    db = _db(tmp_path)
    db.add_audit(actor_type="user", actor_id="alice", action="alert_ack",
                 resource_type="alert", resource_id="1", params={"assignee": "alice"})
    db.add_audit(actor_type="user", actor_id="alice", action="alert_resolve",
                 resource_type="alert", resource_id="1")
    assert db.verify_audit_chain() is True

    # Direct tampering via sqlite3 (never through the Database
    # API -- add_audit/list_audit don't offer UPDATE).
    conn = sqlite3.connect(db.db_path)
    conn.execute("UPDATE audit_log SET action = 'alert_ack_TAMPERED' WHERE action = 'alert_ack'")
    conn.commit()
    conn.close()

    assert db.verify_audit_chain() is False


def test_audit_chain_report_ok_on_clean_chain(tmp_path):
    """FIX 2 (fix/audit-race): audit_chain_report gives the detail -- in a
    healthy chain, ok=True, checked==n rows, broken_ids empty, first_break
    None."""
    db = _db(tmp_path)
    db.add_audit(actor_type="system", actor_id="anonymous", action="a1")
    db.add_audit(actor_type="system", actor_id="anonymous", action="a2")
    db.add_audit(actor_type="system", actor_id="anonymous", action="a3")

    report = db.audit_chain_report()
    assert report == {"ok": True, "checked": 3, "broken_ids": [], "first_break": None}


def test_audit_chain_report_pinpoints_the_broken_row(tmp_path):
    """FIX 2: if ONE row was tampered with, the report says which one
    (broken_ids) and where the first break starts (first_break) -- not just
    a boolean. The rows AFTER the tampered one, which chain correctly again
    among themselves, must not show up as broken (same approach
    verify_audit_chain already had: it compares against the STORED row_hash
    of the previous row, not against the recalculated one)."""
    db = _db(tmp_path)
    db.add_audit(actor_type="user", actor_id="alice", action="alert_ack",
                 resource_type="alert", resource_id="1")
    db.add_audit(actor_type="user", actor_id="alice", action="alert_resolve",
                 resource_type="alert", resource_id="1")
    db.add_audit(actor_type="user", actor_id="alice", action="alert_ack",
                 resource_type="alert", resource_id="2")

    rows_before = db.list_audit(limit=10)
    tampered_id = next(r["id"] for r in rows_before if r["action"] == "alert_ack"
                        and r["resource_id"] == "1")

    conn = sqlite3.connect(db.db_path)
    conn.execute("UPDATE audit_log SET action = 'alert_ack_TAMPERED' WHERE id = ?",
                 (tampered_id,))
    conn.commit()
    conn.close()

    report = db.audit_chain_report()
    assert report["ok"] is False
    assert report["checked"] == 3
    assert report["broken_ids"] == [tampered_id]
    assert report["first_break"] == tampered_id
    assert db.verify_audit_chain() is False  # the boolean wrapper still works


def test_add_audit_never_stores_secret_values_when_caller_passes_field_marker(tmp_path):
    """Contract of the audit() helper in main.py: for actions on secrets
    (API keys, SMTP passwords...) the caller passes the field NAME,
    never the value. This test fixes the contract on the DB side: what
    reaches params is persisted as-is (the responsibility of not leaking
    the value is the caller's, verified in test_smoke_and_routes.py)."""
    db = _db(tmp_path)
    db.add_audit(actor_type="user", actor_id="alice", action="settings_apikey_save",
                 params={"field": "APIFY_API_KEY"})
    row = db.list_audit()[0]
    assert row["params_json"] == '{"field": "APIFY_API_KEY"}'


# --------------------------------------------------------------------- #
# list_audit: filters
# --------------------------------------------------------------------- #
def test_list_audit_filters_by_action(tmp_path):
    db = _db(tmp_path)
    db.add_audit(actor_type="system", actor_id="anonymous", action="note_save", resource_id="u1")
    db.add_audit(actor_type="system", actor_id="anonymous", action="timeline_event_add")

    rows = db.list_audit(action="timeline_event_add")
    assert len(rows) == 1
    assert rows[0]["action"] == "timeline_event_add"


def test_list_audit_filter_by_resource_id_reconstructs_url_history(tmp_path):
    """Acceptance: "filtering by resource reconstructs the complete history
    of a URL"."""
    db = _db(tmp_path)
    url = "https://example.com/about"
    db.add_audit(actor_type="user", actor_id="alice", action="note_save",
                 resource_type="url", resource_id=url, params={"status": "open"})
    db.add_audit(actor_type="system", actor_id="anonymous", action="timeline_event_add",
                 resource_type="event", resource_id="")
    db.add_audit(actor_type="user", actor_id="alice", action="note_save",
                 resource_type="url", resource_id=url, params={"status": "resolved"})

    history = db.list_audit(resource_id=url, limit=50)
    assert len(history) == 2
    assert all(r["resource_id"] == url for r in history)
    statuses = [__import__("json").loads(r["params_json"])["status"] for r in reversed(history)]
    assert statuses == ["open", "resolved"]


def test_list_audit_respects_limit(tmp_path):
    db = _db(tmp_path)
    for i in range(5):
        db.add_audit(actor_type="system", actor_id="anonymous", action=f"a{i}")
    assert len(db.list_audit(limit=2)) == 2


# --------------------------------------------------------------------- #
# Static test: no UPDATE/DELETE on audit_log anywhere in the source code
# --------------------------------------------------------------------- #
def test_no_update_or_delete_on_audit_log_anywhere_in_src():
    src_dir = _MODELS_SRC.parent.parent  # src/
    offenders = []
    pattern = re.compile(r"(UPDATE|DELETE\s+FROM)\s+audit_log", re.IGNORECASE)
    for path in src_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            offenders.append(str(path))
    assert offenders == []


# --------------------------------------------------------------------- #
# Route instrumentation (audit() helper in main.py + routers)
# --------------------------------------------------------------------- #
def test_write_action_shows_up_on_audit_page_with_actor_and_params(client, seeded_db):
    response = client.post("/timeline/add", data={"event_date": "2026-01-15",
                                                  "description": "Press release"})
    assert response.status_code == 200

    rows = seeded_db.list_audit(action="timeline_event_add")
    assert len(rows) == 1
    assert rows[0]["actor_type"] == "system"  # without AUTH_USER/PASS -> anonymous
    assert rows[0]["actor_id"] == "anonymous"
    assert '"description": "Press release"' in rows[0]["params_json"]

    page_resp = client.get("/audit")
    assert page_resp.status_code == 200
    assert "timeline_event_add" in page_resp.text


def test_login_ok_and_failed_are_audited(client, seeded_db, monkeypatch):
    monkeypatch.setenv("AUTH_USER", "alice")
    monkeypatch.setenv("AUTH_PASS", "s3cr3t")

    bad = client.post("/login", data={"username": "alice", "password": "wrong"}, follow_redirects=False)
    assert bad.status_code == 303
    good = client.post("/login", data={"username": "alice", "password": "s3cr3t"}, follow_redirects=False)
    assert good.status_code == 303

    actions = {r["action"]: r for r in seeded_db.list_audit(limit=20)}
    assert "login_failed" in actions
    assert "login_ok" in actions
    assert actions["login_failed"]["actor_id"] == "alice"
    assert actions["login_ok"]["actor_id"] == "alice"


def test_settings_apikey_save_never_stores_the_secret_value(client, seeded_db, monkeypatch):
    # admin.set_env (dashboard.admin) also writes to os.environ, not just
    # the .env file -- without this monkeypatch, the dummy key leaks into
    # the PROCESS's os.environ and contaminates any later test (same
    # alphabetical order) that depends on an empty APIFY_API_KEY for hermeticity.
    monkeypatch.setenv("APIFY_API_KEY", "")
    response = client.post("/settings/apikey", data={"apikey": "sk-super-secret-value"})
    assert response.status_code == 200

    rows = seeded_db.list_audit(action="settings_apikey_save")
    assert len(rows) == 1
    assert "sk-super-secret-value" not in rows[0]["params_json"]
    assert rows[0]["params_json"] == '{"field": "APIFY_API_KEY"}'


def test_notes_save_filter_by_resource_id_reconstructs_url_history(client, seeded_db):
    url = "https://jordanashworth.example/about"
    client.post("/notes/save", data={"url": url, "status": "open", "note": "first"})
    client.post("/notes/save", data={"url": url, "status": "resolved", "note": "second"})
    # Noise: another action on another resource must not mix into the filter.
    client.post("/timeline/add", data={"event_date": "2026-01-15", "description": "noise"})

    response = client.get(f"/audit?resource_id={url}")
    assert response.status_code == 200

    history = seeded_db.list_audit(resource_id=url, limit=50)
    assert len(history) == 2
    assert all(r["action"] == "note_save" for r in history)


def test_audit_page_shows_chain_intact_badge(client):
    response = client.get("/audit")
    assert response.status_code == 200
    assert "Chain intact" in response.text


def test_audit_page_shows_known_historical_break_as_warning_not_alarm(client, seeded_db):
    """FIX 3 (fix/audit-race): a break BEFORE the cutoff stored in
    settings (audit_chain_known_breaks_before_id) is a known historical
    incident -- caused by the FIX 1 concurrency bug, already fixed
    -- not a tampering alarm. The page must NOT say "BROKEN" for
    this row; it must show it as a known warning."""
    seeded_db.add_audit(actor_type="job", actor_id="scheduler", action="job_run_started")
    seeded_db.add_audit(actor_type="job", actor_id="scheduler", action="job_run_started")

    broken_id = seeded_db.audit_chain_report()["checked"]  # last inserted row = highest id
    conn = sqlite3.connect(seeded_db.db_path)
    conn.execute("UPDATE audit_log SET action = 'job_run_started_X' WHERE id = ?", (broken_id,))
    conn.commit()
    conn.close()
    seeded_db.set_setting("audit_chain_known_breaks_before_id", str(broken_id))

    response = client.get("/audit")
    assert response.status_code == 200
    assert "Chain BROKEN" not in response.text
    assert "known historical break" in response.text


def test_audit_page_alarms_on_a_new_break_past_the_known_cutoff(client, seeded_db):
    """FIX 3: a break AFTER the known cutoff must actually raise an alarm
    -- it is indistinguishable, with the available info, from real
    tampering."""
    seeded_db.add_audit(actor_type="user", actor_id="alice", action="alert_ack",
                         resource_type="alert", resource_id="1")
    seeded_db.set_setting("audit_chain_known_breaks_before_id", "0")

    rows = seeded_db.list_audit(limit=1)
    conn = sqlite3.connect(seeded_db.db_path)
    conn.execute("UPDATE audit_log SET action = 'alert_ack_TAMPERED' WHERE id = ?",
                 (rows[0]["id"],))
    conn.commit()
    conn.close()

    response = client.get("/audit")
    assert response.status_code == 200
    assert "Chain BROKEN" in response.text


def test_audit_export_csv_smoke(client, seeded_db):
    seeded_db.add_audit(actor_type="system", actor_id="anonymous", action="smoke_action")

    response = client.get("/audit/export.csv")

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "smoke_action" in response.text


def test_settings_tabbar_links_to_audit_page(client):
    # Navigation is one hierarchy: the sidebar links to the Settings section,
    # whose tab bar lists the Audit page.
    response = client.get("/settings")
    assert response.status_code == 200
    assert "href='#audit'" in response.text and "<section id='audit'" in response.text


def test_settings_apikey_writes_env_file_override_never_repo_env(client, monkeypatch, tmp_path):
    """Regression: this endpoint once clobbered the repo's REAL .env
    on every gate (the operator's APIFY_API_KEY was lost twice). The
    writer respects ENV_FILE — the suite never touches the real .env."""
    from pathlib import Path

    import dashboard.main as main_module

    override = tmp_path / "fake.env"
    monkeypatch.setenv("ENV_FILE", str(override))
    # admin.set_env (dashboard.admin) also writes to os.environ, not just
    # the .env override above -- without this monkeypatch, the dummy key
    # leaks into the PROCESS's os.environ and contaminates any later test
    # (same alphabetical order) that depends on an empty APIFY_API_KEY.
    monkeypatch.setenv("APIFY_API_KEY", "")
    real_env = Path(main_module.project_root) / ".env"
    before = real_env.read_bytes() if real_env.exists() else None

    resp = client.post("/settings/apikey", data={"apikey": "apify_api_dummy_for_test"})
    assert resp.status_code == 200
    assert "apify_api_dummy_for_test" in override.read_text()

    after = real_env.read_bytes() if real_env.exists() else None
    assert before == after  # the real .env stayed intact
