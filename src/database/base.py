"""Base database connection, multi-tenant scoping, and identity helpers."""
import contextlib
import hashlib
import os
import re
import secrets
import sqlite3
from contextvars import ContextVar, Token
from datetime import date
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from auth.passwords import hash_password

if TYPE_CHECKING:
    from database.models import Database

# Tracking parameters discarded when normalizing a URL (2.5): they aren't part of the
# URL's identity, only of the click's origin.
# srsltid is the one that mattered in practice: Google appends a fresh random value
# to many result URLs on EVERY search, so without stripping it one page became a
# "new" URL each run (and lost its human verdict).
_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAMS = {"fbclid", "gclid", "srsltid", "mc_cid", "mc_eid", "_hsenc", "_hsmi",
                    "ref_src", "igshid", "msclkid"}
# Bumped whenever normalize_url changes what counts as "the same page": the startup
# migration (_ensure_url_keys) then recomputes search_results.url_key, and
# scripts/repair_url_identity.py merges the other url_hash-keyed tables (re-run it
# after an upgrade that bumps this). "3": locale path prefixes (_LOCALE_PATH_RULES).
URL_KEY_VERSION = "3"

# Sites that put the visitor's language/storefront in the PATH of one and the
# same page: open.spotify.com/intl-es/artist/X is open.spotify.com/artist/X.
# Conservative on purpose: exact host, and for Apple only a 2-letter storefront
# directly followed by a known content type -- anything else is left alone.
# (host, prefix regex on the path; the match is removed from the identity key)
_LOCALE_PATH_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("open.spotify.com", re.compile(r"^/intl-[a-z]{2}(?:-[a-z0-9]{2,4})?(?=/|$)", re.IGNORECASE)),
    ("music.apple.com", re.compile(
        r"^/[a-z]{2}(?=/(?:album|artist|playlist|song|music-video|station|curator)/)", re.IGNORECASE)),
    ("podcasts.apple.com", re.compile(r"^/[a-z]{2}(?=/podcast/)", re.IGNORECASE)),
)


def strip_locale_path(host: str, path: str) -> str:
    """`path` without a known locale prefix of `host` (lowercase, no port)."""
    for rule_host, rx in _LOCALE_PATH_RULES:
        if host == rule_host:
            return rx.sub("", path, count=1) or "/"
    return path


def is_tracking_param(name: str) -> bool:
    """True for query parameters that only say where a click came from."""
    k = (name or "").lower()
    return k.startswith(_TRACKING_PARAM_PREFIXES) or k in _TRACKING_PARAMS


def strip_tracking(url: str) -> str:
    """`url` without tracking parameters and fragment, otherwise untouched
    (scheme, host case, parameter order kept) -- what is STORED for new
    results, so the saved link still opens the same page."""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if not is_tracking_param(k)]
    query = parts.query if len(kept) == len(pairs) else urlencode(kept)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def normalize_url(url: str) -> str:
    """Normalizes a URL for stable identity (2.5): default scheme
    https, lowercase host, no trailing slash, sorted query, no fragment,
    no tracking parameters (utm_*, srsltid, fbclid, gclid, mc_cid,
    mc_eid, _hsenc, _hsmi, ref_src, igshid, msclkid) and no known locale
    path prefix (_LOCALE_PATH_RULES: Spotify /intl-xx, Apple /xx). It's the basis of
    url_hash() — the URL identity key for the whole system.
    Keep it aligned with the conventions
    of _normalize_url in apify/actors.py (1.1, review LOW #8 -- it used to
    live in apify/runner.py until _flatten_results became
    normalize_google; dedup of the historical data)."""
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    kept = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not is_tracking_param(k)
    )
    netloc = parts.netloc.lower()
    path = strip_locale_path(netloc.split(":")[0], parts.path.rstrip("/") or "/")
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(kept), ""))


def url_hash(url: str) -> str:
    """sha256 hexdigest of the normalized URL (see normalize_url)."""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


