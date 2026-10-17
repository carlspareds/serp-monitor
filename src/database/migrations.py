"""Database schema migrations, indexes, FTS backfills, and seed routines."""
import json
import logging
import os
import re
from datetime import UTC, datetime

from auth.passwords import hash_password
from database.base import (
    _DEFAULT_ROUTING,
    _SUBJECT_TABLES,
    DEFAULT_CLIENT_NAME,
    DEFAULT_SUBJECT_COUNTRIES,
    DEFAULT_SUBJECT_QUERY,
    LEGACY_DEFAULT_CLIENT_NAME,
    LEGACY_NOTE_STATUSES,
    NOTE_STATUSES,
    URL_KEY_VERSION,
    url_hash,
)
from database.schema import (
    _ALERTS_COLUMNS,
    _ALERTS_DDL,
    _GOALS_DDL,
    _INDEXES,
    _NEGATIVE_DOMAINS_DDL,
    _POSITIVE_DOMAINS_DDL,
    _URL_LIFECYCLE_COLUMNS,
    _URL_LIFECYCLE_DDL,
    _URL_NOTES_COLUMNS,
    _URL_NOTES_DDL,
)

# Schema version stamped into `PRAGMA user_version` at the end of
# SchemaMixin._init_db. Bump it whenever a migration below changes the schema
# (new column/table/index, a PK rebuild, a backfill that rewrites rows): a
# database behind this number gets a `<stem>-<ts>-pre-migration.db` copy
# (database.backup, in BACKUP_DIR or data/backups/) BEFORE any _ensure_* runs.
# A fresh file (no tables yet) is never backed up. Databases created before
# this constant existed read 0 and get one copy on their first start.
SCHEMA_VERSION = 1

_log = logging.getLogger("migrations")

# Legacy single-subject settings keys. Until phase 2 moves every router to the subject
# row, these keys and subject 1's row are kept in sync in BOTH directions (see
# legacy_setting_to_subject_fields / subject_fields_to_legacy_settings).
LEGACY_SUBJECT_SETTINGS = ("report_query", "report_countries", "subject_context")


def json_list(raw) -> list[str]:
    """Defensive parse of a *_json list column: anything that isn't a JSON list of
    strings becomes [] (never raises -- a hand-edited row mustn't break a page)."""
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if isinstance(v, str | int | float) and str(v).strip()]


def parse_countries(raw) -> list[str]:
    """'us, CA,gb' / ['us','CA'] -> ['us', 'ca', 'gb'] (lowercase, deduped, order kept)."""
    items = raw.split(",") if isinstance(raw, str) else list(raw or [])
    out: list[str] = []
    for c in items:
        c = str(c).strip().lower()
        if c and c not in out:
            out.append(c)
    return out


def apply_legacy_setting_to_subject_1(cur, key: str, value: str) -> None:
    """Mirror of one legacy settings key (report_query/report_countries/
    subject_context) into subject 1's row -- called by SettingsMixin.set_setting so
    routers that still write the settings keys (phase 1) keep subject 1 coherent.

    report_query -> default_queries_json = [value]; the NAME follows the query only
    while it was empty or still equal to the previous query (a name the operator
    customized, e.g. "Jordan Ashworth (demo)", is left alone)."""
    row = cur.execute(
        "SELECT name, default_queries_json FROM subjects WHERE id = 1"
    ).fetchone()
    if row is None:
        return
    value = (value or "").strip()
    if key == "report_query":
        old_queries = json_list(row[1])
        old_query = old_queries[0] if old_queries else ""
        name = (row[0] or "").strip()
        new_name = value if (not name or name == old_query) and value else name
        cur.execute(
            "UPDATE subjects SET name = ?, default_queries_json = ? WHERE id = 1",
            (new_name, json.dumps([value] if value else [])),
        )
    elif key == "report_countries":
        cur.execute("UPDATE subjects SET default_countries_json = ? WHERE id = 1",
                    (json.dumps(parse_countries(value)),))
    elif key == "subject_context":
        cur.execute("UPDATE subjects SET context = ? WHERE id = 1", (value,))


