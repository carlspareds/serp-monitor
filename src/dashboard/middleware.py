"""Dashboard HTTP middlewares: authentication, first-run setup redirect, and request logging."""
import logging
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

from dashboard.session import (
    _PRE_ADMIN_PATHS,
    _PUBLIC_PATHS,
    SETUP_ADMIN_PATH,
    auth_enabled,
    credentials_exist,
    no_auth_allowed_for,
    session_valid,
)

_http_log = logging.getLogger("http")


def _json_path(path: str) -> bool:
    return path.startswith(("/api/", "/jobs/", "/assistant/")) or (
        path.startswith("/job/") and path.endswith("/status"))


async def auth_middleware(request: Request, call_next):
    """Every route requires a signed-in session except the public ones
    (_PUBLIC_PATHS, /static/) and /api/v1/ (9.3: its OWN auth via the
    X-API-Key header, see dashboard.routers.api_v1.require_api_key).

    * No account exists yet -> only /setup/admin ("Create the admin
      account"), /login, /health and static files are served.
    * ALLOW_NO_AUTH=1 with no account (local demo) -> open, but only to
      loopback clients without proxy headers.
    """
    path = request.url.path
    if path.startswith(("/static/", "/api/v1/")) or path == "/health" or path == "/robots.txt":
        return await call_next(request)
    if not auth_enabled():
        if no_auth_allowed_for(request):
            return await call_next(request)
        return PlainTextResponse("ALLOW_NO_AUTH only serves this machine (loopback, no proxy). "
                                 "Create an admin account to allow remote access.",
                                 status_code=403)
    if not credentials_exist():
        if path in _PRE_ADMIN_PATHS:
            return await call_next(request)
        if _json_path(path):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return RedirectResponse(SETUP_ADMIN_PATH, status_code=303)
    if path in _PUBLIC_PATHS or session_valid(request):
        return await call_next(request)
    if _json_path(path):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


# The setup wizard opens the same integration dialogs as Settings, and their
# Save/Test posts go to the Settings endpoints: reachable during the first
# run too (role/auth middlewares still guard them exactly as on /settings).
SETUP_DIALOG_PREFIXES = ("/ui/integration/", "/settings/integration/")
SETUP_DIALOG_POSTS = frozenset({
    "/settings/apikey", "/settings/ai", "/settings/email", "/settings/notify",
    "/settings/secret/remove", "/settings/ai-test", "/settings/apify-test",
    "/settings/email-test", "/settings/notify-test",
})


async def first_run_middleware(request: Request, call_next):
    """Forces a fresh, unconfigured install to /setup instead of the
    dashboard."""
    path = request.url.path
    if (path in ("/setup", "/onboarding")
            or path.startswith(("/setup/", "/static/", "/api/v1/"))
            or path in ("/health", "/login", "/logout", "/robots.txt")
            or path.startswith(SETUP_DIALOG_PREFIXES) or path in SETUP_DIALOG_POSTS):
        return await call_next(request)
    from dashboard.routers import wizard
    if not wizard.wizard_completed():
        return RedirectResponse("/setup", status_code=303)
    return await call_next(request)


async def request_log_middleware(request: Request, call_next):
    """JSON log per request (9.2): method, path, status, dur_ms."""
    t0 = time.monotonic()
    response = await call_next(request)
    _http_log.info(
        "request",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "dur_ms": int((time.monotonic() - t0) * 1000),
        },
    )
    return response


def setup_middlewares(app: FastAPI) -> None:
    """Register middlewares on FastAPI app in the correct Starlette onion order:
    1. first_run_middleware (innermost: the setup wizard is for a signed-in
       admin, so it only runs once auth has let the request through)
    2. auth_middleware (sign-in, or the "create the admin account" page)
    3. request_log_middleware (outermost, logs all responses including redirects)
    """
    app.middleware("http")(first_run_middleware)
    app.middleware("http")(auth_middleware)
    app.middleware("http")(request_log_middleware)


__all__ = [
    "auth_middleware",
    "first_run_middleware",
    "request_log_middleware",
    "setup_middlewares",
]
