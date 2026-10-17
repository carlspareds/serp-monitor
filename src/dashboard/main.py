"""
SERP Monitor - FastAPI
================================
Full, navigable web platform to operate the whole system:
  - Home (summary / statistics)
  - Search (single country and multi-country)
  - Results (with filters)
  - Run history + per-country detail
  - Domain management (positive / blacklist)
  - Weekly report settings
  - Export to Excel

Run from the project root:
    .venv/bin/uvicorn src.dashboard.main:app --reload --port 8000
"""

import logging
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

# Imports both from the root and from src/
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))

# ponytail: uvicorn starts this module as "src.dashboard.main"; routers do
# "from dashboard.main import ...". Registering the alias avoids re-executing.
sys.modules.setdefault("dashboard.main", sys.modules[__name__])

from common import config_check
from dashboard import deps, logging_setup, security, ui
from dashboard.context import SubjectMiddleware, is_fragment_request
from dashboard.deps import db
from dashboard.jobs import JobManager
from dashboard.middleware import (
    auth_middleware,
    first_run_middleware,
    request_log_middleware,
    setup_middlewares,
)
from dashboard.session import (
    _PUBLIC_PATHS,
    _SESSIONS,
    audit,
    auth_enabled,
    can_write,
    create_session,
    demo_mode_banner,
    is_admin,
    is_demo_mode,
    revoke_sessions,
    session_role,
    session_user,
)
from dashboard.ui_helpers import (
    _MAX_PAGES,
    _MIN_PAGES,
    _RECLASSIFY_BATCH_LIMIT_SETTING,
    COUNTRIES,
    DEFAULT_COUNTRIES,
    DEFAULT_QUERY,
    SHELL_CHROME,
    Shell,
    _clamp_pages,
    _device_radios,
    _engine_checkboxes,
    _env_file,
    _esc,
    _evidence_cell,
    _pill,
    _reclassify_batch_limit,
    _results_table,
    _search_forms_html,
    _severity_pill,
    _short,
    _sort_th,
    _split_valid_countries,
    _split_valid_engines,
    document,
    merged_positive_domains,
    ordered_countries,
    page,
    report_defaults,
    shell,
)
from serp.runner import SerpRunner

# isort: split
from assistant import router as assistant_router
from dashboard.routers import alerts as alerts_router
from dashboard.routers import analysis as analysis_router
from dashboard.routers import api_v1 as api_v1_router
from dashboard.routers import audit as audit_router
from dashboard.routers import auth as auth_router
from dashboard.routers import diff as diff_router
from dashboard.routers import domains as domains_router
from dashboard.routers import evidence as evidence_router
from dashboard.routers import export as export_router
from dashboard.routers import home as home_router
from dashboard.routers import integrations as integrations_router
from dashboard.routers import notes, review, search, timeline
from dashboard.routers import onboarding as onboarding_router
from dashboard.routers import overlays as overlays_router
from dashboard.routers import results as results_router
from dashboard.routers import runs as runs_router
from dashboard.routers import search_runner as search_runner_router
from dashboard.routers import settings as settings_router
from dashboard.routers import subjects as subjects_router
from dashboard.routers import users as users_router
from dashboard.routers import wizard as wizard_router
from scheduler import core as scheduler_core
from scheduler import jobs as scheduler_jobs  # noqa: F401 -- registers JOBS via @job

_startup_log = logging.getLogger("scheduler")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging_setup.setup()
    # Optional at-rest encryption of secret-ish settings (APP_ENCRYPTION_KEY):
    # encrypt any value still stored in plain text. Never blocks startup.
    try:
        db.encrypt_sensitive_settings()
    except Exception:
        logging.getLogger("config").exception("settings_encryption_migration_failed")
    # Validate the configuration once, before serving anything: fatal
    # problems raise ConfigError so uvicorn exits non-zero; missing optional
    # keys (demo mode) only log one warning line. SKIP_CONFIG_CHECK=1 bypasses.
    config_check.validate_on_startup(logging.getLogger("config"))
    # Report files written flat in data/exports/ before reports were stored
    # per subject belong to subject 1 (export.reports.adopt_legacy_files).
    try:
        from export.reports import adopt_legacy_files
        adopt_legacy_files(deps.exports_dir)
    except Exception:  # never block startup over old files
        logging.getLogger("export").exception("legacy_reports_adopt_failed")
    if not os.getenv("DISABLE_SCHEDULER"):
        scheduler_core.start(db)
    yield
    if not os.getenv("DISABLE_SCHEDULER"):
        scheduler_core.shutdown()


