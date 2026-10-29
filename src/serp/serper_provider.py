"""Serper.dev adapter: POST https://google.serper.dev/search.

Google only, desktop only (the API has no device flag), one request per
page, results back in 1-2 s -- so no polling: `on_status` is never called.

Pricing (serper.dev, checked 2026-10-16 through third-party summaries; the
site itself only lists "2,500 free queries" in its static text): prepaid
credit packs, $50 / 50k credits ($1.00 per 1k), $375 / 500k ($0.75),
$1,250 / 2.5M ($0.50), $3,750 / 12.5M ($0.30), credits valid 6 months. One
search of up to 10 results costs 1 credit; asking for 11-100 results costs
2 credits (reported by several guides, not stated on the homepage). Every
response carries `credits` (what THIS request cost), which is what the
cost below uses; `credits_for` is only the fallback. USD = credits ×
SERPER_USD_PER_1K / 1000 -- an ESTIMATE (default 1.00, the Starter rate;
set it to your pack's rate), since Serper never reports dollars.
"""
from __future__ import annotations

import math
import os
import time
import uuid
from typing import Any, ClassVar

import requests

from apify.actors import _extract_domain, _normalize_url
from config.apify_config import lang_for
from serp.base import ENGINE_NOT_CONFIGURED_MSG, ProviderRun, SerpProviderError
from serp.base import check_reason as base_check_reason

SEARCH_URL = "https://google.serper.dev/search"
DEFAULT_USD_PER_1K = 1.0
MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 1
_MIN_NUM, _MAX_NUM = 10, 100


def credits_for(num: int) -> int:
    """Credits one request costs when the response does not say: 1 for up
    to 10 results, 2 above (see the module docstring)."""
    return 1 if num <= 10 else 2


