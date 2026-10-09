"""Database schema definitions, column registries, indexes, and tables creation."""


# Expected columns (col_name -> SQL definition) for incremental migration
_RESULTS_COLUMNS = {
    "run_id": "TEXT",
    "query": "TEXT",
    "url": "TEXT",
    "title": "TEXT",
    "description": "TEXT",
    "displayed_url": "TEXT",
    "position": "INTEGER",
    "country_code": "TEXT",
    "domain": "TEXT",
    "classification": "TEXT",
    "scraped_at": "TIMESTAMP",
    # Confidence score + human review queue (2.1): confidence 0-1 of the verdict;
    # classified_by in {rule, ai, human} (NULL legacy = rows predating this column,
    # treated as 'rule' -- see _ensure_review_status); review_status in {none, pending,
    # approved, corrected}. classified_by='human' is IMMUTABLE against mass
    # reclassifications (see reclassify_all).
    "confidence": "REAL",
    "classified_by": "TEXT",
    "review_status": "TEXT DEFAULT 'none'",
    # Identity disambiguation (2.3): 'yes'|'no'|'unsure'|NULL (NULL = not evaluated --
    # Gemini not configured, or legacy row predating this column). 'no' excludes the row
    # from stats/exports/alerts by default
    # (get_stats/latest_run_per_country/get_history_pivot/generate_alerts/report.weekly
    # always exclude it; get_results_filtered additionally exposes an include_homonyms
    # toggle for /results and /backup/csv). Set by
    # processing/enrich.py::annotate_same_person (automatic) or by set_same_person_verdict
    # (human, the "Not this person" button in /review) -- both IMMUTABLE against automatic
    # re-annotation (annotate_same_person only touches URLs without a prior observation
    # with a non-NULL same_person, the same spirit as the human inheritance of
    # classification in save_results).
    "same_person": "TEXT",
    # Severity 1-5 + topical taxonomy (2.2): severity NULL = not yet calculated (legacy
    # row, non-negative result -- only calculated for negatives, to save tokens -- or AI
    # not configured/failed). topic uses the closed taxonomy {legal, financial, personal,
    # labor, product, opinion, other} (English, see ai_classifier.TOPICS -- public).
    # Calculated best-effort post-save by processing.severity.annotate_severity, NEVER in
    # save_results (same principle as same_person: this method doesn't call the AI).
    # There's NO mass backfill on startup -- historical negatives fill themselves in when
    # they reappear in a new run (severity IS NULL).
    "severity": "INTEGER",
    "topic": "TEXT",
    # Search engine (1.1, multi-engine extraction): 'google'|'bing'| ... (see
    # apify.actors.ACTORS). DEFAULT 'google' covers both NEW rows from legacy callers
    # (without 'engine' in the result dict) and the rows that ALREADY existed after the
    # ALTER TABLE -- before this item everything was Google, so that's the honest value
    # for the historical data.
    "engine": "TEXT DEFAULT 'google'",
    # Device (1.3, desktop/mobile split): 'desktop'|'mobile'. Same approach as 'engine' --
    # a run covers ONE device (like country_code); DEFAULT 'desktop' covers new rows from
    # legacy callers and the ones already existing after the ALTER TABLE (everything was
    # desktop before this item).
    "device": "TEXT DEFAULT 'desktop'",
    # Story cluster (2.4): legacy column from a since-removed clustering feature. Kept
    # (always NULL for new rows) so existing databases need no destructive migration.
    "cluster_id": "INTEGER",
    # Short plain reason the AI gave for its label (classified_by='ai' only, <= 140
    # chars; NULL for rules/humans and for AI verdicts cached before reasons existed).
    # Shown in "Why it's here" and by the assistant's explain_result.
    "ai_reason": "TEXT",
    # Page identity (final live test): url_hash(url) -- sha256 of the NORMALIZED URL
    # (tracking params like srsltid/utm_* and fragments stripped, see
    # database.base.normalize_url). The SQL key for "same page" (review queue, human
    # verdict propagation). Written by save_results; backfilled/recomputed at startup by
    # _ensure_url_keys whenever database.base.URL_KEY_VERSION changes.
    "url_key": "TEXT",
    # When a person gave this row its verdict (set_review_verdict /
    # set_same_person_verdict). "Latest human verdict wins" across the rows of one page;
    # NULL for legacy human rows (ordered by id then).
    "human_at": "TEXT",
}

