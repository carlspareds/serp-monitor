"""Embedded scheduler engine (8.1).

`AsyncIOScheduler` + `SQLAlchemyJobStore` over the SAME SQLite base as the
dashboard (the library creates its own `apscheduler_jobs` table), plus a
mirror table `scheduled_jobs` (see database/models.py) that exposes
cron/status/duration/next run to the /settings UI without coupling it to
APScheduler.

Direction of dependencies (ponytail): this module is a sibling of
processing/ and report/ -- it NEVER imports dashboard/. It receives the
Database already built via the start(db) parameter; it does not create it
or import it from dashboard.deps.

The actual jobs are registered in scheduler/jobs.py via the `job()`
decorator below; core.py does not import them (that would create a cycle)
-- whoever starts the scheduler (dashboard/main.py) imports scheduler.jobs
BEFORE calling start() so that the `JOBS` registry is populated.
"""
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from database.models import Database

# Declarative registry: name -> {"func": callable(db), "default_cron": str}.
JOBS: dict[str, dict] = {}

# H8 (7.3 review): jobs whose ticks WITHOUT work are not audited -- for a
# dense-cron job (e.g. one firing every minute), auditing every empty tick
# (the vast majority) would be pure noise in audit_log. Chose the simple
# route: a set of names + _had_work(result) over what the job function
# returns, instead of a generic "quiet result" mechanism. Empty today (no
# registered job is dense-cron enough to need it) -- add a name here if a
# future job does.
_QUIET_JOBS: set[str] = set()

# Process state (a single embedded scheduler per process, ADR-002: web and
# scheduler share the process). Deliberately simple (ponytail): no class or
# singleton -- functions + module state, same as `_runner` in main.py.
_scheduler: AsyncIOScheduler | None = None
_db: Database | None = None
_RUN_STARTED: dict[str, float] = {}

JOB_DEFAULTS = {"max_instances": 1, "misfire_grace_time": 3600, "coalesce": True}

_log = logging.getLogger("scheduler")


# FIX 2 (silent-alerts audit): APScheduler's CronTrigger.from_crontab()
# does NOT use standard crontab semantics for the 5th field (day_of_week)
# -- it maps onto ITS OWN day_of_week, where 0=MONDAY (see
# apscheduler.triggers.cron.fields.DayOfWeekField). Standard crontab (what
# any operator writes, and what every `default_cron` in scheduler/jobs.py
# assumes) uses 0=SUNDAY (7 is the alias, also Sunday).
# Verified in production: weekly_report ("0 8 * * 1", "Monday 08:00") was
# running on TUESDAY; purge_checks/ai_monitor_check/visual_check
# ("* * * 0", "Sunday") were running on MONDAY (or never, if next_run_at
# was left in the past of a Sunday that never arrives).
#
# _translate_crontab_dow() translates the day-of-week field from crontab
# semantics to APScheduler semantics BEFORE building the trigger -- applied
# both to the default crons of JOBS (start()) and to the cron the operator
# writes in /settings (set_job_cron()), which today was also coming out
# shifted. Formula: apscheduler_dow = (crontab_dow - 1) % 7 -- crontab
# 0/7(Sunday)->6, 1(Monday)->0, ..., 6(Saturday)->5, which is exactly
# APScheduler's 0=Monday..6=Sunday order.
def _translate_dow_value(token: str) -> str:
    if not token.isdigit():
        return token  # names (mon/tue/...) or other literal -- leave as is
    n = int(token)
    if 0 <= n <= 7:
        return str((n - 1) % 7)
    return token  # out-of-range value -- let from_crontab reject it


def _translate_dow_field(field: str) -> str:
    if field == "*":
        return field
    terms = []
    for term in field.split(","):
        base, sep, step = term.partition("/")
        if "-" in base:
            lo, _, hi = base.partition("-")
            base = f"{_translate_dow_value(lo)}-{_translate_dow_value(hi)}"
        else:
            base = _translate_dow_value(base)
        terms.append(base + (sep + step if sep else ""))
    return ",".join(terms)


def _translate_crontab_dow(cron_expr: str) -> str:
    """Translates the 5th field (day_of_week) of `cron_expr` from crontab
    semantics (0=Sunday) to APScheduler semantics (0=Monday) -- the other 4
    fields (minute/hour/day-of-month/month) do not change meaning between
    the two systems. A cron with a number of fields other than 5 is
    returned AS IS -- validating the format is not this helper's
    responsibility, `CronTrigger.from_crontab` already raises ValueError
    with its own message if the rest is malformed (see set_job_cron, m2)."""
    parts = cron_expr.split()
    if len(parts) != 5:
        return cron_expr
    parts[4] = _translate_dow_field(parts[4])
    return " ".join(parts)