app = FastAPI(
    title="SERP Monitor",
    description=(
        "SERP/AI Overviews "
        "monitoring, alerts, domain management and a JSON API "
        "(/api/v1: read, plus alert status updates with the write:alerts scope) for external integrations (n8n, scripts)."
    ),
    lifespan=lifespan,
)

try:
    APP_VERSION = subprocess.run(
        ["git", "describe", "--tags", "--always"],
        capture_output=True, text=True, timeout=5, check=False,
        cwd=str(project_root),
    ).stdout.strip() or "unknown"
except Exception:  # noqa: BLE001
    APP_VERSION = "unknown"

app.version = APP_VERSION
app.mount("/static", StaticFiles(directory=str(project_root / "static")), name="static")
jobs = JobManager()

_runner = None


def get_runner() -> SerpRunner:
    """The search runner over the ACTIVE SERP provider (serp.registry);
    reset to None by Settings whenever a provider key or the provider
    choice changes, so the next search picks the new one up."""
    global _runner
    if _runner is None:
        _runner = SerpRunner(db=db)
    return _runner


# Register template globals
deps.templates.env.globals["demo_mode_banner"] = demo_mode_banner

# Register middlewares
setup_middlewares(app)

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# --------------------------------------------------------------------- #
# Friendly error pages: HTML in the app layout for browser requests, the
# usual JSON for the API and the job-status endpoint.
# --------------------------------------------------------------------- #
_ERROR_COPY = {
    400: ("Something was missing", "Part of the request was missing or invalid. Go back and try again."),
    401: ("Sign in required", "Sign in to continue."),
    403: ("You don't have access", "This area isn't available to your account."),
    404: ("Page not found", "This page doesn't exist or has moved."),
    405: ("Not allowed", "That action isn't available here."),
    413: ("Too much data", "The request was larger than this app accepts."),
    422: ("Something was missing", "Part of the form was missing or invalid. Go back and try again."),
    429: ("Slow down", "Too many requests. Try again in a moment."),
    500: ("Something went wrong", "The error was logged. Try again in a moment."),
}


def _wants_json(request: Request) -> bool:
    path = request.url.path
    if path.startswith(("/api/", "/jobs/")) or path == "/health" or (
            path.startswith("/job/") and path.endswith("/status")):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def error_response(status: int, message: str = "") -> HTMLResponse:
    title, text = _ERROR_COPY.get(status, ("Something went wrong", "The request couldn't be completed."))
    body_text = _esc(message or text)
    if is_fragment_request():
        return HTMLResponse(ui.overlay(title, f"<div class='empty'>{body_text}</div>"), status_code=status)
    body = (f"<div class='card empty-cta'><h2>{_esc(title)}</h2><p>{body_text}</p>"
            "<div class='actions-row'><a class='btn primary' href='/'>Go to the Overview</a>"
            "<a class='btn ghost' href='/' data-history-back>Go back</a></div></div>")
    try:
        html = page(title, body, active="", head=False)
    except Exception:  # noqa: BLE001 -- the error page must render even if the DB is down
        html = document(title, f"<main class='main'>{body}</main>")
    return HTMLResponse(html, status_code=status)


def _forbidden(message: str) -> HTMLResponse:
    return error_response(403, message)


def _admin_only(path: str, method: str) -> bool:
    """Areas only an admin may use: settings writes (secrets, API keys,
    integrations, scheduling), backups, the audit CSV (every user's actions
    and their IP addresses), and the setup wizard's writes. GET /settings is
    open to every signed-in user: it renders only the sections their role may
    see. /users is gated by its own router (it redirects instead of 403)."""
    if path.startswith(("/backup/", "/audit/")):
        return True
    if path == "/settings" or path.startswith("/settings/"):
        return method in _UNSAFE_METHODS
    return method in _UNSAFE_METHODS and path.startswith("/setup/")


# POST /jobs/{id}/seen only hides a finished search from the viewer's own
# top-bar chip: allowed for read-only accounts too.
_SEEN_PATH = re.compile(r"/jobs/[0-9a-f]{1,32}/seen")


async def role_middleware(request: Request, call_next):
    """Role enforcement for logged-in sessions. Requests without a valid
    session pass through untouched: auth_middleware redirects them to
    /login (or serves a public page), and with auth disabled entirely
    every request is the local single user."""
    path = request.url.path
    if (path.startswith(("/api/v1/", "/static/")) or path in ("/login", "/logout")
            or not session_user(request)):
        return await call_next(request)
    if _admin_only(path, request.method) and not is_admin(request):
        return _forbidden("This area is restricted to administrators.")
    if request.method in _UNSAFE_METHODS and not can_write(request) and path not in (
            "/assistant/chat", "/assistant/stop") and not _SEEN_PATH.fullmatch(path):
        return _forbidden("Your account is read-only.")
    return await call_next(request)


