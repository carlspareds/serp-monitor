# SERP Monitor

[![Tests](https://github.com/carlspareds/serp-monitor/actions/workflows/tests.yml/badge.svg)](https://github.com/carlspareds/serp-monitor/actions/workflows/tests.yml)

When someone searches your name (or your business's name) on Google, what
shows up matters, and it changes without warning. SERP Monitor tracks those
search results over time, labels each one positive, negative or neutral,
keeps the history, and alerts you when something changes. It is a
self-hosted FastAPI + SQLite app for one person or a small team, not an
enterprise SEO platform. Accuracy numbers in this repo are measured on real
search results, not assumed; see [`evals/`](evals/README.md).

![Walkthrough of the seeded demo: onboarding guide, dashboard, Compare, review queue, Evidence](docs/demo.gif)

<details>
<summary><b>Screenshots (1280x800, synthetic demo data from <code>make run</code>)</b></summary>
<br>

| Onboarding guide (`/onboarding`) | Dashboard home (`/`) |
| :---: | :---: |
| [![Onboarding guide](docs/screenshots/onboarding.png)](docs/screenshots/onboarding.png) | [![Dashboard home](docs/screenshots/dashboard_home.png)](docs/screenshots/dashboard_home.png) |

| Compare (Results → Compare) | Review (Results → Review) |
| :---: | :---: |
| [![Compare view](docs/screenshots/diff_view.png)](docs/screenshots/diff_view.png) | [![Review queue](docs/screenshots/review_queue.png)](docs/screenshots/review_queue.png) |

| Evidence (Results → More → Evidence) |
| :---: |
| [![Evidence](docs/screenshots/evidence_vault.png)](docs/screenshots/evidence_vault.png) |

The images are regenerated from a freshly seeded database by
`scripts/capture_demo_screenshots.py`, so they show what a fresh
`make run` shows.

</details>

## What it does

- **Searches** Google (desktop and mobile) and Bing (desktop) through hosted SERP providers ([Apify](https://apify.com) actors), per country, on demand or as a weekly scheduled run.
- **Labels** every result with rules (your domains, a blacklist, negative keywords) and sends what the rules can't decide to a human review queue; a person's verdict is final.
- **Alerts** on stored data (negative threshold per country, newly negative URL, tracked URL gone, budget, missed job, overdue note) to email, Telegram, Slack or ntfy, routed per severity.
- **Captures evidence**: a copy of each newly negative page with its SHA-256 stored alongside, so you can tell whether it changed since.
- **Reports**: a weekly summary email, PDF and Excel exports, CSV, run-to-run Compare, a timeline of real-world events, and a JSON API (`/api/v1`) with scoped keys.
- **Optional AI** with your own key (Gemini or any OpenAI-compatible endpoint, including local Ollama / LM Studio): same-name detection, severity, relabelling, a PDF narrative and an in-app assistant. Never the search-time label.
- **Projects and subjects**: several tracked names or clients per install, each with its own data, and read-only viewers limited to one project.

Each of these in detail: [`docs/features.md`](docs/features.md).

## Install and run

Requires Python 3.12 (the only version CI tests) and `make`; or Docker,
see [Run with Docker](#run-with-docker) below.

```
make run
```

This creates a virtualenv, installs dependencies, copies `.env.example` to
`.env` if missing (readable by you only), seeds a synthetic demo (see
[`DATA.md`](DATA.md)) if the database is empty, and serves the dashboard on
`127.0.0.1:8000` (open it in your browser yourself). Re-running it is safe:
every step is skipped if already done. `make run` also starts the embedded
scheduler (weekly report, daily alert check, daily info digest, weekly
purge, watchdog); set `DISABLE_SCHEDULER=1` to turn it off.

**Sign-in is required.** The first time you open it, it asks you to
**create the admin account** (username and a password of at least 12
characters; a passphrase works well), signs you in and shows the demo.
Nothing else is served until that account exists, so create it right away.
For a quick look on your own machine without an account, run `make demo`
instead: it sets `ALLOW_NO_AUTH=1`, which works only on `127.0.0.1` (the
app refuses to start with it on any other address, serves it to local
clients only, and ignores it as soon as an account exists).

The demo needs no API keys: four weekly US runs for the fictional Jordan
Ashworth, with results in the review queue, alerts, evidence snapshots,
timeline events and notes ([`DATA.md`](DATA.md) lists them). A demo-mode
banner stays on every page until a real Apify key is set; `/setup` (the
wizard) and `/onboarding` (the checklist) guide the rest, see
[`docs/features.md`](docs/features.md#in-app-pages-worth-knowing).
`make check` validates `.env` without starting anything (one OK / WARN /
ERROR line per item; `make run` runs it first); missing keys are never
errors, only broken settings are: [`docs/configuration.md`](docs/configuration.md).

### Run with Docker

```
mkdir -p data && docker compose up -d --build
```

Same dashboard on `127.0.0.1:8000`, sign-in required here too. `data/`
holds the database, evidence, exports and the `.env` Settings saves to.
Nothing is seeded: `docker compose run --rm app python scripts/seed_demo_data.py`
loads the demo. Env precedence, `TRUSTED_PROXIES` and HTTPS in
[`docs/deployment.md`](docs/deployment.md).

### From the demo to real data

Demo runs stay in the database when you add a real Apify key. To start clean,
stop the app, delete `data/seo_dashboard.db` and `data/evidence/`, and start
the server directly (`.venv/bin/uvicorn dashboard.main:app --app-dir src --port 8000`;
`make run` would re-seed). Also edit `config/positive_domains.py`, hardcoded to
the demo domains. Details: [`docs/configuration.md`](docs/configuration.md#going-from-the-demo-to-real-data).

Other `make` targets:

```
make check    # validate the configuration
make demo     # like `make run`, without sign-in, on 127.0.0.1 only
make test     # run the test suite
make lint     # ruff check
make audit    # pip-audit: known vulnerabilities in the pinned dependencies
make secrets  # gitleaks over the working tree and history (needs gitleaks)
make seed     # seed demo data (no-op if the database already has runs)
make clean    # remove the virtualenv, the local database, evidence and exports
```

## How to use it

Three destinations in the left rail (a bottom tab bar on phones):
**Overview**, **Results** and **Settings**. Everything else opens as a dialog
or a side panel over the page you are on, so you never lose your place.
The top bar has the *Project ▸ Subject* switcher, "Find in stored results",
the Assistant, the background-search chip, the alerts bell and the account
menu.

**Background searches.** While a search runs, a chip next to the bell says
"Searching… 0:42"; it turns into "Search finished" or "Search failed" and stays
until you open it. It survives reloads; a toast tells you when a search ends.

**Results** shows the active subject's latest run (or a chosen run date) for
one country: a filter bar (country, run date, label chips, "More filters") and
a sortable table (position, result, label with a "needs review" tag, severity,
note and evidence tags). Each row's ⋯ menu adds a note, saves an evidence
snapshot, changes the label, marks the domain as yours or negative, shows it
on the timeline, asks the assistant, or opens the page. Results is the one
home of New search, Review, Compare, Reports and the AI checks. The toolbar opens:

| Tool | Opens as | What it does |
| --- | --- | --- |
| New search | dialog → side panel | Pick the subject, countries and (when several are configured) engines; the search runs in the background and the side panel shows one status box: what it is doing now ("Searching Google in the United Kingdom (2 of 6)…", "Waiting for Apify… 0:35", "Checking same-name results with AI…"), a progress bar, the elapsed time and the countries and engines on one line; the per-country steps are under a collapsed Details (opened by itself when one fails). "Run in background" closes the panel (the top-bar chip keeps track); "View results" switches to that subject. Disabled, with a link to Settings → Search, until an Apify key is set. |
| Review | dialog | One uncertain result at a time ("3 of 7", progress bar): Positive / Negative / Neutral / Not this person (keys 1-4), Skip. Your verdict is final: rules and AI never overwrite it. |
| Compare | dialog | Older run A vs newer run B of a country: entered, left, moved, label changes. |
| Runs | side panel | "Running now" (the subject's searches in progress, each opens its progress panel), then every run with its counts; open one, compare it with the previous one, delete it (admins). |
| More → Timeline | side panel | Log an event (press release, removal request…) and see its measured effect two weeks later; changes detected between runs are listed too. |
| More → Evidence | side panel | Saved copies of negative pages with their SHA-256 fingerprint, per page. |
| More → Notes | side panel | Notes by status (open, in progress, escalated, resolved, dismissed), overdue ones flagged. |
| More → Reports | dialog | New report: pick what it's for (PDF to share, Excel to analyse, CSV for tools), the subject, countries and period, check the preview, create, download. Previous reports lists every file of the subject, including the weekly report. |
| More → AI checks | dialog | "Relabel with AI" (what the rules leave neutral) and "Run AI checks" (same-name and severity on the latest runs, up to 30 per check per click), with the counts waiting now. Disabled, with a link to Settings → AI provider, until a provider is set up. |

Domains (your positive domains and the negative list, plus "Apply to stored
results") live with each subject in **Settings → Projects and subjects**.
Old addresses (`/runs`, `/review`, `/diff`, `/evidence`, `/notes`, `/export`,
`/search_page`…) redirect to Results with the matching dialog or panel open, so
bookmarks keep working. Several names or clients in one install, roles, and the
scheduled jobs per subject: [`docs/projects-and-subjects.md`](docs/projects-and-subjects.md).

## Known limits and what's next

Each limit below is a plain fact about v1.0; what follows it is the planned
change, tracked on the [milestones page](https://github.com/carlspareds/serp-monitor/milestones).

- **The rule classifier is no better than always answering "neutral" on real
  data.** 100% on 11 synthetic fixtures written with its rules in mind
  (circular); 45.28% on 53 real results about public figures and companies,
  exactly the neutral base rate, because it predicts "neutral" for all 53.
  Why, in [`evals/README.md`](evals/README.md). Next: classifier v2 measured
  on a human-verified real dataset, [#14](https://github.com/carlspareds/serp-monitor/issues/14).
- **The review queue catches uncertainty, not errors.** Results no rule
  decides (confidence 0.5) go to Review, but a confident rule hit is never
  queued, and confident hits can be wrong: the demo has a "charges dismissed"
  headline labeled negative because it contains "court". Same fix: [#14](https://github.com/carlspareds/serp-monitor/issues/14).
- **The AI features are not evaluated.** Same-name detection, severity scoring
  and AI reclassification have no eval in this repo, with any provider.
  Search-time classification never calls the AI provider; relabelling and "Run
  AI checks" are manual buttons (Results → More → AI checks). Next: [#13](https://github.com/carlspareds/serp-monitor/issues/13), [#14](https://github.com/carlspareds/serp-monitor/issues/14).
- **Evidence is an integrity check, not proof.** The hash sits in the same local
  database as the file it describes, so anyone who can edit one can edit the
  other. The audit log's hash chain (`src/database/audit.py`) has the same limit,
  no external anchor: it detects accidental or naive edits, not a determined admin.
- **Monitoring of the monitor is limited.** The watchdog notices missed jobs only
  while the process is running; nothing checks database integrity or whether the
  notification dispatcher is actually delivering. Next: nightly backup with a tested
  restore [#6](https://github.com/carlspareds/serp-monitor/issues/6), foreign keys and a weekly `integrity_check` [#7](https://github.com/carlspareds/serp-monitor/issues/7).
- **Needs an Apify account for anything real.** Nothing here scrapes Google or Bing
  directly; without a key you only see the synthetic demo, and rank data is only as
  good as Apify's actors staying in sync with the engines' page structure. Bing has
  no mobile mode, and the Google mobile option passes an actor input key that hasn't
  been verified against a live run. Scraping success rate and live search latency
  aren't measured for the same reason; `evals/README.md` lists what is. Next: pluggable providers, [#3](https://github.com/carlspareds/serp-monitor/issues/3).
- **No ranking-drop alert.** Alerts are about labels and URLs that appear or
  vanish, not positions. Next: tracked keywords, position history and a
  rank-drop alert, [#4](https://github.com/carlspareds/serp-monitor/issues/4).
- **Single SQLite file.** Fine for one person or a small team running their
  own instance; not built for heavy concurrent write load. Next: a light load
  test and documented limits, [#28](https://github.com/carlspareds/serp-monitor/issues/28).
- **Other loose ends:** PDF branding isn't configurable (there is no UI for
  it), the PDF's narrative summary needs an AI provider, and CI only tests
  Python 3.12. Next: a monthly client report with branding, [#19](https://github.com/carlspareds/serp-monitor/issues/19).

## Documentation

- [`docs/features.md`](docs/features.md): every feature in detail, the screens and workflow, the setup wizard and onboarding pages.
- [`docs/configuration.md`](docs/configuration.md): `make check`, every environment variable, demo to real data, accounts and roles.
- [`docs/deployment.md`](docs/deployment.md): HTTPS behind a reverse proxy (Caddy, `FORCE_HTTPS`, `TRUSTED_PROXIES`) and Docker details.
- [`docs/projects-and-subjects.md`](docs/projects-and-subjects.md): several names or clients per install, scheduled jobs per subject, the API's `?subject=`.
- [`docs/architecture.md`](docs/architecture.md): the `src/` tree, where data goes, the design around uncertainty and human review, project history.
- [`docs/adr/`](docs/adr/): architecture decision records, one per design decision, with trade-offs.
- [`SECURITY.md`](SECURITY.md): the security model, threat by threat, and how to report a vulnerability.
- [`DATA.md`](DATA.md): where the demo data comes from, what ships in the repo, where data flows with real keys.
- [`evals/README.md`](evals/README.md): what is measured (parsing, classification on synthetic and real data, alert rules, latency) and what isn't.
- [`CHANGELOG.md`](CHANGELOG.md): notable changes per release.
- [`docs/assistant.md`](docs/assistant.md): design of the in-app assistant (tools, confirmation, audit, provider handling).

## Contributing

Issues and pull requests are welcome. Before opening a PR: `make lint` and
`make test` must pass (CI also runs the gitleaks secret scan and
`pip-audit`), install the pre-commit hook once
(`git config core.hooksPath scripts/hooks`), and keep any test data
synthetic (`.example` domains, fictional names). Report security problems
privately, see [`SECURITY.md`](SECURITY.md).

Add a line for your change to [`CHANGELOG.md`](CHANGELOG.md) under
*Unreleased*. A change to one of the decisions in `docs/adr/` comes with a
new ADR; `docs/adr/README.md` has the template.

## License

MIT, see [`LICENSE`](LICENSE).
