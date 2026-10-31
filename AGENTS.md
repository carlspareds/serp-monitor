# AGENTS.md

Instructions for coding agents (and humans) working on serp-monitor. The
README explains what the app does; this file explains how to change it.

## Where things live

| Area | Where |
| --- | --- |
| Web app, routes, middleware | `src/dashboard/` (`main.py`, `routers/`) |
| Database (SQLite, no ORM) | `src/database/` (`schema.py`, `migrations.py`) |
| Classification, diff, evidence, alerts | `src/processing/` |
| Reports and exports | `src/export/`, `src/report/` |
| Search providers | `src/serp/` (contract, registry, runner, adapters); Apify actors in `src/apify/` |
| Front end | `static/app.js`, `static/style.css`, `templates/` |
| Tests, evals, decisions | `tests/`, `evals/`, `docs/adr/` |

Dashboard features used for verification, with their routes and tests:
[`docs/verification/`](docs/verification/README.md).

| Feature | Router | Tests |
| --- | --- | --- |
| Review | `src/dashboard/routers/review.py` | `tests/test_review_queue.py` |
| Compare | `src/dashboard/routers/diff.py` | `tests/test_diff.py` |
| Reports | `src/dashboard/routers/export.py` | `tests/test_export.py` |
| Evidence | `src/dashboard/routers/evidence.py` | `tests/test_evidence.py` |
| Notes | `src/dashboard/routers/notes.py` | `tests/test_note_workflow.py` |

## Commands

Python 3.12. The same checks as CI:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
.venv/bin/pip-audit --strict -r requirements.txt -r requirements-dev.txt
gitleaks git . --no-banner --redact && gitleaks dir . --no-banner --redact
```

`make lint`, `make test`, `make audit` and `make secrets` wrap them.
`git config core.hooksPath scripts/hooks` installs the pre-commit hook. To
run the app for a check, use the isolated demo instance in
[`docs/verification/README.md`](docs/verification/README.md), not `make run`.

## Rules

- **Small PRs.** One issue per PR, about 300 changed lines at most. Split a
  bigger issue into chained PRs that each end in a working state. Nothing
  outside the issue's scope.
- **Tests first.** Add a test that fails without the change.
- **Verify with evidence.** "The tests pass" is not enough when users can
  see the change (dashboard, API, reports). Walk through the feature in the
  demo instance and attach screenshots, the database state before and after,
  and the list of checks. If you couldn't verify something, say so in the PR.
  A failed walkthrough is evidence too: report it, don't loosen the check.
- **No keys.** Never commit, print or read `.env`, API keys or passwords.
  Never run the app with real keys while developing; the demo instance
  blanks them. `ALLOW_NO_AUTH=1` only on 127.0.0.1.
- **Synthetic data.** `.example` domains and fictional names in tests,
  fixtures and screenshots.
- **Honest numbers.** Don't change figures in the README or eval results
  without measuring them.
- **CHANGELOG.** One line under *Unreleased*. A change to a decision in
  `docs/adr/` needs a new ADR (template in `docs/adr/README.md`).
- **Dependencies.** Don't add one without the maintainer's approval. Browser
  automation for verification (Playwright) stays out of the requirements.
- **Git.** Work on a branch and open a PR; never push to `main`. The
  maintainer merges. No tags or releases, no changes to repository settings
  or secrets.
- English for code, commits, PRs and docs.
