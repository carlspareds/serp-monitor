"""Dashboard session management, authentication state, and audit helpers.

Authentication is ON by default (SECURITY.md, item 6): a fresh install
answers every non-public route with the "Create the admin account" page
(/setup/admin) until an admin exists. The only way to run without sign-in
is ALLOW_NO_AUTH=1 for a local demo, which the startup check refuses when
HOST is not a loopback address and which this module additionally limits
to loopback clients (no_auth_allowed_for).

Sessions (item 9) are server-side: an opaque random token in an HttpOnly,
SameSite=Lax cookie (``__Host-sid`` + Secure over HTTPS, ``sid`` on plain
http://localhost), with an idle timeout (SESSION_IDLE_MINUTES, default 120)
and an absolute lifetime (SESSION_MAX_HOURS, default 12) enforced here, not
by the browser. Login, logout, password changes and deactivation rotate or
revoke them.
"""
import logging
import os
import secrets
import threading
import time

from fastapi import Request

from common.redact import redact_obj
from dashboard.deps import db
from dashboard.security import env_flag, is_loopback, request_is_https

# token -> {"username": str, "via_env": bool, "created": float, "seen": float}
_SESSIONS: dict[str, dict] = {}
_SESSIONS_LOCK = threading.Lock()

# Paths accessible without authentication
_PUBLIC_PATHS = {"/login", "/logout", "/health", "/onboarding", "/robots.txt"}
# Reachable while no admin account exists yet (the first-run "create the
# admin account" state): nothing else is served until one is created.
SETUP_ADMIN_PATH = "/setup/admin"
_PRE_ADMIN_PATHS = {"/health", "/robots.txt", "/login", "/logout", SETUP_ADMIN_PATH}

SESSION_COOKIE = "sid"
SECURE_SESSION_COOKIE = "__Host-sid"

_http_log = logging.getLogger("http")
_audit_log = logging.getLogger("audit")


def _idle_seconds() -> float:
    try:
        return max(60.0, float(os.getenv("SESSION_IDLE_MINUTES") or 120) * 60)
    except ValueError:
        return 7200.0


def _max_seconds() -> float:
    try:
        return max(300.0, float(os.getenv("SESSION_MAX_HOURS") or 12) * 3600)
    except ValueError:
        return 43200.0


def credentials_exist() -> bool:
    """An admin can sign in: the .env AUTH_USER/AUTH_PASS pair, or at least
    one active account in the users table."""
    if os.getenv("AUTH_USER") and os.getenv("AUTH_PASS"):
        return True
    try:
        return any(u["active"] for u in db.list_users())
    except Exception:  # noqa: BLE001 -- unreadable DB: treat as "no account yet" (still auth ON)
        return False


def no_auth_requested() -> bool:
    """ALLOW_NO_AUTH=1 is set (local demo opt-out)."""
    return env_flag("ALLOW_NO_AUTH")


def no_auth_allowed_for(request: Request) -> bool:
    """The opt-out only ever serves the machine itself: a loopback client
    with no proxy in between (a forwarded request means someone else's
    browser, whatever the TCP peer is)."""
    if not no_auth_requested():
        return False
    ip = request.client.host if request.client else ""
    forwarded = any(h in request.headers for h in ("x-forwarded-for", "forwarded", "x-real-ip"))
    return is_loopback(ip) and not forwarded


def auth_enabled() -> bool:
    """True unless the install has NO credentials at all AND runs with the
    explicit local-demo opt-out ALLOW_NO_AUTH=1. Once any account exists,
    sign-in is required regardless of ALLOW_NO_AUTH."""
    if credentials_exist():
        return True
    return not no_auth_requested()


def needs_admin_setup() -> bool:
    """Auth is on but nobody can sign in yet: the first visit must create
    the admin account (/setup/admin)."""
    return auth_enabled() and not credentials_exist()


def is_demo_mode() -> bool:
    """No real Apify key configured -- every result on screen is synthetic demo data."""
    return not bool(os.getenv("APIFY_API_KEY"))


def demo_mode_banner() -> str:
    """Plain-text notice shown only while the ACTIVE subject is the demo one
    (is_demo) -- a real subject never carries it, with or without an Apify key."""
    from dashboard.ui_helpers import (
        demo_banner_html,  # lazy: ui_helpers imports this module
    )
    return demo_banner_html()


def session_token(request: Request) -> str:
    """The session token the browser sent (``__Host-sid`` preferred)."""
    return (request.cookies.get(SECURE_SESSION_COOKIE)
            or request.cookies.get(SESSION_COOKIE) or "")


def _live_session(token: str, touch: bool = True) -> dict | None:
    """The session for `token` if it exists and hasn't expired (idle or
    absolute); an expired one is dropped."""
    if not token:
        return None
    now = time.time()
    with _SESSIONS_LOCK:
        data = _SESSIONS.get(token)
        if data is None:
            return None
        created = data.setdefault("created", now)
        seen = data.setdefault("seen", now)
        if now - seen > _idle_seconds() or now - created > _max_seconds():
            _SESSIONS.pop(token, None)
            return None
        if touch:
            data["seen"] = now
        return data


def session_valid(request: Request) -> bool:
    return _live_session(session_token(request)) is not None


def session_user(request: Request) -> str:
    """The REAL username of the session, or "" if none is valid."""
    token = session_token(request)
    if token and auth_enabled():
        return (_live_session(token) or {}).get("username", "")
    return ""