async def csrf_middleware(request: Request, call_next):
    """Cross-site request protection for the cookie-authenticated UI: an
    unsafe request whose Origin (or, failing that, Referer) names another
    host is rejected. Requests carrying neither header (curl, scripts,
    TestClient) are allowed -- browsers always send at least one on a
    cross-site form POST. /api/v1 authenticates with X-API-Key, not
    cookies, so it's exempt."""
    if request.method in _UNSAFE_METHODS and not request.url.path.startswith("/api/v1/"):
        source = request.headers.get("origin") or request.headers.get("referer")
        if source:
            allowed = {h.lower() for h in (request.headers.get("host"),
                                            request.headers.get("x-forwarded-host")) if h}
            if urlsplit(source).netloc.lower() not in allowed:
                return HTMLResponse("Cross-site request rejected.", status_code=403)
    return await call_next(request)


# Registered after setup_middlewares, so both run before it (outermost
# last): CSRF first, then role checks, then first-run/auth.
app.middleware("http")(role_middleware)
app.middleware("http")(csrf_middleware)
# Viewer, active subject (cookie, validated) and PRG flash for the whole
# request -- pure ASGI so the ContextVars reach sync endpoints too.
app.add_middleware(SubjectMiddleware)
# Outer layers (SECURITY.md): rate limits (429), request body size (413),
# then security headers / HTTPS on everything that goes out -- including
# the 413/429 answers. Outermost of all: trusted reverse-proxy headers
# (TRUSTED_PROXIES), so every layer sees the real client IP and scheme.
app.add_middleware(security.RateLimitMiddleware, account_of=session_user)
app.add_middleware(security.BodySizeLimitMiddleware)
app.add_middleware(security.SecurityHeadersMiddleware)
_trusted = (os.getenv("TRUSTED_PROXIES") or os.getenv("FORWARDED_ALLOW_IPS") or "").strip()
if _trusted:
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    app.add_middleware(ProxyHeadersMiddleware,
                       trusted_hosts=[h.strip() for h in _trusted.split(",") if h.strip()])


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    if _wants_json(request) or exc.status_code < 400:
        return await http_exception_handler(request, exc)
    detail = exc.detail if isinstance(exc.detail, str) and exc.status_code in (400, 403) else ""
    resp = error_response(exc.status_code, detail)
    for k, v in (getattr(exc, "headers", None) or {}).items():
        resp.headers[k] = v
    return resp


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    if _wants_json(request):
        # FastAPI's default body echoes the submitted value ("input") back:
        # keep only where and what, never the value (it could be a secret).
        errors = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")}
                  for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)
    return error_response(422)


@app.exception_handler(Exception)
async def _server_error(request: Request, exc: Exception):
    _startup_log.exception("unhandled_error", extra={"path": request.url.path})
    if _wants_json(request):
        return JSONResponse({"detail": "Internal Server Error"}, status_code=500)
    return error_response(500)


@app.get("/health", tags=["system"])
async def health():
    """Platform status (public, 9.2): DB, scheduler, disk, jobs.
    200 ok/degraded · 503 if the DB doesn't respond or the scheduler should
    be running and isn't. Public, so it never echoes exception text or
    host details: the DB problem is a fixed string and the full error
    goes to the server log."""
    problems = []
    db_ping_ms = None
    try:
        t0 = time.monotonic()
        conn = db._connect()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        db_ping_ms = round((time.monotonic() - t0) * 1000, 1)
    except Exception:  # noqa: BLE001
        _startup_log.exception("health_db_ping_failed")
        problems.append("db: unavailable")

    scheduler_disabled = bool(os.getenv("DISABLE_SCHEDULER"))
    scheduler_running = scheduler_core.is_running()
    if not scheduler_running and not scheduler_disabled:
        problems.append("scheduler: not running")

    # Measure the disk the database lives on; walk up to the nearest existing
    # directory so a fresh checkout without data/ can't crash /health.
    disk_dir = Path(deps.db.db_path).resolve().parent
    while not disk_dir.exists() and disk_dir != disk_dir.parent:
        disk_dir = disk_dir.parent
    disk_free_gb = round(shutil.disk_usage(str(disk_dir)).free / 1024**3, 2)

    jobs_state = {
        j["name"]: {
            "last_status": j.get("last_status"),
            "last_run_at": j.get("last_run_at"),
            "next_run_at": j.get("next_run_at"),
        }
        for j in scheduler_core.list_jobs()
    }

    degraded = disk_free_gb < 2
    body = {
        "status": "error" if problems else ("degraded" if degraded else "ok"),
        "db_ping_ms": db_ping_ms,
        "scheduler_running": scheduler_running,
        "scheduler_disabled": scheduler_disabled,
        "jobs": jobs_state,
        "disk_low": degraded,
        "version": APP_VERSION,
        "problems": problems,
    }
    return JSONResponse(body, status_code=503 if problems else 200)


