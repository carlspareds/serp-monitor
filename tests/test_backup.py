"""Nightly backup + tested restore (hardening, issue #6).

src/database/backup.py (VACUUM INTO, prune, restore), the `nightly_backup`
scheduler job (scheduler/jobs.py), the automatic backup before a schema
migration (MigrationsMixin / PRAGMA user_version) and
scripts/restore_backup.py. Every fixture is synthetic: temporary SQLite
files under tmp_path, fake mtimes for the rotation, no network.
"""
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import SAMPLE_RESULTS
from conftest import make_db as _db

from database import backup
from database.models import Database

_ROOT = Path(__file__).resolve().parent.parent
_RESTORE = _ROOT / "scripts" / "restore_backup.py"


def _seeded(tmp_path) -> Database:
    db = _db(tmp_path)
    db.save_run(apify_run_id="run-b", queries=["q"], country_code="us", status="completed",
                summary={"total": 5, "positive": 2, "negative": 2, "neutral": 1})
    db.save_results(SAMPLE_RESULTS, run_id="run-b")
    db.add_positive_domain("jordanashworth.example")
    return db


def _count(path, table: str) -> int:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 -- test helper, table is a literal
    finally:
        conn.close()


def _tables(path) -> set[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _age(path: Path, days: float) -> None:
    """Fake mtime: `days` ago."""
    t = time.time() - days * 86400
    os.utime(path, (t, t))


# --------------------------------------------------------------------- #
# create_backup
# --------------------------------------------------------------------- #
def test_create_backup_copies_every_table_and_row(tmp_path):
    db = _seeded(tmp_path)
    out = backup.create_backup(db.db_path, tmp_path / "backups")

    assert out.exists() and out.parent == tmp_path / "backups"
    assert _tables(out) >= {"search_results", "runs", "settings", "positive_domains", "users"}
    assert _count(out, "search_results") == len(SAMPLE_RESULTS) == _count(db.db_path, "search_results")
    assert _count(out, "runs") == 1
    assert _count(out, "positive_domains") == 1


def test_create_backup_name_is_stem_and_utc_timestamp_and_leaves_no_temp_file(tmp_path):
    db = _db(tmp_path)  # t.db
    out = backup.create_backup(db.db_path, tmp_path / "backups")

    assert out.name.startswith("t-") and out.suffix == ".db"
    stamp = out.stem[len("t-"):]
    assert len(stamp) == 15 and stamp[8] == "-" and stamp.replace("-", "").isdigit()  # YYYYMMDD-HHMMSS
    assert [p.name for p in (tmp_path / "backups").iterdir()] == [out.name]  # no .tmp left behind


def test_create_backup_suffix_marks_pre_migration_copies(tmp_path):
    db = _db(tmp_path)
    out = backup.create_backup(db.db_path, tmp_path / "backups", suffix="pre-migration")
    assert out.name.endswith("-pre-migration.db")


def test_create_backup_sees_rows_still_in_the_wal(tmp_path):
    """The live DB runs in WAL mode: a backup must include committed pages that
    haven't been checkpointed into the main file yet."""
    db = _seeded(tmp_path)
    out = backup.create_backup(db.db_path, tmp_path / "backups")
    assert _count(out, "search_results") == 5
    assert sqlite3.connect(str(out)).execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_backups_dir_defaults_next_to_the_database_and_honours_env(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    assert backup.backups_dir_for(str(tmp_path / "data" / "x.db")) == tmp_path / "data" / "backups"
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "elsewhere"))
    assert backup.backups_dir_for(str(tmp_path / "data" / "x.db")) == tmp_path / "elsewhere"


