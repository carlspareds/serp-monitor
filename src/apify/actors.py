"""
Multi-engine actor registry (1.1)
================================
A logical run can execute, in addition to Google Search, verticals such
as Bing, Google News, Google Images or YouTube -- each with its own Apify
actor and its own normalizer into the common schema that
ApifyRunner.search saves (position, url, title, description, domain,
query, country_code).

ACTORS maps engine -> {"id": <Apify actor>, "normalize": <function>} for
the CONFIGURED engines (google, bing today -- arbitrated MVP, see brief
1.1). Engines that still have no known actor (news, images, youtube) are
left as registry entries with "id"/"normalize" set to None -- payloads
are NOT invented: ApifyRunner.search skips them with a readable error
("engine not configured yet") instead of trying to launch them.

# ponytail: news/images/youtube are left without an actor -- their real
# payloads are unknown and are not invented (brief 1.1, MVP scope
# arbitration). Trigger: once the corresponding Apify actor for each
# vertical is investigated/confirmed, add its entry here (id + normalize)
# following the same defensive pattern as normalize_bing -- without
# touching ApifyRunner.search, which already knows how to handle any
# newly configured engine.

normalize_google(raw_items, country_code) extracts the ORIGINAL logic of
ApifyRunner._flatten_results (1.0) unchanged -- it trusts the confirmed
payload of the apify~google-search-scraper actor.

normalize_bing(raw_items, country_code) -- CONFIRMED SCHEMA (# design,
pruned after a real run of tri_angle~bing-search-scraper): each `page`
has the SAME top-level shape as normalize_google ({searchQuery,
organicResults, ...}), so extraction is DIRECT by key -- no longer
defensive multi-key (the conjectural `_BING_*` tuples were pruned, with
no real sample to back them). The one real underlying difference from
normalize_google: `organicResults[i].url` is the bing.com/ck/a
REDIRECTOR, NOT the real URL -- see _bing_real_url.
"""

import html as _html
import re
from typing import Any
from urllib.parse import urlparse

from config.apify_config import BING_SEARCH_SCRAPER, GOOGLE_SEARCH_SCRAPER
from database.base import strip_tracking
from serp.base import DEVICES, ENGINE_NOT_CONFIGURED_MSG  # noqa: F401 -- re-exported

# Supported devices (1.3, mobile/desktop split): the app-wide vocabulary
# now lives in serp.base.DEVICES; this name stays for the existing
# imports (Database.get_results_filtered, results/api_v1 filters). Which
# devices an install can SEARCH is serp.registry.valid_devices().
VALID_DEVICES = DEVICES

# RETIRED (F4 wrap-up, fix 1.3→HTML snapshot): "apify~screenshot-url" is
# NO LONGER USED -- two real live runs never
# produced a PNG (aborted at $0.028 and $0.064 with waitUntil
# 'networkidle2'/'load' respectively; the run's key-value store only had
# INPUT and SDK state, never an OUTPUT) -- probable cause: Google
# consent/anti-bot facing the actor's datacenter proxy. (Its HTML-based
# replacement, built on the google payload's `htmlSnapshotUrl`, was later
# removed as a product feature too.)

def _extract_domain(url: str) -> str:
    """Returns the domain (without www.) of a URL. MOVED here from
    apify.runner (1.1, review LOW #8) -- normalize_google needs it just
    like before, and runner.py no longer uses it (_flatten_results became
    normalize_google), so it lives only in this module, not duplicated.
    actors.py does NOT import runner.py to avoid the
    runner->actors->runner cycle (runner needs ACTORS/normalize_* from
    this module)."""
    try:
        netloc = urlparse(url).netloc.lower()
        return netloc.removeprefix("www.")
    except Exception:  # noqa: BLE001 — defensive helper, safe default
        return ""


def _normalize_url(url: str) -> str:
    """Normalizes a URL (lowercase scheme/host, no fragment or trailing
    slash). MOVED here from apify.runner (1.1, review LOW #8, same
    reason as _extract_domain above) -- NOT duplicated, runner.py no
    longer has it."""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        p = urlparse(url)
        scheme = (p.scheme or "https").lower()
        netloc = p.netloc.lower()
        path = p.path.rstrip("/") or "/"
        norm = f"{scheme}://{netloc}{path}"
        if p.query:
            norm += f"?{p.query}"
        # Tracking parameters (srsltid, utm_*...) aren't part of the page:
        # dropped before storing (database.base.strip_tracking).
        return strip_tracking(norm)
    except Exception:  # noqa: BLE001 — defensive helper, safe default
        return url


def _text(v: Any) -> str:
    """Coerces a payload value to a safe str -- '' if it is not a str.
    Unlike _extract_domain/_normalize_url above, this one IS deliberately
    DUPLICATED from apify.runner._text (1.2) -- runner.py still needs its
    own copy for _extract_features (google SERP features, unrelated to
    this module's engine registry). Same duplication approach already
    used in database.models._SERIES_NULL_SEVERITY."""
    return v.strip() if isinstance(v, str) else ""


