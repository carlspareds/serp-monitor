# Security

SERP Monitor is a self-hosted, single-process FastAPI app with one SQLite
file. This document lists what protects it, item by item, where the code
lives, and what is left to the operator. Each section has four parts:
**what it means here**, **how it's done**, **where**, and **residual risk**.

Quick summary for operators:

- Sign-in is required. The first visit asks you to create the admin
  account. `ALLOW_NO_AUTH=1` exists only for a local demo on `127.0.0.1`.
- Put it behind HTTPS before exposing it (see
  [Deploying behind HTTPS](README.md#deploying-behind-https)) and set
  `FORCE_HTTPS=1`.
- Secrets live in `.env` (mode 0600), never in the database or the UI.
  Set `APP_ENCRYPTION_KEY` to encrypt the few secret-ish values the
  database does hold, and encrypt your backups.
- Run one worker (`uvicorn` without `--workers`): sessions, rate limits and
  lockouts are kept in process memory.

CI runs ruff (including the SQL-string rule S608), the full test suite, a
gitleaks secret scan of the whole history, and `pip-audit` on every pull
request. The security tests are `tests/test_security_*.py`.

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Use GitHub's
private reporting instead: the repository's **Security** tab → **Report a
vulnerability**. Include the version (`/health` shows it), what you did,
what happened and what you expected. You should get an answer within a
week. Fixes are released as normal commits on `main` with a note in the
commit message once a fix is available. Only the latest `main` is
supported.

---

## 1. API keys are never shown

**What it means here.** Apify, AI provider, SMTP, Telegram, Slack and ntfy
credentials, and `AUTH_PASS`, must never appear in a page, a JSON answer, a
log line, the audit log, an error message, an export or an assistant
prompt/reply.

**How it's done.**
- Secrets are read from the environment / `.env` only. Settings and the
  setup wizard write them with `admin.set_env`, which creates the file
  **0600** through a temp file and an atomic replace (never a moment with
  the umask's 0644). Settings shows only "configured" and the last
  characters; a saved key is never put back into a form field.
- `common.redact` replaces the current value of every secret-looking
  variable and key-shaped strings (`apify_api_…`, `sk-…`, `AIza…`, Slack
  `xox?-…`, `sm_live_…`, Telegram bot tokens, Slack webhook paths,
  `Bearer …`, `password=…`, `user:pass@` in URLs).
- A logging filter (`logging_setup.RedactingFilter`) runs that redaction on
  every record (message, %-args, `extra` fields, traceback) of the root
  logger and of uvicorn's loggers, and the JSON formatter redacts its
  output once more.
- The audit log runs every parameter through the same redaction before it
  is hashed into the chain. The assistant scrubs every string it returns
  (`assistant.harness.scrub_secrets`).
- FastAPI's JSON validation errors are trimmed so they never echo the
  submitted value.
- `make check` / startup warn when `.env` is readable by group or others.

**Where.** `src/common/redact.py`, `src/dashboard/logging_setup.py`,
`src/dashboard/admin.py` (`set_env`, `write_private`),
`src/dashboard/session.py` (`audit`), `src/assistant/harness.py`,
`src/common/config_check.py` (`_check_env_permissions`),
`src/dashboard/main.py` (`_validation_error`).
Tests: `tests/test_security_http.py` (a crawl of every GET route and every
integration dialog with sentinel secrets set, backups/exports, logs,
uvicorn access-log args, audit, assistant, 0600 writes).

**Residual risk.** Anyone with shell access as the app's user can read
`.env`. Redaction is a safety net for values it knows or recognises; a
secret of an unknown shape that some code logs on purpose would get
through. The AI provider receives result titles/URLs/snippets (see
`DATA.md`), never keys.

## 2. No secrets in Git

**What it means here.** No credential, real name or private data in the
repository or its history.

**How it's done.**
- `.gitignore` covers `.env*` (except `.env.example`), `data/`, every
  database file (`*.db*`, `*.sqlite*`), `*.bak`, exports, live eval results
  (`evals/**/results/live*`), virtualenvs, caches and temporary
  screenshots.
- CI's `security` job downloads a pinned, checksum-verified gitleaks binary
  and scans the **full history** and the working tree on every pull
  request. Fake key-shaped values in tests carry `gitleaks:allow`; the six
  findings in old commits (the same fake values) are listed by fingerprint
  in `.gitleaksignore`.
- `scripts/hooks/pre-commit` runs gitleaks on staged changes and ruff.
  Install it once per clone:
  `git config core.hooksPath scripts/hooks` (and install
  [gitleaks](https://github.com/gitleaks/gitleaks#installing)).
- `make secrets` runs the same scan locally.

**Where.** `.gitignore`, `.gitleaksignore`, `.github/workflows/tests.yml`,
`scripts/hooks/pre-commit`, `Makefile`.

**Residual risk.** A scanner finds known shapes and high-entropy strings,
not everything. The public repository is meant to start from a single clean
commit (see `README.md`, Project history), so anything in the private
history never ships.

## 3. "Public key for the database"

**What it means here.** Hosted-database products give the browser a
public key and rely on database rules. This app has **no** database server
and **no** client-side database key: the browser never talks to SQLite.

**How it's done.** Every read and write goes through server routes and the
`Database` class. The database file path and SQL error text never reach a
client: unhandled errors become a generic 500 page/JSON (the details go to
the server log), `/health` reports `db: unavailable` without the exception,
and there is no endpoint that accepts SQL or a table name.

**Where.** `src/database/` (the only SQL), `src/dashboard/main.py`
(`_server_error`, `health`). Tests: `tests/test_security_data.py`
(`test_db_errors_and_paths_never_reach_the_client`,
`test_no_sql_or_db_endpoints_exist`).

**Residual risk.** Admins can download the whole database
(Settings → Backup); treat those files as sensitive (see item 5).

## 4. Row-level security (per project and subject)

**What it means here.** A subject's runs, results, alerts, notes,
evidence, events, domains and reports are visible only to users allowed to
see that subject: admins and operators see every active subject; a Viewer
(or any non-admin tied to a project) sees only the subjects of that
project.

**How it's done.**
- `SubjectMiddleware` resolves the viewer and the subjects they may see on
  every request and validates the `subject` cookie against that list (an
  unknown, inactive or forbidden id falls back to the first allowed one; a
  viewer allowed nothing gets subject 0, which owns no data).
- The chosen subject is put in a request-scoped ContextVar, and
  `Database._subject_clause()` adds `subject_id = ?` to every query on the
  subject-owned tables. `for_subject(None)` raises instead of silently
  meaning "everything"; `for_all_subjects()` must be asked for explicitly.
- `tests/test_scoping_guard.py` statically fails the build if a
  `Database` method touches a subject table without the clause (or a
  written reason in its allowlist).
- Routes that take a subject id (`/reports/{id}/…`, `/search`,
  `/reports/create`, the Reports dialog, `/api/v1?subject=`) check it
  against the allowed subjects; API keys can be pinned to one subject.
- Background jobs carry their subject explicitly; job status and the
  top-bar list only show jobs of visible subjects.

**Where.** `src/dashboard/context.py`, `src/database/base.py`
(`_subject_clause`, `for_subject`, `subject_scope`),
`src/dashboard/routers/export.py`, `search_runner.py`, `api_v1.py`,
`src/assistant/router.py`. Tests: `tests/test_security_access.py` (a Viewer
and a project-restricted operator of project A against subject B, through
results, runs, review, notes, alerts, timeline, evidence download, report
download, Compare, search, API v1, assistant tools and job status;
writes are checked by snapshotting B's rows), `tests/test_scoping*.py`,
`tests/test_tenancy.py`.

**Residual risk.** Scoping is enforced in application code, not by
SQLite. Admins and operators without a project see everything by design.

## 5. Sensitive data at rest

**What it means here.** Real secrets are not in the database; the database
still holds personal or confidential data (tracked names, notes, evidence
pages) and a few secret-ish settings.

**How it's done.**
- API keys, SMTP password, bot tokens and webhooks: `.env` only (0600).
- Passwords: PBKDF2-SHA256 (item 10). API keys for `/api/v1`: only a
  SHA-256 of a 256-bit random key is stored; the key is shown once.
- Secret-ish settings (`telegram_chat_id`, `common.crypto.SENSITIVE_SETTINGS`)
  are encrypted with Fernet (AES-128-CBC + HMAC-SHA256, `cryptography`)
  when `APP_ENCRYPTION_KEY` is set: stored as `enc:v1:…`, decrypted
  transparently, and existing plain values are encrypted at startup. A
  wrong or missing key reads as empty, never as ciphertext. The startup
  check warns when Telegram is configured without a key and refuses an
  unusable key.
- Notes, evidence and results are **not** encrypted in the database (they
  must stay searchable). Protect them with disk encryption.

**Backups.** Settings → Backup produces the full database (`.db`) or a CSV
of the results: they contain every subject's data, notes and the evidence
hashes. Store them encrypted (for example `age -r <recipient>` or
`gpg --symmetric`), never next to `APP_ENCRYPTION_KEY`, and delete old
copies. `data/evidence/` holds the captured pages themselves.

**Where.** `src/common/crypto.py`, `src/database/settings.py`
(`get_setting`, `set_setting`, `encrypt_sensitive_settings`),
`src/dashboard/main.py` (startup migration), `src/database/audit.py`
(`add_api_key`). Tests: `tests/test_security_data.py`.

**Residual risk.** The key lives in the same environment as the app, so it
protects stolen database files and backups, not a compromised server.

## 6. Authentication is required

**What it means here.** No page or API is reachable without signing in,
including on a fresh install.

**How it's done.**
- A fresh install answers every route with **Create the admin account**
  (`/setup/admin`) until an admin exists; only `/health`, `/robots.txt`,
  static files and `/login` are served meanwhile. The form is guarded by
  the bot checks of item 12, a per-IP lockout, the password policy, and a
  lock so two first visitors can't both create an admin. Once any
  credential exists it redirects to `/login`.
- `AUTH_USER`/`AUTH_PASS` in `.env` remain as an optional env admin.
- `ALLOW_NO_AUTH=1` is the only opt-out, for a local demo (`make demo`):
  it applies only while **no** account exists, the startup check refuses it
  when `HOST` is not a loopback address (even with `SKIP_CONFIG_CHECK=1`),
  and the app serves it only to loopback clients with no proxy headers.
- The setup wizard's admin step can't be skipped.
- `/api/v1` uses API keys (`X-API-Key`), not the session.

**Where.** `src/dashboard/session.py` (`auth_enabled`, `credentials_exist`,
`no_auth_allowed_for`), `src/dashboard/middleware.py` (`auth_middleware`),
`src/dashboard/routers/auth.py` (`/setup/admin`),
`src/dashboard/routers/wizard.py`, `src/common/config_check.py`
(`no_auth_problem`). Tests: `tests/test_security_auth.py`; the rest of the
suite runs with `ALLOW_NO_AUTH=1` (see `tests/conftest.py`) and the
security tests remove it (`strict_auth` fixture).

**Residual risk.** Until the admin account is created, whoever reaches the
app first can create it: create it right after installing, before
exposing the port.

## 7. Access to records by role

**What it means here.** Three roles: `admin` (everything, including
settings, secrets, users, backups, audit export and API keys), `operator`
(day-to-day writes: searches, reviews, notes, alerts, reports) and Viewer
(`client_viewer`, read-only).

**How it's done.** `role_middleware` enforces it server-side on every
request: any unsafe method from a read-only account is a 403 (except the
viewer's own UI state: assistant chat/stop and dismissing a finished
search), admin areas (settings writes, `/backup/*`, `/audit/*`, setup
writes) are a 403 for non-admins, and the users router refuses non-admins
again on its own. The role is read from the database on each request, so a
change applies immediately.

**Where.** `src/dashboard/main.py` (`role_middleware`, `_admin_only`),
`src/dashboard/session.py` (`is_admin`, `can_write`),
`src/dashboard/routers/users.py`. Tests:
`tests/test_security_access.py` enumerates **every** route of the app and
checks that each non-public one requires a session, that every write route
rejects a Viewer and that admin areas reject an operator; a new route
fails the test until it's covered.

**Residual risk.** Route-level checks: a new endpoint that writes on a
`GET` would not be caught by the method rule (the route inventory test
still forces it to be classified).

## 8. Field tampering

**What it means here.** The server never trusts ids, roles, subject ids,
owners or redirect targets sent by the client.

**How it's done.**
- Forms declare exactly the fields they accept (FastAPI parameters), so
  extra fields (`role`, `client_id`, `active`, `password_hash`…) are
  ignored; roles are checked against `USER_ROLES`; the API's PATCH body is
  a two-field Pydantic model.
- Ids in paths and forms are resolved through the subject-scoped
  `Database` (item 4), so another subject's id is "not found".
- `next`/`back` redirect targets go through `safe_next` (same-site
  relative paths only: no `//host`, no backslashes, no schemes).
- The subject cookie is re-validated on every request.

**Where.** `src/dashboard/context.py` (`safe_next`, `resolve_subject_id`),
`src/dashboard/routers/users.py`, `src/dashboard/routers/api_v1.py`.
Tests: `tests/test_security_access.py` (tampered role/owner fields,
escalation attempts, `next` payloads, other subjects' run/result ids,
tampered subject cookies), `tests/test_subject_switching.py`.

**Residual risk.** None known beyond item 4's.

## 9. Session cookies

**What it means here.** The session can't be read by scripts, sent
cross-site, sent over plain HTTP, fixed by an attacker or used forever.

**How it's done.**
- Opaque 256-bit random token, server-side session store.
- Cookie `HttpOnly`, `SameSite=Lax`, `Path=/`; over HTTPS it is
  `__Host-sid` with `Secure` (and every other cookie gets `Secure` too).
  `Lax` rather than `Strict` so links into the dashboard from email
  alerts open signed in.
- Idle timeout (`SESSION_IDLE_MINUTES`, default 120) and absolute lifetime
  (`SESSION_MAX_HOURS`, default 12), enforced on the server.
- A new token on every login (the previous one is dropped); logout
  (`GET` or `POST /logout`) deletes it server-side; a password change
  revokes every session of that account (an admin changing their own gets
  a fresh one); deactivating a user revokes theirs; role changes take
  effect on the next request.
- CSRF: unsafe requests whose `Origin`/`Referer` names another host are
  rejected; `SameSite=Lax` blocks cross-site cookie sends on POST.

**Where.** `src/dashboard/session.py`, `src/dashboard/routers/auth.py`,
`src/dashboard/routers/users.py`, `src/dashboard/security.py`
(`Secure` on cookies), `src/dashboard/main.py` (`csrf_middleware`). Tests:
`tests/test_security_auth.py`.

**Residual risk.** Sessions live in memory: a restart signs everyone out,
and more than one worker process would not share them (run one).

## 10. Password hashing

**What it means here.** Stored passwords can't be reversed cheaply, and
nothing compares them in a way that leaks timing.

**How it's done.** PBKDF2-HMAC-SHA256 with 600,000 iterations (OWASP) and
a 16-byte random salt per hash, compared with `hmac.compare_digest`; a
missing user is checked against a dummy hash so timing doesn't reveal
which accounts exist. If the iteration count is raised, each account is
re-hashed on its next successful login (`needs_rehash`). Policy: at least
12 characters (a passphrase is fine), at most 1024, not the username, not
a short list of common choices, not a single repeated character. Passwords
are never logged or audited.

**Where.** `src/auth/passwords.py`, `src/database/users.py`
(`authenticate_user`). Tests: `tests/test_security_auth.py`,
`tests/test_passwords.py`.

**Residual risk.** PBKDF2 is not memory-hard (argon2/bcrypt would be, but
they are compiled dependencies; see the module docstring). The env admin
(`AUTH_PASS`) is compared in constant time but not subject to the policy:
the startup check warns when it is shorter than 12 characters.

## 11. Rate limiting

**What it means here.** Expensive or guessable endpoints can't be hammered.

**How it's done.** An in-memory token-bucket middleware with per-IP and
per-account buckets, answering 429 with `Retry-After`:

| Rule | Routes | Default | Per |
| --- | --- | --- | --- |
| `login` | `POST /login` | 10 / min | IP |
| `setup` | `POST /setup/*` | 10 / min | IP |
| `password` | `POST /users/add`, `/users/password` | 10 / 5 min | IP and account |
| `api` | `/api/v1/*` | 300 / min | IP (plus 120 / min per API key) |
| `assistant` | `POST /assistant/chat`, `/assistant/confirm` | 20 / min | IP and account |
| `search` | `POST /search`, `/search_multi` | 10 / min | IP and account |
| `reports` | report creation, PDF/backup/audit exports | 10 / min | IP and account |
| `integration_test` | Settings "Test" buttons | 10 / min | IP and account |
| `global` | everything but static files | 1200 / min | IP |

Override with `RATE_LIMIT_<RULE>=N/SECONDS`; `RATE_LIMIT_ENABLED=0` turns
them off.

**Where.** `src/dashboard/security.py` (`RateLimitMiddleware`,
`TokenBuckets`), `src/dashboard/routers/api_v1.py`. Tests:
`tests/test_security_http.py`.

**Residual risk.** Counters are per process and reset on restart. Behind a
reverse proxy, set `TRUSTED_PROXIES` so limits apply to the real client IP
and not to the proxy.

## 12. Bot protection

**What it means here.** Automated sign-in and account-creation attempts
are slowed down and learn nothing.

**How it's done.** The two anonymous forms (sign-in, create the admin
account) carry a hidden honeypot field and a signed "rendered at"
timestamp; a submission with the honeypot filled, a missing/forged token,
or one faster than `BOT_MIN_SUBMIT_SECONDS` (default 1) is rejected with
the same generic answer. After 5 consecutive failures a (username, IP)
pair, or for the setup form an IP, is locked for 60 seconds, doubling with
each further lockout up to an hour. Sign-in errors are one fixed message
("Invalid username or password."), whatever went wrong, and the page never
shows text taken from the URL. `/robots.txt` disallows everything and every
response carries `X-Robots-Tag: noindex, nofollow`.

**Where.** `src/dashboard/security.py` (`bot_check`, `FailureTracker`),
`src/dashboard/routers/auth.py`. Tests: `tests/test_security_auth.py`.

**Residual risk.** No CAPTCHA: a determined, slow, distributed attacker is
limited only by the lockouts and the password policy.

## 13. Parameterized queries

**What it means here.** No user input is ever concatenated into SQL.

**How it's done.** Every value is a bound `?` parameter. The only
f-strings in SQL interpolate fixed fragments: the subject clause
(`subject_id = ?`), lists of `?` placeholders, column/table names from code
or `PRAGMA table_info`, and whitelisted `ORDER BY` columns/directions.
Ruff's S608 rule is enabled in `pyproject.toml`; each of those lines
carries `# noqa: S608 -- <why it's safe>`. Full-text search passes the
user's query as a bound `MATCH` parameter and falls back to a quoted
literal on FTS syntax errors.

**Where.** `src/database/`, `src/processing/lifecycle.py`,
`pyproject.toml`. Tests: `tests/test_security_data.py` (injection payloads
through search, filters, sort and Compare).

**Residual risk.** A future identifier interpolation needs a reviewer to
check its `noqa` reason.

## 14. Input validation

**What it means here.** Every input has a size, a type and, where it makes
sense, an allowlist.

**How it's done.**
- Request bodies over `MAX_BODY_BYTES` (1 MiB) are refused with 413,
  declared or streamed.
- Every string form field declares a `max_length` (ids/enums 64, names
  200, URLs 2048, texts 10,000, secrets 1024); a test fails if a new one
  doesn't. Overlong or malformed input gets a generic 422 page.
- Enums and codes are checked against allowlists (labels, roles, scopes,
  note statuses, countries, engines, devices, report kinds/periods,
  snooze presets); numbers are clamped (limits, offsets, pages,
  thresholds); dates are parsed as ISO dates; URLs saved with a note or
  captured as evidence must be absolute `http(s)` URLs; domains are
  normalised; the assistant's JSON is reduced to an allowlist of
  roles/keys and clipped.
- `common.validate` holds the shared helpers.

**Where.** `src/common/validate.py`, `src/dashboard/security.py`
(`BodySizeLimitMiddleware`), the routers. Tests:
`tests/test_security_http.py`.

**Residual risk.** Lengths are generous on purpose; content is not
interpreted beyond its type.

## 15. Content sanitisation

**What it means here.** Scraped titles, snippets and URLs, and anything
users type, are shown as text, never as HTML or script.

**How it's done.** Jinja2 autoescaping is on; the only `|safe` values are
the app's own server-built shell fragments (a test pins the list). The
HTML built in Python escapes every value with `_esc` (`html.escape`,
quotes included); scraped URLs only become links when they are
`http(s)`. The assistant's markdown-lite escapes the text first and then
allows bold, code, lists, tables and links to in-app paths only. Evidence
(third-party HTML) is downloaded as `application/octet-stream`,
`attachment`, `nosniff`, never rendered. CSV/XLSX exports (results,
reports, the assistant's CSV and the audit CSV) prefix cells that start
with `= + - @ TAB CR` with a quote.

**Where.** `src/dashboard/ui_helpers.py` (`_esc`, link helpers),
`templates/`, `static/app.js` (`md`, `mdInline`),
`src/export/excel_exporter.py` (`neutralize_formula`),
`src/dashboard/routers/evidence.py`, `src/dashboard/routers/audit.py`.
Tests: `tests/test_security_data.py`.

**Residual risk.** The CSP (item 18) is the second line of defence if an
escape is ever missed.

## 16. Files

**What it means here.** No file uploads; downloads can't escape their
folders.

**How it's done.** There is no upload route (a test fails if one
appears). Report downloads accept only names matching
`^[a-z][a-z0-9_-]{2,80}\.(pdf|xlsx|csv)$` inside the subject's own folder;
evidence downloads resolve the stored path and refuse anything outside
the evidence vault; both are `attachment` + `nosniff`. Static files are
served from `static/` only.

**Where.** `src/export/reports.py` (`safe_name`, `find_report`),
`src/dashboard/routers/export.py`, `src/dashboard/routers/evidence.py`,
`src/assistant/router.py`. Tests: `tests/test_security_data.py`
(traversal payloads on every download route and the static mount).

**Residual risk.** None known.

## 17. Return only what's needed

**What it means here.** Responses carry no internal paths, hashes, secrets
or stack traces.

**How it's done.** `/api/v1` responses go through typed models with an
explicit field list (no raw JSON, file paths or key hashes; usernames
appear only where they are the point: the audit log and users list, for
admins). User rows never include `password_hash`. 500 errors are generic
in every environment; tracebacks go to the server log only. `/health` is
public but minimal (status, scheduler, job states, version; no paths, no
exception text). The OpenAPI docs (`/docs`) require a session.

**Where.** `src/dashboard/routers/api_v1.py`, `src/database/users.py`,
`src/dashboard/main.py`. Tests: `tests/test_security_http.py`.

**Residual risk.** `/health` shows the version (useful for monitoring, also
to an attacker); hide it at the proxy if that matters to you.

## 18. Security headers

**What it means here.** The browser enforces what the app intends.

**How it's done.** Every response gets:

```
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
  img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none';
  frame-ancestors 'none'; base-uri 'self'; form-action 'self'
X-Content-Type-Options: nosniff
Referrer-Policy: strict-origin-when-cross-origin
Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()
Cross-Origin-Opener-Policy: same-origin
Cross-Origin-Resource-Policy: same-origin
X-Frame-Options: DENY
X-Robots-Tag: noindex, nofollow
Cache-Control: no-store   (dynamic responses)
```

There is no inline script, inline event handler or `javascript:` URL left
(the theme bootstrap moved to `static/theme-boot.js`); a test scans every
main page and dialog for them, and a Playwright check of the running app
(main pages, dialogs, the assistant) found no CSP violations.
**`style-src` keeps `'unsafe-inline'`**: the server-rendered HTML still
uses `style=""` attributes for small layout tweaks, which a nonce can't
cover. Styles can't run script, so this mainly leaves CSS-based tricks
(e.g. a styled overlay) possible if an escape were missed.

**Where.** `src/dashboard/security.py` (`SecurityHeadersMiddleware`,
`CSP`). Tests: `tests/test_security_http.py`.

**Residual risk.** `'unsafe-inline'` for styles (above).

## 19. HTTPS

**What it means here.** Passwords and session cookies never cross a
network in clear text.

**How it's done.** The app itself listens on `127.0.0.1` by default and
expects an HTTPS reverse proxy in front of it (README: *Deploying behind
HTTPS*, a two-line Caddy config with automatic certificates). With
`FORCE_HTTPS=1`: plain-http requests are redirected (308) to https
(`/health` is exempt for local probes), `Strict-Transport-Security:
max-age=31536000; includeSubDomains` is sent on https responses, and
cookies are `Secure` (`__Host-` prefix for the session).
`X-Forwarded-Proto`/`-For` are believed only from `TRUSTED_PROXIES` (or
uvicorn's `--forwarded-allow-ips`). The startup check warns when `HOST` is
not loopback and `FORCE_HTTPS` is unset.

**Where.** `src/dashboard/security.py`, `src/dashboard/main.py` (proxy
headers), `src/common/config_check.py` (`_check_https`), `README.md`.
Tests: `tests/test_security_http.py`.

**Residual risk.** TLS termination is the proxy's job; HSTS
`includeSubDomains` affects every subdomain of the host you use.

## 20. Dependency scanning

**What it means here.** Known-vulnerable dependencies are caught before
they ship.

**How it's done.** All Python dependencies are pinned. CI runs
`pip-audit --strict` on `requirements.txt` and `requirements-dev.txt`
(no known vulnerabilities at the time of writing); `make audit` runs it
locally. Dependabot opens weekly update PRs for pip and GitHub Actions.
Actions are pinned to full commit SHAs with the version in a comment, jobs
run with `contents: read`, and checkout doesn't persist credentials.
CodeQL (Python and JavaScript) runs once the repository is public (code
scanning on private repositories needs GitHub Advanced Security).

**Where.** `.github/workflows/tests.yml`, `.github/workflows/codeql.yml`,
`.github/dependabot.yml`, `requirements*.txt`, `Makefile`.

**Residual risk.** Transitive dependencies are resolved at install time
(not hash-locked); `pip-audit` checks what resolves at CI time.

## 21. Repository layout

**What it means here.** The repository holds only what users and
reviewers need, and tests never write into it.

**How it's done.** One-off repair/migration tools live in
`scripts/maintenance/` (with a README); the test suite points its default
database, exports and evidence at a temporary directory (`exports/` and
`evidence/` are siblings of the database file); generated eval output is
ignored except the documented offline results. Layout:

```
README.md  SECURITY.md  DATA.md  LICENSE  Makefile  pyproject.toml
requirements.txt  requirements-dev.txt  .env.example
src/  tests/  static/  templates/  scripts/  docs/  evals/  config/  .github/
```

**Where.** `.gitignore`, `scripts/maintenance/README.md`,
`tests/conftest.py`, `src/dashboard/deps.py`.