_RUNS_COLUMNS = {
    "apify_run_id": "TEXT UNIQUE",
    "queries": "TEXT",
    "country_code": "TEXT",
    "status": "TEXT",
    "total_results": "INTEGER",
    "positive": "INTEGER",
    "negative": "INTEGER",
    "neutral": "INTEGER",
    "created_at": "TIMESTAMP",
    # Apify costs/retries (1.4): NULL if the actor doesn't report usageTotalUsd (old
    # actor) or if the run was never saved with a known cost.
    "cost_usd": "REAL",
    "compute_units": "REAL",
    "retries": "INTEGER DEFAULT 0",
    # Idempotency of the post-run job (3.1, H3 arbitration): 1 once update_url_lifecycle
    # has already processed this run -- a second call on the same run_id (retry, duplicate
    # chokepoint) exits early without duplicating missed_runs. DEFAULT 0 covers both NEW
    # rows and ones already existing after the ALTER TABLE (see _ensure_columns).
    "lifecycle_done": "INTEGER DEFAULT 0",
    # Search engine (1.1): one DB run per engine -- see apify.runner.ApifyRunner.search
    # (outcome["engine_runs"]) and report.multi_country.persist_engine_run. Same
    # default/reason as _RESULTS_COLUMNS["engine"].
    "engine": "TEXT DEFAULT 'google'",
    # Device (1.3): same approach/default as _RESULTS_COLUMNS["device"].
    "device": "TEXT DEFAULT 'desktop'",
    # Plain note when the AI steps of this run (same-name, severity) failed:
    # "AI checks skipped: <reason>" -- shown on Results/Overview for the run.
    # NULL when the AI worked or no provider is configured.
    "ai_note": "TEXT",
}

# New `evidence` column (1.3) via incremental migration, same pattern as
# _RESULTS_COLUMNS/_RUNS_COLUMNS. run_id (loose FK, no UNIQUE index or constraint --
# runs.apify_run_id) references the search run this is a snapshot of -- only filled in by
# historical rows of the retired SERP snapshot capture (trigger='serp_snapshot'); the 3.2
# HTML rows (auto_negative/manual) are left with run_id NULL (they aren't tied to a
# specific run, see capture_url). Kept so existing databases need no migration.
_EVIDENCE_COLUMNS = {
    "run_id": "TEXT",
}


# New url_lifecycle columns (3.1) via incremental migration, same pattern as
# _RESULTS_COLUMNS/_RUNS_COLUMNS/_ALERTS_COLUMNS.
_URL_LIFECYCLE_COLUMNS = {
    "same_person": "TEXT",
    # Identity per engine (1.1, review HIGH #1): url_lifecycle already had `engine` + a
    # composite PK (url_hash, country, engine) since the 3.1 CREATE TABLE (brought forward
    # ahead of this item) -- but a preexisting DB created BEFORE that migration (without
    # the column in its actual CREATE TABLE, only in the current code) needs this entry
    # for the incremental ALTER TABLE in _ensure_columns, same as
    # _RESULTS_COLUMNS/_RUNS_COLUMNS. See processing/lifecycle.py (the real consumer).
    "engine": "TEXT DEFAULT 'google'",
}


# Alert lifecycle (7.2): new `alerts` columns via incremental migration (_ensure_columns),
# same as _RESULTS_COLUMNS/_RUNS_COLUMNS. status defaulting to 'open' covers the NEW rows
# inserted after the ALTER TABLE; the rows that ALREADY existed in a preexisting DB are
# migrated separately in _ensure_alert_status (UPDATE ... WHERE status IS NULL).
_ALERTS_COLUMNS = {
    "status": "TEXT DEFAULT 'open'",
    "assignee": "TEXT",
    "snoozed_until": "TEXT",
    "resolved_at": "TEXT",
    "resolved_by": "TEXT",
    # Alert incident correlation (7.3): NULL = a standalone alert (today's
    # behavior -- this repo has no incident-grouping feature, so it is
    # always NULL). Left as a logical FK to a would-be `incidents` table
    # (no declared constraint, same approach as the rest of the loose FKs
    # in the repo, see evidence.run_id) for schema compatibility with
    # existing DBs; nothing in this repo writes or reads it.
    "incident_id": "INTEGER",
}

# Timeline correlated with the SERP (4.5): new `events` column via incremental migration
# (_ensure_columns), same pattern as _ALERTS_COLUMNS/_URL_LIFECYCLE_COLUMNS.
# expected_effect is optional free text ("what was expected to happen") to compare against
# the REAL delta that processing.event_impact calculates -- DEFAULT '' (not NULL) because
# add_event already saved an implicit '' for absent text columns, and list_events shows it
# directly in /timeline without a None check.
_EVENTS_COLUMNS = {
    "expected_effect": "TEXT DEFAULT ''",
}

# Notes -> management workflow (4.6, deep dive §4.6): new `url_notes` columns via
# incremental migration (_ensure_columns), same pattern as
# _EVENTS_COLUMNS/_URL_LIFECYCLE_COLUMNS. `status` ALREADY existed (original CREATE TABLE,
# no DEFAULT) -- it isn't listed here, see _ensure_note_status for the backfill that
# aligns it with the whitelist below. `removal_case_id` is a reserved LOGICAL FK for a
# removal-case tracker that is not part of this repo (unused; kept so existing databases
# need no destructive migration).
_URL_NOTES_COLUMNS = {
    "assignee": "TEXT DEFAULT ''",
    "due_date": "TEXT DEFAULT ''",
    "removal_case_id": "INTEGER",
}

# Indexes (3.3): (name, table, columns). Idempotent via CREATE INDEX IF NOT EXISTS, also
# applied to the existing production DB.
_INDEXES = [
    ("idx_search_results_run_id", "search_results", "run_id"),
    ("idx_search_results_country_scraped", "search_results", "country_code, scraped_at"),
    ("idx_search_results_classification", "search_results", "classification"),
    # Page identity (url_key): human verdict lookups/propagation and the review queue.
    ("idx_search_results_url_key", "search_results", "url_key, subject_id"),
    # alerts(dedupe_key) from the 3.3 canon is already covered by the UNIQUE's autoindex.
    ("idx_alerts_created_seen", "alerts", "created_at, seen"),
    # "Filter by resource (C24): "reconstructs the full history of a URL" -- resource_id +
    # id (insertion order) is the real query of list_audit(resource_id=...).
    ("idx_audit_log_resource_id", "audit_log", "resource_id, id"),
    ("idx_audit_log_action", "audit_log", "action"),
    # Canonical url_lifecycle index (3.1): the real query is
    # "everything that's `gone`" (Wins) and "everything active/fading in this country"
    # (update_url_lifecycle) -- both filter by status.
    ("idx_url_lifecycle_status", "url_lifecycle", "status"),
    # Evidence vault (3.2): url_hash is the key for every read (has_evidence/evidence_for
    # -- "is there already evidence for THIS URL?").
    ("idx_evidence_url_hash", "evidence", "url_hash"),
    # Retired SERP snapshots (1.3) were read by run_id; nothing queries it now, the index
    # is kept so existing databases need no migration.
    ("idx_evidence_run_id", "evidence", "run_id"),
    # Note status log (4.6): note_history(note_id) ALWAYS filters by note_id
    # (Database.note_history -- "the history of THIS note"), same approach as
    # idx_evidence_url_hash.
    ("idx_note_history_note_id", "note_history", "note_id"),
]


# DDL reused by _init_db and by the migration that rebuilds these tables
# (_ensure_subject_scoped_uniques): a single definition, so a migrated DB and a
# freshly created one end up with EXACTLY the same schema. Two copies of the CREATE
# would diverge at the first new column.
_ALERTS_DDL = """
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TIMESTAMP,
            kind TEXT,
            severity TEXT,
            country TEXT,
            message TEXT,
            dedupe_key TEXT,
            seen INTEGER DEFAULT 0
        )
        """
_GOALS_DDL = """
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            country TEXT DEFAULT '',
            metric TEXT,
            target REAL,
            created_at TEXT,
            subject_id INTEGER DEFAULT 1,
            UNIQUE(country, metric, subject_id)
        )
        """
_URL_NOTES_DDL = """
        CREATE TABLE IF NOT EXISTS url_notes (
            url TEXT,
            status TEXT,
            note TEXT,
            updated_at TIMESTAMP
        )
        """
# Multi-subject phase 1: url_lifecycle's identity includes the subject -- two subjects
# can observe the same URL in the same country/engine with independent trajectories.
# The old 3-column PK made the second subject's INSERT collide (and its UPDATEs land on
# the first subject's row). Migrated by _ensure_subject_scoped_uniques.
_URL_LIFECYCLE_DDL = """
            CREATE TABLE IF NOT EXISTS url_lifecycle (
                url_hash TEXT NOT NULL,
                url TEXT,
                country TEXT NOT NULL,
                engine TEXT DEFAULT 'google',
                first_seen TEXT,
                last_seen TEXT,
                best_position INTEGER,
                last_position INTEGER,
                classification TEXT,
                status TEXT DEFAULT 'active',
                gone_since TEXT,
                missed_runs INTEGER DEFAULT 0,
                same_person TEXT,
                subject_id INTEGER DEFAULT 1,
                PRIMARY KEY (url_hash, country, engine, subject_id)
            )
            """
# Per-subject domain lists (multi-subject phase 1): PK (domain, subject_id), so two
# subjects can each own (or blacklist) the same domain independently.
_POSITIVE_DOMAINS_DDL = """
            CREATE TABLE IF NOT EXISTS positive_domains (
                domain TEXT NOT NULL,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                subject_id INTEGER DEFAULT 1,
                PRIMARY KEY (domain, subject_id)
            )
            """
_NEGATIVE_DOMAINS_DDL = _POSITIVE_DOMAINS_DDL.replace("positive_domains", "negative_domains")

# subjects/api_keys columns added by multi-subject phase 1 (incremental migration,
# same pattern as _RESULTS_COLUMNS). is_demo marks the synthetic demo subject (seeded by
# scripts/seed_demo_data.py); api_keys.subject_id NULL = deployment-wide key (today's
# behavior), set = a key bound to one subject (wired in phase 2).
_SUBJECTS_COLUMNS = {
    "is_demo": "INTEGER NOT NULL DEFAULT 0",
}
_API_KEYS_COLUMNS = {
    "subject_id": "INTEGER",
}



class SchemaMixin:
    """Schema table creation and initialization."""

    _ALERTS_DDL = _ALERTS_DDL
    _GOALS_DDL = _GOALS_DDL
    _URL_NOTES_DDL = _URL_NOTES_DDL
    _URL_LIFECYCLE_DDL = _URL_LIFECYCLE_DDL

    def _init_db(self):
        conn = self._connect()
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS search_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                query TEXT,
                url TEXT,
                title TEXT,
                description TEXT,
                displayed_url TEXT,
                position INTEGER,
                country_code TEXT,
                domain TEXT,
                classification TEXT,
                scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                apify_run_id TEXT UNIQUE,
                queries TEXT,
                country_code TEXT,
                status TEXT,
                total_results INTEGER,
                positive INTEGER,
                negative INTEGER,
                neutral INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Positive domains added by the user from the web UI (they add to the ones defined
        # in config/positive_domains.py).
        cur.execute(_POSITIVE_DOMAINS_DDL)
        # Domain blacklist (they classify as 'negative' directly).
        cur.execute(_NEGATIVE_DOMAINS_DDL)
        # Key/value settings (e.g. default report query and countries).
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        # Client branding (5.1): a single GLOBAL row (id=1, CHECK) -- OUTSIDE
        # _SUBJECT_TABLES on purpose (like settings/audit_log): it's not per-subject data,
        # it's the visual identity of THIS dashboard deployment. logo_path is relative to
        # the repo root (e.g. "data/branding/logo.png"), never the name a user uploaded --
        # set via Database.set_branding(), consumed by export.pdf_report/excel_exporter.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS branding (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                logo_path TEXT DEFAULT '',
                primary_color TEXT DEFAULT '#14233F',
                secondary_color TEXT DEFAULT '#1F4E9C',
                footer_text TEXT DEFAULT ''
            )
            """
        )
        # Notes/follow-up status per URL (productivity).
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS url_notes (
                url TEXT,
                status TEXT,
                note TEXT,
                updated_at TIMESTAMP
            )
            """
        )
        # Log of a note's state transitions (4.6, deep dive §4.6): note_id references
        # url_notes.rowid (url_notes has a TEXT PK -- `url` -- not its own numeric id;
        # SQLite still exposes a hidden rowid on any table without WITHOUT ROWID, so it's
        # reused instead of restructuring the PK of a table already in production -- see
        # Database.list_notes, which exposes it as `SELECT rowid AS id`). A LOGICAL FK, no
        # declared constraint, same approach as evidence.run_id/alerts.incident_id -- a
        # deleted note (set_note, delete-on-empty) leaves orphaned history rows on
        # purpose, same as the rest of the repo's loose FKs.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS note_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id INTEGER,
                from_status TEXT,
                to_status TEXT,
                by TEXT,
                at TEXT,
                comment TEXT
            )
            """
        )
        # Manual events for the timeline (to correlate with changes).
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_date TEXT,
                kind TEXT,
                description TEXT,
                created_at TIMESTAMP
            )
            """
        )
        # Alerts (new negatives, thresholds, ...).
        cur.execute(
            _ALERTS_DDL
        )
        # The dedupe_key UNIQUE no longer lives on the column: it becomes a (dedupe_key,
        # subject_id) index created in _ensure_subject_scoped_uniques, AFTER
        # _ensure_subject_id has added the column to old DBs.
        #
        # URL lifecycle (3.1): first/last seen + disappearance
        # detection. Composite PK (url_hash, country, engine) -- the same URL can have
        # independent trajectories per country (it falls off the US top results but is
        # still alive in CA) AND per engine (1.1/F4 will bring more than 'google'; it
        # enters the PK NOW so the table doesn't need to be rebuilt later -- see
        # _ensure_url_lifecycle_engine_in_pk, the migration that recreates a preexisting
        # DB that had the old 2-column PK). classification = the last one seen (to know if
        # a disappearance is a win); missed_runs = consecutive runs in the same country
        # without seeing the URL; same_person = the verdict persisted WHEN MOVING TO
        # 'gone' (H5, 3.1 arbitration: avoids a join to search_results on every read of
        # Wins) -- see processing/lifecycle.py::update_url_lifecycle, the post-run job
        # that maintains all of this.
        cur.execute(_URL_LIFECYCLE_DDL)
        # AI verdict cache per URL (2.5). Key = verdict identity (normalized URL + type +
        # prompt version) => bumping the prompt's version naturally invalidates the cached
        # entry (new PK).
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_verdict_cache (
                url_hash TEXT NOT NULL,
                verdict_type TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                verdict_json TEXT,
                model TEXT,
                created_at TEXT,
                PRIMARY KEY (url_hash, verdict_type, prompt_version)
            )
            """
        )
        # Mirror table for the embedded scheduler (8.1): exposes cron/status to the
        # /settings UI without coupling it to APScheduler (whose own data lives in
        # `apscheduler_jobs`, a table the library creates). One row per job declared in
        # scheduler.core.JOBS.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS scheduled_jobs (
                name TEXT PRIMARY KEY,
                cron_expr TEXT,
                enabled INTEGER DEFAULT 1,
                last_run_at TEXT,
                last_status TEXT,
                last_duration_ms INTEGER,
                next_run_at TEXT
            )
            """
        )
        # Send history per notification channel (7.4): one row per attempt (manual test
        # from /settings or, later, the severity dispatcher from 7.1) -- written by
        # notify.send_via(). alert_id is left NULL on test sends (they aren't tied to any
        # `alerts` row).
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS notify_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_id INTEGER,
                channel TEXT,
                ok INTEGER,
                at TEXT,
                error TEXT
            )
            """
        )
        # Routing by severity (7.1): which channel receives each severity
        # ({info,warning,critical}). One row per severity -- seeded with the deep dive's
        # defaults if the table is empty (see _ensure_alert_routing). Read on every send
        # by notify.dispatcher, so a change takes effect without restarting the process.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS alert_routing (
                severity TEXT PRIMARY KEY,
                channel TEXT
            )
            """
        )
        # Immutable audit log (C24): one row per write action, with
        # a chained hash (row_hash = sha256(prev_hash + canonical repr of the row), see
        # _audit_row_canonical). ONLY INSERT anywhere in the code -- neither UPDATE nor
        # DELETE touch this table (verified by a static grep test over src/). subject_id
        # (subject identity, F2) doesn't exist yet on purpose: it's born in the F2
        # migration along with the rest of that phase's columns, not before.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT,
                actor_type TEXT,
                actor_id TEXT,
                action TEXT,
                resource_type TEXT,
                resource_id TEXT,
                params_json TEXT,
                ip TEXT,
                row_hash TEXT
            )
            """
        )
        # Evidence vault (3.2): one HTML snapshot (with a sha256
        # hash) per capture of a negative URL, or triggered manually from /results.
        # trigger in {auto_negative, manual} (plus legacy serp_snapshot rows). result_id
        # (loose FK, no index/constraint -- search_results.id, NULL if the capture was manual on a URL
        # that doesn't come from a specific result) references the row that triggered the
        # capture. The single write point for HTML: processing.evidence.capture_url.
        #
        # png_path/sha256_png/run_id (1.3, RETIRED from the write path): they remain in
        # the schema (additive columns, never dropped) but NO producer fills them in
        # anymore -- both the SERP screenshot (Apify actor) and its HTML replacement
        # (the SERP snapshot, trigger='serp_snapshot') were removed. The 3.2 HTML rows
        # (auto_negative/manual) never fill them in.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT,
                url_hash TEXT,
                captured_at TEXT,
                html_path TEXT,
                png_path TEXT,
                sha256_html TEXT,
                sha256_png TEXT,
                trigger TEXT,
                result_id INTEGER,
                notes TEXT,
                run_id TEXT
            )
            """
        )
        # Goals per country (4.3) -- RETIRED feature: the /goals pages and the
        # projection were removed, nothing reads or writes this table any more. It is
        # still created (and kept by the UNIQUE migration) so existing databases need
        # no destructive migration; drop it in a future schema cleanup.
        cur.execute(
            _GOALS_DDL
        )
        # API keys (9.3, deep dive §9.3): an infrastructure table (auth for /api/v1/...,
        # NOT per-subject data -- same approach as audit_log/backup_log, see
        # _SUBJECT_TABLES below). key_hash stores sha256(raw_key) -- NEVER the raw key
        # (same approach the repo already uses with .env secrets: a DB dump must never
        # leak usable credentials). The raw key is generated and shown ONCE in /settings (add_api_key) and isn't persisted
        # anywhere -- if it's lost, the only option is to revoke and create a new one.
        # revoked_at NULL = active; see verify_api_key (constant-time comparison,
        # hmac.compare_digest) and revoke_api_key.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_hash TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                scopes TEXT NOT NULL DEFAULT 'read',
                created_at TEXT,
                last_used TEXT,
                revoked_at TEXT,
                subject_id INTEGER
            )
            """
        )
        # Multi-tenant: clients/subjects/users (C21a, deep dive §C21, first of three parts
        # -- ONLY identity here, NEITHER the per-subject scoping of the existing data
        # tables (C21b) NOR the white-label portal (C21c)). `clients` is the top level:
        # one row per deployment client (project 1 is "My project" -- see
        # _ensure_tenancy_seed). `branding_id` is left NULL/without a declared FK on
        # purpose -- the per-client branding schema (beyond the global `branding` row from
        # 5.1) is C21c's job, not this part's.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                branding_id INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT
            )
            """
        )
        # `subjects` generalizes "the person/company this dashboard watches" -- today
        # hardcoded throughout the system via the
        # report_query/report_countries/subject_context settings (dashboard.main). id=1 is
        # MANDATORY and is always seeded (see _ensure_tenancy_seed and the _SUBJECT_TABLES
        # comment above): every data table already has `subject_id INTEGER DEFAULT 1`
        # pointing here since F2 -- without this row, that default would be an orphaned FK
        # from the very first startup. `type` distinguishes person/company (no CHECK --
        # SQLite doesn't allow adding one via ALTER, same approach as
        # NOTE_STATUSES/REMOVAL_STATUSES: it's validated in Python, see add_subject
        # further below once C21b needs it).
        # `aliases_json`/`default_queries_json`/`default_countries_json` are JSON lists
        # stored as-is: simple, without a separate normalized table.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS subjects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL DEFAULT 1,
                name TEXT NOT NULL,
                aliases_json TEXT,
                type TEXT NOT NULL DEFAULT 'person',
                context TEXT,
                default_queries_json TEXT,
                default_countries_json TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT,
                is_demo INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # `users`: real login, replacing the single AUTH_USER/AUTH_PASS pair from .env
        # (which keeps working as a safety net, see dashboard.main.login_submit -- brief
        # C21a §4, CRITICAL: it must never lock the operator out of their own system).
        # `client_id NULL` = staff (sees everything, no client filter); `client_id` set =
        # a specific client's user (read-only portal, real scoping arrives in C21b).
        # `password_hash` uses the auth.passwords format (pbkdf2_sha256$iters$salt$hash)
        # -- NEVER the plaintext password, same approach as api_keys.key_hash. `role` has
        # no CHECK (same reason as subjects' `type` above) -- it's validated in Python
        # against _USER_ROLES, see add_user.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'operator',
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT,
                last_login_at TEXT
            )
            """
        )
        conn.commit()

        # Incremental migration for old databases
        self._ensure_columns(cur, "search_results", _RESULTS_COLUMNS)
        self._ensure_columns(cur, "runs", _RUNS_COLUMNS)
        self._ensure_columns(cur, "alerts", _ALERTS_COLUMNS)
        self._ensure_columns(cur, "url_lifecycle", _URL_LIFECYCLE_COLUMNS)
        self._ensure_columns(cur, "events", _EVENTS_COLUMNS)
        self._ensure_columns(cur, "evidence", _EVIDENCE_COLUMNS)
        self._ensure_columns(cur, "url_notes", _URL_NOTES_COLUMNS)
        self._ensure_columns(cur, "subjects", _SUBJECTS_COLUMNS)
        self._ensure_columns(cur, "api_keys", _API_KEYS_COLUMNS)
        conn.commit()

        self._ensure_subject_id(cur)
        self._ensure_indexes(cur)
        self._ensure_fts(cur)
        self._ensure_alert_severities(cur)
        self._ensure_alert_routing(cur)
        self._ensure_alert_status(cur)
        self._ensure_review_status(cur)
        self._ensure_note_status(cur)
        self._ensure_subject_scoped_uniques(cur)
        # After the PK migration above: the backfill groups by subject_id, and on the
        # old 3-column PK two subjects observing the same URL would collide.
        self._ensure_url_lifecycle_backfill(cur)
        # AGAIN after the migration, and it's not redundant: recreating a table takes its
        # indexes down with it (SQLite moves them with the RENAME and they die with the
        # DROP of the old one). Without this, an already-existing DB would lose
        # idx_alerts_created_seen / idx_alerts_incident_id when migrating -- the alerts
        # panel would get slow without anything giving it away. It's idempotent (CREATE
        # INDEX IF NOT EXISTS), so it costs nothing on a DB that didn't migrate.
        self._ensure_indexes(cur)
        self._ensure_tenancy_seed(cur)
        self._ensure_default_project_name(cur)
        self._ensure_subject_1_backfill(cur)
        self._ensure_url_keys(cur)
        conn.commit()
        conn.close()

    # C21 partial (F2 golden rule): every DATA table carries subject_id INTEGER DEFAULT 1
    # as of now, so A1-A12 are born multi-subject even though the multi-client UI (full
    # C21: subjects/clients/users + scoping) arrives in F7. The constant default makes
    # EVERY existing and new row belong to subject 1 (the default subject) without an explicit
    # backfill. ai_prompts was already born with the column (A1). Infrastructure tables
    # (settings, audit_log, notify_log, monitor_checks, scheduled_jobs, backup_log,
    # ai_verdict_cache) are left out on purpose: they aren't subject data. metric_history
    # will be migrated with the full C21 (its UNIQUE needs to include subject_id — a
    # schema change, not just a column).

