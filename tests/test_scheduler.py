"""Tests for item 8.1: embedded scheduler (APScheduler) + mirror table
`scheduled_jobs`.

TDD, no sleeping or waiting on real crons:
- start() populates scheduled_jobs with the registered jobs.
- set_job_cron() updates the table and the real trigger (without restarting).
- SQLAlchemyJobStore persists the jobs across "restarts" (a new ephemeral
  scheduler against the same DB sees them).
- The @job decorator registers in JOBS.
- A failing job marks last_status='error' (the listener is invoked
  directly -- the minimum that proves the flow, without running a real job).
- report.weekly.run() is importable and orchestrates with stubs, no network.

`core.start()`/`shutdown()` use AsyncIOScheduler, which requires a running
asyncio event loop (same as in production, where FastAPI's async lifespan
calls them) -- that's why the scenarios that start the scheduler run
inside asyncio.run().
"""
import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from database.models import Database
from scheduler import core

# Imports the real jobs to populate JOBS (weekly_report/alerts_check).
from scheduler import jobs as _scheduler_jobs  # noqa: F401


@pytest.fixture(autouse=True)
def _clean_scheduler_state():
    """Isolates scheduler.core's module state between tests: no test
    should depend on _scheduler/_db left behind by the previous one."""
    yield
    core._scheduler = None
    core._db = None
    core._RUN_STARTED.clear()


def _run_scenario(coro_fn):
    """Runs `coro_fn` (an async function that does synchronous
    start()/shutdown() inside) on a real, live event loop from start to
    finish -- AsyncIOScheduler.start()/shutdown() need get_running_loop()."""
    asyncio.run(coro_fn())


# --------------------------------------------------------------------- #
# FIX 2 (silent-alerts audit): APScheduler's CronTrigger.from_crontab()
# maps the 5th field onto ITS OWN day_of_week, where 0=MONDAY -- standard
# crontab (and what any operator writes) uses 0=SUNDAY. Without translation,
# "0 8 * * 1" ("Monday 08:00") fires on TUESDAY. Verified against the
# trigger's real next_run_time, never by comparing cron strings.
# --------------------------------------------------------------------- #
def test_translate_crontab_dow_leaves_wildcard_and_other_fields_untouched():
    assert core._translate_crontab_dow("30 5 * * *") == "30 5 * * *"


def test_translate_crontab_dow_maps_monday_to_apscheduler_zero():
    # crontab 1 = Monday; APScheduler 0 = Monday.
    assert core._translate_crontab_dow("0 8 * * 1") == "0 8 * * 0"


def test_translate_crontab_dow_maps_sunday_to_apscheduler_six():
    # crontab 0 = Sunday; APScheduler 6 = Sunday.
    assert core._translate_crontab_dow("0 4 * * 0") == "0 4 * * 6"


def test_translate_crontab_dow_handles_comma_list():
    # crontab 1,5 (Monday, Friday) -> APScheduler 0,4.
    assert core._translate_crontab_dow("0 8 * * 1,5") == "0 8 * * 0,4"


def test_translate_crontab_dow_ignores_malformed_expression():
    """A cron with the wrong number of fields is left as-is --
    from_crontab is the one that should raise ValueError, not this helper."""
    assert core._translate_crontab_dow("not a cron") == "not a cron"


def test_weekly_report_cron_fires_on_monday_not_tuesday():
    """Direct regression for the bug: '0 8 * * 1' ('Monday 08:00 UTC') must
    fire on MONDAY -- verified against the real next_run_time produced by
    the trigger, ANCHORED on a Monday before 08:00 (if the bug reappeared,
    the next firing would land on Tuesday)."""
    from apscheduler.triggers.cron import CronTrigger

    trigger = CronTrigger.from_crontab(core._translate_crontab_dow("0 8 * * 1"))
    anchor = datetime(2026, 8, 3, 0, 0, 0, tzinfo=UTC)  # Monday 2026-08-03, before 08:00
    nxt = trigger.get_next_fire_time(None, anchor)
    assert nxt.weekday() == 0  # Monday
    assert nxt.date().isoformat() == "2026-08-03"


def test_purge_checks_cron_fires_on_sunday():
    """'0 4 * * 0' ('Sunday 04:00') must fire on SUNDAY."""
    from apscheduler.triggers.cron import CronTrigger

    trigger = CronTrigger.from_crontab(core._translate_crontab_dow("0 4 * * 0"))
    anchor = datetime(2026, 8, 2, 0, 0, 0, tzinfo=UTC)  # Sunday 2026-08-02
    nxt = trigger.get_next_fire_time(None, anchor)
    assert nxt.weekday() == 6  # Sunday
    assert nxt.date().isoformat() == "2026-08-02"


