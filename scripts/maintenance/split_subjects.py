"""Splits a single-subject database whose subject 1 accumulated searches for
OTHER people/brands into one Subject per tracked name (multi-subject phase 1).

    .venv/bin/python scripts/maintenance/split_subjects.py --dry-run [--db PATH]
    .venv/bin/python scripts/maintenance/split_subjects.py [--db PATH]

Before multi-subject support every search landed on subject 1, so a DB used to
look up "Alex Rivera" once next to the tracked "Jordan Ashworth" mixes both
in the same results, lifecycle, alerts... This tool:

  1. Identifies subject 1 by its name, aliases and default queries (after the
     startup backfill from the legacy report_query setting). Refuses to run if
     subject 1 has no name at all (it couldn't tell whose data is whose).
  2. Groups subject 1's runs by their normalized first query
     (runs.queries[0], case/whitespace-insensitive). Every group that doesn't
     match subject 1 becomes a Subject (reused if one already answers to that
     name; otherwise created with name = the query, countries = its runs'
     countries).
  3. Moves the group's runs and their search_results (by run_id).
  4. url_lifecycle: a row moves when its (url, country, engine) was observed
     ONLY by the new subject; a row observed by several subjects stays on
     subject 1 (listed as ambiguous) and every new subject gets a fresh row
     rebuilt from its own results (same aggregation as the first-boot
     backfill: status active, missed_runs 0).
  5. evidence / url_notes (+ their note_history) / url_gone events move only
     when unambiguous: the URL (or, for events, the domain) appears ONLY in the
     new subject's results. Anything shared, or not tied to any result
     (manual events, notes on URLs never seen), stays on subject 1 and the
     shared ones are listed.
  6. Alerts: url_gone follows its lifecycle row. threshold (thr:{c}:{date}) and
     negative_new (newneg:{c}:{date}:{domain}) are tied to the latest run of a
     country on a date: when that (country, date) belongs ONLY to the new
     subject, a threshold alert moves with it, and a negative_new alert --
     which compares against the PREVIOUS date of that country -- is DELETED
     when that previous date belonged to someone else (a cross-subject
     comparison), or moves when the baseline was the new subject's own. A
     (country, date) shared by several subjects is ambiguous: left on subject 1
     and listed. Other alert kinds are left untouched.
  7. Subject 1's positive/negative domains, goals and manual events stay with it.

Safety: the plan is always computed first on a temporary copy (the app's
startup migrations run there, never on your file). --dry-run stops after
printing it. A real run with a non-empty plan backs the DB up next to it
(sqlite backup API: <db>.pre-split-<timestamp>.bak) BEFORE touching it, then
applies every data move in ONE transaction. Idempotent: once split, subject 1
has no foreign runs left and a second run reports "nothing to do".
"""
import argparse
import json
import os
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from database.migrations import aggregate_lifecycle, insert_lifecycle_rows
from database.models import Database, normalize_subject_name, url_hash

PRIMARY = 1  # the subject everything landed on before multi-subject support
_OWN = "__primary__"  # owner key of subject 1 inside the plan


def _sqlite_copy(src: str, dst: str) -> None:
    """Consistent copy (WAL included) through the sqlite backup API."""
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def _first_query(raw: str | None) -> str:
    try:
        queries = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return ""
    if isinstance(queries, list) and queries:
        return " ".join(str(queries[0]).split())
    return ""


