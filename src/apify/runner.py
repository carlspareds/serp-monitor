"""DEPRECATED location -- kept so `from apify.runner import ApifyRunner`
(report.weekly, scripts, older tests) keeps working.

The implementation moved with issue #3 (part 1): the multi-engine loop and
the budget pre-flight are `serp.runner.SerpRunner` (provider-agnostic), the
actor launch/wait/normalize code is `serp.apify_provider.ApifyProvider`.
New code should import those directly.
"""
import time  # noqa: F401 -- tests patch `apify.runner.time.sleep` (the global module)

from serp.apify_provider import (  # noqa: F401 -- re-exported for legacy imports
    MAX_SEARCH_ATTEMPTS,
    RETRY_BASE_SECONDS,
    ApifyProvider,
    _is_retryable,
)
from serp.runner import SerpRunner


class ApifyRunner(SerpRunner):
    """`ApifyRunner(client=fake)` builds a SerpRunner over an ApifyProvider
    with that client; `ApifyRunner(db=db)` resolves the ACTIVE provider
    exactly like SerpRunner (it may not be Apify any more)."""

    def __init__(self, client=None, db=None, provider=None):
        if client is not None and provider is None:
            provider = ApifyProvider(client=client)
        super().__init__(provider=provider, db=db)

    @property
    def client(self):
        """The Apify REST client of an Apify-backed runner (legacy)."""
        return getattr(self.provider, "client", None)

    # Legacy static helpers some tests call on the class.
    _engine_input = staticmethod(ApifyProvider._engine_input)
