# 0004. Search data from hosted SERP providers behind a normalizer, no own scraping

- Status: Accepted, under revision (issue #3)
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

## Under revision

Issue #3 (F4) plans a provider abstraction:
`SerpProvider.search(query, country, device, engine) -> list[SerpResult]`
in `src/serp/`, with `ApifyProvider` (today's code), `SerperProvider`
(Serper.dev) and `DataForSEOProvider` adapters; `apify_budget` becomes a
generic `serp_budget`; the SERP-parsing eval runs against all three
normalizers. The registry-plus-normalizer shape stays; the Apify-only
part is what it replaces.

## Alternatives considered

- Scraping Google and Bing from the server: rejected, see Context.