def test_ai_monitor_check_cron_fires_on_sunday():
    from apscheduler.triggers.cron import CronTrigger

    trigger = CronTrigger.from_crontab(core._translate_crontab_dow("0 6 * * 0"))
    anchor = datetime(2026, 8, 2, 0, 0, 0, tzinfo=UTC)
    nxt = trigger.get_next_fire_time(None, anchor)
    assert nxt.weekday() == 6
    assert nxt.date().isoformat() == "2026-08-02"


def test_visual_check_cron_fires_on_sunday():
    from apscheduler.triggers.cron import CronTrigger

    trigger = CronTrigger.from_crontab(core._translate_crontab_dow("0 7 * * 0"))
    anchor = datetime(2026, 8, 2, 0, 0, 0, tzinfo=UTC)
    nxt = trigger.get_next_fire_time(None, anchor)
    assert nxt.weekday() == 6
    assert nxt.date().isoformat() == "2026-08-02"


def test_untranslated_trigger_would_have_fired_a_day_late_regression_evidence():
    """Documents the ORIGINAL bug (untranslated): raw from_crontab on
    '0 8 * * 1' interprets 1 as Tuesday (APScheduler 0=Monday) -- this is
    how it was verified in production that weekly_report ran on Tuesday."""
    from apscheduler.triggers.cron import CronTrigger

    untranslated = CronTrigger.from_crontab("0 8 * * 1")
    anchor = datetime(2026, 8, 3, 0, 0, 0, tzinfo=UTC)  # Monday
    nxt = untranslated.get_next_fire_time(None, anchor)
    assert nxt.weekday() == 1  # Tuesday -- the bug


def test_start_schedules_weekly_report_to_fire_on_monday(tmp_path):
    """Integration: start() applies the translation -- the live job's real
    next_run_time falls on Monday, not Tuesday."""
    db = Database(db_path=str(tmp_path / "t.db"))

    async def scenario():
        core.start(db)
        try:
            next_run = core._scheduler.get_job("weekly_report").next_run_time
            assert next_run is not None
            assert next_run.weekday() == 0  # Monday
        finally:
            core.shutdown()

    _run_scenario(scenario)


def test_set_job_cron_also_translates_operator_input(tmp_path):
    """The cron the operator writes in /settings goes through the SAME
    helper -- it used to fire off by one day too (brief FIX 2)."""
    db = Database(db_path=str(tmp_path / "t.db"))

    async def scenario():
        core.start(db)
        try:
            core.set_job_cron("weekly_report", "0 9 * * 3", enabled=True)  # Wednesday 09:00
            next_run = core._scheduler.get_job("weekly_report").next_run_time
            assert next_run.weekday() == 2  # Wednesday
        finally:
            core.shutdown()

    _run_scenario(scenario)


def test_job_decorator_registers_default_jobs():
    assert "weekly_report" in core.JOBS
    assert core.JOBS["weekly_report"]["default_cron"] == "0 8 * * 1"
    assert "alerts_check" in core.JOBS
    assert core.JOBS["alerts_check"]["default_cron"] == "0 9 * * *"
    assert callable(core.JOBS["weekly_report"]["func"])


def test_purge_checks_job_registered_weekly(tmp_path):
    """8.2/6.6 (chore F1-close, point 2): purge_checks is in JOBS with the
    weekly cron (Sunday 4am). H20 (A3 review)/H12 (7.3 review): step purge
    of entity_snapshots/notify_queue. FIX 3 (hardening audit): the job now
    delegates to MANY more Database.purge_* (see tests/
    test_hardening_purge.py for full per-table coverage) -- a real tmp_path
    DB is enough here to confirm the job STAYS registered and runs without
    raising, without hand-maintaining a duplicate of every new method it
    calls."""
    assert "purge_checks" in core.JOBS
    assert core.JOBS["purge_checks"]["default_cron"] == "0 4 * * 0"
    assert callable(core.JOBS["purge_checks"]["func"])

    db = Database(db_path=str(tmp_path / "purge-registered.db"))
    result = core.JOBS["purge_checks"]["func"](db)
    assert result["notify_log"] == 0


