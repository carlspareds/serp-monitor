"""Alert auto-resolution helpers shared by the URL lifecycle and the dead-man switch."""
from collections.abc import Callable
from datetime import UTC, datetime


def parse_utc(value: str) -> datetime:
    """`value` naive (assumed UTC -- that's how Database.record_job_run
    writes it, via now(UTC) without tzinfo) or aware with an offset (that's
    how APScheduler next_run_time.isoformat() writes it) -> UTC-aware
    datetime, comparable without ambiguity. Used by scheduler.deadman
    (next_run_at/last_run_at), which faces exactly that naive/aware mix."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def auto_resolve(db, kind: str, is_gone: Callable[[dict], bool]) -> int:
    """Alert auto-resolution (7.2), hooked into the monitor cycle: every
    `run_checks(db)` calls it at the end, once it has this pass's fresh
    results, for the `open`/`acknowledged` alerts of its own `kind` whose
    condition no longer holds now. `is_gone(alert_row) -> bool` decides per
    alert by comparing against that fresh result (e.g. a lambda that looks
    at `by_host[alert['country']]['days_left']`) -- it never triggers
    network calls or queries anything on its own, so it only covers the
    kinds where the current state comes for free from the check that
    already ran (ssl, backup_missing/stale, wp_outdated, mail_error; NOT
    apify_budget, which is monthly and has no "current condition" per
    check). include_snoozed=True: a snoozed alert whose condition went away
    must still close (the cert renewed even if the human snoozed it),
    snooze only hides, it must not block auto-resolution. resolved_by=
    'auto'. Never raises: if `is_gone` blows up (e.g. the alert's target is
    no longer in the fresh result -- it was removed from the list of hosts/
    sites/specs) that alert simply isn't resolved, safer than tanking the
    whole check over a failed lookup. Returns how many alerts it
    resolved."""
    n = 0
    for a in db.open_alerts(include_snoozed=True):
        if a["kind"] != kind:
            continue
        try:
            gone = is_gone(a)
        except Exception:  # noqa: BLE001
            gone = False
        if gone:
            db.resolve_alert(a["id"], by="auto")
            n += 1
    return n