# Default confidence threshold (2.1, settings.review_threshold): a single value -- used by
# Database.review_threshold() and the default of the /settings/review-threshold Form in
# dashboard.main.
REVIEW_THRESHOLD_DEFAULT = 0.7

# Neutral-conservative severity for negatives without a `severity` calculated yet (4.3,
# series_stats.severity_sum): same canonical value as processing.alerts.NULL_SEVERITY (3,
# "medium"). Duplicated as a literal here INSTEAD of imported -- importing
# processing.alerts would create a cycle (processing.alerts -> processing.diff ->
# database.models, see processing/diff.py::`from database.models import url_hash`). If the
# canonical value changes, update this constant too (covered by
# tests/test_series.py::test_series_null_severity_matches_alerts_canon).
_SERIES_NULL_SEVERITY = 3


# Routing by severity (7.1): default channel if `alert_routing` is empty (first startup or
# a preexisting DB with the table unpopulated).
_DEFAULT_ROUTING = {"critical": "telegram", "warning": "email", "info": "digest"}

# Client branding for exports (5.1, deep dive §5.1): default colors IDENTICAL to the ones
# the executive PDF already had hardcoded before this feature (pdf_report._DARK/_BLUE) --
# without a `branding` row (first startup), the PDF/Excel look EXACTLY the same as today.
# get_branding()/set_branding() fall back to these values on a color outside #rrggbb
# instead of saving garbage or raising.
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_BRANDING_DEFAULTS = {
    "logo_path": "",
    "primary_color": "#14233F",
    "secondary_color": "#1F4E9C",
    "footer_text": "",
}




# Whitelist of management workflow states (4.6, deep dive §4.6): snake_case in the DB,
# readable label in the UI via NOTE_STATUS_LABELS. Public (no leading underscore) on
# purpose -- dashboard.routers.notes needs it to build the board's <select> and the form,
# same approach as notify.dispatcher.ROUTING_CHANNELS (also public so main.py can import
# it directly). Database.set_note_status validates AGAINST this tuple, not against a
# transition graph -- any jump between whitelisted states is valid (a simple kanban, not a
# strict state machine); "dismissed" is a legitimate exit from any state.
NOTE_STATUSES = ("open", "in_progress", "escalated", "resolved", "dismissed")
NOTE_STATUS_LABELS = {
    "open": "Open",
    "in_progress": "In progress",
    "escalated": "Escalated",
    "resolved": "Resolved",
    "dismissed": "Dismissed",
}
# Values persisted by older databases (the workflow states used to be stored in Spanish).
# MigrationsMixin._ensure_note_status rewrites them on startup; normalize_note_status also
# maps them on read/write so a straggler (or an old form/API client) keeps working.
LEGACY_NOTE_STATUSES = {
    "pendiente": "open",
    "en_gestion": "in_progress",
    "escalado": "escalated",
    "resuelto": "resolved",
    "descartado": "dismissed",
}
# "Closed" states: an overdue note in one of these does NOT alert (there's nothing left to
# manage) -- see note_is_overdue/Database.overdue_notes.
_NOTE_CLOSED_STATUSES = frozenset({"resolved", "dismissed"})


def normalize_note_status(status: str | None) -> str:
    """Canonical NOTE_STATUSES value for `status`: legacy values are translated,
    and anything empty/unknown falls back to 'open' (a note is never lost from the
    board because of an unexpected value)."""
    s = (status or "").strip()
    s = LEGACY_NOTE_STATUSES.get(s, s)
    return s if s in NOTE_STATUSES else "open"


# API keys (9.3, deep dive §9.3): simple scopes -- 'read' (default, required by all the
# read-only endpoints of /api/v1/...), 'write:alerts' (C22: the only write today -- PATCH
# /api/v1/alerts/{id}, see require_scope in dashboard.routers.api_v1) and 'admin' (a
# ceiling that covers any scope, including write:alerts -- see require_scope).
API_KEY_SCOPES = ("read", "admin", "write:alerts")
API_KEY_SCOPE_DEFAULT = "read"


