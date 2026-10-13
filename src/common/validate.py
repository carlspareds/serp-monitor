"""Central input validation (SECURITY.md, item 14).

Three layers, from the outside in:

1. ``dashboard.security.BodySizeLimitMiddleware``: no request body over
   MAX_BODY_BYTES (1 MiB by default; the app has no uploads).
2. Declarative limits on every form field: each ``Form(...)`` string
   parameter carries a ``max_length`` (FIELD_LIMITS below is the reference
   for the sizes used), enums are ``Literal``/whitelists, and FastAPI
   answers anything else with a generic 422 page (no echo of the input).
3. The helpers in this module for values that need more than a length:
   URLs (http/https only), domains, ISO dates, bounded integers, and
   choices from an allowlist. Country and engine codes are checked against
   ``dashboard.ui_helpers.COUNTRIES`` / ``apify.actors.ACTORS`` by the routes
   that take them.

Every helper is pure, never raises on bad input and returns either the
cleaned value or the caller's default / None.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from urllib.parse import urlsplit

# Reference sizes for form fields (characters).
FIELD_LIMITS = {
    "short": 64,         # ids, enums, flags, codes
    "name": 200,         # names, queries, usernames, topics
    "url": 2048,         # URLs and next= paths
    "text": 10_000,      # notes, descriptions, context
    "secret": 1024,      # passwords, API keys, tokens
}

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")


def clip(value, limit: int) -> str:
    """str(value) stripped and cut to `limit` characters ("" for None)."""
    return ("" if value is None else str(value)).strip()[:limit]


def http_url(value, limit: int = FIELD_LIMITS["url"]) -> str | None:
    """The URL if it is an absolute http(s) URL with a host, within `limit`
    and without control characters; else None."""
    v = ("" if value is None else str(value)).strip()
    if not v or len(v) > limit or any(ord(c) < 32 or ord(c) == 127 for c in v):
        return None
    try:
        parts = urlsplit(v)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    return v


def domain(value) -> str | None:
    """A bare hostname (lowercase, no scheme/path/port), or None."""
    v = ("" if value is None else str(value)).strip().lower()
    if "://" in v:
        v = urlsplit(v).hostname or ""
    v = v.split("/")[0].removeprefix("www.").rstrip(".")
    return v if _DOMAIN_RE.match(v) else None


def iso_date(value) -> str | None:
    """'YYYY-MM-DD' if `value` is a real calendar date, else None."""
    v = ("" if value is None else str(value)).strip()[:10]
    try:
        return date.fromisoformat(v).isoformat()
    except ValueError:
        return None


def int_in(value, lo: int, hi: int, default: int | None = None) -> int | None:
    """int(value) if lo <= it <= hi, else `default`."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return n if lo <= n <= hi else default


def choice(value, allowed: Iterable[str], default: str | None = None) -> str | None:
    """`value` if it is one of `allowed`, else `default`."""
    v = ("" if value is None else str(value)).strip()
    return v if v in set(allowed) else default
