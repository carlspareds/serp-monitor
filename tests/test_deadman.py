"""Tests for item 8.2: dead-man's switch for jobs (scheduler.deadman).

TDD, without a live scheduler: the logic is pure over the mirror table
`scheduled_jobs`, seeded by hand (sqlite3 directly, same pattern as
test_alert_routing.py::test_legacy_severities_are_migrated_on_reopen) to
control exactly next_run_at/last_run_at per scenario -- including THE
TRAP of timezones (next_run_at with local offset, last_run_at naive-UTC).
"""
import sqlite3
from datetime import UTC, datetime, timedelta

from conftest import make_db as _db

from scheduler import deadman


def _seed_job(db_path: str, name: str, enabled: int = 1,
              last_run_at: str | None = None, next_run_at: str | None = None,
              cron_expr: str = "*/15 * * * *") -> None:
    """Insert/update a scheduled_jobs row with exact values --
    Database does not expose a setter that accepts arbitrary
    last_run_at/next_run_at (record_job_run always uses "now"), so it writes
    directly, same as other tests already do to seed legacy rows.

    `cron_expr` (FIX 3, silent-alerts audit): default '*/15 * * * *'
    preserves the behavior of ALL existing tests -- period
    900s, proportional grace max(300, 2×900)=1800s (30min), stays
    below any "overdue" scenario (2h) and above any
    "within grace" (10min) already used in this file. Tests that DO want
    to exercise the proportionality pass a distinct explicit cron."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO scheduled_jobs (name, cron_expr, enabled, last_run_at, next_run_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET cron_expr=excluded.cron_expr, "
        "enabled=excluded.enabled, "
        "last_run_at=excluded.last_run_at, next_run_at=excluded.next_run_at",
        (name, cron_expr, enabled, last_run_at, next_run_at),
    )
    conn.commit()
    conn.close()


NOW = datetime.now(UTC).replace(microsecond=0)


def _iso_naive_utc(dt: datetime) -> str:
    """Like Database.record_job_run: naive, no offset (UTC is assumed)."""
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat()


def _iso_local_offset(dt: datetime, offset_hours: int) -> str:
    """Like APScheduler via next_run_time.isoformat(): aware, with local
    offset (e.g. -04:00 on a UTC-4 host)."""
    from datetime import timezone
    tz = timezone(timedelta(hours=offset_hours))
    return dt.astimezone(tz).isoformat()


# --------------------------------------------------------------------- #
# Base scenarios
# --------------------------------------------------------------------- #
def test_overdue_job_without_last_run_creates_alert(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(deadman, "_dispatch_now", lambda *a, **k: None)
    overdue = NOW - timedelta(hours=2)
    _seed_job(db.db_path, "alerts_check", enabled=1, last_run_at=None,
              next_run_at=_iso_naive_utc(overdue))

    result = deadman.check(db)

    assert result["new"] == 1
    rows = db.list_alerts(status="open")
    assert len(rows) == 1
    assert rows[0]["kind"] == "job_missed"
    assert rows[0]["country"] == "alerts_check"


def test_job_that_ran_after_next_run_does_not_alert(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(deadman, "_dispatch_now", lambda *a, **k: None)
    next_run = NOW - timedelta(hours=2)
    ran_at = next_run + timedelta(minutes=1)  # ran just after it fired
    _seed_job(db.db_path, "alerts_check", enabled=1,
              last_run_at=_iso_naive_utc(ran_at), next_run_at=_iso_naive_utc(next_run))

    result = deadman.check(db)

    assert result["new"] == 0
    assert db.list_alerts(status="open") == []


def test_job_within_grace_window_does_not_alert(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(deadman, "_dispatch_now", lambda *a, **k: None)
    # Overdue by 10 minutes: within the proportional grace of _seed_job's
    # default cron ('*/15 * * * *' -> 30min, see FIX 3 below).
    #
    # LIVE clock, not the module-level `NOW`: `NOW` is fixed at IMPORT time
    # of the file (during pytest collection), and the full suite takes
    # ~45-57 min on this board. By the time this test actually runs, that
    # "10 minutes of delay" measured against a stale NOW is already over an
    # hour of real delay, and the job falls outside the grace -> false failure.
    # Same fix already applied to the two proportional-grace tests.
    next_run = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    _seed_job(db.db_path, "alerts_check", enabled=1, last_run_at=None,
              next_run_at=_iso_naive_utc(next_run))

    result = deadman.check(db)

    assert result["new"] == 0
    assert db.list_alerts(status="open") == []


# --------------------------------------------------------------------- #
# FIX 3 (silent-alerts audit): grace proportional to the job's
# cadence -- max(300, 2×period) instead of the single 3600s constant that
# left an every-minute job ("* * * * *") up to 60 dead cycles before
# alerting.
# --------------------------------------------------------------------- #
def test_grace_seconds_floor_for_high_frequency_job():
    """An every-minute job ('* * * * *', period 60s): 2×60=120 < floor of 300 ->
    the grace is the floor, not double the period."""
    assert deadman._grace_seconds({"cron_expr": "* * * * *"}) == 300


def test_grace_seconds_scales_with_slower_cron():
    """deadman_check itself ('*/15 * * * *', period 900s): grace
    2×900=1800, well below the historical 3600s constant -- a 15-minute
    job can no longer be silent for a full hour without alerting."""
    assert deadman._grace_seconds({"cron_expr": "*/15 * * * *"}) == 1800


def test_grace_seconds_falls_back_to_constant_without_cron_expr():
    assert deadman._grace_seconds({"cron_expr": None}) == deadman.GRACE_SECONDS
    assert deadman._grace_seconds({}) == deadman.GRACE_SECONDS


def test_grace_seconds_falls_back_to_constant_on_illegible_cron():
    assert deadman._grace_seconds({"cron_expr": "not a cron"}) == deadman.GRACE_SECONDS


def test_high_frequency_job_alerts_much_sooner_than_the_old_1h_constant(tmp_path, monkeypatch):
    """Direct regression of the bug: a job every minute, dead for 6
    minutes (well within the old 3600s grace, but MUCH above the
    new proportional grace of 300s), must alert."""
    db = _db(tmp_path)
    monkeypatch.setattr(deadman, "_dispatch_now", lambda *a, **k: None)
    # LIVE `datetime.now(UTC)`, not the module-level `NOW` (fixed at
    # collection of the full suite -- with a margin of only a few minutes
    # against the 300s grace, the real clock could have advanced more than
    # that by the time this test actually runs; see the fix's own
    # regression: a test with a tight margin needs the real "now", not a
    # snapshot from who knows how long ago).
    next_run = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=6)
    _seed_job(db.db_path, "minutely_job", enabled=1, last_run_at=None,
              next_run_at=_iso_naive_utc(next_run), cron_expr="* * * * *")

    result = deadman.check(db)

    assert result["new"] == 1
    alert = db.list_alerts(status="open")[0]
    assert alert["country"] == "minutely_job"
    # the message reports the grace actually applied, not the 3600s fallback
    assert "300s grace" in alert["message"]


def test_high_frequency_job_within_its_own_tighter_grace_does_not_alert(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(deadman, "_dispatch_now", lambda *a, **k: None)
    # Same reason as above: real "now", not the module-level NOW -- the
    # margin against the 300s grace is too tight to depend on a timestamp
    # fixed potentially minutes before this test actually runs within the
    # full suite.
    next_run = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
    _seed_job(db.db_path, "minutely_job", enabled=1, last_run_at=None,
              next_run_at=_iso_naive_utc(next_run), cron_expr="* * * * *")

    result = deadman.check(db)

    assert result["new"] == 0
    assert db.list_alerts(status="open") == []


def test_disabled_job_does_not_alert(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(deadman, "_dispatch_now", lambda *a, **k: None)
    overdue = NOW - timedelta(hours=2)
    _seed_job(db.db_path, "alerts_check", enabled=0, last_run_at=None,
              next_run_at=_iso_naive_utc(overdue))

    result = deadman.check(db)

    assert result["new"] == 0
    assert db.list_alerts(status="open") == []


def test_null_next_run_does_not_alert(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(deadman, "_dispatch_now", lambda *a, **k: None)
    _seed_job(db.db_path, "alerts_check", enabled=1, last_run_at=None, next_run_at=None)

    result = deadman.check(db)

    assert result["new"] == 0
    assert db.list_alerts(status="open") == []


def test_deadman_check_itself_is_never_evaluated(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(deadman, "_dispatch_now", lambda *a, **k: None)
    overdue = NOW - timedelta(hours=2)
    _seed_job(db.db_path, "deadman_check", enabled=1, last_run_at=None,
              next_run_at=_iso_naive_utc(overdue))

    result = deadman.check(db)

    assert result["new"] == 0
    assert db.list_alerts(status="open") == []


# --------------------------------------------------------------------- #
# THE TRAP: mixed timezones
# --------------------------------------------------------------------- #
def test_timezone_mix_overdue_detected_correctly(tmp_path, monkeypatch):
    """next_run_at with local offset (-04:00, as APScheduler writes it);
    now/last_run_at conceptually naive-UTC. The job should have run 2h
    ago (in real UTC) and never ran -> must alert despite the mixed
    formats."""
    db = _db(tmp_path)
    monkeypatch.setattr(deadman, "_dispatch_now", lambda *a, **k: None)
    overdue_utc = NOW - timedelta(hours=2)
    _seed_job(db.db_path, "alerts_check", enabled=1, last_run_at=None,
              next_run_at=_iso_local_offset(overdue_utc, -4))

    result = deadman.check(db)

    assert result["new"] == 1


def test_timezone_mix_no_false_positive_when_within_grace(tmp_path, monkeypatch):
    """A next_run_at with offset -04:00 that in real UTC falls only 10
    minutes ago must NOT trigger a false alert from comparing the raw
    string (with offset) against a naive-UTC without normalizing."""
    db = _db(tmp_path)
    monkeypatch.setattr(deadman, "_dispatch_now", lambda *a, **k: None)
    # LIVE clock for the same reason as the grace test: `NOW` is
    # from import time, not from execution time.
    recent_utc = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=10)
    _seed_job(db.db_path, "alerts_check", enabled=1, last_run_at=None,
              next_run_at=_iso_local_offset(recent_utc, -4))

    result = deadman.check(db)

    assert result["new"] == 0


def test_timezone_mix_last_run_naive_after_next_run_offset_does_not_alert(tmp_path, monkeypatch):
    """last_run_at naive-UTC later than next_run_at with local offset: the
    job DID run on time, must not alert even though the formats differ."""
    db = _db(tmp_path)
    monkeypatch.setattr(deadman, "_dispatch_now", lambda *a, **k: None)
    next_run_utc = NOW - timedelta(hours=2)
    ran_utc = next_run_utc + timedelta(minutes=1)
    _seed_job(db.db_path, "alerts_check", enabled=1,
              last_run_at=_iso_naive_utc(ran_utc),
              next_run_at=_iso_local_offset(next_run_utc, -4))

    result = deadman.check(db)

    assert result["new"] == 0


# --------------------------------------------------------------------- #
# Auto-resolution (7.2) + notification + dedupe
# --------------------------------------------------------------------- #
def test_alert_auto_resolves_when_job_runs_again(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(deadman, "_dispatch_now", lambda *a, **k: None)
    overdue = NOW - timedelta(hours=2)
    _seed_job(db.db_path, "alerts_check", enabled=1, last_run_at=None,
              next_run_at=_iso_naive_utc(overdue))
    deadman.check(db)
    assert len(db.list_alerts(status="open")) == 1

    # The job runs again, after the overdue next_run.
    _seed_job(db.db_path, "alerts_check", enabled=1,
              last_run_at=_iso_naive_utc(overdue + timedelta(minutes=1)),
              next_run_at=_iso_naive_utc(overdue))

    deadman.check(db)

    assert db.list_alerts(status="open") == []
    resolved = db.list_alerts(status="resolved")
    assert len(resolved) == 1
    assert resolved[0]["resolved_by"] == "auto"


def test_two_passes_produce_a_single_alert_dedupe(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(deadman, "_dispatch_now", lambda *a, **k: None)
    overdue = NOW - timedelta(hours=2)
    _seed_job(db.db_path, "alerts_check", enabled=1, last_run_at=None,
              next_run_at=_iso_naive_utc(overdue))

    first = deadman.check(db)
    second = deadman.check(db)

    assert first["new"] == 1
    assert second["new"] == 0
    assert len(db.list_alerts(status="open")) == 1


def test_dispatch_now_called_once_for_new_alert(tmp_path, monkeypatch):
    """H1(b) (7.3 review): deadman dispatches with _dispatch_now, not
    notify_alert -- the watchdog doesn't depend on the conduit it watches."""
    db = _db(tmp_path)
    calls = []
    monkeypatch.setattr(deadman, "_dispatch_now", lambda _db, row: calls.append(row))
    overdue = NOW - timedelta(hours=2)
    _seed_job(db.db_path, "alerts_check", enabled=1, last_run_at=None,
              next_run_at=_iso_naive_utc(overdue))

    deadman.check(db)
    deadman.check(db)  # second pass: dedupe, must not notify again

    assert len(calls) == 1
    assert calls[0]["kind"] == "job_missed"
    assert calls[0]["country"] == "alerts_check"
    assert calls[0]["severity"] == "warning"