# --------------------------------------------------------------------- #
# prune_backups
# --------------------------------------------------------------------- #
def test_prune_deletes_backups_older_than_keep_days_and_keeps_younger(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    old = d / "seo_dashboard-20260101-033000.db"
    old_pre = d / "seo_dashboard-20260102-033000-pre-migration.db"
    young = d / "seo_dashboard-20261010-033000.db"
    edge = d / "seo_dashboard-20261001-033000.db"
    for p in (old, old_pre, young, edge):
        p.write_bytes(b"x")
    _age(old, 20)
    _age(old_pre, 15)
    _age(young, 1)
    _age(edge, 13.5)

    deleted = backup.prune_backups(d, keep_days=14)

    assert sorted(p.name for p in deleted) == [old.name, old_pre.name]
    assert sorted(p.name for p in d.iterdir()) == [edge.name, young.name]


def test_prune_ignores_files_that_are_not_timestamped_backups(tmp_path):
    d = tmp_path / "backups"
    d.mkdir()
    other = d / "notes.db"
    other.write_bytes(b"x")
    _age(other, 400)
    assert backup.prune_backups(d, keep_days=14) == []
    assert other.exists()


def test_prune_missing_dir_is_a_noop(tmp_path):
    assert backup.prune_backups(tmp_path / "nope", keep_days=14) == []


def test_keep_days_setting_then_env_then_default(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.delenv("BACKUP_KEEP_DAYS", raising=False)
    assert backup.keep_days(db) == 14
    monkeypatch.setenv("BACKUP_KEEP_DAYS", "30")
    assert backup.keep_days(db) == 30
    db.set_setting("BACKUP_KEEP_DAYS", "7")
    assert backup.keep_days(db) == 7
    db.set_setting("BACKUP_KEEP_DAYS", "zero")  # garbage -> next source
    assert backup.keep_days(db) == 30
    monkeypatch.setenv("BACKUP_KEEP_DAYS", "-3")
    assert backup.keep_days(db) == 14


# --------------------------------------------------------------------- #
# restore_backup
# --------------------------------------------------------------------- #
def test_restore_backup_restores_row_count_and_keeps_before_restore_copy(tmp_path):
    db = _seeded(tmp_path)
    snapshot = backup.create_backup(db.db_path, tmp_path / "backups")
    # Data changes after the backup: the restore must bring back the old count.
    db.save_results(SAMPLE_RESULTS, run_id="run-c")
    assert _count(db.db_path, "search_results") == 10

    kept = backup.restore_backup(snapshot, db.db_path)

    assert _count(db.db_path, "search_results") == 5
    assert kept.name.startswith("t.db.before-restore-") and kept.exists()
    assert _count(kept, "search_results") == 10  # the pre-restore state is preserved
    assert not Path(db.db_path + "-wal").exists() and not Path(db.db_path + "-shm").exists()
    # The restored file is usable by the app (startup migrations run on it).
    Database(db_path=db.db_path)
    assert _count(db.db_path, "search_results") == 5


def test_restore_backup_refuses_a_source_that_fails_integrity_check(tmp_path):
    db = _seeded(tmp_path)
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a sqlite file" * 100)
    with pytest.raises(backup.BackupError):
        backup.restore_backup(bad, db.db_path)
    assert _count(db.db_path, "search_results") == 5  # untouched
    assert not list(tmp_path.glob("t.db.before-restore-*"))


def test_restore_backup_into_a_missing_database_just_copies(tmp_path):
    db = _seeded(tmp_path)
    snapshot = backup.create_backup(db.db_path, tmp_path / "backups")
    target = tmp_path / "fresh" / "new.db"
    kept = backup.restore_backup(snapshot, str(target))
    assert kept is None
    assert _count(target, "search_results") == 5


# --------------------------------------------------------------------- #
# Automatic backup before migrations (PRAGMA user_version)
# --------------------------------------------------------------------- #
def test_fresh_database_takes_no_pre_migration_backup(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    from database.migrations import SCHEMA_VERSION

    db = _db(tmp_path)
    assert not (tmp_path / "backups").exists()
    conn = sqlite3.connect(db.db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


def test_existing_database_behind_schema_version_is_backed_up_once(tmp_path, monkeypatch):
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    db = _seeded(tmp_path)
    conn = sqlite3.connect(db.db_path)
    conn.execute("PRAGMA user_version = 0")  # what every DB created before this feature has
    conn.commit()
    conn.close()

    Database(db_path=db.db_path)  # startup: migrations run

    copies = list((tmp_path / "backups").glob("t-*-pre-migration.db"))
    assert len(copies) == 1
    assert _count(copies[0], "search_results") == 5

    Database(db_path=db.db_path)  # second start: already at SCHEMA_VERSION, no new copy
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


# --------------------------------------------------------------------- #
# scripts/restore_backup.py
# --------------------------------------------------------------------- #
def _run_restore(*args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(_RESTORE), *args], capture_output=True, text=True,
                          cwd=str(_ROOT), timeout=120, check=False)


def test_restore_script_requires_yes_and_then_restores(tmp_path):
    db = _seeded(tmp_path)
    snapshot = backup.create_backup(db.db_path, tmp_path / "backups")
    db.save_results(SAMPLE_RESULTS, run_id="run-c")
    assert _count(db.db_path, "search_results") == 10

    dry = _run_restore(str(snapshot), "--db", db.db_path)
    assert dry.returncode == 2
    assert "--yes" in dry.stdout + dry.stderr and "stop" in (dry.stdout + dry.stderr).lower()
    assert _count(db.db_path, "search_results") == 10  # nothing changed

    done = _run_restore(str(snapshot), "--db", db.db_path, "--yes")
    assert done.returncode == 0, done.stderr
    assert _count(db.db_path, "search_results") == 5
    assert "before-restore" in done.stdout


def test_restore_script_rejects_missing_backup(tmp_path):
    r = _run_restore(str(tmp_path / "nope.db"), "--db", str(tmp_path / "t.db"), "--yes")
    assert r.returncode != 0


# --------------------------------------------------------------------- #
# Config check: BACKUP_DIR / BACKUP_KEEP_DAYS
# --------------------------------------------------------------------- #
def test_config_check_backup_dir_not_creatable_is_fatal(tmp_path):
    from common import config_check as cc

    blocker = tmp_path / "file"
    blocker.write_text("x")
    env = {"DB_PATH": str(tmp_path / "t.db"), "BACKUP_DIR": str(blocker / "backups")}
    report = cc.check_config(env, cwd=tmp_path)
    assert "BACKUP_DIR" in {f.key for f in report.errors}


def test_config_check_backup_keep_days_must_be_a_positive_integer(tmp_path):
    from common import config_check as cc

    base = {"DB_PATH": str(tmp_path / "t.db")}
    for bad in ("0", "-1", "two"):
        report = cc.check_config({**base, "BACKUP_KEEP_DAYS": bad}, cwd=tmp_path)
        assert "BACKUP_KEEP_DAYS" in {f.key for f in report.errors}, bad
    ok = cc.check_config({**base, "BACKUP_KEEP_DAYS": "30", "BACKUP_DIR": str(tmp_path / "bk")},
                         cwd=tmp_path)
    assert ok.ok and not ok.errors