# --------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------- #
def build_plan(db: Database) -> dict:
    """Pure read: what the split would do on `db`. Owners are referred to by
    group key (normalized query) -- new subjects have no id yet."""
    primary = db.get_subject(PRIMARY)
    if primary is None:
        raise SystemExit("subject 1 does not exist -- nothing to split.")
    own_names = {normalize_subject_name(n)
                 for n in [primary["name"], *primary["aliases"], *primary["queries"]]}
    own_names.discard("")
    if not own_names:
        raise SystemExit(
            "subject 1 has no name, alias or default query (and no report_query "
            "setting to backfill it from) -- set it first, otherwise every search "
            "would be split away from it.")

    conn = db._connect()
    try:
        runs = conn.execute(
            "SELECT id, apify_run_id, queries, country_code FROM runs "
            "WHERE subject_id = ? ORDER BY id", (PRIMARY,)).fetchall()
        groups: dict[str, dict] = {}
        run_owner: dict[str, str] = {}
        for r in runs:
            query = _first_query(r["queries"])
            key = normalize_subject_name(query)
            if not key or key in own_names:
                continue
            g = groups.setdefault(key, {"key": key, "query": query, "countries": [],
                                        "run_ids": [], "apify_run_ids": [], "results": 0})
            g["query"] = query  # the most recent spelling wins
            country = (r["country_code"] or "").lower()
            if country and country not in g["countries"]:
                g["countries"].append(country)
            g["run_ids"].append(r["id"])
            if r["apify_run_id"]:
                g["apify_run_ids"].append(r["apify_run_id"])
                run_owner[r["apify_run_id"]] = key

        plan = {"db_path": db.db_path, "own_names": sorted(own_names), "groups": [],
                "lifecycle_move": [], "lifecycle_ambiguous": [],
                "evidence_move": [], "evidence_ambiguous": [],
                "notes_move": [], "notes_ambiguous": [], "note_history_move": [],
                "events_move": [], "events_ambiguous": [],
                "alerts_move": [], "alerts_delete": [], "alerts_ambiguous": []}
        if not groups:
            return plan

        for g in groups.values():
            existing = db.find_subject_by_name(g["query"])
            g["existing_subject_id"] = existing["id"] if existing else None
            plan["groups"].append(g)

        # Ownership maps over subject 1's results, as they'd be AFTER the move.
        results = conn.execute(
            "SELECT run_id, url, domain, country_code, engine, scraped_at "
            "FROM search_results WHERE subject_id = ?", (PRIMARY,)).fetchall()
        url_owners: dict[str, set] = defaultdict(set)
        key_owners: dict[tuple, set] = defaultdict(set)
        domain_owners: dict[str, set] = defaultdict(set)
        day_owners: dict[tuple, set] = defaultdict(set)
        for r in results:
            owner = run_owner.get(r["run_id"], _OWN)
            if owner != _OWN:
                g = groups[owner]
                g["results"] += 1
            h = url_hash(r["url"] or "")
            country = (r["country_code"] or "").lower()
            url_owners[h].add(owner)
            key_owners[(h, country, (r["engine"] or "google").lower())].add(owner)
            if r["domain"]:
                domain_owners[r["domain"].lower()].add(owner)
            if r["scraped_at"]:
                day_owners[(country, r["scraped_at"][:10])].add(owner)

        def sole_foreign(owners: set) -> str | None:
            return next(iter(owners)) if len(owners) == 1 and _OWN not in owners else None

        moved_lifecycle: set[tuple] = set()
        for row in conn.execute(
                "SELECT url_hash, url, country, engine FROM url_lifecycle WHERE subject_id = ?",
                (PRIMARY,)):
            k = (row["url_hash"], row["country"] or "", (row["engine"] or "google").lower())
            owners = key_owners.get(k, set())
            target = sole_foreign(owners)
            if target:
                plan["lifecycle_move"].append({"key": list(k), "url": row["url"],
                                               "to": target})
                moved_lifecycle.add(k)
            elif len(owners) > 1:
                plan["lifecycle_ambiguous"].append({"url": row["url"], "country": k[1],
                                                    "engine": k[2],
                                                    "owners": sorted(owners)})

        for row in conn.execute(
                "SELECT id, url, url_hash FROM evidence WHERE subject_id = ?", (PRIMARY,)):
            owners = url_owners.get(row["url_hash"] or url_hash(row["url"] or ""), set())
            target = sole_foreign(owners)
            if target:
                plan["evidence_move"].append({"id": row["id"], "url": row["url"], "to": target})
            elif len(owners) > 1:
                plan["evidence_ambiguous"].append({"id": row["id"], "url": row["url"],
                                                   "owners": sorted(owners)})

        for row in conn.execute(
                "SELECT rowid AS rid, url FROM url_notes WHERE subject_id = ?", (PRIMARY,)):
            owners = url_owners.get(url_hash(row["url"] or ""), set())
            target = sole_foreign(owners)
            if target:
                history = [h["id"] for h in conn.execute(
                    "SELECT id FROM note_history WHERE note_id = ? AND subject_id = ?",
                    (row["rid"], PRIMARY))]
                plan["notes_move"].append({"rowid": row["rid"], "url": row["url"],
                                           "to": target, "history_ids": history})
                plan["note_history_move"].extend({"id": h, "to": target} for h in history)
            elif len(owners) > 1:
                plan["notes_ambiguous"].append({"url": row["url"], "owners": sorted(owners)})

        for row in conn.execute(
                "SELECT id, kind, description FROM events WHERE subject_id = ? "
                "AND kind = 'url_gone'", (PRIMARY,)):
            domain = (row["description"] or "").rsplit(":", 1)[-1].strip().lower()
            owners = domain_owners.get(domain, set())
            target = sole_foreign(owners)
            if target:
                plan["events_move"].append({"id": row["id"], "description": row["description"],
                                            "to": target})
            elif len(owners) > 1:
                plan["events_ambiguous"].append({"id": row["id"],
                                                 "description": row["description"],
                                                 "owners": sorted(owners)})

        dates_by_country: dict[str, list[str]] = defaultdict(list)
        for country, day in sorted(day_owners):
            dates_by_country[country].append(day)
        for row in conn.execute(
                "SELECT id, kind, dedupe_key, message FROM alerts WHERE subject_id = ?",
                (PRIMARY,)):
            key = row["dedupe_key"] or ""
            parts = key.split(":")
            entry = {"id": row["id"], "kind": row["kind"], "dedupe_key": key}
            if parts[0] == "url_gone" and len(parts) >= 4:
                k = (parts[1], parts[2], parts[3])
                owners = key_owners.get(k, set())
                if k in moved_lifecycle:
                    plan["alerts_move"].append({**entry, "to": sole_foreign(owners)})
                elif len(owners) > 1:
                    plan["alerts_ambiguous"].append({**entry, "owners": sorted(owners)})
            elif parts[0] in ("thr", "newneg") and len(parts) >= 3:
                country, day = parts[1], parts[2]
                owners = day_owners.get((country, day), set())
                target = sole_foreign(owners)
                if len(owners) > 1:
                    plan["alerts_ambiguous"].append({**entry, "owners": sorted(owners)})
                elif target and parts[0] == "thr":
                    plan["alerts_move"].append({**entry, "to": target})
                elif target:  # newneg: whose run was the baseline (previous date)?
                    days = dates_by_country.get(country, [])
                    prev = [d for d in days if d < day]
                    baseline = day_owners.get((country, prev[-1]), set()) if prev else set()
                    if not baseline or baseline == {target}:
                        plan["alerts_move"].append({**entry, "to": target})
                    else:
                        plan["alerts_delete"].append({**entry, "baseline": sorted(baseline)})
    finally:
        conn.close()
    return plan


