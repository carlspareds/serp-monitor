"""Tests for item C21a (deep dive §C21, first of three parts): the
`clients`/`subjects`/`users` tables, idempotent seed on boot, and auth of
real users against the table (with .env as a safety net).

TDD, no network or TestClient: direct Database against its own tmp_path
(NEVER conftest.seeded_db/client here -- we need fine-grained, explicit
control over AUTH_USER/AUTH_PASS in every test, see the docstring of
test_env_fallback_always_works_even_with_users_in_the_table below).

AUTH_USER/AUTH_PASS WARNING (see the item's final report): the repo's REAL
.env carries credentials and `apify.client.load_dotenv()` (run when the
app is imported, once per process) leaves them alive in `os.environ`
unless a test explicitly clears them -- that's why EVERY test in this
file that depends on AUTH_USER/AUTH_PASS's state sets or deletes them
with monkeypatch before building the Database, instead of assuming
they're empty."""
import json

import pytest

from auth.passwords import verify_password
from database.models import Database


def _db(tmp_path, name="t.db") -> str:
    return str(tmp_path / name)


# --------------------------------------------------------------------- #
# Tables + idempotent seed
# --------------------------------------------------------------------- #
def test_clients_subjects_users_tables_exist_with_expected_columns(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)
    db = Database(db_path=_db(tmp_path))
    conn = db._connect()
    client_cols = {r[1] for r in conn.execute("PRAGMA table_info(clients)").fetchall()}
    subject_cols = {r[1] for r in conn.execute("PRAGMA table_info(subjects)").fetchall()}
    user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    conn.close()
    assert {"id", "name", "branding_id", "active", "created_at"} <= client_cols
    assert {"id", "client_id", "name", "aliases_json", "type", "context",
            "default_queries_json", "default_countries_json", "active", "created_at"} <= subject_cols
    assert {"id", "client_id", "username", "password_hash", "role", "active",
            "created_at", "last_login_at"} <= user_cols


def test_seed_is_idempotent_constructing_the_database_twice_does_not_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_USER", "alice")
    monkeypatch.setenv("AUTH_PASS", "s3cr3t")
    path = _db(tmp_path)
    Database(db_path=path)
    Database(db_path=path)  # second "boot" against the same file

    conn = Database(db_path=path)._connect()
    assert conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    conn.close()


def test_seed_client_1_is_named_my_project(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)
    db = Database(db_path=_db(tmp_path))
    conn = db._connect()
    row = conn.execute("SELECT * FROM clients WHERE id = 1").fetchone()
    conn.close()
    assert row is not None
    assert row["name"] == "My project"
    assert row["active"] == 1


def test_subject_1_exists_so_existing_data_is_never_orphaned(tmp_path, monkeypatch):
    """Golden rule F2: EVERY data table already has subject_id DEFAULT 1 --
    if subjects didn't have a row with id=1, every existing row in
    search_results/runs/alerts/... would end up with an orphaned FK."""
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)
    db = Database(db_path=_db(tmp_path))
    conn = db._connect()
    row = conn.execute("SELECT * FROM subjects WHERE id = 1").fetchone()
    conn.close()
    assert row is not None
    assert row["client_id"] == 1
    assert row["active"] == 1


def test_subject_1_defaults_use_the_existing_report_settings_not_hardcoded(tmp_path, monkeypatch):
    """Subject 1 doesn't hardcode the name/country/context -- it takes them
    from the settings the rest of the repo ALREADY uses for the default
    query/country (report_query/report_countries/subject_context, see
    the database layer's subject-1 bridge). Simulates a real
    production DB: settings already populated by the operator BEFORE
    subjects exists (the seed runs in a later _init_db, on the SAME DB)."""
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)
    path = _db(tmp_path)
    db1 = Database(db_path=path)
    db1.set_setting("report_query", "Jane Doe")
    db1.set_setting("report_countries", "es,fr")
    db1.set_setting("subject_context", "CEO of Acme Corp")
    conn = db1._connect()
    conn.execute("DELETE FROM subjects")  # forces a re-seed on the next _init_db
    conn.commit()
    conn.close()

    db2 = Database(db_path=path)
    conn = db2._connect()
    row = conn.execute("SELECT * FROM subjects WHERE id = 1").fetchone()
    conn.close()
    assert row["name"] == "Jane Doe"
    assert row["context"] == "CEO of Acme Corp"
    assert json.loads(row["default_countries_json"]) == ["es", "fr"]


def test_subject_1_falls_back_to_default_query_and_countries_on_a_fresh_install(tmp_path, monkeypatch):
    """Regression: DEFAULT_SUBJECT_QUERY used to be the demo fixture's name
    ("Jordan Ashworth") baked in as a real fallback -- a genuinely fresh
    install (no seed script run) would silently pre-fill every search box
    and this row with a fictional person nobody configured. It's empty
    now; the wizard/Settings are the only things that ever set a real
    name, and the required `query` field on /search_page forces the
    operator to type one instead of accepting a hidden default."""
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)
    db = Database(db_path=_db(tmp_path))
    conn = db._connect()
    row = conn.execute("SELECT * FROM subjects WHERE id = 1").fetchone()
    conn.close()
    assert row["name"] == ""  # dashboard.main.DEFAULT_QUERY
    assert json.loads(row["default_countries_json"]) == ["us", "ca", "gb"]


def test_users_table_seeds_admin_from_auth_env_vars_when_table_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_USER", "opadmin")
    monkeypatch.setenv("AUTH_PASS", "hunter2-super-secret")
    db = Database(db_path=_db(tmp_path))
    conn = db._connect()
    row = conn.execute("SELECT * FROM users WHERE id = 1").fetchone()
    conn.close()
    assert row["username"] == "opadmin"
    assert row["role"] == "admin"
    assert row["client_id"] is None  # staff: ve todo
    assert row["active"] == 1
    assert verify_password("hunter2-super-secret", row["password_hash"]) is True


def test_users_table_stays_empty_without_auth_env_vars(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)
    db = Database(db_path=_db(tmp_path))
    conn = db._connect()
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    assert count == 0


def test_seed_never_overwrites_an_admin_username_or_password_already_edited_by_the_operator(tmp_path, monkeypatch):
    """The seed only acts if the table is EMPTY -- an operator who already
    created/edited their admin user (e.g. changed the username or
    password via /users) must not see that change overwritten on the
    process's next restart just because AUTH_USER/AUTH_PASS are still in
    .env."""
    monkeypatch.setenv("AUTH_USER", "opadmin")
    monkeypatch.setenv("AUTH_PASS", "first-password")
    path = _db(tmp_path)
    db1 = Database(db_path=path)
    db1.set_user_password(1, "changed-by-operator")

    Database(db_path=path)  # "restart" -- AUTH_USER/AUTH_PASS stay the same in the environment

    conn = db1._connect()
    row = conn.execute("SELECT * FROM users WHERE id = 1").fetchone()
    conn.close()
    assert verify_password("changed-by-operator", row["password_hash"]) is True
    assert verify_password("first-password", row["password_hash"]) is False


# --------------------------------------------------------------------- #
# User CRUD (for the /users router)
# --------------------------------------------------------------------- #
@pytest.fixture
def empty_db(tmp_path, monkeypatch) -> Database:
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)
    return Database(db_path=_db(tmp_path))


def test_add_user_then_list_users_never_exposes_password_hash(empty_db):
    user_id = empty_db.add_user("alice", "s3cret!", role="operator")
    rows = empty_db.list_users()
    assert len(rows) == 1
    assert rows[0]["id"] == user_id
    assert rows[0]["username"] == "alice"
    assert rows[0]["role"] == "operator"
    assert "password_hash" not in rows[0]


def test_add_user_rejects_duplicate_username(empty_db):
    empty_db.add_user("alice", "s3cret!", role="operator")
    with pytest.raises(Exception):  # noqa: B017 -- sqlite3's UNIQUE constraint, no wrapper of our own
        empty_db.add_user("alice", "another-pass", role="operator")


def test_set_user_active_toggles_and_deactivated_user_cannot_authenticate(empty_db):
    user_id = empty_db.add_user("bob", "correct-pass", role="operator")
    assert empty_db.authenticate_user("bob", "correct-pass") is not None

    empty_db.set_user_active(user_id, False)
    assert empty_db.authenticate_user("bob", "correct-pass") is None

    empty_db.set_user_active(user_id, True)
    assert empty_db.authenticate_user("bob", "correct-pass") is not None


def test_set_user_password_changes_which_password_authenticates(empty_db):
    user_id = empty_db.add_user("carol", "old-pass", role="operator")
    empty_db.set_user_password(user_id, "new-pass")
    assert empty_db.authenticate_user("carol", "old-pass") is None
    assert empty_db.authenticate_user("carol", "new-pass") is not None


def test_list_clients_includes_the_seeded_default_project(empty_db):
    clients = empty_db.list_clients()
    assert any(c["id"] == 1 and c["name"] == "My project" for c in clients)


def test_legacy_internal_project_is_renamed_once_and_custom_names_are_kept(tmp_path, monkeypatch):
    """Project 1 used to be seeded as "Internal": the startup migration renames
    it to "My project" ONLY while it is still exactly that legacy default, and
    re-running it is a no-op. A name the operator chose is never touched."""
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)
    path = _db(tmp_path)
    db = Database(db_path=path)
    with db._connect() as conn:
        conn.execute("UPDATE clients SET name = 'Internal' WHERE id = 1")
        conn.commit()
    db = Database(db_path=path)
    assert db.get_client(1)["name"] == "My project"
    db = Database(db_path=path)
    assert db.get_client(1)["name"] == "My project"

    db.rename_client(1, "Acme Corp")
    db = Database(db_path=path)
    assert db.get_client(1)["name"] == "Acme Corp"


# --------------------------------------------------------------------- #
# authenticate_user (auth against the table)
# --------------------------------------------------------------------- #
def test_authenticate_user_ok_with_correct_password(empty_db):
    empty_db.add_user("dave", "right-password", role="admin")
    result = empty_db.authenticate_user("dave", "right-password")
    assert result is not None
    assert result["username"] == "dave"
    assert result["role"] == "admin"
    assert "password_hash" not in result


def test_authenticate_user_rejects_wrong_password(empty_db):
    empty_db.add_user("dave", "right-password", role="admin")
    assert empty_db.authenticate_user("dave", "wrong-password") is None


def test_authenticate_user_rejects_nonexistent_username(empty_db):
    assert empty_db.authenticate_user("ghost", "whatever") is None


def test_authenticate_user_updates_last_login_only_on_success(empty_db):
    user_id = empty_db.add_user("erin", "right-password", role="operator")
    empty_db.authenticate_user("erin", "wrong-password")
    conn = empty_db._connect()
    row = conn.execute("SELECT last_login_at FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    assert row["last_login_at"] is None

    empty_db.authenticate_user("erin", "right-password")
    conn = empty_db._connect()
    row = conn.execute("SELECT last_login_at FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    assert row["last_login_at"] is not None


# --------------------------------------------------------------------- #
# .env as a safety net (critical -- brief C21a §4)
# --------------------------------------------------------------------- #
def test_env_fallback_always_works_even_with_users_in_the_table(tmp_path, monkeypatch):
    """CRITICAL (brief): AUTH_USER/AUTH_PASS must keep opening the door
    even when the `users` table already has real users -- it's the safety
    net so the operator is never locked out of their own system.
    authenticate_user() (against the table) is a piece separate from the
    .env fallback, which lives in the /login route (dashboard.main) --
    this test pins down authenticate_user()'s contract: a table user that
    is NOT the .env one must not interfere with that check, both pieces
    coexist."""
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    db = Database(db_path=_db(tmp_path))  # seeds admin id=1 = envadmin
    db.add_user("someoneelse", "another-pass", role="operator")

    # the .env user still authenticates against the table (it was seeded there)...
    assert db.authenticate_user("envadmin", "env-password") is not None
    # ...and the .env fallback itself (literal comparison, in dashboard.main.
    # login_submit) is a layer INDEPENDENT of the table -- exercised in
    # tests/test_login.py via TestClient, not here (there's no HTTP
    # Request/session in this file).


def test_ensure_admin_seed_seeds_on_demand_when_auth_env_added_after_first_boot(tmp_path, monkeypatch):
    """Covers the brief's case: the table was empty WITHOUT AUTH_USER/
    AUTH_PASS on the first boot (nobody seeded anything), and they're
    then defined in the environment -- ensure_admin_seed() (called from
    login, brief §4) must seed the admin RIGHT THEN, without waiting for
    a process restart."""
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)
    db = Database(db_path=_db(tmp_path))
    conn = db._connect()
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    conn.close()

    monkeypatch.setenv("AUTH_USER", "lateadmin")
    monkeypatch.setenv("AUTH_PASS", "late-password")
    seeded = db.ensure_admin_seed()
    assert seeded is not None
    assert seeded["username"] == "lateadmin"
    assert seeded["role"] == "admin"
    assert db.authenticate_user("lateadmin", "late-password") is not None


def test_ensure_admin_seed_is_a_noop_without_auth_env_vars(empty_db):
    assert empty_db.ensure_admin_seed() is None
    conn = empty_db._connect()
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    conn.close()


def test_ensure_admin_seed_returns_none_when_table_has_users_but_none_matches_auth_user(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    db = Database(db_path=_db(tmp_path))  # seeds envadmin at boot
    # Changes AUTH_USER to something that does NOT exist in the table
    # (scenario: .env is edited after real users already existed, none of
    # them named the same) -- ensure_admin_seed must NOT seed a second
    # admin (the table is no longer empty) nor invent a row.
    monkeypatch.setenv("AUTH_USER", "someoneelse")
    assert db.ensure_admin_seed() is None