def write_legacy_settings_from_subject_1(cur, fields: dict) -> None:
    """Reverse mirror: subject 1's row changed through the Subject API -> rewrite the
    legacy settings keys the not-yet-migrated routers read. `fields` uses the
    Subject API names (query, countries, context)."""
    mapping = {}
    if "query" in fields:
        mapping["report_query"] = fields["query"] or ""
    if "countries" in fields:
        mapping["report_countries"] = ",".join(parse_countries(fields["countries"]))
    if "context" in fields:
        mapping["subject_context"] = fields["context"] or ""
    for key, value in mapping.items():
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def aggregate_lifecycle(rows) -> dict[tuple, dict]:
    """Shared aggregator of url_lifecycle rows from search_results observations
    ((url, country, engine, subject_id, position, classification, scraped_at), in
    insertion order): key = (url_hash, country, engine, subject_id); first_seen =
    first observation; last_*/classification = the most recent; best_position = the
    minimum. Used by the first-boot backfill and by scripts/maintenance/split_subjects.py."""
    agg: dict[tuple, dict] = {}
    for url, country, engine, subject_id, position, classification, scraped_at in rows:
        h = url_hash(url or "")
        if not h:
            continue
        key = (h, country or "", (engine or "google").lower(), subject_id or 1)
        entry = agg.get(key)
        if entry is None:
            agg[key] = {
                "url": url,
                "first_seen": scraped_at,
                "last_seen": scraped_at,
                "best_position": position if isinstance(position, int) else None,
                "last_position": position,
                "classification": classification,
            }
        else:
            entry["url"] = url
            entry["last_seen"] = scraped_at
            entry["last_position"] = position
            entry["classification"] = classification
            if isinstance(position, int) and (
                entry["best_position"] is None or position < entry["best_position"]
            ):
                entry["best_position"] = position
    return agg


def insert_lifecycle_rows(cur, agg: dict[tuple, dict]) -> int:
    """INSERT OR IGNORE the aggregated rows (status active, missed_runs 0) -- an
    existing row for the same key is never overwritten."""
    cur.executemany(
        "INSERT OR IGNORE INTO url_lifecycle (url_hash, url, country, engine, subject_id, "
        "first_seen, last_seen, best_position, last_position, classification, status, "
        "missed_runs) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 0)",
        [
            (h, e["url"], country, engine, sid, e["first_seen"], e["last_seen"],
             e["best_position"], e["last_position"], e["classification"])
            for (h, country, engine, sid), e in agg.items()
        ],
    )
    return len(agg)


