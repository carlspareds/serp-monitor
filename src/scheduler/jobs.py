"""Jobs registered with the embedded scheduler.

Every function receives `db` (the Database that scheduler.core._dispatch
rebuilds on the process db_path) and delegates to report.weekly, which knows
nothing about the scheduler or the dashboard (dependency direction:
scheduler/ -> report/, never the other way around).

Imported from dashboard/main.py BEFORE scheduler.core.start() so that the
@job decorators populate the JOBS registry.

Multi-subject behaviour (documented in README "Projects and subjects"):
  * weekly_report, alerts_check: once per scheduled subject (active, NOT
    demo, in a live project), each with db.for_subject(sid) inside
    subject_scope(sid) -- see report.weekly.run_scheduled.
  * info_digest: ONE email per run for the whole deployment, a section per
    subject (notify.dispatcher.send_info_digest); demo subjects excluded.
  * purge_checks: deployment-wide retention (db.for_all_subjects()).
  * nightly_backup: one copy of the whole file (every subject), see below.
  * deadman_check and the Apify budget check: deployment-wide, unchanged.
"""
from database import backup
from notify.dispatcher import emit_alert, send_info_digest
from processing.auto_resolve import auto_resolve
from report import weekly
from scheduler import deadman
from scheduler.core import job

job("weekly_report", "0 8 * * 1")(weekly.run_scheduled)
job("alerts_check", "0 9 * * *")(weekly.run_alert_checks_scheduled)
# Daily digest of info-level alerts: one email for the whole day.
job("info_digest", "0 8 * * *")(send_info_digest)
# Dead-man's switch: every 15 min, detect enabled jobs that did not run when
# they should have (process down, job paused by hand, etc).
job(deadman.SELF_NAME, "*/15 * * * *")(deadman.check)


# Retention per table, in days. Each one can be overridden by a setting of
# the same name without the leading underscore and the _DAYS suffix
# (e.g. PURGE_EVIDENCE_DAYS). audit_log and search_results are never purged.
_PURGE_NOTIFY_LOG_DAYS = 180
_PURGE_ALERTS_RESOLVED_DAYS = 180
_PURGE_EVIDENCE_DAYS = 365
_PURGE_URL_LIFECYCLE_GONE_DAYS = 365


def _purge_days(db, setting: str, default: int) -> int:
    """Retention in days for a purge: reads `setting` and falls back to
    `default` when it is unset or not a positive integer."""
    try:
        n = int(db.get_setting(setting, str(default)) or default)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


@job("purge_checks", "0 4 * * 0")
def purge_checks(db):
    """Weekly purge (Sunday 4am): keeps the tables that grow without bound
    within a reasonable retention. Each purge is a single SQL statement;
    the counts are printed so they show up in the scheduler log.
    Deployment-wide on purpose: the same retention applies to every
    subject, so it runs on a for_all_subjects() instance (the purge_*
    methods only DELETE by age and return counts, never rows)."""
    db = db.for_all_subjects()
    counts = {
        "notify_log": db.purge_notify_log(
            _purge_days(db, "PURGE_NOTIFY_LOG_DAYS", _PURGE_NOTIFY_LOG_DAYS)
        ),
        "alerts_resolved": db.purge_resolved_alerts(
            _purge_days(db, "PURGE_ALERTS_RESOLVED_DAYS", _PURGE_ALERTS_RESOLVED_DAYS)
        ),
        "evidence": db.purge_evidence(
            _purge_days(db, "PURGE_EVIDENCE_DAYS", _PURGE_EVIDENCE_DAYS)
        ),
        "url_lifecycle_gone": db.purge_gone_url_lifecycle(
            _purge_days(db, "PURGE_URL_LIFECYCLE_GONE_DAYS", _PURGE_URL_LIFECYCLE_GONE_DAYS)
        ),
    }
    print(f"[purge_checks] rows deleted per table: {counts}")
    return counts


# Nightly backup (hardening #6). Alert kind raised when it fails; resolved
# automatically by the next successful run.
BACKUP_FAILED_KIND = "backup_failed"
BACKUP_JOB_NAME = "nightly_backup"


def _alert_backup_failed(db, error: Exception) -> None:
    """One open `backup_failed` alert (deduped per job name) through
    notify.dispatcher.emit_alert, the single create-and-dispatch point every
    producer must use (tests/test_dispatch_guard.py). Never raises -- the
    original error is what the caller re-raises, and an alerting hiccup
    must not hide it."""
    message = f"Nightly backup failed: {error}"[:500]
    try:
        emit_alert(db, BACKUP_FAILED_KIND, "warning", BACKUP_JOB_NAME, message,
                   f"{BACKUP_FAILED_KIND}:{BACKUP_JOB_NAME}")
    except Exception as e:  # noqa: BLE001 -- see docstring
        print(f"[nightly_backup] could not raise the backup_failed alert: {e}")


@job(BACKUP_JOB_NAME, "30 3 * * *")
def nightly_backup(db):
    """Nightly (03:30 UTC) `VACUUM INTO` copy of the database into
    BACKUP_DIR (default data/backups/ next to the file), then deletes copies
    older than BACKUP_KEEP_DAYS (default 14). Records when/where/how big in
    the settings table (Settings -> Backup shows it), audits the file name,
    and raises a `backup_failed` alert -- then re-raises, so the scheduler
    marks the run as error -- when anything goes wrong. The whole file is
    copied, every subject included, so it is deployment-wide by nature."""
    backups_dir = backup.backups_dir_for(db.db_path)
    try:
        path = backup.create_backup(db.db_path, backups_dir)
        pruned = backup.prune_backups(backups_dir, backup.keep_days(db))
    except Exception as e:  # any failure is alert-worthy; re-raised below
        _alert_backup_failed(db, e)
        raise
    size = path.stat().st_size
    backup.record_last_backup(db, path)
    db.add_audit(actor_type="job", actor_id=BACKUP_JOB_NAME, action="backup_created",
                 resource_type="backup", resource_id=path.name,
                 params={"bytes": size, "pruned": len(pruned)})
    auto_resolve(db, BACKUP_FAILED_KIND, lambda _alert: True)
    print(f"[nightly_backup] wrote {path} ({size} bytes), pruned {len(pruned)}")
    return {"path": str(path), "bytes": size, "pruned": len(pruned)}