def test_start_populates_scheduled_jobs_table(tmp_path):
    db = Database(db_path=str(tmp_path / "t.db"))

    async def scenario():
        core.start(db)
        try:
            rows = {r["name"]: r for r in db.list_scheduled_jobs()}
            assert "weekly_report" in rows
            assert "alerts_check" in rows
            assert rows["weekly_report"]["cron_expr"] == "0 8 * * 1"
            assert rows["weekly_report"]["enabled"] == 1
            assert rows["weekly_report"]["next_run_at"] is not None
            assert rows["alerts_check"]["next_run_at"] is not None
        finally:
            core.shutdown()

    _run_scenario(scenario)


def test_set_job_cron_updates_table_and_trigger_without_restart(tmp_path):
    db = Database(db_path=str(tmp_path / "t.db"))

    async def scenario():
        core.start(db)
        try:
            before = core._scheduler.get_job("weekly_report").next_run_time

            # Changes from "Monday 08:00" to "daily 03:00": the live job's
            # real next_run_time must reflect it without calling start() again.
            core.set_job_cron("weekly_report", "0 3 * * *", enabled=True)

            row = db.get_scheduled_job("weekly_report")
            assert row["cron_expr"] == "0 3 * * *"
            assert row["enabled"] == 1

            after = core._scheduler.get_job("weekly_report").next_run_time
            assert after != before
            assert row["next_run_at"] is not None
        finally:
            core.shutdown()

    _run_scenario(scenario)


def test_set_job_cron_disabled_pauses_job(tmp_path):
    db = Database(db_path=str(tmp_path / "t.db"))

    async def scenario():
        core.start(db)
        try:
            core.set_job_cron("alerts_check", "0 9 * * *", enabled=False)
            row = db.get_scheduled_job("alerts_check")
            assert row["enabled"] == 0
            # A paused job has no next_run_time.
            assert core._scheduler.get_job("alerts_check").next_run_time is None
        finally:
            core.shutdown()

    _run_scenario(scenario)


def test_jobstore_persists_jobs_across_scheduler_restart(tmp_path):
    """Acceptance: 'restarting the service keeps the jobs'. Verifies
    APScheduler's jobstore directly (without calling scheduler.core.start()
    again, which would re-register them from JOBS anyway) -- this way it
    confirms that SQLAlchemyJobStore really persisted the state in the DB,
    not that our code recreates them every time."""
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

    db_path = str(tmp_path / "t.db")
    db = Database(db_path=db_path)

    async def scenario():
        core.start(db)
        core.shutdown()

    _run_scenario(scenario)

    store = SQLAlchemyJobStore(url=f"sqlite:///{db_path}")
    persisted_ids = {j.id for j in store.get_all_jobs()}
    assert "weekly_report" in persisted_ids
    assert "alerts_check" in persisted_ids


def test_restart_preserves_persisted_next_run(tmp_path):
    """Regression from review M1: on a second boot, a job already present in
    the jobstore is NOT re-added (re-adding would discard the persisted
    next_run_time, and an execution missed during the restart would be
    skipped instead of recovered via misfire_grace_time)."""
    from datetime import datetime, timedelta

    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

    db_path = str(tmp_path / "t.db")
    db = Database(db_path=db_path)

    async def first_boot():
        core.start(db)
        core.shutdown()

    _run_scenario(first_boot)

    # Marks the persisted next_run with a distinctive FUTURE instant that
    # the cron would never produce (so nothing fires during the test -- a
    # within-grace-period past would run weekly_report for real). If the
    # boot re-added the job, this value would be lost and revert to the
    # next Monday 08:00 computed by the trigger.
    store = SQLAlchemyJobStore(url=f"sqlite:///{db_path}")
    j = store.lookup_job("weekly_report")
    sentinel = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=123456)
    j.next_run_time = sentinel
    store.update_job(j)

    async def second_boot():
        core.start(db)
        try:
            live = core._scheduler.get_job("weekly_report")
            # The persisted next_run survives the boot (it wasn't re-added,
            # which would have recalculated the next firing from the trigger).
            assert live.next_run_time == sentinel
        finally:
            core.shutdown()

    _run_scenario(second_boot)


def test_start_removes_orphan_jobs_not_in_registry(tmp_path):
    """Regression from review m3: a pickled job from a previous deploy that
    no longer exists in JOBS gets removed from the jobstore on boot (if it
    remained, it would trigger _dispatch -> KeyError on every cycle)."""
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.triggers.cron import CronTrigger

    db_path = str(tmp_path / "t.db")
    db = Database(db_path=db_path)

    async def seed_orphan():
        core.start(db)
        core._scheduler.add_job(
            core._dispatch, args=["ghost_job", db_path],
            trigger=CronTrigger.from_crontab("0 0 * * *"), id="ghost_job",
        )
        core.shutdown()

    _run_scenario(seed_orphan)

    async def reboot():
        core.start(db)
        try:
            assert core._scheduler.get_job("ghost_job") is None
        finally:
            core.shutdown()

    _run_scenario(reboot)

    store = SQLAlchemyJobStore(url=f"sqlite:///{db_path}")
    assert store.lookup_job("ghost_job") is None