def note_is_overdue(due_date: str, status: str, today: str) -> bool:
    """True if `due_date` (ISO 'YYYY-MM-DD') has already passed and
    `status` isn't one of the closed states (_NOTE_CLOSED_STATUSES) --
    the single criterion shared by the red badge in /notes,
    Database.overdue_notes and processing.alerts.check_overdue_notes
    (DRY: a single definition of "overdue"). due_date/today empty or
    malformed => False (no due date, NEVER raises -- same defensive
    approach as processing.event_impact.parse_event_date)."""
    if normalize_note_status(status) in _NOTE_CLOSED_STATUSES:
        return False
    try:
        due = date.fromisoformat((due_date or "").strip()[:10])
        today_d = date.fromisoformat((today or "").strip()[:10])
    except ValueError:
        return False
    return due < today_d



# --------------------------------------------------------------------- #
# Multi-tenant (C21a). These constants live HERE and not in dashboard.main because the
# seeding runs inside `Database._init_db`, and models.py can't import dashboard (cycle:
# dashboard.deps builds the Database). dashboard.main re-exports them so there's a SINGLE
# source of truth for the default subject.
# --------------------------------------------------------------------- #
DEFAULT_CLIENT_NAME = "My project"
# The name fresh installs used to give project 1. _ensure_default_project_name renames
# it to DEFAULT_CLIENT_NAME at startup -- ONLY when it is still exactly this string
# (a name someone typed is never touched).
LEGACY_DEFAULT_CLIENT_NAME = "Internal"
# Project the demo subject lives in (scripts/seed_demo_data.py), so a demo install
# reads "Demo ▸ Jordan Ashworth (demo)" and real subjects go to DEFAULT_CLIENT_NAME.
DEMO_CLIENT_NAME = "Demo"
DEFAULT_CLIENT_ID = 1
DEFAULT_SUBJECT_QUERY = ""
DEFAULT_SUBJECT_COUNTRIES = "us,ca,gb"
# C21b (brief arbitration, point 1 -- "lock first, filtering second"): while scoping
# doesn't yet cover the data tables, creating a second subject would be a silent leak, not
# a remote possibility -- `add_subject` checks it and refuses while it's False. It flips
# to True ONLY when tests/test_scoping_guard.py (the static guard) confirms that no method
# is left unfiltered without a reason written in its allowlist. A test monkeypatches it to
# True to exercise the happy path (see tests/test_scoping.py) -- the lock itself isn't
# touched by hand in production until that guard is truly clean.
# Multi-subject phase 1: the last global key (url_lifecycle's PK) now includes subject_id,
# positive/negative domains are per subject, and test_scoping_guard.py is clean -- the
# lock is open.
SCOPING_COMPLETE = True
# The subject a Database() with no explicit subject sees: the primary one, the one this
# deployment watches. See Database._subject_clause for why the default filters instead of
# showing everything.
DEFAULT_SUBJECT_ID = 1
# Sentinel for "I want to see ALL subjects" (cost aggregates, maintenance jobs,
# migrations). It's its own object and not None on purpose: None is what arrives when
# someone forgot to pass the subject, and confusing an oversight with an explicit request
# to see everything is exactly the failure this mechanism exists to prevent.
_ALL_SUBJECTS = object()

# Request-scoped active subject (multi-subject phase 1). A Database instance WITHOUT an
# explicit subject (the `db` singleton of dashboard.deps, the one ~30 routers import)
# resolves its subject as: explicit for_subject() id -> this ContextVar -> DEFAULT_SUBJECT_ID.
#
# Why a ContextVar and not a mutable attribute on the singleton: each asyncio task (one
# per request) gets its own copy of the context, and Starlette's threadpool
# (anyio.to_thread.run_sync, used for sync `def` endpoints and sync dependencies) runs the
# callable inside a COPY of the caller's context -- so a value set by an ASGI middleware
# or an async dependency is visible to the sync endpoint, and two concurrent requests
# never see each other's value.
#
# What does NOT inherit it: a `threading.Thread` started by hand (Python 3.12 starts each
# new thread with an EMPTY context), APScheduler jobs, and anything submitted to a
# ThreadPoolExecutor without copy_context(). Background work must therefore carry the
# subject explicitly: `db.for_subject(sid)` for the data, and `subject_scope(sid)` around
# it when the work calls code that reads the module-level injected Database (e.g.
# processing.ai_classifier.subject_name()).
#
# A value set in a SYNC dependency does NOT reach the endpoint either (the dependency ran
# in its own copied context in the threadpool): set it in an ASGI middleware or in an
# `async def` dependency.
_ACTIVE_SUBJECT: ContextVar[int | None] = ContextVar("serp_active_subject", default=None)


