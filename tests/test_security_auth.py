"""SECURITY.md items 6, 9, 10, 12: auth on by default, the first-run
"Create the admin account" page, the ALLOW_NO_AUTH opt-out, session cookies
and lifetime, password hashing/policy, and bot protection on the anonymous
forms. Every test here runs with production auth (`strict_auth`)."""
import base64
import hashlib
import logging
import time

import pytest
from conftest import sign_in

from auth import passwords
from common import config_check as cc

ADMIN_PW = "correct horse battery"


def _setup_admin(client, username="boss", password=ADMIN_PW, password2=None, **extra):
    data = {"username": username, "password": password,
            "password2": password if password2 is None else password2, **extra}
    return client.post("/setup/admin", data=data, follow_redirects=False)


# --------------------------------------------------------------------- #
# Item 6: auth on by default, first visit creates the admin
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/", "/results", "/settings", "/setup", "/onboarding",
                                  "/ui/search", "/export/pdf", "/login"])
def test_fresh_install_serves_only_the_create_admin_page(client, strict_auth, path):
    resp = client.get(path, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/setup/admin"


def test_fresh_install_still_serves_health_static_and_robots(client, strict_auth):
    assert client.get("/health").status_code in (200, 503)
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/robots.txt").text.startswith("User-agent: *\nDisallow: /")
    page = client.get("/setup/admin")
    assert page.status_code == 200 and "Create the admin account" in page.text


def test_fresh_install_json_routes_answer_401(client, strict_auth):
    assert client.get("/jobs/active").status_code == 401
    assert client.get("/api/v1/results").status_code == 401


def test_create_admin_signs_in_and_cannot_be_replayed(client, strict_auth, seeded_db):
    resp = _setup_admin(client)
    assert resp.status_code == 303 and resp.headers["location"] in ("/", "/setup")
    assert resp.cookies.get("sid")
    users = seeded_db.list_users()
    assert [(u["username"], u["role"]) for u in users] == [("boss", "admin")]
    client.cookies.set("sid", resp.cookies["sid"])
    assert client.get("/", follow_redirects=False).status_code == 200

    other = type(client)(client.app)
    again = _setup_admin(other, username="intruder")
    assert again.status_code == 303 and again.headers["location"] == "/login"
    assert [u["username"] for u in seeded_db.list_users()] == ["boss"]
    assert other.get("/", follow_redirects=False).headers["location"] == "/login"


@pytest.mark.parametrize("password,password2,message", [
    ("short", None, "at least 12"),
    ("a-long-enough-pass", "something-else-1", "match"),
    ("password1234", None, "too easy"),
    ("aaaaaaaaaaaaaaa", None, "too repetitive"),
])
def test_create_admin_enforces_the_password_policy(client, strict_auth, seeded_db,
                                                   password, password2, message):
    resp = _setup_admin(client, password=password, password2=password2)
    assert resp.status_code == 400 and message in resp.text
    assert seeded_db.list_users() == []


def test_create_admin_honeypot_and_min_time(client, strict_auth, seeded_db, monkeypatch):
    from dashboard import security
    assert _setup_admin(client, website="http://spam.example").status_code == 400
    monkeypatch.setenv("BOT_MIN_SUBMIT_SECONDS", "2")
    # Missing and too-fresh tokens are refused, an old-enough one passes.
    assert _setup_admin(client).status_code == 400
    assert _setup_admin(client, form_ts=security.form_token()).status_code == 400
    assert _setup_admin(client, form_ts="123.forged").status_code == 400
    ok = _setup_admin(client, form_ts=security.form_token(now=time.time() - 5))
    assert ok.status_code == 303
    assert [u["username"] for u in seeded_db.list_users()] == ["boss"]


def test_create_admin_locks_out_after_repeated_failures(client, strict_auth, seeded_db):
    for _ in range(5):
        _setup_admin(client, password="short")
    locked = _setup_admin(client)
    assert locked.status_code == 429
    assert seeded_db.list_users() == []


def test_wizard_admin_step_cannot_be_skipped(client, seeded_db):
    """Under the ALLOW_NO_AUTH opt-out the wizard shows the admin step; it
    has no Skip and a ?skip=1 post creates nothing and skips nothing."""
    seeded_db.set_setting("wizard_skipped_auth", "1")
    from dashboard.routers import wizard
    assert wizard._step_done("auth") is False
    client.post("/setup/auth?skip=1", data={})
    assert seeded_db.list_users() == [] and wizard._step_done("auth") is False


# --------------------------------------------------------------------- #
# ALLOW_NO_AUTH: local demos only
# --------------------------------------------------------------------- #
def test_allow_no_auth_serves_loopback_only(client, seeded_db):
    assert client.get("/", follow_redirects=False).status_code == 200      # 127.0.0.1
    remote = type(client)(client.app, client=("203.0.113.9", 4000))
    assert remote.get("/", follow_redirects=False).status_code == 403
    proxied = client.get("/", headers={"X-Forwarded-For": "203.0.113.9"},
                         follow_redirects=False)
    assert proxied.status_code == 403


def test_allow_no_auth_is_ignored_once_an_account_exists(client, seeded_db):
    seeded_db.add_user("boss", ADMIN_PW, role="admin")
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_allow_no_auth_with_a_public_host_is_refused_at_startup(tmp_path):
    env = {"ALLOW_NO_AUTH": "1", "HOST": "0.0.0.0"}
    assert "ALLOW_NO_AUTH" in {f.key for f in cc.check_config(env, cwd=tmp_path).errors}
    # Not bypassable: SKIP_CONFIG_CHECK skips everything else, not this.
    with pytest.raises(cc.ConfigError):
        cc.validate_on_startup(logging.getLogger("t"), env={**env, "SKIP_CONFIG_CHECK": "1"})
    for host in ("127.0.0.1", "localhost", "::1", ""):
        ok = cc.check_config({"ALLOW_NO_AUTH": "1", "HOST": host}, cwd=tmp_path)
        assert "ALLOW_NO_AUTH" not in {f.key for f in ok.errors}


# --------------------------------------------------------------------- #
# Item 9: session cookies
# --------------------------------------------------------------------- #
def test_session_cookie_flags_on_http(client, strict_auth, seeded_db):
    seeded_db.add_user("boss", ADMIN_PW, role="admin")
    resp = client.post("/login", data={"username": "boss", "password": ADMIN_PW},
                       follow_redirects=False)
    cookie = next(c for c in resp.headers.get_list("set-cookie") if c.startswith("sid="))
    low = cookie.lower()
    assert "httponly" in low and "samesite=lax" in low and "path=/" in low
    assert "max-age=" in low and "secure" not in low   # plain http://localhost


def test_session_cookie_is_host_prefixed_and_secure_over_https(client, strict_auth, seeded_db):
    seeded_db.add_user("boss", ADMIN_PW, role="admin")
    https = type(client)(client.app, base_url="https://testserver")
    resp = https.post("/login", data={"username": "boss", "password": ADMIN_PW},
                      follow_redirects=False)
    cookies = resp.headers.get_list("set-cookie")
    sid = next(c for c in cookies if c.startswith("__Host-sid="))
    assert "Secure" in sid and "HttpOnly" in sid and "Path=/" in sid and "Domain" not in sid
    https.cookies.set("__Host-sid", resp.cookies["__Host-sid"])
    assert https.get("/", follow_redirects=False).status_code == 200
    # Every other cookie set over HTTPS is Secure too (subject switch).
    sw = https.get("/subject/switch", params={"id": 1, "next": "/"}, follow_redirects=False)
    assert all("Secure" in c for c in sw.headers.get_list("set-cookie"))


def test_login_rotates_the_session_token(client, strict_auth, seeded_db):
    from dashboard import session
    old = sign_in(client, seeded_db, "boss", ADMIN_PW)
    resp = client.post("/login", data={"username": "boss", "password": ADMIN_PW},
                       follow_redirects=False)
    new = resp.cookies["sid"]
    assert new != old and old not in session._SESSIONS and new in session._SESSIONS


def test_fixated_unknown_token_is_never_adopted(client, strict_auth, seeded_db):
    from dashboard import session
    seeded_db.add_user("boss", ADMIN_PW, role="admin")
    client.cookies.set("sid", "attacker-chosen-token")
    resp = client.post("/login", data={"username": "boss", "password": ADMIN_PW},
                       follow_redirects=False)
    assert resp.cookies["sid"] != "attacker-chosen-token"
    assert "attacker-chosen-token" not in session._SESSIONS


def test_logout_invalidates_server_side(client, strict_auth, seeded_db):
    from dashboard import session
    token = sign_in(client, seeded_db, "boss", ADMIN_PW)
    resp = client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303 and token not in session._SESSIONS
    client.cookies.set("sid", token)    # replaying the old cookie
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_idle_and_absolute_session_expiry(client, strict_auth, seeded_db, monkeypatch):
    from dashboard import session
    token = sign_in(client, seeded_db, "boss", ADMIN_PW)
    monkeypatch.setenv("SESSION_IDLE_MINUTES", "30")
    session._SESSIONS[token]["seen"] = time.time() - 31 * 60
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert token not in session._SESSIONS

    token = sign_in(client, seeded_db, "boss", ADMIN_PW)
    monkeypatch.setenv("SESSION_MAX_HOURS", "1")
    session._SESSIONS[token]["created"] = time.time() - 3700   # active, but too old
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"


def test_password_change_revokes_sessions_and_rotates_own(client, strict_auth, seeded_db):
    from dashboard import session
    seeded_db.add_user("alice", "alice-password", role="operator")
    other = type(client)(client.app)
    alice_token = sign_in(other, seeded_db, "alice", "alice-password")
    boss_token = sign_in(client, seeded_db, "boss", ADMIN_PW)
    alice = seeded_db.get_user_by_username("alice")
    client.post("/users/password", data={"user_id": alice["id"], "password": "a-new-passphrase"})
    assert alice_token not in session._SESSIONS
    me = seeded_db.get_user_by_username("boss")
    resp = client.post("/users/password", data={"user_id": me["id"], "password": "boss-new-passphrase"},
                       follow_redirects=False)
    assert boss_token not in session._SESSIONS
    assert resp.cookies.get("sid") and resp.cookies["sid"] in session._SESSIONS


def test_deactivation_revokes_sessions(client, strict_auth, seeded_db):
    from dashboard import session
    seeded_db.add_user("alice", "alice-password", role="operator")
    other = type(client)(client.app)
    alice_token = sign_in(other, seeded_db, "alice", "alice-password")
    sign_in(client, seeded_db, "boss", ADMIN_PW)
    alice = seeded_db.get_user_by_username("alice")
    client.post("/users/toggle", data={"user_id": alice["id"], "active": "0"})
    assert alice_token not in session._SESSIONS


def test_cross_site_post_is_rejected(client, strict_auth, seeded_db):
    sign_in(client, seeded_db, "boss", ADMIN_PW)
    resp = client.post("/alerts/seen", headers={"Origin": "https://evil.example"},
                       follow_redirects=False)
    assert resp.status_code == 403
    same = client.post("/alerts/seen", headers={"Origin": "http://testserver"},
                       follow_redirects=False)
    assert same.status_code in (200, 303)


# --------------------------------------------------------------------- #
# Item 10: password hashing
# --------------------------------------------------------------------- #
def test_hashes_are_salted_pbkdf2_600k():
    a, b = passwords.hash_password("same password!"), passwords.hash_password("same password!")
    assert a != b and a.startswith("pbkdf2_sha256$600000$")
    assert passwords.verify_password("same password!", a)
    assert not passwords.verify_password("Same password!", a)
    assert not passwords.verify_password("x" * 2000, a)          # no unbounded PBKDF2
    assert not passwords.verify_password("x", "pbkdf2_sha256$999999999$AA==$AA==")


def test_verify_uses_constant_time_compare():
    import inspect
    assert "hmac.compare_digest" in inspect.getsource(passwords.verify_password)


def _old_hash(password: str, iterations: int = 1000) -> str:
    salt = b"0123456789abcdef"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return (f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}$"
            f"{base64.b64encode(digest).decode()}")