def test_corrupt_timestamp_row_does_not_break_the_pass(tmp_path, monkeypatch):
    """Review 8.2 C1: a row with an unreadable next_run_at does not crash the
    pass -- the other jobs are still evaluated."""
    db = _db(tmp_path)
    monkeypatch.setattr(deadman, "_dispatch_now", lambda *a, **k: None)
    _seed_job(db.db_path, "aaa_corrupt", enabled=1, last_run_at=None,
              next_run_at="not-a-date")
    overdue = NOW - timedelta(hours=2)
    _seed_job(db.db_path, "zzz_overdue", enabled=1, last_run_at=None,
              next_run_at=_iso_naive_utc(overdue))

    out = deadman.check(db)

    assert out["new"] == 1  # the corrupt one was skipped; the overdue one did alert
    assert db.list_alerts(status="open")[0]["country"] == "zzz_overdue"


def test_disabling_job_auto_resolves_its_open_alert(tmp_path, monkeypatch):
    """Review 8.2 C2: disabling a job with an open job_missed alert closes
    it on the next pass (disabled = we no longer care)."""
    import sqlite3

    db = _db(tmp_path)
    monkeypatch.setattr(deadman, "_dispatch_now", lambda *a, **k: None)
    overdue = NOW - timedelta(hours=2)
    _seed_job(db.db_path, "alerts_check", enabled=1, last_run_at=None,
              next_run_at=_iso_naive_utc(overdue))
    deadman.check(db)
    assert db.list_alerts(status="open")

    conn = sqlite3.connect(db.db_path)
    conn.execute("UPDATE scheduled_jobs SET enabled = 0 WHERE name = 'alerts_check'")
    conn.commit()
    conn.close()

    deadman.check(db)

    assert db.list_alerts(status="open") == []
    resolved = db.list_alerts(status="resolved")
    assert resolved and resolved[0]["resolved_by"] == "auto"
