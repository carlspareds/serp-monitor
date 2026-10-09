"""User authentication routes: login form, credentials validation, and session termination."""
import hmac
import os
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dashboard.deps import db
from dashboard.session import _SESSIONS, audit, create_session
from dashboard.ui_helpers import _esc

router = APIRouter()

# Login throttling: in-memory, per (username, client IP). After
# _MAX_FAILURES consecutive failures that pair is locked out for
# _LOCKOUT_SECONDS. Process-local on purpose -- this is a single-process
# app, and a restart clearing the counters is acceptable.
_MAX_FAILURES = 5
_LOCKOUT_SECONDS = 60
_MAX_TRACKED = 10_000
# (username, ip) -> [consecutive failures, locked-until monotonic time]
_FAILED_LOGINS: dict[tuple[str, str], list[float]] = {}


def _throttle_key(request: Request, username: str) -> tuple[str, str]:
    ip = request.client.host if request.client else ""
    return (username.strip().lower(), ip)


def _locked_out(key: tuple[str, str]) -> bool:
    entry = _FAILED_LOGINS.get(key)
    if not entry or not entry[1]:
        return False
    if entry[1] > time.monotonic():
        return True
    _FAILED_LOGINS.pop(key, None)  # lockout expired: start counting afresh
    return False


def _record_failure(key: tuple[str, str]) -> None:
    if len(_FAILED_LOGINS) >= _MAX_TRACKED:
        _FAILED_LOGINS.clear()  # crude memory bound against username spraying
    entry = _FAILED_LOGINS.setdefault(key, [0, 0.0])
    entry[0] += 1
    if entry[0] >= _MAX_FAILURES:
        entry[1] = time.monotonic() + _LOCKOUT_SECONDS


@router.get("/login", response_class=HTMLResponse, tags=["auth"])
async def login_form(error: str = ""):
    err = (f"<div class='lg-err'>{_esc(error)}</div>") if error else ""
    body = f"""<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width, initial-scale=1'>
    <title>Sign in · SERP Monitor</title>
    <link rel='stylesheet' href='/static/style.css'>
    <script>try{{var t=localStorage.getItem('theme');if(t)document.documentElement.setAttribute('data-theme',t)}}catch(e){{}}</script>
    </head>
    <body class='lg-body'>
      <form class='lg-card' action='/login' method='post'>
        <div class='lg-logo'><svg viewBox='0 0 24 24' aria-hidden='true' focusable='false'>
          <rect width='24' height='24' rx='6.5' fill='currentColor'/>
          <path d='M6.5 15.5 10 12l2.5 2.5 5-5.5' fill='none' stroke='#fff' stroke-width='2'
            stroke-linecap='round' stroke-linejoin='round'/></svg></div>
        <h1>SERP Monitor</h1>
        <p class='sub'>SERP tracking & reputation alerts</p>
        {err}
        <label>Username</label>
        <input type='text' name='username' placeholder='username' autofocus required>
        <label>Password</label>
        <input type='password' name='password' placeholder='••••••••' required>
        <button type='submit'>Sign in</button>
        <div class='lg-foot'>Private access · <a href='/onboarding'>Getting started guide</a></div>
      </form>
    </body></html>"""
    return HTMLResponse(body)


@router.post("/login", tags=["auth"])
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    key = _throttle_key(request, username)
    if _locked_out(key):
        audit(request, "login_locked", actor_type="user", actor_id=username)
        return RedirectResponse("/login?error=Too+many+failed+attempts.+Try+again+in+a+minute.",
                                status_code=303)
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
            _record_failure(key)
            audit(request, "login_failed", actor_type="user", actor_id=username)
            return RedirectResponse("/login?error=Invalid+credentials", status_code=303)
        via_env = True
    _FAILED_LOGINS.pop(key, None)
    canonical = os.getenv("AUTH_USER", "") if via_env else user["username"]
    token = create_session(canonical, via_env=via_env)
    audit(request, "login_ok", actor_type="user", actor_id=canonical)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("sid", token, httponly=True, samesite="lax", max_age=86400)
    return resp


@router.get("/logout", tags=["auth"])
async def logout(request: Request):
    audit(request, "logout")
    sid = request.cookies.get("sid")
    if sid:
        _SESSIONS.pop(sid, None)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("sid")
    return resp