# Include all application routers
app.include_router(home_router.router)
app.include_router(search_runner_router.router)
app.include_router(results_router.router)
app.include_router(alerts_router.router)
app.include_router(runs_router.router)
app.include_router(domains_router.router)
app.include_router(settings_router.router)
app.include_router(integrations_router.router)
app.include_router(export_router.router)
app.include_router(auth_router.router)

# Pre-existing Jinja2 and API routers
app.include_router(notes.router, tags=["notes"])
app.include_router(timeline.router, tags=["timeline"])
app.include_router(search.router, tags=["search"])
app.include_router(audit_router.router, tags=["audit"])
app.include_router(review.router, tags=["review"])
app.include_router(diff_router.router, tags=["diff"])
app.include_router(evidence_router.router, tags=["evidence"])
app.include_router(analysis_router.router, tags=["analysis"])
app.include_router(users_router.router, tags=["users"])
app.include_router(wizard_router.router, tags=["setup"])
app.include_router(onboarding_router.router, tags=["public"])
app.include_router(api_v1_router.router)
app.include_router(assistant_router.router)
app.include_router(subjects_router.router)
# LAST on purpose: holds the placeholder fragments (/ui/search, /ui/reports,
# /ui/review, /ui/compare, /assistant/panel). A router included above with the
# same path wins, so the real dialogs can land without editing this one.
app.include_router(overlays_router.router)


# Route function aliases for backwards compatibility with tests / callers
show_results = results_router.show_results
home = home_router.home
search_page = search_runner_router.search_page
run_search = search_runner_router.run_search
search_multi = search_runner_router.search_multi
show_job = search_runner_router.show_job
alerts_page = alerts_router.alerts_page
show_runs = runs_router.show_runs
show_run = runs_router.show_run
show_domains = domains_router.show_domains
show_settings = settings_router.show_settings
export_dialog = export_router.export_dialog
export_run = export_router.export_run
export_pdf = export_router.export_pdf
backup_db = export_router.backup_db
backup_csv = export_router.backup_csv
login_form = auth_router.login_form
login_submit = auth_router.login_submit
logout = auth_router.logout


__all__ = [
    "APP_VERSION",
    "COUNTRIES",
    "DEFAULT_COUNTRIES",
    "DEFAULT_QUERY",
    "SHELL_CHROME",
    "_MAX_PAGES",
    "_MIN_PAGES",
    "_PUBLIC_PATHS",
    "_RECLASSIFY_BATCH_LIMIT_SETTING",
    "_SESSIONS",
    "Shell",
    "_clamp_pages",
    "_device_radios",
    "_engine_checkboxes",
    "_env_file",
    "_esc",
    "_evidence_cell",
    "_pill",
    "_reclassify_batch_limit",
    "_results_table",
    "_search_forms_html",
    "_severity_pill",
    "_short",
    "_sort_th",
    "_split_valid_countries",
    "_split_valid_engines",
    "alerts_page",
    "app",
    "audit",
    "auth_enabled",
    "auth_middleware",
    "backup_csv",
    "backup_db",
    "can_write",
    "create_session",
    "demo_mode_banner",
    "error_response",
    "export_dialog",
    "export_pdf",
    "export_run",
    "first_run_middleware",
    "get_runner",
    "health",
    "home",
    "is_admin",
    "is_demo_mode",
    "jobs",
    "lifespan",
    "login_form",
    "login_submit",
    "logout",
    "merged_positive_domains",
    "ordered_countries",
    "page",
    "report_defaults",
    "request_log_middleware",
    "revoke_sessions",
    "run_search",
    "search_multi",
    "search_page",
    "session_role",
    "session_user",
    "setup_middlewares",
    "shell",
    "show_domains",
    "show_job",
    "show_results",
    "show_run",
    "show_runs",
    "show_settings",
]


if __name__ == "__main__":
    import uvicorn

    # Loopback by default: set HOST=0.0.0.0 explicitly to expose it.
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=8000)