def set_active_subject(subject_id: int | None) -> Token:
    """Sets the request-scoped active subject; returns the token for reset_active_subject."""
    if subject_id is not None:
        subject_id = int(subject_id)
    return _ACTIVE_SUBJECT.set(subject_id)


def reset_active_subject(token: Token) -> None:
    _ACTIVE_SUBJECT.reset(token)


def current_subject_id() -> int:
    """The active subject of the CURRENT context (ContextVar), or DEFAULT_SUBJECT_ID.
    Doesn't know about for_subject() instances -- use Database._effective_subject_id()
    when you have an instance."""
    sid = _ACTIVE_SUBJECT.get()
    return sid if sid is not None else DEFAULT_SUBJECT_ID


@contextlib.contextmanager
def subject_scope(subject_id: int):
    """`with subject_scope(sid): ...` -- sets the ContextVar for the block and restores
    it afterwards. For background threads/jobs that don't inherit the request's
    context (see _ACTIVE_SUBJECT)."""
    token = set_active_subject(subject_id)
    try:
        yield subject_id
    finally:
        reset_active_subject(token)
# Roles: `admin` manages users and settings; `operator` uses the dashboard;
# `client_viewer` is a client's read-only portal (its real scoping arrives in C21b). No
# CHECK on the table -- SQLite doesn't allow adding one via ALTER, same approach as
# NOTE_STATUSES/REMOVAL_STATUSES: it's validated in Python, in add_user.
USER_ROLES = ("admin", "operator", "client_viewer")
# Hash of a password nobody can have (random bytes): it's checked against when the user
# doesn't exist, so the login costs the same whether they exist or not. It's calculated
# ONCE at import time, not per attempt.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_hex(32))




API_KEY_PREFIX = "sm_live_"
DB_PATH = os.environ.get("DB_PATH", "data/seo_dashboard.db")
DEFAULT_SUBJECT_NAME = "Default"

_SUBJECT_TABLES = (
    "search_results", "runs", "alerts", "url_lifecycle",
    "evidence", "events", "url_notes", "goals", "note_history",
    # Multi-subject phase 1: each subject owns its own positive/negative domain lists
    # (PK (domain, subject_id), see MigrationsMixin._ensure_subject_scoped_uniques).
    "positive_domains", "negative_domains",
)

_VALID_MENTION_SOURCES = ("reddit", "news", "reviews")
_VALID_VISUAL_KINDS = ("image", "video")
_VALID_ASSET_TYPES = ("owned", "earned")
_VALID_ASSET_STATUSES = ("idea", "draft", "published", "ranking")