def test_weaker_hashes_are_upgraded_on_login(seeded_db):
    seeded_db.add_user("legacy", "legacy-password", role="operator")
    conn = seeded_db._connect()
    conn.execute("UPDATE users SET password_hash = ? WHERE username = 'legacy'",
                 (_old_hash("legacy-password"),))
    conn.commit()
    conn.close()
    assert passwords.needs_rehash(_old_hash("x"))
    assert seeded_db.authenticate_user("legacy", "legacy-password") is not None
    conn = seeded_db._connect()
    stored = conn.execute("SELECT password_hash FROM users WHERE username='legacy'").fetchone()[0]
    conn.close()
    assert stored.startswith("pbkdf2_sha256$600000$") and not passwords.needs_rehash(stored)


@pytest.mark.parametrize("pw,ok", [("short", False), ("a b c d e f g", True),
                                   ("correct horse battery", True), ("x" * 1025, False),
                                   ("administrator", False)])
def test_password_policy(pw, ok):
    assert (passwords.password_problem(pw, "someone") is None) is ok
    assert passwords.password_problem("the-username-1", "the-username-1") is not None


def test_passwords_never_reach_the_logs_or_audit(client, strict_auth, seeded_db, caplog):
    seeded_db.add_user("boss", ADMIN_PW, role="admin")
    with caplog.at_level(logging.DEBUG):
        client.post("/login", data={"username": "boss", "password": "wrong-but-secret-pw"})
        client.post("/login", data={"username": "boss", "password": ADMIN_PW})
    assert "wrong-but-secret-pw" not in caplog.text and ADMIN_PW not in caplog.text
    audit = str(seeded_db.list_audit(limit=50))
    assert "wrong-but-secret-pw" not in audit and ADMIN_PW not in audit


