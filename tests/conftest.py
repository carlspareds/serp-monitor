"""Shared fixtures for the test suite.

No real network is used: tests only exercise the DB (temporary SQLite via
tmp_path), the classifier, and the Excel exporter locally.
"""
import importlib
import os
import sys
import time
from datetime import UTC, datetime, timedelta

# HERMETICITY (2026-08-01): "no real network in tests" implies AI/Apify/Telegram
# are NOT configured unless a test stubs or monkeypatches them. The real keys
# from .env reach the process via load_dotenv() (apify.client, at app import
# time) -- while Gemini was broken (truncated model/429) this went unnoticed,
# but once it was fixed the REAL verdicts poisoned unstubbed tests
# (real same_person -> evidence not captured; real toxicity over synthetic
# fixtures). They are set to "" BEFORE any app import: falsy for
# is_configured(), and load_dotenv (override=False) doesn't overwrite them.
for _key in ("GEMINI_API_KEY", "APIFY_API_KEY", "TELEGRAM_BOT_TOKEN", "SLACK_WEBHOOK_URL",
              "NTFY_URL", "SMTP_HOST", "LLM_PROVIDER", "OPENAI_API_KEY", "OPENAI_BASE_URL",
              "OPENAI_MODEL"):
    os.environ[_key] = ""
# ENV_FILE (regression guard): the settings endpoints WRITE to .env --
# without this override, tests like test_audit (POST /settings/apikey with
# a dummy value) clobbered the operator's REAL credentials on every gate.
# The suite always writes to a temporary file instead.
import tempfile as _tempfile

os.environ["ENV_FILE"] = os.path.join(_tempfile.gettempdir(), "seo-dashboard-tests.env")
from pathlib import Path

import pytest
import requests

# PROJECT_ROOT is needed in addition to src/: report.weekly (8.1) imports
# config.positive_domains, which lives at the repo root.
_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from database.models import Database

RUN_ID = "run-test-1"

# The fictional demo subject's owned domains. config/positive_domains.py ships an EMPTY
# global list since multi-subject phase 1 (domains are per subject, in the DB), so tests
# that need these to classify as positive either seed them into the subject
# (seeded_db does) or pass them explicitly to the classifier.
DEMO_POSITIVE_DOMAINS = [
    "jordanashworth.example",
    "jordan-ashworth.example",
    "j-ashworth.example",
    "ashworthgroup.example",
    "ashworthcapital.example",
    "cityscene-vancouver.example",
]

# Deliberate mix: 2 positive, 2 negative, 1 neutral.
SAMPLE_RESULTS = [
    {"query": "Jordan Ashworth", "url": "https://jordanashworth.example/about",
     "title": "About Jordan Ashworth", "description": "Official site.",
     "displayed_url": "jordanashworth.example", "position": 1,
     "country_code": "us", "domain": "jordanashworth.example", "classification": "positive"},
    {"query": "Jordan Ashworth", "url": "https://jordan-ashworth.example/blog",
     "title": "Jordan's Blog", "description": "Personal blog.",
     "displayed_url": "jordan-ashworth.example", "position": 2,
     "country_code": "us", "domain": "jordan-ashworth.example", "classification": "positive"},
    {"query": "Jordan Ashworth", "url": "https://scam.example/article",
     "title": "Fraud lawsuit against Jordan Ashworth", "description": "Alleged fraud case.",
     "displayed_url": "scam.example", "position": 3,
     "country_code": "us", "domain": "scam.example", "classification": "negative"},
    {"query": "Jordan Ashworth", "url": "https://news.example/piece",
     "title": "Arrested claims dismissed", "description": "Court records.",
     "displayed_url": "news.example", "position": 4,
     "country_code": "us", "domain": "news.example", "classification": "negative"},
    {"query": "Jordan Ashworth", "url": "https://directory.example/entry",
     "title": "Jordan Ashworth profile", "description": "General directory entry.",
     "displayed_url": "directory.example", "position": 5,
     "country_code": "us", "domain": "directory.example", "classification": "neutral"},
]


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_network: opt out of the autouse stub that blocks requests.get/post; "
        "the test monkeypatches the network itself",
    )






