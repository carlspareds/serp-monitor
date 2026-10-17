#!/usr/bin/env python3
"""Restore the SERP Monitor database from a backup.

    .venv/bin/python scripts/restore_backup.py data/backups/<file>.db --yes

STOP THE SERVER FIRST. The script cannot tell whether the app is running,
and a running app keeps writing to the old file; that is why it refuses to
do anything without --yes. The backup is verified (PRAGMA integrity_check)
before anything is touched; the current database and its -wal/-shm are
moved to <db>.before-restore-<timestamp>, never deleted. --db defaults to
DB_PATH (or data/seo_dashboard.db). Start the server again afterwards: it
runs its startup migrations on the restored file.
"""
import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from database.backup import BackupError, format_bytes, restore_backup, verify_backup

DEFAULT_DB = os.environ.get("DB_PATH") or str(_ROOT / "data" / "seo_dashboard.db")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Restore the database from a backup "
                                                 "(stop the server first).")
    parser.add_argument("backup", help="backup file, e.g. data/backups/seo_dashboard-20261016-033000.db")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="database to replace (default: DB_PATH or data/seo_dashboard.db)")
    parser.add_argument("--yes", action="store_true",
                        help="actually restore; without it only the plan is printed")
    args = parser.parse_args(argv)

    src = Path(args.backup)
    try:
        verify_backup(src)
    except BackupError as e:
        print(f"refusing to restore: {e}", file=sys.stderr)
        return 2
    print(f"Backup:   {src} ({format_bytes(src.stat().st_size)}, integrity ok)")
    print(f"Database: {args.db}")
    if not args.yes:
        print("Nothing changed. This replaces the database with the backup: stop the server "
              "first (this script cannot tell whether it is running), then re-run with --yes. "
              "The current file is kept as <db>.before-restore-<timestamp>.")
        return 2

    kept = restore_backup(src, args.db)
    print(f"Restored {src} -> {args.db}")
    if kept is not None:
        print(f"Previous database kept at {kept} (delete it once you have checked the restore)")
    print("Start the server again; it runs its startup migrations on the restored file.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
