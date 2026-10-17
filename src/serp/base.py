"""The SERP provider contract.

A provider turns ONE query on ONE engine/device/country into a
`ProviderRun`: the normalized organic results plus what the search cost.
The runner (`serp.runner.SerpRunner`) loops engines, tags rows, checks the
budget and builds the legacy outcome dict the rest of the app consumes.

SerpItem -- the normalized result row. It stays a plain dict (the whole
pipeline, the DB and the exports already consume this shape); the keys are:

    query          str          the searched term
    country_code   str          lower-case search country (us, gb, global ...)
    position       int | None   organic position on THIS engine (1-based)
    title          str
    url            str          normalized (apify.actors._normalize_url: no
                                fragment/trailing slash, tracking params dropped)
    displayed_url  str          what the engine showed, '' if unknown
    description    str          snippet
    domain         str          host without www. (apify.actors._extract_domain)

The runner adds `engine` and `device` before the rows are saved.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

SerpItem = dict[str, Any]

# Engine vocabulary of the whole app (results filters, API, UI chips) and
# the devices it distinguishes. Which of these an install can actually
# search is `registry.configured_engines()` / `registry.valid_devices()`,
# derived from the ACTIVE provider's `engines`.
ENGINES: tuple[str, ...] = ("google", "bing", "news", "images", "youtube")
DEVICES: tuple[str, ...] = ("desktop", "mobile")

# Readable text for an engine the active provider cannot search; the runner
# appends " (<provider>)" so the warning says which provider lacks it.
ENGINE_NOT_CONFIGURED_MSG = "engine not configured yet"


class SerpProviderError(Exception):
    """A search failed at the provider. `retryable` says whether a later
    attempt may succeed (timeout, HTTP 5xx/429, connection failure) or not
    (bad key, invalid input, a run the provider itself marked failed)."""

    def __init__(self, message: str = "", retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class ProviderRun:
    """What one provider search returns.

    `features` is best-effort SERP-feature presence for F1 (persisted later):
    `ai_overview`, `local_pack`, `people_also_ask`, `video`, `sitelinks`,
    `knowledge_panel` (+ `answer_box`) -> bool or count, present ONLY when the
    provider exposes that block. `feature_rows` is the legacy per-row list
    (paa/related/aio/knowledge_panel, see ApifyProvider._extract_features)
    the runner still returns as outcome["features"]."""

    items: list[SerpItem]
    cost_usd: float | None
    run_id: str
    provider: str
    engine: str
    device: str
    country: str
    features: dict[str, Any] = field(default_factory=dict)
    raw: Any = None
    feature_rows: list[dict[str, Any]] = field(default_factory=list)
    compute_units: float | None = None
    retries: int = 0
    status: str = "SUCCEEDED"


@runtime_checkable
class SerpProvider(Protocol):
    name: str
    # engine -> devices it can render ({"google": {"desktop", "mobile"}, ...}).
    engines: dict[str, set[str]]

    def search(self, query: str, country: str, engine: str, device: str, *,
               language: str = "en", pages: int = 1, per_page: int = 10,
               max_wait: int = 300, on_status: Any = None) -> ProviderRun:
        """One query on one engine. Raises SerpProviderError (with
        `retryable`) when the provider cannot answer; never returns a
        partial run."""
        ...

    def check_credentials(self) -> tuple[bool, str]:
        """(ok, plain reason) for the Settings/wizard Test button. Never
        raises; never returns the provider's raw response body."""
        ...

    def configured(self) -> bool:
        """The provider has what it needs (its key) to run searches."""
        ...


def check_reason(detail: str, label: str) -> str:
    """Plain reason for a failed key check of provider `label`, from the
    error text of its client -- never the provider's raw response body."""
    m = re.search(r"HTTP (\d{3})", detail or "")
    if not m:
        return f"{label} could not be reached to verify this key"
    code = int(m.group(1))
    if code == 429 or code >= 500:
        return f"{label} returned an error while verifying this key (HTTP {code})"
    return f"{label} rejected this key (HTTP {code})"
