# Configuration

How the configuration is checked, what every environment variable does, and
how to go from the seeded demo to real data.

[Back to README](../README.md)

## Checking the configuration

`make check` (or `.venv/bin/python scripts/check_config.py`) validates `.env`
and the environment without starting anything, prints one OK / INFO / WARN /
ERROR line per item plus which variables are set (values are never shown),
and exits 1 if there is an error. `make run` runs it first, and the same
checks run once at server startup: errors stop the server with a list of
what to fix, warnings are logged as one line. Set `SKIP_CONFIG_CHECK=1` to
bypass both.

Missing keys are never errors: with none at all you get the demo. Only
broken settings are:

| Variable | Without it | Error when |
|---|---|---|
| `APIFY_API_KEY` | no Apify; demo mode if `SERPER_API_KEY` is unset too (warning) | — |
| `SERPER_API_KEY` | no Serper.dev; demo mode if `APIFY_API_KEY` is unset too (warning) | — |
| `SERP_PROVIDER` | auto: `apify` if `APIFY_API_KEY` is set, else `serper` if `SERPER_API_KEY` is set | — (warning if unknown, or if the chosen provider's key is missing: no fallback, searches are disabled) |
| `SERPER_USD_PER_1K` | `1.00`: the USD per 1,000 Serper.dev credits used to *estimate* its spend for the budget (Serper bills credits; Starter pack $1.00, down to $0.30) | — (warning if not a number ≥ 0) |
| `AUTH_USER` / `AUTH_PASS` | no env admin: the first visit creates the admin account | only one of the two is set (warning if `AUTH_PASS` is under 12 characters) |
| `ALLOW_NO_AUTH` | sign-in required | `HOST` is not a loopback address (also with `SKIP_CONFIG_CHECK`; local demos only) |
| `FORCE_HTTPS` | no redirect (fine on `127.0.0.1`) | — (warning if `HOST` is not loopback and it's unset); see [Deploying behind HTTPS](deployment.md#deploying-behind-https) |
| `TRUSTED_PROXIES` | `X-Forwarded-*` ignored (uvicorn trusts `127.0.0.1`) | — |
| `APP_ENCRYPTION_KEY` | secret-ish DB settings stored in plain text | not a Fernet key or a 16+ character passphrase (warning if unset while Telegram is set up) |
| `SESSION_IDLE_MINUTES` / `SESSION_MAX_HOURS` | 120 minutes idle / 12 hours | — |
| `MAX_BODY_BYTES` | 1048576 (1 MiB) | — |
| `RATE_LIMIT_ENABLED` / `RATE_LIMIT_<RULE>` | on, defaults in `SECURITY.md` | — |
| `BOT_MIN_SUBMIT_SECONDS` | 1 | — |
| `DB_PATH` | `data/seo_dashboard.db` | its directory can't be created or written, or it is a directory |
| `ENV_FILE` | Settings writes to the repo `.env` | it is a directory, or its directory doesn't exist / isn't writable (a missing file is a warning: it is created on first save) |
| `SMTP_HOST` / `SMTP_USER` / `SMTP_PASS` | no email | some but not all three are set |
| `SMTP_PORT` | 465 (SSL; anything else uses STARTTLS) | SMTP is configured and it isn't an integer in 1-65535 |
| `SMTP_FROM` / `ALERT_EMAIL` | default to `SMTP_USER` | — (warning if set without SMTP) |
| `TELEGRAM_BOT_TOKEN` | no Telegram (also needs the chat id, saved in Settings → Notifications) | — |
| `SLACK_WEBHOOK_URL` | no Slack | not an http(s) URL |
| `NTFY_URL` / `NTFY_TOPIC` | no ntfy | `NTFY_URL` not an http(s) URL |
| `LLM_PROVIDER` | auto: `gemini` if `GEMINI_API_KEY` is set, else `openai` if `OPENAI_BASE_URL` or `OPENAI_API_KEY` is set | — (warning if unknown, or if the chosen provider is incomplete) |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | no Gemini; model `gemini-2.5-flash-lite` | — (warning if a non-default model is set while Gemini isn't active) |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` (full base including `/v1`; e.g. Ollama `http://localhost:11434/v1`, LM Studio `http://localhost:1234/v1`) | — (warning if not an http(s) URL) |
| `OPENAI_API_KEY` | none (optional for local servers) | — (warning if missing for api.openai.com) |
| `OPENAI_MODEL` | `gpt-4o-mini` on api.openai.com only | — (warning if missing for any other base URL) |
| `AI_REASONING_EFFORT` | `low` (OpenAI-compatible only; `medium`/`high`, or `off` to never send it; dropped automatically for servers that reject it) | — |
| `DISABLE_SCHEDULER` | scheduler on | — (any non-empty value disables it, even `0`: warning) |
| `SUBJECT_QUERY` | name used in AI prompts and report titles when no tracked name is saved | — |
| `HOST` | optional | — (see `ALLOW_NO_AUTH` and `FORCE_HTTPS`) |

## Going from the demo to real data

The demo runs and their alerts and evidence stay in the database when you
add a real SERP provider key (Apify or Serper.dev). To start clean, stop the app, delete
`data/seo_dashboard.db` and `data/evidence/`, then start the server
directly with `.venv/bin/uvicorn dashboard.main:app --app-dir src --port 8000`
(the new database has no accounts, so the first visit asks you to create
the admin account again). `make run` would re-seed the empty database with
the demo. Also edit
`config/positive_domains.py`: its `POSITIVE_DOMAINS` list is hardcoded to
the fictional Jordan Ashworth domains and is merged with whatever domains
you add in the UI.

## Accounts and roles

Accounts have one of three roles: `admin` (everything, including
settings, secrets, backups, users and API keys), `operator` (day-to-day
work such as searches, alerts and notes) and Viewer (read-only; stored as
the `client_viewer` role). Add them in Settings → Users. An optional env
admin can also be set with `AUTH_USER`/`AUTH_PASS` in `.env`. Sign-in is
rate limited and locks for 60 seconds (doubling up to an hour) after 5
failures, sessions expire after 2 hours idle or 12 hours in total, and
form posts from another origin are rejected. See
[`../SECURITY.md`](../SECURITY.md) for everything else. What a Viewer tied
to a project can see is in [Projects and subjects](projects-and-subjects.md).
