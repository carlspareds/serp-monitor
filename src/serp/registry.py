"""Which SERP provider is active, and what it can search.

Selection (`SERP_PROVIDER`, written by Settings → Search or set in .env):
`apify` / `serper` use that provider and ONLY that one (an explicit choice
without its key is "not configured", never a silent fallback -- same rule
as LLM_PROVIDER); empty or unknown is auto: Apify if APIFY_API_KEY is set,
else Serper.dev if SERPER_API_KEY is set, else None (demo mode).

Adding a provider (DataForSEO, part 2): one adapter module and one entry in
PROVIDERS / KEY_OF / LABELS. Nothing else in the app names providers.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

from serp.base import DEVICES, SerpProvider


def _apify():
    from serp.apify_provider import ApifyProvider
    return ApifyProvider()


def _serper():
    from serp.serper_provider import SerperProvider
    return SerperProvider()


# name -> factory (lazy imports: apify.client loads .env on import).
PROVIDERS = {"apify": _apify, "serper": _serper}
# The env variable that makes each provider "configured".
KEY_OF = {"apify": "APIFY_API_KEY", "serper": "SERPER_API_KEY"}
LABELS = {"apify": "Apify", "serper": "Serper.dev"}
# Auto-detect order: the first provider whose key is set wins.
_AUTO_ORDER = ("apify", "serper")

# Engine/device metadata shown while NO provider is configured (demo mode):
# Apify's, the most complete set, so the search form keeps its shape.
_DEFAULT_ENGINES: dict[str, set[str]] = {"google": {"desktop", "mobile"}, "bing": {"desktop"}}


def _get(env: Mapping[str, str], key: str) -> str:
    return (env.get(key) or "").strip()


def requested_provider(env: Mapping[str, str] | None = None) -> str:
    """The SERP_PROVIDER value if it names a known provider, else ''."""
    env = os.environ if env is None else env
    value = _get(env, "SERP_PROVIDER").lower()
    return value if value in PROVIDERS else ""


def active_provider_name(env: Mapping[str, str] | None = None) -> str:
    """Name of the provider searches run through, '' in demo mode. Cheap:
    reads the environment only, no provider is built."""
    env = os.environ if env is None else env
    explicit = requested_provider(env)
    if explicit:
        return explicit if _get(env, KEY_OF[explicit]) else ""
    for name in _AUTO_ORDER:
        if _get(env, KEY_OF[name]):
            return name
    return ""


def get_provider(env: Mapping[str, str] | None = None) -> SerpProvider | None:
    """A fresh instance of the active provider, or None (demo mode)."""
    name = active_provider_name(env)
    return PROVIDERS[name]() if name else None


def provider_label(name: str) -> str:
    return LABELS.get(name, name or "")


def engines_of(name: str = "") -> dict[str, set[str]]:
    """engine -> devices of provider `name` (the active one by default),
    Apify's set when none is configured."""
    name = name or active_provider_name()
    if not name:
        return {k: set(v) for k, v in _DEFAULT_ENGINES.items()}
    return {k: set(v) for k, v in PROVIDERS[name]().engines.items()}


def configured_engines() -> frozenset[str]:
    """Engines the active provider can search -- what the search form,
    the assistant and the launch endpoints accept."""
    return frozenset(engines_of())


def valid_devices() -> tuple[str, ...]:
    """Devices at least one engine of the active provider can render, in
    DEVICES order."""
    supported = set().union(*engines_of().values()) if engines_of() else set()
    return tuple(d for d in DEVICES if d in supported) or ("desktop",)