class MigrationsMixin:
    """Incremental column migrations, FTS indexing, and tenancy backfills."""

    def _backup_before_migration(self, cur) -> None:
        """Pre-migration safety copy (hardening #6): if the file already has
        tables and its user_version is behind SCHEMA_VERSION, write a
        `-pre-migration` backup before the _ensure_* chain touches anything.
        A failure here is deliberately fatal: migrating without a copy on a
        disk that cannot hold one is the exact risk this exists to remove
        (`make check` validates BACKUP_DIR beforehand)."""
        if cur.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            return
        if cur.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone() is None:
            return  # brand-new database: nothing to protect yet
        from database.backup import pre_migration_backup  # stdlib-only module, no cycle
        path = pre_migration_backup(self.db_path)
        _log.info("pre_migration_backup", extra={"path": str(path), "schema_version": SCHEMA_VERSION})

    @staticmethod
    def _stamp_schema_version(cur) -> None:
        cur.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")

    def _ensure_subject_id(self, cur):
        for table in self._SUBJECT_TABLES:
            self._ensure_columns(cur, table, {"subject_id": "INTEGER DEFAULT 1"})

    @staticmethod
    def _ensure_columns(cur, table: str, expected: dict[str, str]):
        cur.execute(f"PRAGMA table_info({table})")
        existing = {row[1] for row in cur.fetchall()}
        for col, decl in expected.items():
            if col not in existing:
                # UNIQUE can't be set in ALTER; strip it if it appears
                clean_decl = decl.replace(" UNIQUE", "")
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {clean_decl}")

    @staticmethod
    def _ensure_url_keys(cur):
        """search_results.url_key (page identity, see schema._RESULTS_COLUMNS):
        fills the rows that have none and, when normalize_url changed
        (settings.url_key_version != URL_KEY_VERSION), recomputes every row.
        Idempotent: a second start finds nothing to do. Only this column is
        rewritten -- tables keyed by url_hash are merged by
        scripts/maintenance/repair_url_identity.py (dry-run first)."""
        row = cur.execute("SELECT value FROM settings WHERE key = 'url_key_version'").fetchone()
        current = row[0] if row else None
        where = "" if current != URL_KEY_VERSION else " WHERE url_key IS NULL"
        rows = cur.execute(f"SELECT id, url, url_key FROM search_results{where}").fetchall()  # noqa: S608 -- where is built from constant fragments; values bound
        updates = [(url_hash(r[1] or ""), r[0]) for r in rows if url_hash(r[1] or "") != r[2]]
        if updates:
            cur.executemany("UPDATE search_results SET url_key = ? WHERE id = ?", updates)
        if current != URL_KEY_VERSION:
            cur.execute("INSERT INTO settings (key, value) VALUES ('url_key_version', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (URL_KEY_VERSION,))

    @staticmethod
    def _ensure_indexes(cur):
        """Creates (idempotently) the 3.3 indexes. Also runs against the
        existing production DB on service restart."""
        for name, table, cols in _INDEXES:
            cur.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})")

    @staticmethod
    def _ensure_fts(cur):
        """FTS5 virtual table over search_results' title/description +
        triggers that keep it synced on INSERT/UPDATE/DELETE, plus an
        initial backfill (only if the FTS is empty and search_results isn't).
        Idempotent: CREATE ... IF NOT EXISTS + conditional backfill.
        """
        cur.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS results_fts USING fts5("
            "title, description, content='search_results', content_rowid='id')"
        )
        cur.execute(
            """
            CREATE TRIGGER IF NOT EXISTS results_fts_ai AFTER INSERT ON search_results BEGIN
                INSERT INTO results_fts(rowid, title, description)
                VALUES (new.id, new.title, new.description);
            END
            """
        )
        cur.execute(
            """
            CREATE TRIGGER IF NOT EXISTS results_fts_ad AFTER DELETE ON search_results BEGIN
                INSERT INTO results_fts(results_fts, rowid, title, description)
                VALUES ('delete', old.id, old.title, old.description);
            END
            """
        )
        cur.execute(
            """
            CREATE TRIGGER IF NOT EXISTS results_fts_au AFTER UPDATE ON search_results BEGIN
                INSERT INTO results_fts(results_fts, rowid, title, description)
                VALUES ('delete', old.id, old.title, old.description);
                INSERT INTO results_fts(rowid, title, description)
                VALUES (new.id, new.title, new.description);
            END
            """
        )
        # Initial backfill (preexisting DB migrating to 3.3). NOTE: on an external-content
        # FTS5, COUNT(*) over results_fts delegates to the content table and does NOT tell
        # you if the index is populated; you have to look at the results_fts_data shadow
        # table (an empty index has <=2 structural rows). If there are results but the
        # index is empty, 'rebuild' rebuilds it completely — idempotent and self-
        # repairing.
        sr_count = cur.execute("SELECT COUNT(*) FROM search_results").fetchone()[0]
        idx_rows = cur.execute("SELECT COUNT(*) FROM results_fts_data").fetchone()[0]
        if sr_count > 0 and idx_rows <= 2:
            cur.execute("INSERT INTO results_fts(results_fts) VALUES('rebuild')")

    @staticmethod
    def _ensure_alert_severities(cur):
        """One-shot migration (7.1) for preexisting DBs: `alerts` rows
        with a legacy severity ('med'/'high') move to the canonical one
        ({warning,critical}). Additive and idempotent -- after the first
        pass there are no 'med'/'high' rows left to update."""
        cur.execute("UPDATE alerts SET severity = 'warning' WHERE severity = 'med'")
        cur.execute("UPDATE alerts SET severity = 'critical' WHERE severity = 'high'")

    @staticmethod
    def _ensure_alert_routing(cur):
        """Seeds the default routing (7.1) if `alert_routing` is
        empty -- first startup or a preexisting DB without this table populated.
        Doesn't override changes the operator already made from /alerts."""
        cur.execute("SELECT COUNT(*) FROM alert_routing")
        if cur.fetchone()[0] == 0:
            cur.executemany(
                "INSERT INTO alert_routing (severity, channel) VALUES (?, ?)",
                _DEFAULT_ROUTING.items(),
            )

    @staticmethod
    def _ensure_alert_status(cur):
        """One-shot migration (7.2) for preexisting DBs: `alerts` rows
        without `status` (created before this column) move to 'open'. The
        column's DEFAULT (see _ALERTS_COLUMNS) already covers the NEW
        rows inserted after the ALTER TABLE; this is just the safety
        net for the ones that were already there. Additive and idempotent."""
        cur.execute("UPDATE alerts SET status = 'open' WHERE status IS NULL")

    @staticmethod
    def _ensure_review_status(cur):
        """One-shot migration (2.1) for preexisting DBs: `search_results`
        rows without classified_by (created before this column)
        are treated as classified by rules (classified_by='rule'). They are NOT
        marked 'pending' retroactively -- that would flood the review
        queue with historical data; only new runs (and AI
        reclassifications) enter the queue (see save_results / reclassify_all).
        Additive and idempotent. (review_status doesn't need a backfill: its
        DEFAULT 'none' in _RESULTS_COLUMNS is already applied by ALTER TABLE to
        preexisting rows -- see the migration test.)"""
        cur.execute("UPDATE search_results SET classified_by = 'rule' WHERE classified_by IS NULL")

    @staticmethod
    def _ensure_url_lifecycle_backfill(cur):
        """Initial backfill of `url_lifecycle` (3.1), idempotent: only if the
        table is empty and `search_results` isn't -- same spirit as the
        _ensure_fts backfill. Aggregates the ENTIRE history by (url_hash,
        country) in a single pass ordered by id (insertion order =
        real chronological order, same assumption as _known_same_person in
        processing/enrich.py): first_seen = the first time it was seen, last_seen/
        last_position/classification = the MOST RECENT observation,
        best_position = the historical minimum. status='active', missed_runs=0
        for ALL rows -- the backfill does NOT evaluate disappearances or
        generate alerts/events, that's exclusive to the post-run job
        (processing.lifecycle.update_url_lifecycle) over NEW runs."""
        cur.execute("SELECT COUNT(*) FROM url_lifecycle")
        if cur.fetchone()[0] != 0:
            return
        # Multi-subject phase 1: grouped per subject_id (and engine) -- each subject's
        # lifecycle is built only from its own observations.
        cur.execute(
            "SELECT url, country_code, engine, subject_id, position, classification, "
            "scraped_at FROM search_results ORDER BY id"
        )
        rows = cur.fetchall()
        if not rows:
            return
        insert_lifecycle_rows(cur, aggregate_lifecycle(rows))

    @staticmethod
    def _ensure_subject_scoped_uniques(cur):
        """Rebuilds the GLOBAL UNIQUE constraints that would prevent two
        subjects from coexisting (C21b-2). Idempotent: it looks at the real
        schema and only acts if it finds the old shape.

        SQLite doesn't allow dropping a constraint with ALTER TABLE, so the
        only way is to recreate the table and copy the rows (the procedure
        SQLite itself documents). It's done here, in _init_db, so that
        an existing DB migrates itself on startup -- same as the rest of
        the repo's migrations.

        The three are SILENT traps, which is what makes them dangerous
        (see the debt tests in tests/test_scoping.py):

          - `alerts.dedupe_key` was a global UNIQUE and `add_alert` uses INSERT
            OR IGNORE: the second subject's alert didn't error out, it got
            DISCARDED. A subject could remain unaware that their certificate
            was expiring just because another subject already had that same key.
          - `goals UNIQUE(country, metric)`: the upsert didn't touch
            `subject_id`, so the second subject overwrote the first one's
            target, the row stayed owned by the first subject, and the end
            result was one subject seeing a number they didn't set and the
            other not seeing their own.
          - `url_notes` had the url as PRIMARY KEY, so two subjects
            couldn't annotate the same URL. This one at least failed
            noisily.

        `url_notes` is copied WITH its explicit rowid: `note_history.note_id`
        points to those rowids, and letting SQLite reassign them while copying
        would have silently rewritten each note's history, attributing
        transitions to the wrong note."""
        def _table_sql(table: str) -> str:
            cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
            row = cur.fetchone()
            return (row[0] if row else "") or ""

        def _recreate(table: str, ddl: str, extra_columns: dict, preserve_rowid: bool = False):
            """Renames, recreates with the final DDL + its migrated columns, and
            copies ONLY the columns that exist in both. Copying by an
            explicit list and not with `SELECT *` is what makes the migration safe
            regardless of column order."""
            old_cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})")]
            cur.execute(f"ALTER TABLE {table} RENAME TO {table}_pre_c21b2")
            cur.execute(ddl)
            MigrationsMixin._ensure_columns(cur, table, extra_columns)
            # The newly created table is born with the BASE DDL, so it's missing
            # `subject_id` -- _ensure_subject_id adds it, and it already ran earlier on
            # the old table. Without this, recreating a table would leave it without the
            # column that gives the whole migration its point.
            if table in _SUBJECT_TABLES:
                MigrationsMixin._ensure_columns(cur, table, {"subject_id": "INTEGER DEFAULT 1"})
            new_cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})")]
            shared = [c for c in old_cols if c in new_cols]
            col_list = ", ".join(shared)
            if preserve_rowid:
                cur.execute(f"INSERT INTO {table} (rowid, {col_list}) "  # noqa: S608 -- columns come from PRAGMA table_info, not user input; table is a fixed identifier from code, never user input
                             f"SELECT rowid, {col_list} FROM {table}_pre_c21b2")
            else:
                cur.execute(f"INSERT INTO {table} ({col_list}) "  # noqa: S608 -- columns come from PRAGMA table_info, not user input; table is a fixed identifier from code, never user input
                             f"SELECT {col_list} FROM {table}_pre_c21b2")
            cur.execute(f"DROP TABLE {table}_pre_c21b2")

        if "dedupe_key TEXT UNIQUE" in _table_sql("alerts"):
            _recreate("alerts", _ALERTS_DDL, _ALERTS_COLUMNS)
        # Idempotent and outside the `if`: a new DB is already born without the column
        # UNIQUE, but it still needs the index -- dedupe must keep working WITHIN each
        # subject.
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_dedupe_subject "
                     "ON alerts (dedupe_key, subject_id)")

        if "UNIQUE(country, metric)\n" in _table_sql("goals"):
            _recreate("goals", _GOALS_DDL, {})

        # Multi-subject phase 1: url_lifecycle's PK gains subject_id (the last global key
        # in _SUBJECT_TABLES -- see tests/test_scoping_guard.py::_UNIQUES_PENDING).
        # Rows are copied as-is: unique under the old 3-column key => unique under the
        # new 4-column one.
        pk = re.search(r"PRIMARY\s+KEY\s*\(([^)]*)\)", _table_sql("url_lifecycle"))
        if pk and "subject_id" not in pk.group(1):
            _recreate("url_lifecycle", _URL_LIFECYCLE_DDL, _URL_LIFECYCLE_COLUMNS)
        # Per-subject domain lists: `domain TEXT PRIMARY KEY` -> PK (domain, subject_id).
        # Existing rows keep subject_id 1 (the column default _ensure_subject_id gave
        # them), i.e. they stay with the subject that has always used them.
        if "domain TEXT PRIMARY KEY" in _table_sql("positive_domains"):
            _recreate("positive_domains", _POSITIVE_DOMAINS_DDL, {})
        if "domain TEXT PRIMARY KEY" in _table_sql("negative_domains"):
            _recreate("negative_domains", _NEGATIVE_DOMAINS_DDL, {})

        if "url TEXT PRIMARY KEY" in _table_sql("url_notes"):
            # explicit rowid: note_history.note_id hangs off it, and letting SQLite
            # reassign it would have silently rewritten each note's history, attributing
            # transitions to the wrong one.
            _recreate("url_notes", _URL_NOTES_DDL, _URL_NOTES_COLUMNS,
                       preserve_rowid=True)
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_url_notes_url_subject "
                     "ON url_notes (url, subject_id)")

    @staticmethod
    def _ensure_tenancy_seed(cur):
        cur.execute("SELECT COUNT(*) FROM clients")
        if cur.fetchone()[0] == 0:
            cur.execute(
                "INSERT INTO clients (id, name, active, created_at) VALUES (1, ?, 1, ?)",
                (DEFAULT_CLIENT_NAME, datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")),
            )

        cur.execute("SELECT COUNT(*) FROM subjects WHERE id = 1")
        if cur.fetchone()[0] == 0:
            cur.execute("SELECT value FROM settings WHERE key = 'report_query'")
            row = cur.fetchone()
            name = (row["value"] if row else "") or DEFAULT_SUBJECT_QUERY
            cur.execute("SELECT value FROM settings WHERE key = 'report_countries'")
            row = cur.fetchone()
            countries_raw = (row["value"] if row else "") or DEFAULT_SUBJECT_COUNTRIES
            countries = [c.strip().lower() for c in countries_raw.split(",") if c.strip()]
            cur.execute("SELECT value FROM settings WHERE key = 'subject_context'")
            row = cur.fetchone()
            context = (row["value"] if row else "") or ""
            cur.execute(
                "INSERT INTO subjects (id, client_id, name, aliases_json, type, context, "
                "default_queries_json, default_countries_json, active, created_at) "
                "VALUES (1, 1, ?, ?, 'person', ?, ?, ?, 1, ?)",
                (name, json.dumps([]), context, json.dumps([name] if name else []),
                 json.dumps(countries),
                 datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")),
            )

        cur.execute("SELECT COUNT(*) FROM users")
        if cur.fetchone()[0] == 0:
            username = os.getenv("AUTH_USER", "").strip()
            password = os.getenv("AUTH_PASS", "")
            if username and password:
                cur.execute(
                    "INSERT INTO users (client_id, username, password_hash, role, active, "
                    "created_at) VALUES (NULL, ?, ?, 'admin', 1, ?)",
                    (username, hash_password(password),
                     datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")),
                )

    @staticmethod
    def _ensure_default_project_name(cur):
        """Idempotent: project 1 used to be seeded as "Internal"; it is now
        DEFAULT_CLIENT_NAME ("My project"). Renamed only while it is still exactly
        the legacy default -- a name the operator set is never touched."""
        cur.execute("UPDATE clients SET name = ? WHERE id = 1 AND name = ?",
                    (DEFAULT_CLIENT_NAME, LEGACY_DEFAULT_CLIENT_NAME))

    @staticmethod
    def _ensure_subject_1_backfill(cur):
        """Multi-subject phase 1, idempotent: subject 1 is seeded ONCE (first boot) from
        the legacy settings -- on a DB where the seed ran before the operator set the
        query (fresh install, then the wizard/demo seed), the row was left with an
        empty name / [""] queries / the default country list. Fill ONLY what's empty
        (or still the untouched default) from report_query/report_countries/
        subject_context; never overwrite a value someone set."""
        row = cur.execute(
            "SELECT name, context, default_queries_json, default_countries_json "
            "FROM subjects WHERE id = 1"
        ).fetchone()
        if row is None:
            return
        settings = {
            r[0]: (r[1] or "").strip()
            for r in cur.execute(
                "SELECT key, value FROM settings WHERE key IN (?, ?, ?)",
                LEGACY_SUBJECT_SETTINGS,
            )
        }
        query = settings.get("report_query", "")
        countries = parse_countries(settings.get("report_countries", ""))
        context = settings.get("subject_context", "")
        name, row_context, raw_queries, raw_countries = row[0], row[1], row[2], row[3]
        if query and not (name or "").strip():
            cur.execute("UPDATE subjects SET name = ? WHERE id = 1", (query,))
        if query and not json_list(raw_queries):
            cur.execute("UPDATE subjects SET default_queries_json = ? WHERE id = 1",
                        (json.dumps([query]),))
        row_countries = json_list(raw_countries)
        if countries and (not row_countries
                          or row_countries == parse_countries(DEFAULT_SUBJECT_COUNTRIES)):
            cur.execute("UPDATE subjects SET default_countries_json = ? WHERE id = 1",
                        (json.dumps(countries),))
        if context and not (row_context or "").strip():
            cur.execute("UPDATE subjects SET context = ? WHERE id = 1", (context,))

    @staticmethod
    def _ensure_note_status(cur):
        """Startup migration (4.6) for preexisting DBs, idempotent:

          1. The workflow states used to be persisted in Spanish
             (LEGACY_NOTE_STATUSES: 'pendiente', 'en_gestion', ...) -- they are
             rewritten to their English NOTE_STATUSES equivalent in `url_notes`
             AND in `note_history` (from_status/to_status), so the board and
             the per-note history read consistently.
          2. Rows with `status` NULL/empty (the column already existed WITHOUT
             a DEFAULT, see CREATE TABLE) or any other value outside the
             whitelist (free-form statuses from before 4.6) fall back to
             'open' -- no preexisting note is left out of the board.

        Same pattern as _ensure_alert_status/_ensure_review_status. audit_log
        params are NOT rewritten: they are hash-chained and immutable."""
        for old, new in LEGACY_NOTE_STATUSES.items():
            cur.execute("UPDATE url_notes SET status = ? WHERE status = ?", (new, old))
            cur.execute("UPDATE note_history SET from_status = ? WHERE from_status = ?",
                        (new, old))
            cur.execute("UPDATE note_history SET to_status = ? WHERE to_status = ?", (new, old))
        placeholders = ",".join("?" for _ in NOTE_STATUSES)
        cur.execute(
            "UPDATE url_notes SET status = 'open' "  # noqa: S608 -- only '?' placeholders are interpolated
            f"WHERE status IS NULL OR TRIM(status) = '' OR status NOT IN ({placeholders})",
            NOTE_STATUSES,
        )