def normalize_google(raw_items: list[dict], country_code: str) -> list[dict[str, Any]]:
    """Normalizer for apify~google-search-scraper -- logic IDENTICAL to
    what ApifyRunner._flatten_results had before 1.1, just relocated here
    to live alongside the rest of the engine registry. Each `item` of the
    dataset is a page with `searchQuery` and `organicResults`."""
    flattened: list[dict[str, Any]] = []
    for page in raw_items:
        search_query = page.get("searchQuery", {}) or {}
        term = search_query.get("term", "")
        # ALWAYS use the requested country (each run is for one country).
        # Apify fills in countryCode='us' when global is requested, which
        # is why we don't use it.
        page_country = (country_code or "").lower()

        for org in page.get("organicResults", []) or []:
            url = _normalize_url(org.get("url", "") or "")
            flattened.append(
                {
                    "query": term,
                    "country_code": page_country,
                    "position": org.get("position"),
                    "title": org.get("title", "") or "",
                    "url": url,
                    "displayed_url": org.get("displayedUrl", "") or "",
                    "description": org.get("description", "") or "",
                    "domain": _extract_domain(url),
                }
            )
    return flattened


# Separator Bing uses when showing displayedUrl as a breadcrumb
# ("https://x.example › a › b") -- seen live on a real run.
_BING_BREADCRUMB_RE = re.compile(r"\s*›\s*")


def _bing_real_url(item: dict) -> str:
    """REAL URL of a Bing organicResult (# design, confirmed on a real
    run): `item['url']` is the
    bing.com/ck/a REDIRECTOR (confirmed live -- the 10 rows of that run
    ended up with domain='bing.com', corrupting classification/
    lifecycle/dedupe/wins for the entire engine). `item['displayedUrl']`
    carries the real URL as a breadcrumb ("https://x.example › a › b") -- the
    '›' separator (with its spaces) is cleaned up to '/' to reconstruct
    it ("https://x.example/a/b"). If `displayedUrl` is missing, is not a
    string, or the result does not parse as http(s) (urlparse.scheme/
    netloc), it falls back to the redirector (`url`) and flags it with a
    print -- NEVER silently, so the number of rows left with the "fake"
    redirector URL can be audited."""
    displayed = item.get("displayedUrl")
    if isinstance(displayed, str) and displayed.strip():
        candidate = _BING_BREADCRUMB_RE.sub("/", displayed.strip())
        parsed = urlparse(candidate)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return candidate

    raw_url = item.get("url")
    raw_url = raw_url if isinstance(raw_url, str) else ""
    print(f"[apify.actors] normalize_bing: no usable displayedUrl, falling back to the "
          f"redirector for title={item.get('title')!r}")
    return raw_url


def normalize_bing(raw_items: list[dict], country_code: str) -> list[dict[str, Any]]:
    """Normalizer for tri_angle~bing-search-scraper -- CONFIRMED schema
    (see the module docstring). Any unexpected
    shape (non-dict page, non-list `organicResults`, non-dict item or one
    with no usable URL) is ignored instead of raising -- captures what is
    present, discards what is absent, NEVER raises.

    `position` arrives as a STRING in the real payload ("1") -- it is
    defensively converted to int (same try/except pattern as Database.
    get_results_filtered); if missing or not convertible, it falls back
    to the 1-based index within ITS page (same approach as before this
    fix, unlike normalize_google which preserves position=None if
    missing)."""
    results: list[dict[str, Any]] = []
    if not isinstance(raw_items, list):
        return results

    page_country = (country_code or "").lower()
    for page in raw_items:
        if not isinstance(page, dict):
            continue
        search_query = page.get("searchQuery")
        term = _text(search_query.get("term")) if isinstance(search_query, dict) else ""

        organic = page.get("organicResults")
        if not isinstance(organic, list):
            continue

        for idx, item in enumerate(organic, start=1):
            if not isinstance(item, dict):
                continue
            url = _bing_real_url(item)
            if not url:
                continue
            url = _normalize_url(url)

            try:
                pos = int(item.get("position"))
            except (TypeError, ValueError):
                pos = idx

            results.append(
                {
                    "query": term,
                    "country_code": page_country,
                    "position": pos,
                    "title": _html.unescape(_text(item.get("title"))),
                    "url": url,
                    "displayed_url": _text(item.get("displayedUrl")),
                    "description": _html.unescape(_text(item.get("description"))),
                    "domain": _extract_domain(url),
                }
            )
    return results


# Engine registry: only google/bing have a configured actor; news/images/
# youtube are placeholders (id/normalize None) and render as disabled
# "coming soon" options on the search page.
ACTORS: dict[str, dict[str, Any]] = {
    "google": {"id": GOOGLE_SEARCH_SCRAPER, "normalize": normalize_google},
    "bing": {"id": BING_SEARCH_SCRAPER, "normalize": normalize_bing},
    "news": {"id": None, "normalize": None},
    "images": {"id": None, "normalize": None},
    "youtube": {"id": None, "normalize": None},
}

# Engines with a REAL Apify actor today (google/bing) -- different from
# ACTORS.keys() (which also lists news/images/youtube, with no actor).
# This is APIFY's set only: what the install can launch depends on the
# active provider -- serp.registry.configured_engines() is what the search
# form, the launch endpoints and the assistant consult.
CONFIGURED_ENGINES = frozenset(engine for engine, entry in ACTORS.items() if entry.get("id"))
