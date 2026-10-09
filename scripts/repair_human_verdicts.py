"""Re-applies every human verdict to all the rows of the same PAGE, per subject.

    .venv/bin/python scripts/repair_human_verdicts.py --dry-run [--db PATH] [--subject ID]
    .venv/bin/python scripts/repair_human_verdicts.py [--db PATH] [--subject ID]

Human verdicts always win, per page: a page (same normalized URL -- tracking
parameters such as srsltid/utm_* and fragments don't count, see
database.base.normalize_url) that a person labelled in ANY run carries that
verdict on every row of the subject (all runs, countries, engines):

  * classification = the LATEST human label of the page (when it was given,
    then row id), classified_by='human', confidence 1.0;
  * same_person = 'no' when a person marked any row of the page "Not this
    person".

The app does this by itself for new verdicts and before every AI write; this
script repairs rows written before that (e.g. srsltid variants of one page
that kept their old AI label). Run scripts/repair_url_identity.py first if
you haven't (it doesn't need to be re-run).

Safety: --dry-run computes the changes on a temporary copy (the app's startup
migrations run there, never on your file) and prints them. A real run backs
the DB up next to it (<db>.pre-verdicts-<timestamp>.bak) before writing.
Idempotent: a second run reports nothing to change.
"""
import argparse
import io
import os
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from database.models import Database

_SHOW = 20  # changes listed per subject


def _sqlite_copy(src: str, dst: str) -> None:
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def _subject_ids(db_path: str, only: int | None) -> list[int]:
    if only is not None:
        return [only]
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT DISTINCT subject_id FROM search_results "
                            "WHERE subject_id IS NOT NULL ORDER BY subject_id").fetchall()
    finally:
        conn.close()
    return [int(r[0]) for r in rows]


def repair(db: Database, db_path: str, dry_run: bool, only: int | None = None,
           out=sys.stdout) -> int:
    """Re-applies the human verdicts subject by subject; prints and returns
    the number of rows changed (or that would change)."""
    total = 0
    for sid in _subject_ids(db_path, only):
        changes = db.for_subject(sid).reapply_human_verdicts(dry_run=dry_run)
        total += len(changes)
        if not changes:
            out.write(f"subject {sid}: nothing to change\n")
            continue
        relabelled = sum(1 for c in changes if c["from"] != c["to"])
        out.write(f"subject {sid}: {len(changes)} row(s) take the human verdict of their page "
                  f"({relabelled} change label)\n")
        for c in changes[:_SHOW]:
            sp = " same-person=no" if c["same_person"] == "no" else ""
            out.write(f"  #{c['id']} run {c['run_id']}: {c['from']} ({c['from_by'] or 'rule'}) -> "
                      f"{c['to']} (human){sp}  {c['url']}\n")
        if len(changes) > _SHOW:
            out.write(f"  ... and {len(changes) - _SHOW} more\n")
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=os.getenv("DB_PATH")
                        or str(_ROOT / "data" / "seo_dashboard.db"))
    parser.add_argument("--dry-run", action="store_true", help="print the changes, write nothing")
    parser.add_argument("--subject", type=int, default=None, help="only this subject id")
    args = parser.parse_args(argv)
    db_path = args.db
    if not Path(db_path).is_file():
        print(f"no database at {db_path}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        probe = str(Path(tmp) / "probe.db")
        _sqlite_copy(db_path, probe)
        n = repair(Database(db_path=probe), probe, dry_run=True, only=args.subject)
        if args.dry_run or not n:
            print("--dry-run: nothing was changed." if args.dry_run else "Nothing to change.")
            return 0

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{db_path}.pre-verdicts-{stamp}.bak"
    _sqlite_copy(db_path, backup)
    print(f"Backup written: {backup}")
    # The plan was printed above; the real pass only reports its total.
    n = repair(Database(db_path=db_path), db_path, dry_run=False, only=args.subject,
               out=io.StringIO())
    print(f"Done: {n} row(s) updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