def plan_is_empty(plan: dict) -> bool:
    return not plan["groups"]


def print_plan(plan: dict, out=sys.stdout) -> None:
    w = out.write
    w(f"Subject 1 answers to: {', '.join(plan['own_names'])}\n")
    if plan_is_empty(plan):
        w("Nothing to do: every run of subject 1 is for subject 1.\n")
        return
    for g in plan["groups"]:
        target = (f"existing subject {g['existing_subject_id']}"
                  if g["existing_subject_id"] else "NEW subject")
        w(f"- {g['query']!r} -> {target}: {len(g['run_ids'])} run(s), "
          f"{g['results']} result(s), countries {g['countries']}\n")
    for label, k in (("url_lifecycle rows to move", "lifecycle_move"),
                     ("evidence rows to move", "evidence_move"),
                     ("notes to move", "notes_move"),
                     ("note_history rows to move", "note_history_move"),
                     ("url_gone events to move", "events_move"),
                     ("alerts to move", "alerts_move"),
                     ("alerts to DELETE (cross-subject comparisons)", "alerts_delete")):
        w(f"  {label}: {len(plan[k])}\n")
        for item in plan[k]:
            w(f"      {json.dumps(item, default=str)}\n")
    for label, k in (("url_lifecycle", "lifecycle_ambiguous"),
                     ("evidence", "evidence_ambiguous"), ("notes", "notes_ambiguous"),
                     ("events", "events_ambiguous"), ("alerts", "alerts_ambiguous")):
        if plan[k]:
            w(f"  AMBIGUOUS {label} (left on subject 1): {len(plan[k])}\n")
            for item in plan[k]:
                w(f"      {json.dumps(item, default=str)}\n")