def usd_per_1k_setting(env=None) -> float:
    """SERPER_USD_PER_1K as a float, the default when unset or invalid."""
    env = os.environ if env is None else env
    raw = (env.get("SERPER_USD_PER_1K") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_USD_PER_1K
    except ValueError:
        return DEFAULT_USD_PER_1K
    return value if math.isfinite(value) and value >= 0 else DEFAULT_USD_PER_1K


def _text(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _items(v: Any) -> list:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _is_retryable(exc: Exception) -> bool:
    """Same classes as apify.runner._is_retryable: 429/5xx, connection
    failures and timeouts retry; any other HTTP 4xx does not."""
    if isinstance(exc, SerpProviderError):
        return exc.retryable
    return isinstance(exc, requests.RequestException)


def normalize_serper(payload: dict, query: str, country: str, offset: int = 0) -> list[dict]:
    """organic[] -> the normalized row schema (serp.base). Rows without a
    usable link are dropped; `position` is Serper's when present, else the
    1-based index, both shifted by `offset` (results of earlier pages)."""
    rows: list[dict] = []
    country = (country or "").lower()
    for idx, item in enumerate(_items(payload.get("organic")), start=1):
        link = _text(item.get("link"))
        if not link:
            continue
        url = _normalize_url(link)
        pos = item.get("position")
        pos = pos if isinstance(pos, int) and pos > 0 else idx
        rows.append({
            "query": query,
            "country_code": country,
            "position": pos + offset,
            "title": _text(item.get("title")),
            "url": url,
            "displayed_url": _text(item.get("displayedLink")) or url.split("://", 1)[-1].split("/", 1)[0],
            "description": _text(item.get("snippet")),
            "domain": _extract_domain(url),
        })
    return rows


def extract_features(payload: dict, query: str) -> tuple[dict[str, Any], list[dict]]:
    """(summary for F1, legacy feature rows) from the blocks Serper
    returns next to `organic`. Only blocks that are present count; the
    summary always answers for `ai_overview`/`knowledge_panel` (False
    when absent) because Serper's /search would carry them."""
    paa = [i for i in _items(payload.get("peopleAlsoAsk")) if _text(i.get("question"))]
    related = [i for i in _items(payload.get("relatedSearches")) if _text(i.get("query"))]
    kg = payload.get("knowledgeGraph")
    kg = kg if isinstance(kg, dict) and kg else None
    aio = payload.get("aiOverview")
    aio_text = _text(aio.get("text") or aio.get("content")) if isinstance(aio, dict) else _text(aio)
    sitelinks = sum(len(_items(i.get("sitelinks"))) for i in _items(payload.get("organic")))

    summary: dict[str, Any] = {
        "ai_overview": bool(aio_text),
        "answer_box": isinstance(payload.get("answerBox"), dict) and bool(payload.get("answerBox")),
        "knowledge_panel": kg is not None,
    }
    if paa or "peopleAlsoAsk" in payload:
        summary["people_also_ask"] = len(paa)
    if "places" in payload:
        summary["local_pack"] = len(_items(payload.get("places")))
    if "videos" in payload:
        summary["video"] = len(_items(payload.get("videos")))
    if sitelinks:
        summary["sitelinks"] = sitelinks

    rows: list[dict] = [{
        "feature_type": "paa", "seed_query": query,
        "content": {"question": _text(i.get("question")), "answer": _text(i.get("snippet")),
                    "url": _text(i.get("link"))},
    } for i in paa]
    rows += [{"feature_type": "related", "seed_query": query,
              "content": {"title": _text(i.get("query")), "url": ""}} for i in related]
    if aio_text:
        sources = aio.get("sources") if isinstance(aio, dict) else None
        rows.append({"feature_type": "aio", "seed_query": query,
                     "content": {"text": aio_text, "sources": sources if isinstance(sources, list) else []}})
    if kg is not None:
        rows.append({"feature_type": "knowledge_panel", "seed_query": query, "content": kg})
    return summary, rows


class SerperProvider:
    name = "serper"
    engines: ClassVar[dict[str, set[str]]] = {"google": {"desktop"}}

    def __init__(self, api_key: str | None = None, usd_per_1k: float | None = None,
                 timeout: int = 30, max_attempts: int = MAX_ATTEMPTS):
        self.api_key = (api_key if api_key is not None else os.getenv("SERPER_API_KEY") or "").strip()
        self.usd_per_1k = usd_per_1k_setting() if usd_per_1k is None else float(usd_per_1k)
        self.timeout = timeout
        self.max_attempts = max_attempts

    def configured(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------ #
    def _post(self, body: dict) -> tuple[dict, int]:
        """One search request with retries. Returns (payload, retries)."""
        if not self.api_key:
            raise SerpProviderError(
                "No Serper.dev key configured. Add it in Settings → Search (or the setup wizard).")
        headers = {"X-API-KEY": self.api_key, "Content-Type": "application/json"}
        retries = 0
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = requests.post(SEARCH_URL, headers=headers, json=body, timeout=self.timeout)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise SerpProviderError(
                        f"HTTP {resp.status_code} from Serper.dev: {resp.text[:300]}", retryable=True)
                if resp.status_code >= 400:
                    raise SerpProviderError(
                        f"HTTP {resp.status_code} from Serper.dev: {resp.text[:300]}", retryable=False)
                try:
                    payload = resp.json()
                except ValueError:
                    raise SerpProviderError(
                        f"Non-JSON response from Serper.dev: {resp.text[:300]}", retryable=True)
                if not isinstance(payload, dict):
                    raise SerpProviderError("Unexpected Serper.dev payload (not an object)")
                return payload, retries
            except (requests.RequestException, SerpProviderError) as exc:
                last_exc = exc
                if attempt >= self.max_attempts or not _is_retryable(exc):
                    if isinstance(exc, SerpProviderError):
                        raise
                    raise SerpProviderError(f"Serper.dev request failed: {exc}", retryable=True) from exc
                backoff = RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                retries += 1
                print(f"[Serper] Attempt {attempt} failed ({exc}). Retrying in {backoff}s...")
                time.sleep(backoff)
        raise SerpProviderError(f"Serper.dev request failed: {last_exc}", retryable=True)  # pragma: no cover

    def search(self, query: str, country: str, engine: str, device: str, *,
               language: str = "", pages: int = 1, per_page: int = 10,
               max_wait: int = 300, on_status: Any = None) -> ProviderRun:
        if engine not in self.engines:
            raise SerpProviderError(f"{engine}: {ENGINE_NOT_CONFIGURED_MSG} ({self.name})")
        country = (country or "").lower()
        num = max(_MIN_NUM, min(_MAX_NUM, int(per_page or _MIN_NUM)))
        base: dict[str, Any] = {"q": query, "hl": language or lang_for(country), "num": num}
        if country and country != "global":
            base["gl"] = country

        items: list[dict] = []
        credits = 0
        retries = 0
        raw_pages: list[dict] = []
        for page in range(1, max(1, int(pages or 1)) + 1):
            payload, r = self._post({**base, "page": page})
            retries += r
            raw_pages.append(payload)
            page_credits = payload.get("credits")
            credits += page_credits if isinstance(page_credits, int | float) and page_credits > 0 \
                else credits_for(num)
            items += normalize_serper(payload, query, country, offset=(page - 1) * num)
        features, feature_rows = extract_features(raw_pages[0], query)
        print(f"[Serper] google: {len(items)} results extracted from {len(raw_pages)} page(s), "
              f"{credits} credit(s).")
        return ProviderRun(
            items=items,
            cost_usd=round(credits * self.usd_per_1k / 1000, 6),
            run_id=f"serper-{uuid.uuid4().hex[:16]}",
            provider=self.name, engine=engine, device="desktop", country=country,
            features=features, raw=raw_pages[0] if len(raw_pages) == 1 else raw_pages,
            feature_rows=feature_rows, compute_units=None, retries=retries,
        )

    def check_credentials(self) -> tuple[bool, str]:
        """Serper has no free account endpoint: the check is one tiny
        search (1 credit, about a tenth of a cent)."""
        if not self.api_key:
            return False, "no Serper.dev key saved"
        try:
            payload, _retries = self._post({"q": "serp monitor key check", "num": 10})
        except SerpProviderError as exc:
            return False, check_reason(str(exc))
        credits = payload.get("credits") if isinstance(payload.get("credits"), int) else 1
        return True, f"key verified ({credits} credit used)"


def check_reason(detail: str) -> str:
    return base_check_reason(detail, "Serper.dev")
