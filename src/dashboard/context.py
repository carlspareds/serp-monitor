"""Request context for the UI shell: who is viewing, which subject is active,
and the one-shot flash message of a Post/Redirect/Get.

Everything here is set ONCE per request by `SubjectMiddleware` (a pure ASGI
middleware, so the ContextVars it sets reach both async endpoints and sync
endpoints running in the threadpool -- see database.base._ACTIVE_SUBJECT for
why a sync dependency would not work) and read anywhere below it without a
`Request`: the shell (top bar, switcher, account menu), page(), the routers.

Active subject
--------------
Cookie ``subject`` (httponly, SameSite=Lax) holds the subject id the viewer
picked with GET /subject/switch. It is re-validated on EVERY request against
the subjects this viewer may see:

  * staff (admins, operators -- users without a project) see every active
    subject;
  * a client viewer (role ``client_viewer``, or any non-admin tied to a
    project through ``users.client_id``) only sees the active subjects of
    that project;
  * with auth disabled (local single-user install) the local user is staff.

An unknown, inactive or forbidden id silently falls back to the first allowed
subject. A viewer allowed nothing gets subject id 0, which owns no data (fail
closed, never the default subject).

Flash
-----
``redirect_with_flash(url, message, kind)`` answers a POST with a 303 and a
short-lived ``sm_flash`` cookie. The next full HTML page reads it (``flash()``)
and the middleware deletes the cookie on that response. Fragment requests
(the overlay loader sends ``X-SM-Fragment: 1``) never consume it, so a form
submitted from a modal still shows its toast on the page it lands on.
Secrets that must be shown exactly once (a new API key) travel in a
process-local one-time store, never in the cookie: ``stash_once()`` /
``pop_once()``.
"""
from __future__ import annotations

import json
import secrets
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

from fastapi.responses import RedirectResponse, Response
from starlette.requests import Request

from dashboard.deps import db
from database.base import reset_active_subject, set_active_subject

SUBJECT_COOKIE = "subject"
FLASH_COOKIE = "sm_flash"
FRAGMENT_HEADER = "x-sm-fragment"

# Paths that never need the UI context (no shell, no subject-scoped page).
_SKIP_PREFIXES = ("/static/", "/api/v1/", "/health")


@dataclass(frozen=True)
class Viewer:
    username: str = ""
    role: str = "admin"
    client_id: int | None = None
    auth: bool = False           # auth enabled on this install
    is_admin: bool = True
    can_write: bool = True
    restricted: bool = False     # limited to the subjects of `client_id`
    subjects: tuple = field(default_factory=tuple)   # allowed subjects (dicts)

    @property
    def signed_in(self) -> bool:
        return bool(self.username)


_VIEWER: ContextVar[Viewer | None] = ContextVar("sm_viewer", default=None)
_FLASH: ContextVar[dict | None] = ContextVar("sm_flash", default=None)
_FRAGMENT: ContextVar[bool] = ContextVar("sm_fragment", default=False)

# One-time secrets (token -> payload), e.g. a just-created API key. Bounded.
_ONCE: dict[str, dict] = {}
_ONCE_MAX = 200


# --------------------------------------------------------------------- #
# Viewer and subjects
# --------------------------------------------------------------------- #
def _allowed_subjects(restricted: bool, client_id: int | None, auth_on: bool,
                      signed_in: bool) -> list[dict]:
    if auth_on and not signed_in:
        return []
    try:
        subjects = db.list_subjects()
    except Exception:  # noqa: BLE001 -- a broken DB must not break every page
        return []
    try:
        live_projects = {c["id"] for c in db.list_clients(include_inactive=False)}
    except Exception:  # noqa: BLE001
        live_projects = set()
    subjects = [s for s in subjects
                if s["client_id"] in live_projects or s["id"] == 1 or not live_projects]
    if restricted:
        return [s for s in subjects if client_id is not None and s["client_id"] == client_id]
    return subjects


def build_viewer(request: Request) -> Viewer:
    from dashboard.session import auth_enabled, session_role, session_user

    auth_on = auth_enabled()
    if not auth_on:
        subjects = _allowed_subjects(False, None, False, False)
        return Viewer(subjects=tuple(subjects))
    username = session_user(request)
    role = session_role(request) if username else ""
    client_id = None
    if username:
        row = db.get_user_by_username(username)
        client_id = row.get("client_id") if row else None
    restricted = bool(username) and role != "admin" and (
        role == "client_viewer" or client_id is not None)
    subjects = _allowed_subjects(restricted, client_id, True, bool(username))
    return Viewer(username=username, role=role or "", client_id=client_id, auth=True,
                  is_admin=role == "admin", can_write=role in ("admin", "operator"),
                  restricted=restricted, subjects=tuple(subjects))


