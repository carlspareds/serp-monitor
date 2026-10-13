"""Authentication routes: sign-in, sign-out, and the first-run "Create the
admin account" page.

Abuse protection on the two anonymous forms (SECURITY.md, items 11-12):
  * a honeypot field and a signed render timestamp with a minimum
    time-to-submit (dashboard.security.bot_check);
  * consecutive-failure lockouts with exponential back-off, per
    (username, client IP) and per client IP for the setup form;
  * per-IP token buckets in dashboard.security.RateLimitMiddleware;
  * one generic message for every failure ("Invalid username or password"),
    so the form can't be used to find out which accounts exist.
"""
import hmac
import os
import threading

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from auth.passwords import password_problem
from dashboard.deps import db
from dashboard.security import FailureTracker, bot_check, bot_fields_html
from dashboard.session import (
    SETUP_ADMIN_PATH,
    audit,
    clear_session_cookie,
    credentials_exist,
    end_session,
    start_session,
)
from dashboard.ui_helpers import _esc

router = APIRouter()

# Login throttling: in-memory. After _MAX_FAILURES consecutive failures a
# (username, client IP) pair is locked for _LOCKOUT_SECONDS, doubling with
# each further lockout up to an hour. Process-local on purpose -- this is a
# single-process app, and a restart clearing the counters is acceptable.
_MAX_FAILURES = 5
_LOCKOUT_SECONDS = 60
_FAILED_LOGINS = FailureTracker(_MAX_FAILURES, _LOCKOUT_SECONDS, 3600)
# The first-admin form is keyed by client IP only (there is no account yet).
_FAILED_SETUPS = FailureTracker(_MAX_FAILURES, _LOCKOUT_SECONDS, 3600)
_SETUP_LOCK = threading.Lock()

_GENERIC_LOGIN_ERROR = "Invalid username or password."
_LOCKED_ERROR = "Too many failed attempts. Try again later."


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _throttle_key(request: Request, username: str) -> tuple[str, str]:
    return ((username or "").strip().lower(), _ip(request))


_LOGO = ("<div class='lg-logo'><svg viewBox='0 0 24 24' aria-hidden='true' focusable='false'>"
         "<rect width='24' height='24' rx='6.5' fill='currentColor'/>"
         "<path d='M6.5 15.5 10 12l2.5 2.5 5-5.5' fill='none' stroke='#fff' stroke-width='2' "
         "stroke-linecap='round' stroke-linejoin='round'/></svg></div>")


def _card_page(title: str, inner: str) -> str:
    return f"""<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width, initial-scale=1'>
    <meta name='robots' content='noindex, nofollow'>
    <title>{_esc(title)} · SERP Monitor</title>
    <link rel='stylesheet' href='/static/style.css'>
    <script src='/static/theme-boot.js'></script>
    </head>
    <body class='lg-body'>{inner}</body></html>"""


@router.get("/login", response_class=HTMLResponse, tags=["auth"])
async def login_form(request: Request, error: str = ""):
    if not credentials_exist():
        return RedirectResponse(SETUP_ADMIN_PATH, status_code=303)
    # Only our own fixed messages are shown: an arbitrary ?error= text
    # would let a link put words in the sign-in page.
    message = error if error in (_GENERIC_LOGIN_ERROR, _LOCKED_ERROR) else (
        _GENERIC_LOGIN_ERROR if error else "")
    err = f"<div class='lg-err' role='alert'>{_esc(message)}</div>" if message else ""
    inner = f"""
      <form class='lg-card' action='/login' method='post' autocomplete='on'>
        {_LOGO}
        <h1>SERP Monitor</h1>
        <p class='sub'>SERP tracking & reputation alerts</p>
        {err}
        <label for='lg-user'>Username</label>
        <input id='lg-user' type='text' name='username' placeholder='username' autofocus required
          maxlength='150' autocomplete='username'>
        <label for='lg-pass'>Password</label>
        <input id='lg-pass' type='password' name='password' placeholder='••••••••' required
          maxlength='1024' autocomplete='current-password'>
        {bot_fields_html()}
        <button type='submit'>Sign in</button>
        <div class='lg-foot'>Private access · <a href='/onboarding'>Getting started guide</a></div>
      </form>"""
    return HTMLResponse(_card_page("Sign in", inner))


def _login_error(message: str = _GENERIC_LOGIN_ERROR) -> RedirectResponse:
    from urllib.parse import urlencode
    return RedirectResponse("/login?" + urlencode({"error": message}), status_code=303)


