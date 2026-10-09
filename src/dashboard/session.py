"""Dashboard session management, authentication state, and audit helpers."""
import logging
import os
import secrets

from fastapi import Request

from dashboard.deps import db

# token -> {"username": str, "via_env": bool}
_SESSIONS: dict[str, dict] = {}

# Paths accessible without authentication
_PUBLIC_PATHS = {"/login", "/logout", "/health", "/onboarding"}

_http_log = logging.getLogger("http")
_audit_log = logging.getLogger("audit")


def auth_enabled() -> bool:
    """Protects the app if ANY credential is configured: the .env pair or
    at least one active user in the table (C21a)."""
    if os.getenv("AUTH_USER") and os.getenv("AUTH_PASS"):
        return True
    try:
        return any(u["active"] for u in db.list_users())
    except Exception:  # noqa: BLE001 -- an unreadable DB must not lock the operator out
        return False


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


def session_user(request: Request) -> str:
    """The REAL username of the session, or "" if none is valid."""
    sid = request.cookies.get("sid")
    if auth_enabled() and sid:
        return (_SESSIONS.get(sid) or {}).get("username", "")
    return ""


def revoke_sessions(username: str) -> int:
    """Closes ALL of a user's open sessions and returns how many."""
    victims = [t for t, data in _SESSIONS.items() if data.get("username") == username]
    for token in victims:
        _SESSIONS.pop(token, None)
    return len(victims)


def session_role(request: Request) -> str:
    """Role of the session's user ("" if there's no valid session)."""
    who = session_user(request)
    if not who:
        return ""
    sid = request.cookies.get("sid") or ""
    if (_SESSIONS.get(sid) or {}).get("via_env"):
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


def create_session(username: str, via_env: bool = False) -> str:
    """Opens a session for `username` and returns the token."""
    token = secrets.token_hex(16)
    _SESSIONS[token] = {"username": username, "via_env": via_env}
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
        db.add_audit(actor_type=actor_type, actor_id=actor_id or "", action=action,
                     resource_type=resource_type, resource_id=str(resource_id),
                     params=params, ip=ip)
    except Exception:  # noqa: BLE001 -- auditing must never break the route
        _audit_log.exception("audit_write_failed", extra={"action": action})


__all__ = [
    "_PUBLIC_PATHS",
    "_SESSIONS",
    "audit",
    "auth_enabled",
    "can_write",
    "create_session",
    "demo_mode_banner",
    "has_live_access",
    "is_admin",
    "is_demo_mode",
    "revoke_sessions",
    "session_role",
    "session_user",
]
