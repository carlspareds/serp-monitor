# 0004. Search data from hosted SERP providers behind a normalizer, no own scraping

- Status: Accepted; provider abstraction implemented in the PR for issue #3 (part 1: `src/serp/`, Apify and Serper.dev adapters). DataForSEO is part 2.
- Date: 2026-10-16 (written retrospectively at v1.0.0)

## Context

The app needs the organic results Google and Bing return for a name, per
country and device, on demand and once a week. Scraping search engines
directly means proxies, consent pages and anti-bot handling, and an eval
that "made paid API calls on every run would be a bad default for anyone
cloning it" (`evals/README.md`).

## Decision

Searches run "through Apify's hosted scraper actors" (`README.md`, What
it does); "nothing here scrapes Google or Bing directly" (Honest
limitations). `src/apify/actors.py` is a registry
`ACTORS: engine -> {"id": <actor>, "normalize": <function>}`; each
normalizer turns the actor's raw payload into one common schema
(position, url, title, description, domain, query, country_code) that
`ApifyRunner.search` saves. Engines without a confirmed actor (news,
images, youtube) are registered as `None` and skipped with a readable
error: "payloads are NOT invented". The README records the decision and
its cost, not a separate reason for not scraping in-house. The code does
record how fragile even the hosted path is: a screenshot actor was retired
after two live runs produced nothing (`src/apify/actors.py`).

## Consequences

### Positive

- No proxy pool, captcha handling or Google HTML parsing in this repo.
  The SERP-parsing eval checks the normalizers against recorded payloads
  (100% on 4 payloads) and needs no key.
- Adding an engine is one registry entry plus a normalizer.

### Negative

- "Needs an Apify account for anything real. ... Without a key you only
  see the synthetic demo" (`README.md`, Honest limitations). Each search
  sends the query, country and engine to Apify (`DATA.md`).
- "Rank data is only as good as Apify's actors staying in sync with the
  engines' page structure" (`README.md`, Honest limitations).
- Scraping success rate and live search latency are not measured
  (`evals/README.md`); every search costs money (the `apify_budget` alert).

## Provider abstraction (issue #3, part 1)

`src/serp/` now holds the contract: `SerpProvider.search(query, country,
engine, device, ...) -> ProviderRun` (`serp/base.py`), the registry that
picks the active provider from `SERP_PROVIDER` / the saved keys
(`serp/registry.py`), `SerpRunner` (the former `ApifyRunner`, now
provider-agnostic: engine loop, budget pre-flight, legacy outcome dict) and
two adapters, `ApifyProvider` (the actor launch/wait/normalize code moved
as-is; `apify.actors` and `apify.client` are untouched) and
`SerperProvider` (Serper.dev, Google desktop only). The monthly budget
counts every provider (`runs.provider`, `Database.serp_month_cost`); the
alert keeps the `apify_budget` kind so stored rows and the routing table
keep working, and its text says "SERP". `ProviderRun.features` carries the
best-effort SERP-feature presence F1 will persist. The SERP-parsing eval
runs over Apify google/bing and a synthetic Serper.dev payload. Still
planned: `DataForSEOProvider` (part 2). The registry-plus-normalizer shape
stayed; only the Apify-only wiring was replaced.

## Alternatives considered

- Scraping Google and Bing from the server: rejected, see Context.