def revoke_sessions(username: str, keep: str | None = None) -> int:
    """Closes ALL of a user's open sessions (except `keep`, a token) and
    returns how many."""
    with _SESSIONS_LOCK:
        victims = [t for t, data in _SESSIONS.items()
                   if data.get("username") == username and t != keep]
        for token in victims:
            _SESSIONS.pop(token, None)
    return len(victims)


def end_session(request: Request) -> None:
    token = session_token(request)
    if token:
        with _SESSIONS_LOCK:
            _SESSIONS.pop(token, None)


def session_role(request: Request) -> str:
    """Role of the session's user ("" if there's no valid session)."""
    who = session_user(request)
    if not who:
        return ""
    if (_live_session(session_token(request), touch=False) or {}).get("via_env"):
        return "admin"
    row = db.get_user_by_username(who)
    if row:
        return row["role"] if row["active"] else ""
    return ""


# Roles allowed to change data through the web UI. client_viewer is
# read-only; admin-only areas (settings, secrets, backups, users) are
# gated separately by is_admin().
_WRITE_ROLES = {"admin", "operator"}


def is_admin(request: Request) -> bool:
    """Shared admin gate. With auth disabled entirely (a local,
    single-user install with no credentials configured) everyone is
    implicitly admin -- the same fail-open auth_middleware applies."""
    if not auth_enabled():
        return True
    return session_role(request) == "admin"


def can_write(request: Request) -> bool:
    """Whether the session may perform day-to-day writes (ack alerts,
    notes, searches...). Same auth-disabled rule as is_admin()."""
    if not auth_enabled():
        return True
    return session_role(request) in _WRITE_ROLES


def has_live_access(request: Request) -> bool:
    """Whether a page may show live workspace data: always with auth
    disabled (local demo), otherwise only with a valid session. Used by
    the public page (/onboarding) so an anonymous visitor
    never sees the tracked subject, run counts or configured integrations."""
    return not auth_enabled() or bool(session_user(request))


def create_session(username: str, via_env: bool = False, replaces: str | None = None) -> str:
    """Opens a session for `username` and returns the token (256 random
    bits). `replaces`: the token the browser held before (session fixation
    guard -- a login always gets a NEW token and the old one is dropped)."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _SESSIONS_LOCK:
        if replaces:
            _SESSIONS.pop(replaces, None)
        _SESSIONS[token] = {"username": username, "via_env": via_env,
                            "created": now, "seen": now}
    return token


def session_cookie_name(request: Request) -> str:
    return SECURE_SESSION_COOKIE if request_is_https(request) else SESSION_COOKIE


def set_session_cookie(response, request: Request, token: str) -> None:
    """HttpOnly, SameSite=Lax, Path=/; over HTTPS ``__Host-sid`` + Secure.
    Max-Age matches the absolute session lifetime (the server enforces it
    anyway, plus the idle timeout)."""
    secure = request_is_https(request)
    response.set_cookie(session_cookie_name(request), token, httponly=True, samesite="lax",
                        secure=secure, path="/", max_age=int(_max_seconds()))
    # A stale cookie under the other name must not shadow the new one.
    other = SESSION_COOKIE if secure else SECURE_SESSION_COOKIE
    if other in request.cookies:
        response.delete_cookie(other, path="/", secure=other == SECURE_SESSION_COOKIE)


def clear_session_cookie(response, request: Request) -> None:
    for name in (SESSION_COOKIE, SECURE_SESSION_COOKIE):
        response.delete_cookie(name, path="/", httponly=True, samesite="lax",
                               secure=name == SECURE_SESSION_COOKIE)


def start_session(response, request: Request, username: str, via_env: bool = False) -> str:
    """create_session (rotating any token the browser held) + the cookie."""
    token = create_session(username, via_env=via_env, replaces=session_token(request) or None)
    set_session_cookie(response, request, token)
    return token


def audit(request: Request, action: str, resource_type: str = "", resource_id="",
          actor_type: str | None = None, actor_id: str | None = None, **params) -> None:
    """Logs a write action to audit_log (C24). Never raises."""
    try:
        if actor_type is None:
            who = session_user(request)
            if who:
                actor_type, actor_id = "user", who
            else:
                actor_type, actor_id = "system", "anonymous"
        ip = request.client.host if request.client else ""
        # Belt and braces: callers never pass secret values, and anything
        # key-shaped is redacted before it reaches the hash-chained log.
        db.add_audit(actor_type=actor_type, actor_id=actor_id or "", action=action,
                     resource_type=resource_type, resource_id=str(resource_id),
                     params=redact_obj(params), ip=ip)
    except Exception:  # noqa: BLE001 -- auditing must never break the route
        _audit_log.exception("audit_write_failed", extra={"action": action})


__all__ = [
    "SETUP_ADMIN_PATH",
    "_PUBLIC_PATHS",
    "_SESSIONS",
    "audit",
    "auth_enabled",
    "can_write",
    "clear_session_cookie",
    "create_session",
    "credentials_exist",
    "demo_mode_banner",
    "end_session",
    "has_live_access",
    "is_admin",
    "is_demo_mode",
    "needs_admin_setup",
    "no_auth_allowed_for",
    "revoke_sessions",
    "session_role",
    "session_token",
    "session_user",
    "session_valid",
    "set_session_cookie",
    "start_session",
]
