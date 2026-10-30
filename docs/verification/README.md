# Feature map for verification

How to show that a change works in the running app, not only in the tests.
Each file below is one dashboard feature: how a user reaches it, how a
headless browser drives it, what to check in the database, and known traps.
Where each feature's code and tests live: [`AGENTS.md`](../../AGENTS.md).

| Feature | File | Opens as | Walkthrough tested |
| --- | --- | --- | --- |
| Review | [review.md](review.md) | dialog | yes (2026-10-29) |
| Compare | [compare.md](compare.md) | dialog | no |
| Reports | [reports.md](reports.md) | dialog | no |
| Evidence | [evidence.md](evidence.md) | side panel | no |
| Notes | [notes.md](notes.md) | side panel | no |

Only Review has an end-to-end walkthrough that has been run and passed. The
other four files were written from the code (`src/dashboard/routers/`); treat
their selectors as a starting point and fix the file when a walkthrough shows
otherwise.

## Isolated demo instance

Never use the repo `.env` or real keys. Start a throwaway instance with its
own database, `.env` and port, on loopback only (`ALLOW_NO_AUTH=1` is refused
on any other host):

```bash
RUN=$(mktemp -d)
: > "$RUN/.env"
export DB_PATH="$RUN/seo_dashboard.db" ENV_FILE="$RUN/.env" \
       DISABLE_SCHEDULER=1 ALLOW_NO_AUTH=1 HOST=127.0.0.1 \
       APIFY_API_KEY= LLM_PROVIDER= SMTP_HOST=
.venv/bin/python scripts/seed_demo_data.py
.venv/bin/uvicorn dashboard.main:app --app-dir src --host 127.0.0.1 --port 8791
```

Blank every other key you have set in your shell too. Ports 8000
(`make run`) and 8765 (`scripts/capture_demo_screenshots.py`) are taken by
other tools. The seed creates the subject "Jordan Ashworth (demo)" with 4 US
runs, 2 results in the Review queue, 5 alerts, 3 evidence snapshots and 2
notes. Reports go to `$RUN/exports/`, snapshots to `$RUN/evidence/`.

## Driving the dashboard

- Locate by role, accessible name, `id` or `data-*` attribute, never by
  coordinates. Dialogs are `section.modal[role=dialog]`, side panels
  `section.panel[role=dialog]`; both close with `[data-sm-close]` or Escape.
- Results rows are `tr#r-<id>` with `data-result-url`, `data-domain` and
  `data-label`. A row's ⋯ menu (`[data-popover^='#rm-']`) and the toolbar's
  More menu (`[data-popover='#results-more']`) are cloned from a
  `<template>`: open the popover, then pick the `menuitem` by name.
- Use the UI path a user takes; don't POST by hand when a button exists.
- Read the database in read-only mode
  (`sqlite3.connect("file:...?mode=ro", uri=True)`) before and after.
