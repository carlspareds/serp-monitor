"""Merges URL identities that are the same page once tracking parameters (and
known locale path prefixes) are ignored.

    .venv/bin/python scripts/repair_url_identity.py [--db PATH]          # dry run (default)
    .venv/bin/python scripts/repair_url_identity.py --apply [--db PATH]

normalize_url (database.base) now drops tracking parameters -- srsltid (added by
Google with a fresh random value on many results, every search), utm_*, gclid,
fbclid, mc_cid, mc_eid, _hsenc, _hsmi, ref_src, igshid, msclkid -- and
fragments -- and, since URL_KEY_VERSION 3, known locale path prefixes
(open.spotify.com/intl-es/... and music.apple.com/us/album/... are the same
page as without the prefix; database.base._LOCALE_PATH_RULES). url_hash() is
computed from it, so data written before the change can hold several keys for
one page: re-run this script (dry run, then --apply) after an upgrade that
bumps URL_KEY_VERSION. What this script does, per subject:

  1. search_results.url_key (the page key used for human verdicts and the review
     queue): recomputed. The app also does this by itself at startup
     (URL_KEY_VERSION), so this step usually reports 0.
  2. url_lifecycle (PK url_hash + country + engine + subject): rows of one page
     are MERGED into a single row keyed by the new hash -- first_seen = earliest,
     last_seen = latest, best_position = best; status, last position, label,
     missed runs and same-person come from the most recently seen row.
  3. evidence.url_hash: recomputed from evidence.url (no key to merge, the
     snapshots themselves are untouched).
  4. Reported only, never changed: url_notes (keyed by the literal URL; notes on
     two spellings of one page are listed so a person can merge them) and
     ai_verdict_cache (entries under old keys are simply never hit again).

Human verdicts across the merged rows are NOT changed here: run
scripts/repair_human_verdicts.py afterwards.

Safety: without --apply nothing is written -- the plan is computed on a
temporary copy (the app's startup migrations run there, never on your file).
--apply backs the DB up next to it (<db>.pre-url-identity-<timestamp>.bak),
then writes everything in ONE transaction. Idempotent: a second run reports
nothing to do.
"""
import argparse
import os
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from database.models import Database, normalize_url, url_hash


def _sqlite_copy(src: str, dst: str) -> None:
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def _merge_lifecycle(rows: list[sqlite3.Row], new_hash: str) -> dict:
    latest = max(rows, key=lambda r: (r["last_seen"] or "", r["first_seen"] or ""))
    firsts = [r["first_seen"] for r in rows if r["first_seen"]]
    lasts = [r["last_seen"] for r in rows if r["last_seen"]]
    bests = [r["best_position"] for r in rows if r["best_position"] is not None]
    merged = dict(latest)
    merged.update(url_hash=new_hash, first_seen=min(firsts) if firsts else None,
                  last_seen=max(lasts) if lasts else None, best_position=min(bests) if bests else None)
    return merged


def build_plan(conn: sqlite3.Connection) -> dict:
    """Everything that would change, computed read-only."""
    conn.row_factory = sqlite3.Row
    plan: dict = {"url_key": [], "lifecycle": [], "evidence": [], "notes": [], "cache_entries": 0}
    for r in conn.execute("SELECT id, url, url_key FROM search_results"):
        h = url_hash(r["url"] or "")
        if h != r["url_key"]:
            plan["url_key"].append((h, r["id"]))

    groups: dict[tuple, list] = defaultdict(list)
    for r in conn.execute("SELECT * FROM url_lifecycle"):
        key = (url_hash(r["url"] or "") if r["url"] else r["url_hash"], r["country"],
               r["engine"] or "google", r["subject_id"])
        groups[key].append(r)
    for (h, _country, _engine, _sid), rows in groups.items():
        if len(rows) == 1 and rows[0]["url_hash"] == h:
            continue
        plan["lifecycle"].append({"old": rows, "new": _merge_lifecycle(rows, h)})

    for r in conn.execute("SELECT id, url, url_hash FROM evidence WHERE url IS NOT NULL"):
        h = url_hash(r["url"])
        if h != r["url_hash"]:
            plan["evidence"].append((h, r["id"]))

    notes: dict[tuple, list] = defaultdict(list)
    for r in conn.execute("SELECT url, subject_id FROM url_notes WHERE url IS NOT NULL"):
        notes[(r["subject_id"], normalize_url(r["url"]))].append(r["url"])
    plan["notes"] = [(sid, urls) for (sid, _n), urls in notes.items() if len(urls) > 1]
    plan["cache_entries"] = conn.execute("SELECT COUNT(*) FROM ai_verdict_cache").fetchone()[0]
    return plan


