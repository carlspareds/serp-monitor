# 0001. SQLite in a single file, hand-written `Database` class, no ORM

- Status: Accepted
- Date: 2026-10-16 (written retrospectively at v1.0.0)

## Context

SERP Monitor is "a self-hosted FastAPI app for one person or a small
team, not an enterprise SEO platform" (`README.md`, intro). `make run`
must install, seed and serve it with nothing else running on the machine.
Every stored row belongs to exactly one subject and must be filtered by it
on every query (`SECURITY.md`, item 4).

## Decision

Data lives in one SQLite file (`DB_PATH`, default `data/seo_dashboard.db`),
"accessed through a hand-written `Database` class (`src/database/`)
rather than an ORM" (`README.md`, Architecture). `Database`
(`src/database/models.py`) is composed from per-domain mixins; each
connection opens with `journal_mode=WAL`, `synchronous=NORMAL` and
`busy_timeout=5000` (`src/database/base.py::_connect`). The schema is a
set of column registries (`src/database/schema.py::_RESULTS_COLUMNS` and
friends) used for additive migrations at startup; a removed feature
leaves its column in place (`cluster_id`) "so existing databases need no
destructive migration". The README records the decision without a
separate justification; what the code relies on it for is listed below.

## Consequences

### Positive

- No database server, no connection string, no client-side key: "the
  browser never talks to SQLite", and `src/database/` is "the only SQL"
  (`SECURITY.md`, item 3).
- With all SQL hand-written in one package, two guards are cheap: ruff's
  S608 flags SQL built from strings, each hit carrying a `noqa` reason
  (`pyproject.toml`, `SECURITY.md` item 13), and `tests/test_scoping_guard.py`
  statically fails the build if a `Database` method touches a subject-owned
  table without the `subject_id = ?` clause (`SECURITY.md`, item 4).

### Negative

- "Fine for one person or a small team running their own instance; not
  built for heavy concurrent write load" (`README.md`, Honest
  limitations). The limit is not measured yet (issue #28).
- Row-level scoping "is enforced in application code, not by SQLite"
  (`SECURITY.md`, item 4); `PRAGMA foreign_keys` is not enabled (issue #7).
- More SQL per table than an ORM would need, and the embedded scheduler
  still pulls SQLAlchemy in for APScheduler's `SQLAlchemyJobStore`
  (ADR 0003).

## Alternatives considered

- PostgreSQL or another server database: better under concurrent writes,
  but it turns `make run` into a deployment and contradicts the
  single-file, self-hosted target.
- An ORM (SQLAlchemy, SQLModel) over SQLite: less SQL to write, but the
  static scoping guard and the S608 review would have to be redone against
  the ORM's query builder.