def test_set_job_cron_rejects_invalid_expression(tmp_path):
    """Regression from review m2: an invalid cron is NOT persisted (it
    would brick the next boot)."""
    db = Database(db_path=str(tmp_path / "t.db"))
    db.ensure_scheduled_job("weekly_report", "0 8 * * 1")

    async def scenario():
        core.start(db)
        try:
            with pytest.raises(ValueError):
                core.set_job_cron("weekly_report", "not a cron", enabled=True)
            assert db.get_scheduled_job("weekly_report")["cron_expr"] == "0 8 * * 1"
        finally:
            core.shutdown()

    _run_scenario(scenario)


def test_list_jobs_reads_mirror_table(tmp_path):
    db = Database(db_path=str(tmp_path / "t.db"))

    async def scenario():
        core.start(db)
        try:
            names = {j["name"] for j in core.list_jobs()}
            assert {"weekly_report", "alerts_check"} <= names
        finally:
            core.shutdown()

    _run_scenario(scenario)


def test_list_jobs_without_started_scheduler_returns_empty():
    assert core.list_jobs() == []


def test_job_error_marks_last_status_error(tmp_path, monkeypatch):
    db = Database(db_path=str(tmp_path / "t.db"))
    db.ensure_scheduled_job("boom", "* * * * *")
    monkeypatch.setattr(core, "_db", db)

    core._RUN_STARTED["boom"] = 0.0  # simulates _dispatch already having started
    core._on_error(SimpleNamespace(job_id="boom"))

    row = db.get_scheduled_job("boom")
    assert row["last_status"] == "error"
    assert row["last_duration_ms"] is not None


def test_job_executed_marks_last_status_ok(tmp_path, monkeypatch):
    db = Database(db_path=str(tmp_path / "t.db"))
    db.ensure_scheduled_job("ok-job", "* * * * *")
    monkeypatch.setattr(core, "_db", db)

    core._RUN_STARTED["ok-job"] = 0.0
    core._on_executed(SimpleNamespace(job_id="ok-job"))

    row = db.get_scheduled_job("ok-job")
    assert row["last_status"] == "ok"


# --------------------------------------------------------------------- #
# Timezone: the UI says "All times are UTC", so the scheduler must not
# fall back to the host's local zone (it ran in EDT on a US-hosted box).
# --------------------------------------------------------------------- #
def test_scheduler_and_triggers_are_utc(tmp_path):
    db = Database(db_path=str(tmp_path / "t.db"))

    async def scenario():
        core.start(db)
        try:
            assert str(core._scheduler.timezone) == "UTC"
            for name in core.JOBS:
                j = core._scheduler.get_job(name)
                assert str(j.trigger.timezone) == "UTC", name
            weekly = core._scheduler.get_job("weekly_report").next_run_time
            assert (weekly.utcoffset().total_seconds(), weekly.hour, weekly.weekday()) == (0, 8, 0)
            row = db.get_scheduled_job("weekly_report")
            assert row["next_run_at"].endswith("+00:00")
        finally:
            core.shutdown()

    _run_scenario(scenario)


def test_restored_local_time_trigger_is_rescheduled_in_utc(tmp_path):
    """A job persisted before the scheduler was pinned to UTC carries a
    local-zone trigger in the jobstore; same cron text, so the old string
    comparison kept it. start() must replace it with the UTC trigger."""
    from zoneinfo import ZoneInfo

    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.triggers.cron import CronTrigger

    db_path = str(tmp_path / "t.db")
    db = Database(db_path=db_path)

    async def first_boot():
        core.start(db)
        core.shutdown()

    _run_scenario(first_boot)

    store = SQLAlchemyJobStore(url=f"sqlite:///{db_path}")
    j = store.lookup_job("weekly_report")
    j.trigger = CronTrigger.from_crontab(core._translate_crontab_dow("0 8 * * 1"),
                                         timezone=ZoneInfo("America/New_York"))
    store.update_job(j)

    async def second_boot():
        core.start(db)
        try:
            live = core._scheduler.get_job("weekly_report")
            assert str(live.trigger.timezone) == "UTC"
            assert live.next_run_time.astimezone(UTC).hour == 8
        finally:
            core.shutdown()

    _run_scenario(second_boot)