def viewer() -> Viewer:
    """The current request's viewer (a local admin outside a request)."""
    v = _VIEWER.get()
    return v if v is not None else Viewer()


def allowed_subjects() -> list[dict]:
    return list(viewer().subjects)


def resolve_subject_id(cookie_value: str | None, subjects) -> int:
    ids = [s["id"] for s in subjects]
    try:
        wanted = int(cookie_value or "")
    except ValueError:
        wanted = None
    if wanted in ids:
        return wanted
    return ids[0] if ids else 0


def is_fragment_request() -> bool:
    return _FRAGMENT.get()


# --------------------------------------------------------------------- #
# Flash (PRG toasts)
# --------------------------------------------------------------------- #
def safe_next(url: str | None, default: str = "/") -> str:
    """Only same-site relative paths ("/x?y#z"); anything else -> default."""
    url = (url or "").strip()
    if not url.startswith("/") or url.startswith("//") or "\\" in url:
        return default
    parts = urlsplit(url)
    if parts.scheme or parts.netloc:
        return default
    return url


def redirect_with_flash(url: str, message: str = "", kind: str = "ok",
                        once: dict | None = None) -> Response:
    """303 to `url` with a one-shot toast. kind: ok | error | info.

    For a fetch-submitted overlay form (X-SM-Fragment: 1) the answer is a 200
    with an `X-SM-Redirect` header instead: fetch would follow a 303 silently
    and drop the #anchor, so static/app.js navigates itself."""
    if is_fragment_request():
        resp: Response = Response(status_code=200, headers={"X-SM-Redirect": url,
                                                            "Cache-Control": "no-store"})
    else:
        resp = RedirectResponse(url, status_code=303)
    payload: dict = {}
    if message:
        payload["m"] = message[:500]
        payload["k"] = kind if kind in ("ok", "error", "info") else "ok"
    if once:
        payload["o"] = stash_once(once)
    if payload:
        resp.set_cookie(FLASH_COOKIE, quote(json.dumps(payload)), max_age=120,
                        httponly=True, samesite="lax", path="/")
    return resp


def flash() -> dict | None:
    """{'message', 'kind', 'once'} for this page, or None."""
    return _FLASH.get()


def _parse_flash(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        data = json.loads(unquote(raw))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return {"message": str(data.get("m") or ""), "kind": str(data.get("k") or "ok"),
            "once": str(data.get("o") or "")}


def stash_once(payload: dict) -> str:
    if len(_ONCE) >= _ONCE_MAX:
        _ONCE.clear()
    token = secrets.token_urlsafe(16)
    _ONCE[token] = payload
    return token


def pop_once(token: str | None) -> dict | None:
    return _ONCE.pop(token, None) if token else None


# --------------------------------------------------------------------- #
# Middleware
# --------------------------------------------------------------------- #
class SubjectMiddleware:
    """Pure ASGI: sets the viewer, the active subject and the flash for the
    request, and deletes a consumed flash cookie on the HTML page that
    showed it."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path", "").startswith(_SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            v = build_viewer(request)
        except Exception:  # noqa: BLE001 -- never take the app down over the shell
            v = Viewer(subjects=())
        sid = resolve_subject_id(request.cookies.get(SUBJECT_COOKIE), v.subjects)
        fragment = request.headers.get(FRAGMENT_HEADER) == "1"
        flash_data = None if fragment else _parse_flash(request.cookies.get(FLASH_COOKIE))

        t_viewer = _VIEWER.set(v)
        t_flash = _FLASH.set(flash_data)
        t_frag = _FRAGMENT.set(fragment)
        t_subject = set_active_subject(sid)

        async def send_wrapper(message):
            if (flash_data is not None and message["type"] == "http.response.start"
                    and message.get("status") == 200):
                headers = list(message.get("headers") or [])
                ctype = next((v_ for k, v_ in headers if k.lower() == b"content-type"), b"")
                if ctype.startswith(b"text/html"):
                    headers.append((b"set-cookie",
                                    f"{FLASH_COOKIE}=; Max-Age=0; Path=/; SameSite=lax".encode()))
                    message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            reset_active_subject(t_subject)
            _FRAGMENT.reset(t_frag)
            _FLASH.reset(t_flash)
            _VIEWER.reset(t_viewer)


__all__ = [
    "FLASH_COOKIE",
    "SUBJECT_COOKIE",
    "SubjectMiddleware",
    "Viewer",
    "allowed_subjects",
    "build_viewer",
    "flash",
    "is_fragment_request",
    "pop_once",
    "redirect_with_flash",
    "resolve_subject_id",
    "safe_next",
    "stash_once",
    "viewer",
]
