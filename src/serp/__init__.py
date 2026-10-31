"""Pluggable SERP providers (issue #3, part 1).

`serp.base` is the contract (SerpProvider, ProviderRun, SerpProviderError),
`serp.registry` picks the active provider from the configuration,
`serp.runner.SerpRunner` runs one search across engines on top of it, and
`serp.apify_provider` / `serp.serper_provider` are the two adapters.
DataForSEO (part 2) is one more module plus one `registry.PROVIDERS` entry.
"""
