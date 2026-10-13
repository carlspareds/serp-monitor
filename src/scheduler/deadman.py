"""Dead-man's switch for jobs (8.2)
================================
Job `deadman_check` (registered in scheduler/jobs.py, cron `*/15 * * * *`):
scans the mirror table `scheduled_jobs` and generates a `job_missed` alert
for any enabled job that should have run and didn't -- e.g. an operator
who paused the scheduler process (or the job itself via APScheduler)
without disabling it in the mirror table, or a job that started failing
before it even fired the listener that updates `last_run_at`.

Lives outside scheduler/core.py (which is the generic engine, unaware of
alert kinds or notify.dispatcher) as a sibling module of report/ and
scheduler/jobs.py -- same pattern as weekly.run_alert_checks: a `check(db)`
function that scheduler/jobs.py registers with `job()`.

THE TRAP (timezone): `next_run_at` is written by
scheduler.core._sync_next_run from `next_run_time.isoformat()` from
APScheduler -- aware, with the process's LOCAL offset (e.g. "-04:00" on a
host in a UTC-4 timezone). `last_run_at` is written by
Database.record_job_run -- naive, UTC. Comparing both strings (or
comparing an aware one with a naive one) without normalizing first compares
apples to oranges: processing.auto_resolve.parse_utc() brings both to
aware-UTC datetimes before any comparison.
"""
from datetime import UTC, datetime, timedelta

from notify.dispatcher import _dispatch_now
from processing.auto_resolve import auto_resolve, parse_utc
from scheduler import core as scheduler_core

KIND = "job_missed"
# Name under which jobs.py registers this job (self-exclusion from the
# loop): shared constant so a rename doesn't silently break the exclusion.
SELF_NAME = "deadman_check"

# Historical GRACE_SECONDS: now only the FALLBACK when the proportional
# grace (see _grace_seconds, FIX 3) cannot be computed (cron_expr missing
# or unreadable) -- conservative, must never bring down the pass.
GRACE_SECONDS = 3600

# FIX 3 (silent-alerts audit): GRACE_SECONDS used to be a GLOBAL 1h
# constant -- correct for daily/weekly jobs, but a job running every
# minute ("* * * * *") could be dead for 60 cycles before anyone noticed.
# Window proportional to the job's real cadence:
# max(_MIN_GRACE_SECONDS, 2 × period) -- scheduler.core.cron_period_seconds
# measures the real period between 2 consecutive firings of the trigger
# itself (same crontab->APScheduler translation as start()/set_job_cron(),
# FIX 2). The 300s floor avoids an absurdly short grace for sub-minute
# crons (none exist today, but the floor is cheap).
_MIN_GRACE_SECONDS = 300


def _grace_seconds(row: dict) -> float:
    """Proportional grace (FIX 3) for `row` -- max(300, 2 × period of THAT
    job's cron). Falls back to the historical GRACE_SECONDS (1h) if
    cron_expr is missing or unreadable: same conservative approach as
    _is_missed with corrupt timestamps (C1) -- one weird row must not bring
    down the pass."""
    cron_expr = row.get("cron_expr")
    if not cron_expr:
        return GRACE_SECONDS
    try:
        period = scheduler_core.cron_period_seconds(cron_expr)
    except Exception:  # noqa: BLE001 -- conservative fallback, see docstring
        return GRACE_SECONDS
    return max(_MIN_GRACE_SECONDS, 2 * period)


def _is_missed(row: dict, now: datetime) -> bool:
    """True if `row` (a scheduled_jobs row) should have run and didn't, as
    of `now` (aware-UTC). A job with no next_run_at (the scheduler hasn't
    synced yet), disabled, or with corrupt timestamps never counts as
    missed (review 8.2 C1/C2: one weird row must not bring down the pass,
    and a disabled job doesn't matter to us -- its open alert gets
    closed)."""
    if not row.get("enabled"):
        return False
    next_run_at = row.get("next_run_at")
    if not next_run_at:
        return False
    try:
        next_run = parse_utc(next_run_at)
        grace = _grace_seconds(row)
        if now <= next_run + timedelta(seconds=grace):
            return False  # still within the grace window
        last_run_at = row.get("last_run_at")
        if not last_run_at:
            return True  # never ran and the grace period has already passed
        return parse_utc(last_run_at) < next_run  # ran before the expected firing
    except ValueError as e:
        print(f"[deadman] unreadable timestamp in {row.get('name')!r}: {e}")
        return False


def check(db) -> dict:
    """Scans scheduled_jobs and creates/resolves `job_missed` alerts.
    Auto-resolution (7.2) first -- so a job that ran again between two
    passes closes its alert before re-evaluating whether it's still
    missed. Returns {"new": n} (new alerts created in this pass)."""
    now = datetime.now(UTC)
    rows = {r["name"]: r for r in db.list_scheduled_jobs()}

    auto_resolve(
        db, KIND,
        lambda a: a["country"] not in rows or not _is_missed(rows[a["country"]], now),
    )

    new = 0
    for name, row in rows.items():
        if name == SELF_NAME:
            continue
        if not _is_missed(row, now):  # covers enabled=0 and corrupt timestamps
            continue
        severity = "warning"
        grace = int(_grace_seconds(row))
        message = (
            f"Job '{name}' did not run within the expected window "
            f"(next_run_at + {grace}s grace)."
        )
        alert_id = db.add_alert(KIND, severity, name, message, f"job_missed:{name}")
        if alert_id:
            new += 1
            # H1(b) (7.3 review): _dispatch_now DIRECTLY, not via
            # emit_alert/notify_alert. The watchdog must never depend on a
            # conduit that a scheduled job drains (e.g. the info digest),
            # because that job may be precisely the one that is down.
            # _dispatch_now is immediate and doesn't depend on any other job.
            _dispatch_now(db, {
                "id": alert_id, "kind": KIND, "severity": severity,
                "country": name, "message": message,
            })
    return {"new": new}