# --------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------- #
def apply_plan(db: Database, plan: dict) -> dict[str, int]:
    """Creates/reuses the subjects, then applies every data move in ONE
    transaction. Returns {group key: subject id}."""
    ids: dict[str, int] = {}
    for g in plan["groups"]:
        if g["existing_subject_id"]:
            ids[g["key"]] = g["existing_subject_id"]
        else:
            ids[g["key"]] = db.add_subject(g["query"], countries=g["countries"],
                                           queries=[g["query"]])

    conn = db._connect()
    conn.isolation_level = None
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        for g in plan["groups"]:
            sid = ids[g["key"]]
            marks = ",".join("?" for _ in g["run_ids"])
            cur.execute(f"UPDATE runs SET subject_id = ? WHERE subject_id = ? "  # noqa: S608 -- only '?' placeholders are interpolated
                        f"AND id IN ({marks})", (sid, PRIMARY, *g["run_ids"]))
            if g["apify_run_ids"]:
                marks = ",".join("?" for _ in g["apify_run_ids"])
                cur.execute(f"UPDATE search_results SET subject_id = ? WHERE subject_id = ? "  # noqa: S608 -- only '?' placeholders are interpolated
                            f"AND run_id IN ({marks})", (sid, PRIMARY, *g["apify_run_ids"]))
        # OR IGNORE (lifecycle, notes): only an EXISTING target subject can already
        # hold the same key; its own row wins and subject 1's stays where it was.
        for item in plan["lifecycle_move"]:
            h, country, engine = item["key"]
            cur.execute("UPDATE OR IGNORE url_lifecycle SET subject_id = ? WHERE subject_id = ? "
                        "AND url_hash = ? AND country = ? AND engine = ?",
                        (ids[item["to"]], PRIMARY, h, country, engine))
        # Every new subject gets lifecycle rows for what it observed and didn't
        # inherit (shared URLs): rebuilt from its own results, never overwriting.
        for g in plan["groups"]:
            sid = ids[g["key"]]
            rows = cur.execute(
                "SELECT url, country_code, engine, subject_id, position, classification, "
                "scraped_at FROM search_results WHERE subject_id = ? ORDER BY id",
                (sid,)).fetchall()
            insert_lifecycle_rows(cur, aggregate_lifecycle(rows))
        for item in plan["evidence_move"]:
            cur.execute("UPDATE evidence SET subject_id = ? WHERE id = ? AND subject_id = ?",
                        (ids[item["to"]], item["id"], PRIMARY))
        for item in plan["notes_move"]:
            cur.execute("UPDATE OR IGNORE url_notes SET subject_id = ? WHERE rowid = ? "
                        "AND subject_id = ?", (ids[item["to"]], item["rowid"], PRIMARY))
            if cur.rowcount != 1:
                continue  # the note stayed, so does its history
            for hid in item["history_ids"]:
                cur.execute("UPDATE note_history SET subject_id = ? WHERE id = ? "
                            "AND subject_id = ?", (ids[item["to"]], hid, PRIMARY))
        for item in plan["events_move"]:
            cur.execute("UPDATE events SET subject_id = ? WHERE id = ? AND subject_id = ?",
                        (ids[item["to"]], item["id"], PRIMARY))
        for item in plan["alerts_move"]:
            cur.execute("UPDATE alerts SET subject_id = ? WHERE id = ? AND subject_id = ?",
                        (ids[item["to"]], item["id"], PRIMARY))
        for item in plan["alerts_delete"]:
            cur.execute("DELETE FROM alerts WHERE id = ? AND subject_id = ?",
                        (item["id"], PRIMARY))
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return ids


def counts_per_subject(db_path: str) -> dict[int, dict[str, int]]:
    """{subject_id: {table: rows}} over every subject-owned table."""
    conn = sqlite3.connect(db_path)
    out: dict[int, dict[str, int]] = defaultdict(dict)
    try:
        for table in Database._SUBJECT_TABLES:
            for sid, n in conn.execute(
                    f"SELECT subject_id, COUNT(*) FROM {table} GROUP BY subject_id"):  # noqa: S608 -- table is a fixed identifier from code, never user input
                out[sid][table] = n
    finally:
        conn.close()
    return dict(out)


def print_counts(db_path: str, title: str, out=sys.stdout) -> None:
    out.write(f"{title}\n")
    for sid, tables in sorted(counts_per_subject(db_path).items()):
        body = ", ".join(f"{t}={n}" for t, n in sorted(tables.items()))
        out.write(f"  subject {sid}: {body}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=os.getenv("DB_PATH")
                        or str(_ROOT / "data" / "seo_dashboard.db"))
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan, change nothing")
    args = parser.parse_args(argv)
    db_path = args.db
    if not Path(db_path).is_file():
        print(f"no database at {db_path}", file=sys.stderr)
        return 2

    # The plan is always computed on a migrated temporary copy: Database() runs the
    # startup migrations, and a dry run must not write to the real file.
    with tempfile.TemporaryDirectory() as tmp:
        probe = str(Path(tmp) / "probe.db")
        _sqlite_copy(db_path, probe)
        probe_plan = build_plan(Database(db_path=probe))
        print_counts(probe, "Before (after startup migrations):")
        print_plan(probe_plan)
        if args.dry_run or plan_is_empty(probe_plan):
            if args.dry_run:
                print("--dry-run: nothing was changed.")
            return 0

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = f"{db_path}.pre-split-{stamp}.bak"
    _sqlite_copy(db_path, backup)
    print(f"Backup written: {backup}")

    db = Database(db_path=db_path)
    plan = build_plan(db)  # recomputed on the real file (it may differ from the probe)
    ids = apply_plan(db, plan)
    print("Subjects: " + ", ".join(f"{g['query']!r}={ids[g['key']]}" for g in plan["groups"]))
    print_counts(db_path, "After:")
    return 0


if __name__ == "__main__":
    sys.exit(main())