# --------------------------------------------------------------------- #
# Item 12: bot protection and no username enumeration on login
# --------------------------------------------------------------------- #
def test_login_errors_are_generic(client, strict_auth, seeded_db):
    seeded_db.add_user("boss", ADMIN_PW, role="admin")
    a = client.post("/login", data={"username": "boss", "password": "nope-nope-nope"},
                    follow_redirects=False)
    b = client.post("/login", data={"username": "ghost", "password": "nope-nope-nope"},
                    follow_redirects=False)
    assert a.headers["location"] == b.headers["location"]
    # The page shows only its own messages, never an arbitrary ?error= text.
    page = client.get("/login", params={"error": "<b>Call 555-0100 to recover</b>"})
    assert "555-0100" not in page.text and "Invalid username or password." in page.text


def test_login_form_carries_bot_fields(client, strict_auth, seeded_db):
    seeded_db.add_user("boss", ADMIN_PW, role="admin")
    text = client.get("/login").text
    assert "name='website'" in text and "name='form_ts'" in text and "class='hp'" in text


def test_login_honeypot_and_min_time(client, strict_auth, seeded_db, monkeypatch):
    from dashboard import security
    seeded_db.add_user("boss", ADMIN_PW, role="admin")
    bot = client.post("/login", data={"username": "boss", "password": ADMIN_PW,
                                      "website": "x"}, follow_redirects=False)
    assert "sid" not in bot.cookies
    monkeypatch.setenv("BOT_MIN_SUBMIT_SECONDS", "2")
    fast = client.post("/login", data={"username": "boss", "password": ADMIN_PW,
                                       "form_ts": security.form_token()}, follow_redirects=False)
    assert "sid" not in fast.cookies
    human = client.post("/login", data={"username": "boss", "password": ADMIN_PW,
                                        "form_ts": security.form_token(time.time() - 3)},
                        follow_redirects=False)
    assert human.cookies.get("sid")


def test_lockout_backs_off_exponentially():
    from dashboard.security import FailureTracker
    t = FailureTracker(max_failures=3, base_seconds=10, max_seconds=35)
    key = ("u", "1.2.3.4")
    for _ in range(3):
        t.failure(key, now=0)
    assert t.locked(key, now=1) == pytest.approx(9)
    for _ in range(3):
        t.failure(key, now=20)
    assert t.locked(key, now=20) == pytest.approx(20)     # doubled
    for _ in range(3):
        t.failure(key, now=100)
    assert t.locked(key, now=100) == pytest.approx(35)    # capped
    t.success(key)
    assert t.locked(key, now=101) == 0


def test_robots_and_noindex(client):
    assert "Disallow: /" in client.get("/robots.txt").text
    assert client.get("/").headers["x-robots-tag"] == "noindex, nofollow"
