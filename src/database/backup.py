"""Database backups: nightly copies, rotation, pre-migration copy, restore.

Copies are made with SQLite's `VACUUM INTO`: a consistent snapshot (pages
still in the WAL included) written as a compact, defragmented file in
rollback-journal mode, readable by any sqlite3 build. Each copy goes to a
`.tmp` name first and is renamed into place, so a crash mid-write never
leaves a half backup with a final name.

Layout: `<BACKUP_DIR or dirname(DB_PATH)/backups>/<stem>-YYYYMMDD-HHMMSS.db`
(UTC), `...-pre-migration.db` for the copy MigrationsMixin takes before a
schema migration. `prune_backups` deletes timestamped copies older than
BACKUP_KEEP_DAYS (default 14) by mtime; files that don't follow the naming
are left alone.

Restore is a CLI job done with the server stopped (scripts/restore_backup.py):
the source is checked with `PRAGMA integrity_check`, the live file (and its
-wal/-shm) is moved to `<db>.before-restore-<ts>` instead of deleted, then the
backup is copied in. The next startup runs the usual migrations on it.

Deliberately stdlib-only and unaware of the Database class: imported by
database.migrations (startup) and by scheduler.jobs, and must never create
an import cycle with database.models. Functions that read settings take any
object with `get_setting`/`set_setting`.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_KEEP_DAYS = 14
# settings rows the nightly job writes and the Settings -> Backup card reads.
LAST_BACKUP_AT = "last_backup_at"
LAST_BACKUP_BYTES = "last_backup_bytes"
LAST_BACKUP_PATH = "last_backup_path"
# Only files named by create_backup are ever pruned.
_BACKUP_NAME_RE = re.compile(r"^.+-\d{8}-\d{6}(-[a-z][a-z-]*)?\.db$")


class BackupError(RuntimeError):
    """A backup or restore that must not proceed (missing/corrupt source)."""


def backups_dir_for(db_path: str | os.PathLike) -> Path:
    """`BACKUP_DIR` if set, else `backups/` next to the database file."""
    raw = (os.environ.get("BACKUP_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(db_path).absolute().parent / "backups"


def _positive_int(value) -> int | None:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def keep_days(db=None) -> int:
    """Rotation window: the `BACKUP_KEEP_DAYS` settings row (editable without
    a restart, same as the PURGE_*_DAYS rows), else the environment variable,
    else DEFAULT_KEEP_DAYS. Anything that isn't a positive integer is skipped."""
    if db is not None:
        try:
            n = _positive_int(db.get_setting("BACKUP_KEEP_DAYS", ""))
        except Exception:  # noqa: BLE001 -- a broken settings read must not stop a backup
            n = None
        if n:
            return n
    return _positive_int(os.environ.get("BACKUP_KEEP_DAYS")) or DEFAULT_KEEP_DAYS


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def create_backup(db_path: str | os.PathLike, backups_dir: str | os.PathLike,
                  suffix: str = "") -> Path:
    """Writes `<backups_dir>/<stem>-YYYYMMDD-HHMMSS[-suffix].db` with VACUUM INTO
    (via a `.tmp` name, renamed when complete) and returns its path."""
    src = Path(db_path)
    if not src.is_file():
        raise BackupError(f"no database at {src}")
    out_dir = Path(backups_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{src.stem}-{_stamp()}{'-' + suffix if suffix else ''}.db"
    target = out_dir / name
    tmp = out_dir / (name + ".tmp")
    tmp.unlink(missing_ok=True)  # VACUUM INTO refuses an existing file
    # autocommit: VACUUM cannot run inside a transaction.
    conn = sqlite3.connect(str(src), isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("VACUUM INTO ?", (str(tmp),))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        conn.close()
    os.replace(tmp, target)
    return target


def pre_migration_backup(db_path: str | os.PathLike) -> Path:
    """The copy taken before a schema migration (see MigrationsMixin)."""
    return create_backup(db_path, backups_dir_for(db_path), suffix="pre-migration")


def prune_backups(backups_dir: str | os.PathLike, keep_days: int = DEFAULT_KEEP_DAYS) -> list[Path]:
    """Deletes timestamped backups whose mtime is older than `keep_days` days.
    Returns the deleted paths. A missing directory is a no-op."""
    out_dir = Path(backups_dir)
    if not out_dir.is_dir():
        return []
    cutoff = datetime.now(UTC).timestamp() - keep_days * 86400
    deleted: list[Path] = []
    for p in sorted(out_dir.iterdir()):
        if not p.is_file() or not _BACKUP_NAME_RE.match(p.name):
            continue
        if p.stat().st_mtime < cutoff:
            p.unlink()
            deleted.append(p)
    return deleted


def verify_backup(src: str | os.PathLike) -> None:
    """Raises BackupError unless `src` is a SQLite file with tables that
    passes `PRAGMA integrity_check`. Opened read-only: never modifies it."""
    path = Path(src)
    if not path.is_file():
        raise BackupError(f"backup not found: {path}")
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            tables = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        raise BackupError(f"{path} is not a usable SQLite database: {e}") from e
    if not row or row[0] != "ok":
        raise BackupError(f"integrity_check failed for {path}: {row[0] if row else 'no result'}")
    if not tables:
        raise BackupError(f"{path} has no tables")


def restore_backup(src: str | os.PathLike, db_path: str | os.PathLike) -> Path | None:
    """Replaces `db_path` with a copy of `src` (verified first). The current
    file and its -wal/-shm are moved to `<db>.before-restore-<ts>` (returned;
    None when there was no database to keep). For use with the server
    stopped: a running app holds the old inode and keeps writing to it."""
    src_path = Path(src)
    verify_backup(src_path)
    db = Path(db_path)
    kept: Path | None = None
    if db.exists():
        kept = db.with_name(f"{db.name}.before-restore-{_stamp()}")
        os.replace(db, kept)
        for ext in ("-wal", "-shm"):
            side = Path(str(db) + ext)
            if side.exists():
                os.replace(side, Path(str(kept) + ext))
    else:
        db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_path, db)
    return kept


def record_last_backup(db, path: str | os.PathLike) -> None:
    """Remembers when/where/how big the last successful nightly backup was."""
    p = Path(path)
    db.set_setting(LAST_BACKUP_AT, datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"))
    db.set_setting(LAST_BACKUP_BYTES, str(p.stat().st_size))
    db.set_setting(LAST_BACKUP_PATH, str(p))


def last_backup(db) -> dict | None:
    """{"at", "bytes", "path"} of the last successful nightly backup, or None."""
    at = db.get_setting(LAST_BACKUP_AT, "")
    if not at:
        return None
    return {"at": at, "path": db.get_setting(LAST_BACKUP_PATH, ""),
            "bytes": _positive_int(db.get_setting(LAST_BACKUP_BYTES, "")) or 0}


def format_bytes(n: float) -> str:
    """'3.0 MB' (1024-based) for the Settings card and the restore script."""
    size = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