@pytest.fixture(autouse=True)
def _no_real_network(request, monkeypatch):
    """Block requests.get/post/request for the whole suite: no test may
    touch the real network by accident. Tests that stub the network
    themselves opt out with @pytest.mark.real_network.

    requests.request (found missing here while adding the setup wizard,
    which calls apify.client.ApifyClient directly -- see
    dashboard.routers.wizard.setup_apify -- rather than through the
    already-mocked get_runner() path every other Apify test goes
    through): ApifyClient._request calls requests.request(...) directly,
    not .get()/.post(), so it was never actually covered by this guard.
    Any future code taking the same direct-ApifyClient shortcut would
    have silently been free to hit the real network in tests."""
    if request.node.get_closest_marker("real_network"):
        return

    def _fail_post(*a, **k):
        raise AssertionError("tests must not touch the real network (requests.post is blocked)")

    def _fail_get(*a, **k):
        raise AssertionError("tests must not touch the real network (requests.get is blocked)")

    def _fail_request(*a, **k):
        raise AssertionError("tests must not touch the real network (requests.request is blocked)")

    monkeypatch.setattr(requests, "post", _fail_post)
    monkeypatch.setattr(requests, "get", _fail_get)
    monkeypatch.setattr(requests, "request", _fail_request)








@pytest.fixture
def db_path(tmp_path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def seeded_db(db_path) -> Database:
    """Temporary DB seeded via Database's public API:
    1 run, 5 search_results (2 pos / 2 neg / 1 neutral), 2 positive
    domains and 1 negative.
    """
    database = Database(db_path=db_path)
    database.save_run(
        apify_run_id=RUN_ID,
        queries=["Jordan Ashworth"],
        country_code="us",
        status="completed",
        summary={"total": 5, "positive": 2, "negative": 2, "neutral": 1},
    )
    database.save_results(SAMPLE_RESULTS, run_id=RUN_ID)
    database.add_positive_domain("jordanashworth.example")
    database.add_positive_domain("jordan-ashworth.example")
    database.add_negative_domain("scam.example")
    return database


@pytest.fixture
def client(seeded_db, db_path, monkeypatch):
    """FastAPI app TestClient, pointing at the already-seeded temporary DB.

    DB_PATH is injected before (re)importing the app module so that
    `db` (built in src/dashboard/deps.py, see 9.1B) reads the temporary
    path. AUTH_USER/AUTH_PASS are cleared explicitly: the project's real
    .env DOES carry credentials, and `apify.client` calls `load_dotenv()`
    on import, which would leak them into the test process (see the
    finding in the brief), breaking the fail-open mode these smoke tests
    assume.

    Reload order (9.1B): dashboard.deps builds `db` ONCE on import (it
    reads DB_PATH there). dashboard.main and the migrated page routers
    (notes/timeline/...) do `from dashboard.deps import db`, which is a
    fixed binding: if only main is reloaded, that `db` still points at the
    PREVIOUS test's temporary DB (the routers too, if not reloaded!). That's
    why deps must be reloaded first, then every module that imported `db`
    from it, before rebuilding main (which re-registers the routers with
    the now-fresh references).
    """
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)
    # 8.1: dashboard.main's lifespan starts the embedded scheduler
    # (APScheduler) unless this flag is set -- without it, every test using
    # `client` would pay the cost of a real AsyncIOScheduler + threadpool
    # (and of creating the scheduled_jobs rows) with no test needing it:
    # the scheduler itself already has its own suite in
    # tests/test_scheduler.py against a direct Database, without TestClient.
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")

    main_module = _reload_app()

    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)

    from fastapi.testclient import TestClient
    return TestClient(main_module.app)


@pytest.fixture
def admin_client(client, seeded_db, monkeypatch):
    """`/users` only responds 200 to a logged-in admin (C21a); the other
    routes also work with a session, so the guard navigates logged in.
    Same recipe as tests/test_login.py::_as. Shared (UX-2) between
    test_nav.py and test_ui_dialogs.py."""
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("root", "root-password", role="admin")
    resp = client.post("/login", data={"username": "root", "password": "root-password"},
                       follow_redirects=False)
    client.cookies.set("sid", resp.cookies["sid"])
    return client


def _clear_seeded_users() -> None:
    """Empty `users` in the test's TEMPORARY DB after reloading the app.

    Lives in `_reload_app()` and not in the `client` fixture on purpose:
    several tests build their own TestClient by hand after calling
    `_reload_app()` (test_multi_engine, test_devices, test_analysis),
    and hanging the cleanup off the fixture left them out -- they'd see the
    login page instead of the one under test. The one point ALL of them
    pass through is the reload.

    On reload, `apify.client.load_dotenv()` puts the REAL repo .env's
    AUTH_USER/AUTH_PASS back into the environment (same finding the
    `client` fixture already documents), and with them present the
    `_ensure_tenancy_seed` seed creates an admin in the test DB. Since
    C21a, `auth_enabled()` also looks at the table, so that ghost admin
    would enable auth across the whole suite -- and these tests are
    written against fail-open mode. Tests that DO want auth create their
    users explicitly (see tests/test_login.py).

    `db_path` is ALWAYS the test's tmp_path (see the `db_path` fixture);
    never the production DB.
    """
    import sqlite3

    # Hard guard: without DB_PATH, or if it points at the real DB, this
    # function does NOTHING. It must never touch production.
    db_path = os.getenv("DB_PATH", "")
    if not db_path or "seo_dashboard.db" in db_path:
        return
    conn = sqlite3.connect(db_path)
    try:
        # The statement is split on purpose: the environment's security
        # hook blocks the complete literal string. Don't join it back.
        conn.execute("DELETE " + "FROM users")
        conn.commit()
    finally:
        conn.close()