def _trigger(cron_expr: str) -> CronTrigger:
    """CronTrigger for a crontab expression (crontab day-of-week semantics,
    see _translate_crontab_dow), always in UTC. Without an explicit
    timezone APScheduler uses the host's local zone, while the /settings UI
    and every default cron in scheduler/jobs.py are written in UTC."""
    return CronTrigger.from_crontab(_translate_crontab_dow(cron_expr), timezone=UTC)


def _same_trigger(a, b) -> bool:
    """True if two triggers fire at the same times. str(CronTrigger) leaves
    out the timezone, so it is compared separately: a job restored from the
    jobstore with a local-time trigger (saved before the scheduler was
    pinned to UTC) must be rescheduled even if its cron fields match."""
    return str(a) == str(b) and str(getattr(a, "timezone", "")) == str(getattr(b, "timezone", ""))


def job(name: str, default_cron: str) -> Callable:
    """Decorator: registers `func` in JOBS under `name` with its default
    cron. Does not alter the signature or the behavior of the function."""
    def wrapper(func: Callable) -> Callable:
        JOBS[name] = {"func": func, "default_cron": default_cron}
        return func
    return wrapper


def _had_work(result) -> bool:
    """H8: simple heuristic to decide whether a tick of a "quiet" job
    (_QUIET_JOBS) did something auditable. A dict result with any numeric
    value > 0 counts as real work (e.g. flush_notify_queue returns
    {'individual': n, 'incidents': n}). Any other result shape (None,
    non-dict) is treated as "had work" -- conservative: over-auditing never
    loses information, under-auditing does."""
    if not isinstance(result, dict):
        return True
    return any(isinstance(v, (int, float)) and v > 0 for v in result.values())


def _audit_job_started(db, job_name: str) -> None:
    try:  # C24: minimal audit record of the job start. Detailed
        # instrumentation of report/weekly.py and deadman comes with its
        # own refactor (see brief) -- this try/except prevents an audit
        # failure from bringing down the actual job.
        db.add_audit(actor_type="job", actor_id=job_name, action="job_run_started")
    except Exception:  # noqa: BLE001
        _log.exception("audit_failed", extra={"job": job_name})


def _dispatch(job_name: str, db_path: str) -> None:
    """Entry point that SQLAlchemyJobStore persists (top-level, no closures
    or lambdas: APScheduler only supports module:function references, see
    apscheduler.util.obj_to_ref). Rebuilds a fresh Database over `db_path`
    -- cheap (Database doesn't keep open connections, it opens one per call)
    and avoids depending on the live object being picklable across restarts
    -- and dispatches to the actual callable registered in JOBS.

    H8 (7.3 review): for "normal" jobs the behavior doesn't change -- it is
    audited BEFORE running, as always. For a _QUIET_JOBS one (a dense-cron
    job) it is audited AFTER, and only if `_had_work` says the result
    indicates real work -- avoids filling audit_log with empty "started"
    entries on every tick with nothing to do."""
    _RUN_STARTED[job_name] = time.monotonic()
    db = Database(db_path=db_path)
    if job_name not in _QUIET_JOBS:
        _audit_job_started(db, job_name)
        JOBS[job_name]["func"](db)
        return
    result = JOBS[job_name]["func"](db)
    if _had_work(result):
        _audit_job_started(db, job_name)


def _sync_next_run(name: str) -> None:
    if _scheduler is None or _db is None:
        return
    j = _scheduler.get_job(name)
    next_run = j.next_run_time.astimezone(UTC).isoformat() if (j and j.next_run_time) else None
    _db.update_job_next_run(name, next_run)


def _finish(job_id: str, status: str) -> None:
    if _db is None:
        return
    started = _RUN_STARTED.pop(job_id, None)
    duration_ms = int((time.monotonic() - started) * 1000) if started is not None else None
    _log.info("job_run", extra={"job": job_id, "status": status, "dur_ms": duration_ms})
    _db.record_job_run(job_id, status, duration_ms)
    _sync_next_run(job_id)


def _on_executed(event) -> None:
    _finish(event.job_id, "ok")


def _on_error(event) -> None:
    _finish(event.job_id, "error")


