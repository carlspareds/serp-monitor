"""Tests for login with real users (C21a, deep dive §C21, part 1 of 3).

What's being pinned down here is the contract of the TWO entry paths and,
above all, that they coexist: the `users` table is the real login as of
this item, and AUTH_USER/AUTH_PASS from .env still open the door as a SAFETY
NET. That second path isn't technical debt pending removal: it's what
prevents an error in the table (a row deactivated by mistake, a
lost password, a half-finished migration) from locking the operator out of a
system only reachable over a private network/VPN.

`client` (conftest) clears AUTH_USER/AUTH_PASS and reloads the app, so
each test that needs auth enabled sets them with monkeypatch -- `auth_
enabled()` reads them live on every request, not at import time."""
from database.models import Database


def _login(client, username, password):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)


def test_a_real_user_from_the_table_can_log_in(client, seeded_db, monkeypatch):
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("alice", "alice-password", role="operator")

    resp = _login(client, "alice", "alice-password")

    assert resp.status_code == 303
    assert resp.cookies.get("sid")


def test_env_credentials_still_work_as_a_safety_net(client, seeded_db, monkeypatch):
    """CRITICAL: even if the table has real users, the .env pair
    still gets in. Never lock the operator out of their own system."""
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("alice", "alice-password", role="operator")

    resp = _login(client, "envadmin", "env-password")

    assert resp.status_code == 303
    assert resp.cookies.get("sid")


def test_wrong_password_is_rejected_by_both_paths(client, seeded_db, monkeypatch):
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("alice", "alice-password", role="operator")

    assert _login(client, "alice", "not-her-password").status_code == 303
    assert not _login(client, "alice", "not-her-password").cookies.get("sid")
    assert not _login(client, "ghost", "whatever").cookies.get("sid")
    # and the .env password doesn't open another user's account
    assert not _login(client, "alice", "env-password").cookies.get("sid")


def test_a_deactivated_user_cannot_log_in(client, seeded_db, monkeypatch):
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    user_id = seeded_db.add_user("bob", "bob-password", role="operator")
    seeded_db.set_user_active(user_id, False)

    assert not _login(client, "bob", "bob-password").cookies.get("sid")


def test_the_audit_log_records_who_actually_acted_not_the_env_user(client, seeded_db, monkeypatch):
    """The reason _SESSIONS stores the username and not just the token:
    with two different users, the audit_log (C24) has to be able to
    distinguish them. Before C21a everything was attributed to AUTH_USER, which was
    correct by coincidence because there could only be one identity."""
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("alice", "alice-password", role="operator")
    resp = _login(client, "alice", "alice-password")
    client.cookies.set("sid", resp.cookies["sid"])

    client.post("/timeline/add", data={"event_date": "2026-01-15", "description": "x"},
                follow_redirects=False)

    rows = [r for r in seeded_db.list_audit(limit=20) if r["action"] == "timeline_event_add"]
    assert rows, "the action was not audited"
    assert rows[0]["actor_id"] == "alice"     # not 'envadmin'
    assert rows[0]["actor_type"] == "user"


def test_logout_invalidates_only_its_own_session(client, seeded_db, monkeypatch):
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("alice", "alice-password", role="operator")
    alice = _login(client, "alice", "alice-password").cookies["sid"]
    envadmin = _login(client, "envadmin", "env-password").cookies["sid"]

    client.cookies.set("sid", alice)
    client.get("/logout", follow_redirects=False)

    from dashboard import main as dashboard_main
    assert alice not in dashboard_main._SESSIONS
    assert dashboard_main._SESSIONS[envadmin]["username"] == "envadmin"


def test_login_seeds_the_admin_when_env_vars_appear_after_first_boot(tmp_path, monkeypatch):
    """The login's on-demand seeding: without AUTH_USER/AUTH_PASS at the first
    boot the table stays empty; if they're added later, logging in shouldn't
    require restarting the process."""
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)
    path = str(tmp_path / "late.db")
    db = Database(db_path=path)
    assert db.list_users() == []

    monkeypatch.setenv("AUTH_USER", "lateadmin")
    monkeypatch.setenv("AUTH_PASS", "late-password")
    db.ensure_admin_seed()

    assert [u["username"] for u in db.list_users()] == ["lateadmin"]
    assert db.authenticate_user("lateadmin", "late-password") is not None