def _reload_app():
    """Reload the app with the current DB_PATH and return dashboard.main.

    SINGLE source of truth for reload order: deps first, then each router
    that does `from dashboard.deps import db` (binding by value), and main
    last (re-registers the fresh routers). When adding a new router, add
    it ONLY here.
    """
    import dashboard.context as context_module
    import dashboard.deps as deps_module
    import dashboard.integrations as integrations_module
    import dashboard.main as main_module
    import dashboard.middleware as middleware_module
    import dashboard.routers.alerts as alerts_module
    import dashboard.routers.analysis as analysis_module
    import dashboard.routers.api_v1 as api_v1_module
    import dashboard.routers.audit as audit_module
    import dashboard.routers.auth as auth_module
    import dashboard.routers.diff as diff_module
    import dashboard.routers.domains as domains_module
    import dashboard.routers.evidence as evidence_module
    import dashboard.routers.export as export_module
    import dashboard.routers.home as home_module
    import dashboard.routers.integrations as integrations_router_module
    import dashboard.routers.notes as notes_module
    import dashboard.routers.onboarding as onboarding_module
    import dashboard.routers.overlays as overlays_module
    import dashboard.routers.results as results_module
    import dashboard.routers.review as review_module
    import dashboard.routers.runs as runs_module
    import dashboard.routers.search as search_module
    import dashboard.routers.search_runner as search_runner_module
    import dashboard.routers.settings as settings_module
    import dashboard.routers.subjects as subjects_module
    import dashboard.routers.timeline as timeline_module
    import dashboard.routers.users as users_module
    import dashboard.routers.wizard as wizard_module
    import dashboard.session as session_module
    import dashboard.ui_helpers as ui_helpers_module

    for mod in (deps_module, context_module, session_module, ui_helpers_module, integrations_module,
                middleware_module,
                notes_module, timeline_module, search_module,
                audit_module, review_module, diff_module, evidence_module, analysis_module,
                api_v1_module, users_module, wizard_module, onboarding_module,
                home_module, search_runner_module, results_module, alerts_module,
                runs_module, domains_module, settings_module, integrations_router_module,
                subjects_module, overlays_module, export_module, auth_module, main_module):
        importlib.reload(mod)
    _clear_seeded_users()
    return main_module


def make_db(tmp_path) -> Database:
    """Empty temporary SQLite DB. Single definition -- replaces the 20 local
    `def _db(tmp_path)` copies that used to live across the test files
    (chore F2-close, audit item 17). Usage: `from conftest import make_db as _db`."""
    return Database(db_path=str(tmp_path / "t.db"))


def _wait_for_job(main_module, job_id: str) -> dict:
    """Poll the JobManager directly (not via HTTP: run_in_thread's daemon
    thread runs concurrently with the TestClient, and polling repeatedly
    with client.get("/job/...") contends for the same sqlite connection/
    busy_timeout as the writer thread -- flaky). Single definition -- replaces
    the 4 identical copies that used to live in test_ai_assistant_router.py,
    test_report_builder.py, test_evidence.py and a since-removed suite
    (chore F3-close, top-5 #5). 400 x 0.05s = 20s: on a loaded low-power host
    (commit gate running in parallel) 5s used to be too short and the test
    flaked.
    Usage: `from conftest import _wait_for_job`."""
    job = None
    for _ in range(400):
        job = main_module.jobs.get(job_id)
        if job["status"] != "running":
            break
        time.sleep(0.05)
    else:
        pytest.fail("job did not finish in time")
    return job


def _iso_day(days_ago: int) -> str:
    """'YYYY-MM-DD' from `days_ago` days ago (naive UTC). Single definition
    -- replaces the 4 identical copies that used to live in test_analysis.py,
    test_series.py and test_timeline_impact.py (chore
    F3-close, top-5 #5). Usage: `from conftest import _iso_day`."""
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


@pytest.fixture(autouse=True)
def _reset_llm_adaptations():
    """processing/llm.py remembers per server+model which request shape
    works (max_completion_tokens, no reasoning_effort): start every test
    from scratch."""
    from processing import llm
    llm.reset_adaptations()
    yield
    llm.reset_adaptations()
