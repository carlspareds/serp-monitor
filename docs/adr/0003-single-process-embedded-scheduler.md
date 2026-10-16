# 0003. Single FastAPI process with embedded APScheduler and in-process watchdog

- Status: Accepted
- Date: 2026-10-16 (written retrospectively at v1.0.0)

## Context

Besides the dashboard and the JSON API, the app has recurring work: the
weekly report search, the daily alert check and digest, retention purges.
The target operator runs `make run` on one machine behind a reverse proxy
(`README.md`, Run it); there is no broker, no cron entry, no second service.

## Decision

"A single FastAPI app ... serves a server-rendered HTML dashboard and the
JSON API from one process. ... An embedded APScheduler runs the weekly
report, the daily alert check and info digest, retention purges, and an
in-process watchdog that raises a `job_missed` alert when another job
misses its window" (`README.md`, Architecture). The scheduler starts in
the FastAPI lifespan unless `DISABLE_SCHEDULER` is set
(`src/dashboard/main.py`): an `AsyncIOScheduler` with a
`SQLAlchemyJobStore` "over the SAME SQLite base as the dashboard", plus a
mirror table `scheduled_jobs` that exposes cron/status/next run to the
Settings UI "without coupling it to APScheduler" (`src/scheduler/core.py`).
Jobs register declaratively in `src/scheduler/jobs.py`; `core.py` never
imports `dashboard/`. The watchdog `deadman_check`
(`src/scheduler/deadman.py`, every 15 minutes) scans the mirror table for
enabled jobs that should have run and didn't, with a grace window
proportional to each job's cadence, and dispatches its alert directly:
"the watchdog cannot depend on the very conduit it watches"
(`src/notify/dispatcher.py`).

Code comments call this decision "ADR-002" (`src/scheduler/core.py`), a
number from the private repository's notes; this file is its public record.

## Consequences

### Positive

- One command, one process, one file to back up. Job state survives a
  restart because it lives in the same SQLite file.
- Scheduler state is visible in Settings → Scheduling and on `/health`.

### Negative

- "Because it runs inside the same process, it cannot tell you the
  process itself is down" (`README.md`, Architecture); "the watchdog
  notices missed jobs only while the process is running" (Honest
  limitations). Something outside must poll `/health` for that.
- "Run a single worker: sessions and rate limits live in the process"
  (`README.md`, Deploying behind HTTPS); a second uvicorn worker would
  also start a second scheduler.
- A long job shares the event loop and the SQLite file with web requests.

## Alternatives considered

- System cron calling scripts (`scripts/` has the weekly-report script):
  no watchdog, no UI, and a second thing for the operator to set up.
- A separate worker or task queue (Celery, RQ): needs a broker or a
  second service, out of proportion for one person or a small team.
