"""Dashboard HTTP middlewares: authentication, first-run setup redirect, and request logging."""
import logging
import time

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

from dashboard.session import _PUBLIC_PATHS, _SESSIONS, auth_enabled

_http_log = logging.getLogger("http")


async def auth_middleware(request: Request, call_next):
    # /api/v1/ (9.3): has its OWN auth via the X-API-Key header (see
    # dashboard.routers.api_v1.require_api_key) -- it coexists with, but
    # doesn't depend on, the web session.
    if (not auth_enabled() or request.url.path in _PUBLIC_PATHS
            or request.url.path.startswith("/static/")
            or request.url.path.startswith("/api/v1/")):
        return await call_next(request)
    sid = request.cookies.get("sid")
    if sid and sid in _SESSIONS:
        return await call_next(request)
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
            or path in ("/health", "/login", "/logout")
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
    1. auth_middleware (innermost)
    2. first_run_middleware (runs before auth so first run reaches /setup)
    3. request_log_middleware (outermost, logs all responses including redirects)
    """
    app.middleware("http")(auth_middleware)
    app.middleware("http")(first_run_middleware)
    app.middleware("http")(request_log_middleware)


__all__ = [
    "auth_middleware",
    "first_run_middleware",
    "request_log_middleware",
    "setup_middlewares",
]