# --------------------------------------------------------------------- #
# /users page: the system's privilege boundary (whoever can create
# accounts can grant themselves access to everything)
# --------------------------------------------------------------------- #
def _as(client, seeded_db, username, password, role="operator"):
    seeded_db.add_user(username, password, role=role)
    resp = _login(client, username, password)
    client.cookies.set("sid", resp.cookies["sid"])
    return resp


def test_users_page_is_only_for_admins(client, seeded_db, monkeypatch):
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    _as(client, seeded_db, "alice", "alice-password", role="operator")

    resp = client.get("/users", follow_redirects=False)

    # Same redirect as without a session: confirming "it exists but isn't for you"
    # only helps someone who's probing.
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_an_admin_sees_the_users_page_and_can_create_a_user(client, seeded_db, monkeypatch):
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    _as(client, seeded_db, "root", "root-password", role="admin")

    page = client.get("/users")
    assert page.status_code == 200
    assert "root" in page.text

    client.post("/users/add", data={"username": "newbie", "password": "newbie-pass",
                                    "role": "operator"},
                follow_redirects=False)

    assert seeded_db.authenticate_user("newbie", "newbie-pass") is not None
    assert [r for r in seeded_db.list_audit(limit=20) if r["action"] == "user_add"]


def test_users_page_reachable_when_auth_is_disabled(client, seeded_db):
    """Regression: on a fresh install (no AUTH_USER/AUTH_PASS, no active user
    row -- auth_enabled() False), /users used to redirect to "/" no matter
    who asked, because session_role() has no session to read a role from and
    always returned "". That made it impossible to ever reach the one page
    that lets an operator create their first real user. auth_middleware
    already fail-opens the rest of the app in this exact state; _require_admin
    must agree with it instead of silently bouncing this one page."""
    page = client.get("/users", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/settings#users"
    page = client.get("/settings")
    assert "/ui/settings/user/new" in page.text and "No accounts yet." in page.text
    dialog = client.get("/ui/settings/user/new", headers={"X-SM-Fragment": "1"})
    assert "action='/users/add'" in dialog.text


def test_first_user_signs_the_creator_in(client, seeded_db):
    """With auth off, creating the first account turns sign-in on; the
    creator must not be locked out by the very next request."""
    r = client.post("/users/add", data={"username": "first", "password": "first-pass",
                                        "role": "admin"}, follow_redirects=False)
    assert r.status_code == 303 and "sid" in r.cookies
    client.cookies.set("sid", r.cookies["sid"])
    page = client.get("/settings", follow_redirects=False)
    assert page.status_code == 200 and "signed in as this account" in page.text


def test_first_user_must_be_an_admin(client, seeded_db):
    r = client.post("/users/add", data={"username": "op", "password": "op-pass",
                                        "role": "operator"}, follow_redirects=False)
    assert "sid" not in r.cookies
    assert seeded_db.get_user_by_username("op") is None


def test_the_users_page_never_renders_a_password_hash(client, seeded_db, monkeypatch):
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    _as(client, seeded_db, "root", "root-password", role="admin")

    page = client.get("/users")

    assert "pbkdf2_sha256$" not in page.text


def test_an_admin_cannot_deactivate_their_own_account(client, seeded_db, monkeypatch):
    """The one page error that's closed off from outside: it would leave the
    system with no one to administer users, and the rescue would be editing the
    DB by hand."""
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    _as(client, seeded_db, "root", "root-password", role="admin")
    me = seeded_db.get_user_by_username("root")

    resp = client.post("/users/toggle", data={"user_id": str(me["id"]), "active": "0"})

    assert resp.status_code == 200
    assert "cannot deactivate your own account" in resp.text
    assert seeded_db.get_user_by_username("root")["active"] == 1


def test_a_viewer_is_attached_to_the_default_client(client, seeded_db, monkeypatch):
    """No Client field any more: a viewer (role value client_viewer, kept
    as-is in the DB) gets the install's default client automatically;
    admins/operators stay staff (no client)."""
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    _as(client, seeded_db, "root", "root-password", role="admin")

    client.post("/users/add", data={"username": "viewer", "password": "viewer-pass",
                                    "role": "client_viewer"}, follow_redirects=False)
    client.post("/users/add", data={"username": "op", "password": "op-pass",
                                    "role": "operator"}, follow_redirects=False)

    viewer = seeded_db.get_user_by_username("viewer")
    assert viewer["role"] == "client_viewer"
    assert viewer["client_id"] == seeded_db.list_clients()[0]["id"]
    assert seeded_db.get_user_by_username("op")["client_id"] is None


def test_users_page_shows_viewer_label_and_no_client_column(client, seeded_db, monkeypatch):
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("viewer", "viewer-pass", role="client_viewer",
                       client_id=seeded_db.list_clients()[0]["id"])
    _as(client, seeded_db, "root", "root-password", role="admin")

    text = client.get("/users").text
    dialog = client.get("/ui/settings/user/new", headers={"X-SM-Fragment": "1"}).text

    assert "Viewer (read-only)" in text
    assert "name='role' value='client_viewer'" in dialog and "Viewer (read-only)" in dialog
    assert "<th>Client</th>" not in text and "name='client_id'" not in text


def test_deactivating_a_user_revokes_their_ability_to_log_in(client, seeded_db, monkeypatch):
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("alice", "alice-password", role="operator")
    _as(client, seeded_db, "root", "root-password", role="admin")
    alice = seeded_db.get_user_by_username("alice")

    client.post("/users/toggle", data={"user_id": str(alice["id"]), "active": "0"},
                follow_redirects=False)

    client.cookies.clear()
    assert not _login(client, "alice", "alice-password").cookies.get("sid")


def test_deactivating_a_user_also_kills_their_open_session(client, seeded_db, monkeypatch):
    """Revoking access has to revoke access: without this, the already-open
    session would keep working until that person logged out on their own,
    because the middleware only checks that the token exists."""
    from dashboard import main as dashboard_main

    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("alice", "alice-password", role="operator")
    alice_sid = _login(client, "alice", "alice-password").cookies["sid"]
    assert dashboard_main._SESSIONS[alice_sid]["username"] == "alice"

    _as(client, seeded_db, "root", "root-password", role="admin")
    alice = seeded_db.get_user_by_username("alice")
    client.post("/users/toggle", data={"user_id": str(alice["id"]), "active": "0"},
                follow_redirects=False)

    assert alice_sid not in dashboard_main._SESSIONS
    # And that cookie no longer opens anything: the middleware rejects it.
    client.cookies.clear()
    client.cookies.set("sid", alice_sid)
    resp = client.get("/results", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


# ======================================================================= #
# Regressions from the C21a REVIEW. The first block is the reviewer's
# probe: a space in the form disabled ALL revocation, and
# the 42 previous tests still passed.
# ======================================================================= #
def test_a_padded_username_still_gets_a_canonical_session(client, seeded_db, monkeypatch):
    """Logging in as "  alice  " authenticates the `alice` row (it's normalized
    when looking it up), but the session must end up under the name `alice`. If it stored
    what was typed, no name comparison would later recognize that
    session: revoking it would return 0, deactivating the user wouldn't close it, an
    admin could self-deactivate bypassing the guard, and the audit_log
    would record an actor that doesn't exist in the table."""
    from dashboard import main as dashboard_main

    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("alice", "alice-password", role="operator")

    sid = _login(client, "  alice  ", "alice-password").cookies["sid"]

    assert dashboard_main._SESSIONS[sid]["username"] == "alice"
    assert dashboard_main.revoke_sessions("alice") == 1
    assert sid not in dashboard_main._SESSIONS


def test_deactivating_closes_even_a_session_opened_with_a_padded_username(
    client, seeded_db, monkeypatch,
):
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("alice", "alice-password", role="operator")
    padded_sid = _login(client, " alice ", "alice-password").cookies["sid"]

    _as(client, seeded_db, "root", "root-password", role="admin")
    alice = seeded_db.get_user_by_username("alice")
    client.post("/users/toggle", data={"user_id": str(alice["id"]), "active": "0"},
                follow_redirects=False)

    client.cookies.clear()
    client.cookies.set("sid", padded_sid)
    resp = client.get("/results", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


def test_an_admin_cannot_dodge_the_self_deactivation_guard_with_whitespace(
    client, seeded_db, monkeypatch,
):
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("root", "root-password", role="admin")
    resp = _login(client, "  root  ", "root-password")
    client.cookies.set("sid", resp.cookies["sid"])
    me = seeded_db.get_user_by_username("root")

    page = client.post("/users/toggle", data={"user_id": str(me["id"]), "active": "0"})

    assert "cannot deactivate your own account" in page.text
    assert seeded_db.get_user_by_username("root")["active"] == 1


def test_the_env_rescue_login_can_reach_the_users_page_even_with_its_row_disabled(
    client, seeded_db, monkeypatch,
):
    """The safety net has to reach the page that exists to
    rescue it. With the AUTH_USER row deactivated —the literal example
    from the design— the operator could log in but /users bounced them to "/", and the
    rescue was left as editing SQLite by hand."""
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    uid = seeded_db.add_user("envadmin", "table-password", role="operator")
    seeded_db.set_user_active(uid, False)

    resp = _login(client, "envadmin", "env-password")
    assert resp.cookies.get("sid")
    client.cookies.set("sid", resp.cookies["sid"])

    assert client.get("/users").status_code == 200


def test_login_timing_does_not_reveal_which_usernames_exist(client, seeded_db, monkeypatch):
    """pbkdf2 (600,000 iterations) was only paid for if the row existed, so
    login was an enumeration oracle: measured, ~370 ms for an
    existing user versus ~53 ms for a made-up one. Now it's always verified
    against a real hash."""
    import time

    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("alice", "alice-password", role="operator")

    def _median_ms(username):
        times = []
        for _ in range(3):
            start = time.perf_counter()
            _login(client, username, "definitely-wrong")
            times.append((time.perf_counter() - start) * 1000)
        return sorted(times)[1]

    exists = _median_ms("alice")
    missing = _median_ms("nobody-by-that-name")

    # Deliberately lax threshold (this board is slow and irregular): what's
    # being ruled out is the order of magnitude that made enumeration trivial,
    # not a millisecond difference.
    assert missing > exists * 0.5, f"exists={exists:.0f}ms missing={missing:.0f}ms"


def test_changing_a_password_also_closes_that_users_sessions(client, seeded_db, monkeypatch):
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("alice", "alice-password", role="operator")
    alice_sid = _login(client, "alice", "alice-password").cookies["sid"]

    _as(client, seeded_db, "root", "root-password", role="admin")
    alice = seeded_db.get_user_by_username("alice")
    client.post("/users/password", data={"user_id": str(alice["id"]), "password": "brand-new"},
                follow_redirects=False)

    from dashboard import main as dashboard_main
    assert alice_sid not in dashboard_main._SESSIONS


def test_a_non_ascii_username_with_a_wrong_password_is_a_clean_rejection(
    client, seeded_db, monkeypatch,
):
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("josé", "jose-password", role="operator")

    assert _login(client, "josé", "jose-password").cookies.get("sid")
    bad = _login(client, "josé", "typo")
    assert bad.status_code == 303  # not a 500, which also gave away that user
    assert not bad.cookies.get("sid")


def test_auth_stays_enabled_when_the_env_pair_is_removed_but_users_exist(
    client, seeded_db, monkeypatch,
):
    """After C21a the obvious reading is "I already have users, the .env is no longer needed".
    Whoever acts on it can't end up with the entire dashboard wide open."""
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("alice", "alice-password", role="operator")

    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)

    resp = client.get("/results", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
