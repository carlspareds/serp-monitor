# Architecture

How the app is put together, where data goes, the design idea behind the
review queue and the evals, and where the code came from. The individual
decisions, with their reasons and trade-offs, are in [`adr/`](adr/).

[Back to README](../README.md)

## Architecture

A single FastAPI app (`src/dashboard/main.py` + `src/dashboard/routers/`)
serves a server-rendered HTML dashboard and the JSON API from one process.
Data lives in one SQLite file, accessed through a hand-written `Database`
class (`src/database/`) rather than an ORM. An embedded APScheduler runs
the weekly report, the daily alert check and info digest, retention purges, and an
in-process watchdog that raises a `job_missed` alert when another job
misses its window. Because it runs inside the same process, it cannot
tell you the process itself is down.

```
src/
  dashboard/   FastAPI app, routes, HTML rendering, setup wizard,
               security middlewares (headers, HTTPS, limits)
  database/    Database class (schema, queries, multi-tenant scoping)
  auth/        password hashing and policy
  common/      config check, redaction, validation, encryption, network guard
  assistant/   the in-app assistant (tools, model harness)
  apify/       Apify actor client + per-engine result normalizers
  processing/  classification, alerts, diffing, evidence capture
  notify/      email / Telegram / Slack / ntfy senders
  scheduler/   embedded APScheduler jobs + watchdog
  export/      Excel and PDF report builders
  report/      weekly report orchestration
tests/         pytest suite (tests/test_security_*.py: the SECURITY.md checks)
static/, templates/   CSS, JS (no inline scripts), fonts, Jinja templates
scripts/       seed, config check, weekly report, screenshots, git hook;
               maintenance/ holds one-off repair tools for old databases
config/        static domain lists and Apify actor settings
docs/          screenshots, assistant and UI notes
evals/         classifier and assistant evals (see evals/README.md)
```

Security model, threat by threat: [`../SECURITY.md`](../SECURITY.md).
The main design decisions, with their reasons and trade-offs, decision by
decision: [`adr/`](adr/). How the server-rendered UI is built (shell,
dialogs, side panels, job status): [`ui-architecture.md`](ui-architecture.md).

**Where data goes.** Everything is stored locally in the SQLite file and
`data/evidence/`. Data leaves the machine in these cases:

- Apify, for every search (the query, country, and engine).
- The AI provider, if one is set (Gemini or an OpenAI-compatible API):
  result titles, URLs, and snippets for the AI features listed in
  [`features.md`](features.md). A
  local endpoint (Ollama, LM Studio on your machine) keeps this data on
  the machine.
- The sites behind newly negative results, which the server fetches
  directly for evidence capture.
- The notification channels you configure. ntfy defaults to the
  **public** `ntfy.sh` server if you only set `NTFY_TOPIC`. Set `NTFY_URL`
  to a self-hosted instance for anything sensitive.

## Design: uncertainty, human review, and evaluation

The core design is built around not trusting the model blindly: a
classifier that reports a confidence and sends the cases it can't decide
to a human review queue instead of guessing (Results → Review), human verdicts
that later automation can't overwrite, an evidence trail that stores and
hashes what a decision was based on (`src/processing/evidence.py`), and
an audit log of every write a user makes
(`src/dashboard/routers/audit.py`). In short: don't auto-decide the
ambiguous cases, keep a record of what informed each decision, and log
who changed what.

The eval matters more than the architecture. The classifier looks perfect
on its own fixtures and is no better than a constant baseline on real
data. The repo reports that number instead of tuning the keyword list
until the eval passes, which would only move the circularity one level
up. It also shows the limits of the safety design: a review queue that
only catches *low-confidence* cases doesn't help when the model is
confidently wrong. The most useful thing in the repo may be the eval that
shows its classifier doesn't work on real data; the README's "Known limits
and what's next" section lists the consequences and the roadmap items that
address them.

## Project history

The public repo is published as a single clean commit. It was extracted
from a larger private tool, and all client data was removed (see
[`../DATA.md`](../DATA.md)). Starting a fresh history was the only way to be sure nothing
from the original repo (credentials, real names, real domains) survived in
an old diff. Carrying the old history forward and scrubbing it afterwards
would have meant trusting a find-and-replace pass never to miss anything.

Before publishing, the code went through a security pass, documented item
by item in [`../SECURITY.md`](../SECURITY.md) (sign-in required from the first
visit, CSP and security headers, rate limits, encrypted secret-ish
settings, CI secret and dependency scanning), and one-off repair tools for
databases from older versions moved to `scripts/maintenance/`.

## Data and evals

- [`../DATA.md`](../DATA.md): where the demo data comes from, what ships in the
  repo, and where data flows when you use real keys.
- [`../evals/`](../evals/): SERP-parsing accuracy, alert-rule correctness, and
  processing latency, plus classification accuracy measured twice: on
  synthetic fixtures (100%, circular) and on 53 real search results
  (45.28%, equal to always answering "neutral"). Run with
  `.venv/bin/python evals/run_evals.py`.