def plan_is_empty(plan: dict) -> bool:
    return not (plan["url_key"] or plan["lifecycle"] or plan["evidence"])


def print_plan(plan: dict, out=sys.stdout) -> None:
    out.write(f"search_results.url_key to recompute: {len(plan['url_key'])}\n")
    merges = [p for p in plan["lifecycle"] if len(p["old"]) > 1]
    out.write(f"url_lifecycle: {len(merges)} page(s) merged "
              f"({sum(len(p['old']) for p in merges)} rows -> {len(merges)}), "
              f"{len(plan['lifecycle']) - len(merges)} row(s) re-keyed\n")
    for p in merges[:20]:
        n = p["new"]
        out.write(f"  subject {n['subject_id']} {n['country']}/{n['engine']}: {len(p['old'])} rows -> "
                  f"{n['url']} (first {n['first_seen']}, last {n['last_seen']}, status {n['status']})\n")
    out.write(f"evidence.url_hash to recompute: {len(plan['evidence'])}\n")
    if plan["notes"]:
        out.write(f"url_notes on several spellings of one page (NOT merged, review by hand): "
                  f"{len(plan['notes'])}\n")
        for sid, urls in plan["notes"][:20]:
            out.write(f"  subject {sid}: " + " | ".join(urls) + "\n")
    out.write(f"ai_verdict_cache: {plan['cache_entries']} entries kept as they are "
              "(old keys are just never hit again)\n")


def apply_plan(conn: sqlite3.Connection, plan: dict) -> None:
    cur = conn.cursor()
    cur.execute("BEGIN")
    try:
        cur.executemany("UPDATE search_results SET url_key = ? WHERE id = ?", plan["url_key"])
        for p in plan["lifecycle"]:
            for old in p["old"]:
                cur.execute("DELETE FROM url_lifecycle WHERE url_hash = ? AND country = ? AND "
                            "COALESCE(engine, 'google') = ? AND subject_id = ?",
                            (old["url_hash"], old["country"], old["engine"] or "google",
                             old["subject_id"]))
            cols = list(p["new"].keys())
            cur.execute(f"INSERT INTO url_lifecycle ({', '.join(cols)}) "
                        f"VALUES ({', '.join('?' for _ in cols)})", [p["new"][c] for c in cols])
        cur.executemany("UPDATE evidence SET url_hash = ? WHERE id = ?", plan["evidence"])
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=os.getenv("DB_PATH")
                        or str(_ROOT / "data" / "seo_dashboard.db"))
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default: dry run, nothing is written)")
    args = parser.parse_args(argv)
    db_path = args.db
    if not Path(db_path).is_file():
        print(f"no database at {db_path}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        probe = str(Path(tmp) / "probe.db")
        _sqlite_copy(db_path, probe)
        Database(db_path=probe)  # startup migrations, on the copy only
        conn = sqlite3.connect(probe)
        try:
            plan = build_plan(conn)
        finally:
            conn.close()
        print_plan(plan)
        if not args.apply or plan_is_empty(plan):
            print("Dry run: nothing was changed (use --apply to write)." if not args.apply
                  else "Nothing to do.")
            return 0

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{db_path}.pre-url-identity-{stamp}.bak"
    _sqlite_copy(db_path, backup)
    print(f"Backup written: {backup}")
    Database(db_path=db_path)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        plan = build_plan(conn)  # recomputed on the real file
        apply_plan(conn, plan)
    finally:
        conn.close()
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