class BaseDatabase:
    """Core SQLite connection and multi-tenant scoping manager."""

    _SUBJECT_TABLES = _SUBJECT_TABLES
    _VALID_MENTION_SOURCES = _VALID_MENTION_SOURCES
    _VALID_VISUAL_KINDS = _VALID_VISUAL_KINDS
    _VALID_ASSET_TYPES = _VALID_ASSET_TYPES
    _VALID_ASSET_STATUSES = _VALID_ASSET_STATUSES

    def __init__(self, db_path: str = "data/seo_dashboard.db"):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        # ponytail: AI cache hit-rate (2.5) in the process's memory — a "session hit-
        # rate", it resets on restart. Good enough for /settings; if a persistent history
        # were needed, move it up to a table/setting.
        self._ai_cache_hits = 0
        self._ai_cache_misses = 0
        # C21b: None = no active subject, behavior IDENTICAL to before this item
        # (compatibility, mandatory requirement 4) -- filtering by subject only activates
        # on instances returned by for_subject().
        self._subject_id: int | None = None
        self._init_db()

    def for_subject(self, subject_id: int) -> "Database":
        """Returns a NEW, lightweight instance that shares `db_path` with
        `self` but carries `subject_id` as the active subject for filtering
        (C21b). Deliberately NOT a mutable attribute on `self`: the
        `db` singleton (dashboard.deps) is imported by ~30 routers with `from
        dashboard.deps import db`, and two concurrent requests from
        different subjects stepping on that attribute would be a real
        data race -- request A could read with the subject B just
        set. A new instance per call is cheap (it doesn't
        rerun `_init_db`/migrations, `object.__new__` deliberately skips
        `__init__`) and doesn't share mutable state with anyone.

        The AI cache counters (_ai_cache_hits/_ai_cache_misses,
        the "session hit-rate" of /settings) start at 0 on the new
        instance -- if a scoped instance is used for AI calls, that
        hit-rate stops accumulating on the singleton; the real
        session data keeps living in `db` (dashboard.deps), which is the
        only instance that feeds /settings today."""
        if subject_id is None:
            # `None` is what arrives when the caller forgot to resolve the subject (e.g. a
            # user without a subject assigned in C21c). It used to degrade to "sees
            # EVERYTHING", which is failing open in the one system mechanism whose job is
            # to close.
            raise ValueError(
                "for_subject(None): use for_all_subjects() if you really want to see "
                "every subject; None usually means an unresolved subject."
            )
        return self._clone_with_subject(subject_id)

    def for_all_subjects(self) -> "Database":
        """Instance that does NOT filter by subject: for deployment-wide
        aggregates (monthly Apify cost, maintenance jobs,
        migrations). Exists so that "see everything" is a phrase someone
        wrote on purpose and can be found with grep, instead of the side
        effect of not having passed a subject."""
        return self._clone_with_subject(_ALL_SUBJECTS)

    def _clone_with_subject(self, subject_id):
        scoped = object.__new__(self.__class__)
        scoped.db_path = self.db_path
        scoped._ai_cache_hits = 0
        scoped._ai_cache_misses = 0
        scoped._subject_id = subject_id
        return scoped

    def _effective_subject_id(self) -> int:
        """The subject this instance reads/writes: an explicit for_subject() id ALWAYS
        wins; otherwise the request-scoped ContextVar (set_active_subject); otherwise
        DEFAULT_SUBJECT_ID. A for_all_subjects() instance has no single subject -- it
        answers DEFAULT_SUBJECT_ID here (only reachable from writes, which need one)."""
        sid = self._subject_id
        if sid is _ALL_SUBJECTS or sid is None:
            return current_subject_id()
        return sid

    def _subject_clause(self, alias: str = "") -> tuple[str, list]:
        if self._subject_id is _ALL_SUBJECTS:
            return "1=1", []
        prefix = f"{alias}." if alias else ""
        return f"{prefix}subject_id = ?", [self._effective_subject_id()]

    def _active_subject_id(self, explicit: int | None) -> int:
        """Resolves the effective subject_id of an INSERT into one of the 5
        core tables: the `explicit` value the CALLER passes always wins
        (e.g. a caller writing on behalf of another subject without going
        through for_subject) -- if it isn't
        given, the active subject of this instance is used (for_subject) and,
        failing both, 1 -- EXACTLY the default from before C21b,
        so that a Database() with no subject keeps writing the same as
        it always did (mandatory requirement 4)."""
        if explicit is not None:
            return explicit
        return self._effective_subject_id()

    # ------------------------------------------------------------------ #
    # Connection / schema
    # ------------------------------------------------------------------ #
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Opening PRAGMAs (3.3): WAL for concurrent readers/writers, NORMAL (faster, safe
        # under WAL) and a generous busy_timeout so a writer doesn't immediately fail with
        # "database is locked". They're run on every connection because
        # synchronous/busy_timeout are connection-scoped (journal_mode does persist in the
        # file, but repeating it here is cheap and idempotent).
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn


