# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Nightly database backup: the scheduler job `nightly_backup` writes a
  `VACUUM INTO` copy to `data/backups/` (or `BACKUP_DIR`) at 03:30 UTC and
  deletes copies older than `BACKUP_KEEP_DAYS` (14). A failed run raises a
  `backup_failed` alert, auto-resolved by the next success; Settings →
  Backup shows the last copy's time and size. A `-pre-migration` copy is
  taken automatically before a schema migration (`PRAGMA user_version` vs
  `SCHEMA_VERSION`). `scripts/restore_backup.py <file> --yes` restores a
  verified backup with the server stopped, keeping the current file as
  `<db>.before-restore-<timestamp>`. (#6)

## [1.0.0] - 2026-10-16

First public release. The code was extracted from a larger private tool,
all client data was removed, and the repository was published as a single
clean commit (see `README.md`, "Project history", and `DATA.md`).

### Added

- Searches of Google (desktop and mobile) and Bing (desktop) through
  Apify's hosted scraper actors, per country, on demand from Results → New
  search. The one scheduled search is the weekly report (Monday 08:00 UTC
  by default).
- Rule-based classification (positive-domain list, negative-domain
  blacklist, negative keywords) with a confidence score. Results no rule
  decides land in a human review queue; a human verdict is never
  overwritten by later bulk reclassification.
- Evals (`evals/`): SERP-parsing accuracy, alert-rule correctness,
  processing latency, and classification accuracy measured twice: 100% on
  11 synthetic cases (circular) and 45.28% on 53 real search results, the
  same as always answering "neutral". The real-data number is reported as
  is, not tuned away.
- Alerts computed on stored data (negative threshold per country, newly
  negative URL, tracked negative URL gone, Apify budget, missed job,
  overdue note), routed per severity to email, Telegram, Slack or ntfy.
- Evidence capture: a best-effort download of the HTML of newly negative
  result pages, with its SHA-256 stored in the database; purged after 365
  days by default.
- Optional AI features with your own key (Gemini or any OpenAI-compatible
  endpoint, including a local Ollama / LM Studio server): same-name
  detection, severity 1-5, "Relabel with AI", "Run AI checks", a PDF
  narrative summary, and an in-app assistant with an offline eval. The AI
  never decides the search-time label (the rules do) and has no accuracy
  eval.
- Projects and subjects: several tracked names per install, every row
  scoped to one subject, read-only viewers limited to one project.
- Reports and integrations: weekly summary email, PDF and Excel exports,
  CSV, and a JSON API (`/api/v1`) with scoped API keys. Plus a timeline of
  real-world events, run-to-run Compare, notes, and an audit log with a
  hash chain.
- Embedded APScheduler (weekly report, daily alert check and digest,
  retention purges) with an in-process watchdog that raises `job_missed`.
- Setup wizard, onboarding checklist, synthetic demo data seeded by
  `make run`, and `make check` to validate the configuration.

### Security

- Sign-in required from the first visit (the admin account is created
  then); three roles enforced server-side on every request;
  `ALLOW_NO_AUTH=1` accepted on loopback only.
- Secrets live in `.env` (mode 0600) and are redacted from pages, logs,
  the audit log and assistant output; secret-ish settings are encrypted
  with Fernet when `APP_ENCRYPTION_KEY` is set.
- Sessions: opaque server-side tokens, `HttpOnly`, `SameSite=Lax`,
  `__Host-` and `Secure` under HTTPS, idle and absolute timeouts;
  origin-checked CSRF; PBKDF2-SHA256 passwords (600,000 iterations); rate
  limits, lockouts and bot checks on the anonymous forms.
- CSP with no inline scripts and the full security-header set;
  parameterized SQL enforced by ruff's S608; body-size limit and
  allowlisted inputs; no file uploads; path-safe downloads.
- CI runs ruff, the test suite, a gitleaks scan of the full history and
  `pip-audit`; Dependabot is enabled and Actions are pinned by commit SHA.
  The full list, item by item, is in `SECURITY.md`.

[Unreleased]: https://github.com/carlspareds/serp-monitor/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/carlspareds/serp-monitor/releases/tag/v1.0.0