@router.post("/login", tags=["auth"])
def login_submit(request: Request, username: str = Form(..., max_length=150),
                 password: str = Form(..., max_length=1024),
                 website: str = Form("", max_length=200), form_ts: str = Form("", max_length=200)):
    key = _throttle_key(request, username)
    if _FAILED_LOGINS.locked(key):
        audit(request, "login_locked", actor_type="user", actor_id=username)
        return _login_error(_LOCKED_ERROR)
    if not bot_check(website, form_ts):
        # Same answer as a wrong password: a bot learns nothing.
        _FAILED_LOGINS.failure(key)
        audit(request, "login_bot_rejected", actor_type="user", actor_id=username)
        return _login_error()
    user = db.authenticate_user(username, password)
    if user is None and db.ensure_admin_seed():
        user = db.authenticate_user(username, password)
    via_env = False
    if user is None:
        exp_user = os.getenv("AUTH_USER", "")
        exp_pass = os.getenv("AUTH_PASS", "")
        user_ok = hmac.compare_digest(username.encode("utf-8"), exp_user.encode("utf-8"))
        pass_ok = hmac.compare_digest(password.encode("utf-8"), exp_pass.encode("utf-8"))
        if not (user_ok and pass_ok and exp_user and exp_pass):
            _FAILED_LOGINS.failure(key)
            audit(request, "login_failed", actor_type="user", actor_id=username)
            return _login_error()
        via_env = True
    _FAILED_LOGINS.success(key)
    canonical = os.getenv("AUTH_USER", "") if via_env else user["username"]
    resp = RedirectResponse("/", status_code=303)
    # New token on every login (session fixation): any token the browser
    # already held is dropped.
    start_session(resp, request, canonical, via_env=via_env)
    audit(request, "login_ok", actor_type="user", actor_id=canonical)
    return resp


def _logout_response(request: Request) -> RedirectResponse:
    audit(request, "logout")
    end_session(request)
    resp = RedirectResponse("/login", status_code=303)
    clear_session_cookie(resp, request)
    return resp


@router.get("/logout", tags=["auth"])
async def logout(request: Request):
    return _logout_response(request)


@router.post("/logout", tags=["auth"])
async def logout_post(request: Request):
    return _logout_response(request)


# --------------------------------------------------------------------- #
# First run: create the admin account
# --------------------------------------------------------------------- #
def _setup_admin_page(error: str = "", username: str = "") -> str:
    err = f"<div class='lg-err' role='alert'>{_esc(error)}</div>" if error else ""
    inner = f"""
      <form class='lg-card' action='{SETUP_ADMIN_PATH}' method='post' autocomplete='off'>
        {_LOGO}
        <h1>Create the admin account</h1>
        <p class='sub'>Sign-in is required. This first account manages users, settings and keys.</p>
        {err}
        <label for='sa-user'>Username</label>
        <input id='sa-user' type='text' name='username' value='{_esc(username)}' required
          maxlength='150' autocomplete='username' autofocus>
        <label for='sa-pass'>Password</label>
        <input id='sa-pass' type='password' name='password' required minlength='12'
          maxlength='1024' autocomplete='new-password'>
        <label for='sa-pass2'>Repeat the password</label>
        <input id='sa-pass2' type='password' name='password2' required minlength='12'
          maxlength='1024' autocomplete='new-password'>
        <p class='lg-hint'>At least 12 characters. A passphrase of a few words works well.</p>
        {bot_fields_html()}
        <button type='submit'>Create the admin account</button>
        <div class='lg-foot'>Private access · <a href='/health'>Status</a></div>
      </form>"""
    return _card_page("Create the admin account", inner)


@router.get(SETUP_ADMIN_PATH, response_class=HTMLResponse, tags=["auth"])
async def setup_admin_form(request: Request):
    if credentials_exist():
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(_setup_admin_page())


@router.post(SETUP_ADMIN_PATH, tags=["auth"])
async def setup_admin_submit(request: Request, username: str = Form("", max_length=150),
                             password: str = Form("", max_length=1024),
                             password2: str = Form("", max_length=1024),
                             website: str = Form("", max_length=200), form_ts: str = Form("", max_length=200)):
    """Creates the FIRST admin. Only while no credentials exist at all;
    afterwards it's a plain redirect to /login (no replay, no second admin)."""
    if credentials_exist():
        return RedirectResponse("/login", status_code=303)
    key = ("setup", _ip(request))
    if _FAILED_SETUPS.locked(key):
        return PlainTextResponse(_LOCKED_ERROR, status_code=429)
    username = " ".join((username or "").split())
    if not bot_check(website, form_ts):
        _FAILED_SETUPS.failure(key)
        return HTMLResponse(_setup_admin_page("Please try again.", username), status_code=400)
    problem = None
    if not username:
        problem = "Choose a username."
    elif password != password2:
        problem = "The two passwords don't match."
    else:
        problem = password_problem(password, username)
    if problem:
        _FAILED_SETUPS.failure(key)
        return HTMLResponse(_setup_admin_page(problem, username), status_code=400)

    def _create() -> bool:
        # Serialized: two simultaneous first visitors can't both create an admin.
        with _SETUP_LOCK:
            if credentials_exist():
                return False
            db.add_user(username, password, role="admin")
            return True

    try:
        created = await run_in_threadpool(_create)   # PBKDF2 off the event loop
    except Exception:  # noqa: BLE001 -- UNIQUE or any other DB rejection
        return HTMLResponse(_setup_admin_page("Could not create the account. Try another "
                                              "username.", username), status_code=400)
    if not created:
        return RedirectResponse("/login", status_code=303)
    _FAILED_SETUPS.success(key)
    audit(request, "setup_admin_create", actor_type="user", actor_id=username,
          username=username, role="admin")
    from dashboard.routers import wizard
    resp = RedirectResponse("/" if wizard.wizard_completed() else "/setup", status_code=303)
    start_session(resp, request, username)
    audit(request, "login_ok", actor_type="user", actor_id=username)
    return resp


@router.get("/robots.txt", include_in_schema=False)
async def robots_txt():
    """A private dashboard: nothing here is for search engines."""
    return PlainTextResponse("User-agent: *\nDisallow: /\n")