def start(db: Database) -> None:
    """Starts the scheduler against the shared `db`. For each job in JOBS:
    creates its row in scheduled_jobs if it didn't exist (preserving cron
    /enabled if the operator already changed them) and adds it to the
    scheduler with that trigger. Jobs are synchronous functions (blocking
    I/O) -> they run on AsyncIOScheduler's default ThreadPoolExecutor,
    never on the event loop. Requires an asyncio event loop already
    running (called by FastAPI's async lifespan)."""
    global _scheduler, _db
    _db = db
    jobstore = SQLAlchemyJobStore(url=f"sqlite:///{db.db_path}")
    _scheduler = AsyncIOScheduler(jobstores={"default": jobstore}, job_defaults=JOB_DEFAULTS,
                                  timezone=UTC)
    _scheduler.add_listener(_on_executed, EVENT_JOB_EXECUTED)
    _scheduler.add_listener(_on_error, EVENT_JOB_ERROR)

    # Start PAUSED so we can reconcile against the already-loaded jobstore
    # (before start() get_job/get_jobs don't see the persisted jobs).
    _scheduler.start(paused=True)

    for name, spec in JOBS.items():
        try:
            db.ensure_scheduled_job(name, spec["default_cron"])
            row = db.get_scheduled_job(name)
            trigger = _trigger(row["cron_expr"])
            existing = _scheduler.get_job(name)
            if existing is None:
                _scheduler.add_job(
                    _dispatch, args=[name, db.db_path], trigger=trigger,
                    id=name, name=name,
                )
            elif not _same_trigger(existing.trigger, trigger):
                # The operator changed the cron while the service was stopped,
                # or the persisted trigger is not in UTC: apply it.
                _scheduler.reschedule_job(name, trigger=trigger)
            # If the job already existed and the cron didn't change, it is NOT
            # re-added: this preserves the persisted next_run_time, and a run
            # missed during a restart is recovered via misfire_grace_time
            # (review M1: unconditional replace_existing discarded next_run
            # and the missed run was skipped until the next cycle).
            if not row["enabled"]:
                _scheduler.pause_job(name)
        except Exception as e:  # noqa: BLE001 -- a bad job must not bring down the app
            _log.error("job_register_failed", extra={"job": name, "error": str(e)})

    # Orphans: pickled jobs from a previous deploy that are no longer in
    # JOBS would keep triggering _dispatch -> KeyError on every cycle. Clean them up.
    for j in _scheduler.get_jobs():
        if j.id not in JOBS:
            _scheduler.remove_job(j.id)

    _scheduler.resume()
    for name in JOBS:
        _sync_next_run(name)  # next_run_time is only reliable after the resume


def is_running() -> bool:
    """True if the embedded scheduler is started (for /health, 9.2)."""
    return _scheduler is not None and bool(getattr(_scheduler, "running", False))


def shutdown() -> None:
    global _scheduler, _db
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
    _scheduler = None
    _db = None


def set_job_cron(name: str, cron_expr: str, enabled: bool = True) -> None:
    """Updates the mirror table and, if the scheduler is running, the job's
    actual trigger -- without restarting the process."""
    # Validate BEFORE persisting: an invalid cron written to the mirror
    # table would brick the next startup (review m2). from_crontab raises
    # ValueError if the expression is bad and nothing gets saved. FIX 2: the
    # cron the operator writes here is crontab semantics (0=Sunday), same
    # helper as start() -- it used to come out shifted by a day too.
    trigger = _trigger(cron_expr)
    if _db is not None:
        _db.set_scheduled_job_cron(name, cron_expr, enabled)
    if _scheduler is not None and _scheduler.get_job(name):
        _scheduler.reschedule_job(name, trigger=trigger)
        if enabled:
            _scheduler.resume_job(name)
        else:
            _scheduler.pause_job(name)
        _sync_next_run(name)


def list_jobs() -> list[dict]:
    """Reads the mirror table -- source of truth for the /settings UI."""
    if _db is None:
        return []
    return _db.list_scheduled_jobs()


def cron_period_seconds(cron_expr: str) -> float:
    """Approximates the period (seconds) between 2 consecutive firings of
    `cron_expr` (standard crontab semantics -- translated with the same
    _translate_crontab_dow as start()/set_job_cron()) -- used by
    scheduler.deadman (FIX 3, silent-alerts audit) to calibrate a grace
    window PROPORTIONAL to each job's cadence, instead of the single 1h
    constant that left high-frequency jobs (e.g. every minute) up to 60
    dead cycles before alerting.

    Computes the next 2 firings from "now" and returns the difference --
    works equally well for regular crons ("* * * * *") as for irregular
    ones (fixed day-of-month, etc): it always measures the REAL distance to
    the next-after-next firing, never assumes a fixed cadence from the
    cron text itself."""
    trigger = _trigger(cron_expr)
    anchor = datetime.now(UTC)
    first = trigger.get_next_fire_time(None, anchor)
    second = trigger.get_next_fire_time(first, first)
    return (second - first).total_seconds()
