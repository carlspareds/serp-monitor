# 0006. Scope: no keyword-volume, backlink or content-optimization index

- Status: Accepted
- Date: 2026-10-16 (written retrospectively at v1.0.0)

## Context

Commercial SEO suites (Semrush, Ahrefs, Moz and the like) are built on
large proprietary indices: keyword search volumes, backlink graphs,
content-scoring models. Those indices are paid products in their own
right, crawled and maintained continuously, and none of that data is
observable from the search results page the app already fetches.

## Decision

SERP Monitor is "a self-hosted FastAPI app for one person or a small
team, not an enterprise SEO platform" (`README.md`, intro). It covers
monitoring (what shows up for a name, per country and engine, over
time), reputation (labels, review, evidence, alerts), reporting (weekly
email, PDF, Excel, CSV, API), and, as planned work, visibility in AI
answers (issue #13, F3). It does not build or resell a keyword-volume,
backlink or content-optimization index, and it does not try to.

## Consequences

### Positive

- The only paid external dependency is the SERP provider (ADR 0004) and,
  optionally, the operator's own AI key (ADR 0005).
- The data model stays small: runs, results, alerts, notes, evidence,
  events, all per subject.

### Negative

- No keyword research, no backlink audit, no on-page scoring. "There is
  no ranking-drop alert" either (`README.md`, What it does); tracked
  keywords with position history are planned separately (issue #4, F1).
- Users who want those things need another tool next to this one.

## Alternatives considered

- Integrating a third-party SEO API (Semrush, Ahrefs, DataForSEO Labs):
  possible later as an import, like the planned Search Console (#5), GA4
  (#24) and Bing Webmaster (#25) imports, but it would add a paid
  subscription and is not the project's focus.
