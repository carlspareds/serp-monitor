# Architecture decision records

An ADR is a short note that records one design decision: the situation
that called for it, what was decided, what that costs and buys, and what
else was on the table. ADRs are written once and not edited afterwards;
when a decision changes, a new ADR supersedes the old one and the old
one's status is updated to point at it.

These first six were written retrospectively on 2026-10-16, at v1.0.0,
from the decisions already visible in the code and in `README.md`,
`SECURITY.md`, `DATA.md` and `evals/README.md`. They document what is;
they do not reconstruct discussions that were never written down. Where
the repository gives a reason, the ADR cites it; where it doesn't, the ADR
says so.

| ADR | Title | Status |
| --- | --- | --- |
| [0001](0001-sqlite-single-file-no-orm.md) | SQLite in a single file, hand-written `Database` class, no ORM | Accepted |
| [0002](0002-rule-classifier-confidence-human-review.md) | Rule-based classification with confidence and a human review queue | Accepted |
| [0003](0003-single-process-embedded-scheduler.md) | Single FastAPI process with embedded APScheduler and in-process watchdog | Accepted |
| [0004](0004-hosted-serp-providers-behind-normalizer.md) | Search data from hosted SERP providers behind a normalizer, no own scraping | Accepted; provider abstraction implemented (#3, part 1) |
| [0005](0005-bring-your-own-key-ai-providers.md) | Bring-your-own-key AI providers, optional, never the search-time classifier | Accepted |
| [0006](0006-scope-no-keyword-backlink-index.md) | Scope: no keyword-volume, backlink or content-optimization index | Accepted |

## Template (MADR-lite)

Each file is `NNNN-short-title.md` and has these sections, in this order:

```
# NNNN. Title

- Status: Proposed | Accepted | Accepted, under revision | Superseded by NNNN | Deprecated
- Date: YYYY-MM-DD

## Context
## Decision
## Consequences
### Positive
### Negative
## Alternatives considered
```

Keep it between 25 and 60 lines. Name files and functions so a reader can
check the claim in the code. State negative consequences plainly; an ADR
that lists only benefits is not useful.

## Adding one

1. Copy the template above into `docs/adr/NNNN-short-title.md`, using the
   next free number.
2. Start with `Status: Proposed` in the pull request; set it to `Accepted`
   when the PR that implements the decision merges.
3. If it replaces an earlier ADR, change that one's status to
   `Superseded by NNNN` (the only edit an existing ADR gets).
4. Add a row to the table above and a line to `CHANGELOG.md` under
   *Unreleased*.
