"""Apify adapter: the hosted scraper actors behind `serp.base.SerpProvider`.

One engine per `search()` call (the runner loops engines):

    1. Builds the actor input (_build_input/_engine_input).
    2. Launches the run (start_actor).
    3. Polls until it finishes (wait_for_run).
    4. Downloads the dataset.
    5. NORMALIZES the results with the engine's normalizer (see
       apify.actors.ACTORS) into the row schema of serp.base.

The actor registry, the normalizers and the Apify REST client stay in
`apify.actors` / `apify.client` untouched; this module is the former
`apify.runner.ApifyRunner` minus the multi-engine loop and the budget
pre-flight, which now live in `serp.runner.SerpRunner` for every provider.

Retries with backoff (1.4) in TWO separate phases -- different from the
low-level retry of ApifyClient._request (retries ONE HTTP request):
  LAUNCH phase: if start_actor fails transiently, relaunching is safe
    (no run is billing yet).
  WAIT phase: if wait_for_run fails (wait timeout or 5xx during
    polling), the run is STILL ALIVE on Apify -- the WAIT is retried on
    the SAME run_id, the actor is NEVER relaunched (relaunching = real
    double spend; review 1.4 HIGH finding).

SERP features (1.2) are extracted from the google payload only (the
verticals do not carry them): `ProviderRun.feature_rows` keeps the legacy
per-row list (_extract_features), `ProviderRun.features` the F1 summary.
"""

import os
import time
from collections.abc import Callable
from typing import Any, ClassVar

from apify.actors import ACTORS
from apify.client import ApifyClient, ApifyError
from config.apify_config import DEFAULT_INPUT
from serp.base import (
    ENGINE_NOT_CONFIGURED_MSG,
    ProviderRun,
    SerpProviderError,
    check_reason,
)

MAX_SEARCH_ATTEMPTS = 3
RETRY_BASE_SECONDS = 30

# Markers (substring, case-insensitive) of transient errors in the
# ApifyError text. Not retryable by default; also, any HTTP 4xx (except
# 429) is cut short BEFORE looking at markers -- 4xx messages embed the
# response body, which may mention "timeout" without actually being one.
_RETRYABLE_MARKERS = ("http 5", "http 429", "connection", "timeout", "timed out",
                      "did not finish in")


def _is_retryable(exc: Exception) -> bool:
    """Classifies an ApifyError as retryable (timeout, HTTP 5xx/429,
    connection failure) or not (client 4xx, invalid input, FAILED run)."""
    msg = str(exc).lower()
    if "http 4" in msg and "http 429" not in msg:
        return False
    return any(marker in msg for marker in _RETRYABLE_MARKERS)


def _text(v: Any) -> str:
    """Coerces a payload value to a safe str -- '' if it is not a str.

    H1 (review 1.2): avoids .strip() on a truthy non-string scalar (e.g.
    {"question": 42}) -- `42 or ""` is truthy (42), and .strip() on an int
    raises AttributeError. Used in ALL text extractions of
    _extract_features."""
    return v.strip() if isinstance(v, str) else ""


def _iter(v: Any) -> list:
    """Coerces a payload value to a safe list -- [] if it is not a list.

    H1 (review 1.2): avoids iterating a truthy scalar (e.g.
    {"peopleAlsoAsk": 42}) -- `42 or []` is truthy (42), and
    `for x in 42` raises TypeError. Used in ALL lists of
    _extract_features."""
    return v if isinstance(v, list) else []


# Known page keys (1.2): organicResults is consumed by
# apify.actors.normalize_google; the rest, by _extract_features. Used
# ONLY for logging (H4, state honesty) top-level keys that the real actor
# may carry and that this parser does not yet recognize -- it does not
# validate the schema, does not affect extraction. `htmlSnapshotUrl` (a
# signed URL to an HTML copy of the SERP) is present on every google run;
# it is listed here so it isn't logged as an "unknown page key", but it is
# deliberately NOT consumed (the SERP HTML snapshot feature was removed).
_KNOWN_PAGE_KEYS = {
    "searchQuery", "organicResults", "peopleAlsoAsk", "relatedQueries",
    "aiOverview", "aiOverviewText", "aio", "knowledgeGraph", "knowledgePanel",
    "htmlSnapshotUrl",
}


class ApifyProvider:
    name = "apify"
    # Only google takes a device key (`mobileResults`, see _engine_input);
    # bing has no known device input and always renders desktop (1.3, H5).
    engines: ClassVar[dict[str, set[str]]] = {"google": {"desktop", "mobile"}, "bing": {"desktop"}}

    def __init__(self, client: ApifyClient | None = None):
        # `client` injected by tests (a fake); otherwise built lazily so an
        # unconfigured install never raises just by being inspected.
        self._client = client

    @property
    def client(self) -> ApifyClient:
        if self._client is None:
            self._client = ApifyClient()
        return self._client

    def configured(self) -> bool:
        return self._client is not None or bool((os.getenv("APIFY_API_KEY") or "").strip())

    def check_credentials(self) -> tuple[bool, str]:
        try:
            res = self.client.check_account()
        except Exception as exc:  # noqa: BLE001 -- reported, never raised
            res = {"ok": False, "detail": str(exc)}
        if res.get("ok"):
            return True, f"key verified (user: {res.get('username')})"
        return False, check_reason(res.get("detail") or "", "Apify")

    def search(self, query: str, country: str, engine: str, device: str, *,
               language: str = "en", pages: int = 1, per_page: int = 10,
               max_wait: int = 300, on_status: Callable | None = None) -> ProviderRun:
        entry = ACTORS.get(engine) or {}
        actor_id, normalize = entry.get("id"), entry.get("normalize")
        if engine not in self.engines or not actor_id or not normalize:
            raise SerpProviderError(f"{engine}: {ENGINE_NOT_CONFIGURED_MSG} ({self.name})")
        device = device if device in self.engines[engine] else "desktop"
        base_input = self._build_input([query], country, language, pages, per_page)
        input_data = self._engine_input(engine, base_input, device)
        try:
            run_id, status, raw_items, cost_usd, compute_units, retries = self._run_actor(
                engine, actor_id, input_data, max_wait, on_status
            )
        except ApifyError as exc:
            exc.retryable = _is_retryable(exc)
            raise
        results = normalize(raw_items, country)
        feature_rows = self._extract_features(raw_items) if engine == "google" else []
        print(f"[Apify] {engine}: {len(results)} results extracted "
              f"from {len(raw_items)} page(s).")
        return ProviderRun(
            items=results, cost_usd=cost_usd, run_id=run_id, provider=self.name,
            engine=engine, device=device, country=country,
            features=_feature_summary(feature_rows) if engine == "google" else {},
            raw=raw_items, feature_rows=feature_rows, compute_units=compute_units,
            retries=retries, status=status,
        )

    def _run_actor(
        self,
        engine: str,
        actor_id: str,
        input_data: dict[str, Any],
        max_wait: int,
        on_status: Callable | None,
        max_attempts: int = MAX_SEARCH_ATTEMPTS,
    ) -> tuple[str, str, list[dict], float | None, float | None, int]:
        """Launches+waits for ONE actor with the same two-phase retry
        that search() had before 1.1 (see the module docstring),
        parameterized by engine/actor. Returns (run_id, status,
        raw_items, cost_usd, compute_units, retries); raises ApifyError
        if it does not finish successfully after exhausting retries (the
        caller decides whether that brings down the whole outcome or
        just this engine, see search()).

        max_attempts (1.3, H6): defaults to MAX_SEARCH_ATTEMPTS (3, same
        as before this item); auxiliary actors can pass 1 (best-effort, no
        retry) to bound their time cost to ~max_wait per cycle."""
        retries = 0

        # --- LAUNCH phase: retryable (no run is billing yet) ------------
        for attempt in range(1, max_attempts + 1):
            try:
                print(f"[Apify] Launching {actor_id} ({engine}) | country={input_data.get('countryCode')} "
                      f"| queries={input_data.get('queries')}")
                run = self.client.start_actor(actor_id, input_data)
                run_id = run.get("id")
                if not run_id:
                    raise ApifyError(f"No run id obtained when launching the actor ({engine}): {run}")
                break
            except ApifyError as exc:
                if attempt >= max_attempts or not _is_retryable(exc):
                    raise
                backoff = RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                retries += 1
                print(f"[Apify] Launch {engine} (attempt {attempt}) failed ({exc}). "
                      f"Retrying in {backoff}s...")
                time.sleep(backoff)

        # --- WAIT phase: retries the WAIT on the SAME run_id.
        # start_actor is NEVER called again here: a wait timeout or a 5xx
        # from polling does not mean the run died -- relaunching it would
        # leave the original run alive and billing (double spend).
        print(f"[Apify] Run {run_id} ({engine}) launched. Waiting for completion...")
        for attempt in range(1, max_attempts + 1):
            try:
                final_run = self.client.wait_for_run(
                    run_id, max_wait=max_wait, on_status=on_status
                )
                status = final_run.get("status")
                if status != "SUCCEEDED":
                    raise ApifyError(
                        f"Run {run_id} ({engine}) finished with status '{status}'. "
                        f"Check the Apify console for more detail."
                    )
                break
            except ApifyError as exc:
                if attempt >= max_attempts or not _is_retryable(exc):
                    raise
                backoff = RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                retries += 1
                print(f"[Apify] Wait for run {run_id} ({engine}) (attempt {attempt}) failed "
                      f"({exc}). Retrying the wait in {backoff}s...")
                time.sleep(backoff)

        dataset_id = final_run.get("defaultDatasetId")
        if not dataset_id:
            raise ApifyError(f"Run {run_id} ({engine}) does not expose defaultDatasetId.")

        raw_items = self.client.get_dataset_items(dataset_id)
        cost_usd = final_run.get("usageTotalUsd")
        compute_units = (final_run.get("stats") or {}).get("computeUnits")
        return run_id, status, raw_items, cost_usd, compute_units, retries

    def _build_input(
        self,
        queries: list[str],
        country_code: str,
        language_code: str,
        max_pages: int,
        results_per_page: int,
    ) -> dict[str, Any]:
        """Builds the actor input from the defaults.

        1.1: reused for ALL engines with a configured actor (not just
        google) -- with no real confirmed sample of the input expected by
        tri_angle~bing-search-scraper, this is the best reasonable
        assumption (same keys as google), pending validation against a
        real run (same approach as apify.actors.normalize_bing)."""
        # "global" = non-localized search -> empty countryCode for Apify
        apify_country = "" if (country_code or "").lower() == "global" else country_code
        input_data = dict(DEFAULT_INPUT)
        input_data.update(
            {
                # The actor accepts queries as a string (one per line).
                "queries": "\n".join(queries),
                "countryCode": apify_country,
                "languageCode": language_code,
                "maxPagesPerQuery": max_pages,
                "resultsPerPage": results_per_page,
            }
        )
        return input_data

    @staticmethod
    def _engine_input(engine: str, base_input: dict[str, Any], device: str) -> dict[str, Any]:
        """Adds the engine-specific device key (1.3) to the base input
        (shared across engines, see _build_input). ONLY google:
        `mobileResults: True` when device=='mobile' -- conjectural, with
        no real confirmed sample of the apify~google-search-scraper
        payload for this field (# ponytail: prune after the first real
        run, same approach as apify.actors.normalize_bing). bing: no
        known device key -- it is omitted (documented, brief 1.3) instead
        of guessing a key with no basis at all; the rest of the engines
        (with no configured actor today) don't receive it either."""
        input_data = dict(base_input)
        if device == "mobile" and engine == "google":
            input_data["mobileResults"] = True
        return input_data

    def _extract_features(self, raw_items: list[dict]) -> list[dict[str, Any]]:
        """
        Extracts SERP features (1.2, DATA_MODEL §B.1) from each page of
        the dataset: PAA (one row per question), related queries (one
        row per suggestion), AI Overview and Knowledge Panel (one row per
        page, if they appear).

        DEFENSIVE by keys: as of this item there is NO real confirmed
        payload of the apify~google-search-scraper actor for
        aiOverview/knowledgeGraph -- the keys tried are reasonable
        GUESSES (names used by other SERP actors/scrapers), pending
        verification against a real run. Any unexpected shape (wrong
        type, item missing the expected key, page that is not a dict,
        scalar where text/list was expected -- see _text/_iter) is
        ignored instead of raising -- captures what is present, discards
        what is absent, NEVER raises. H4: unrecognized page keys are
        logged once per call (per run) to discover what the real actor
        carries -- they do not change behavior, only inform.

        `seed_query` comes as-is from searchQuery.term of EACH page (or
        "" if missing/malformed) -- any consumer that needs the run's
        authoritative query resolves it downstream (P4: no duplicated
        fallback layer here).
        """
        features: list[dict[str, Any]] = []
        if not isinstance(raw_items, list):
            return features

        unknown_keys: set[str] = set()
        for page in raw_items:
            if not isinstance(page, dict):
                continue
            unknown_keys.update(k for k in page if k not in _KNOWN_PAGE_KEYS)

            search_query = page.get("searchQuery")
            term = _text(search_query.get("term") if isinstance(search_query, dict) else "")

            for item in _iter(page.get("peopleAlsoAsk")):
                if not isinstance(item, dict):
                    continue
                question = _text(item.get("question"))
                if not question:
                    continue
                features.append({
                    "feature_type": "paa",
                    "seed_query": term,
                    "content": {
                        "question": question,
                        "answer": _text(item.get("answer")),
                        "url": _text(item.get("url")),
                    },
                })

            for item in _iter(page.get("relatedQueries")):
                if not isinstance(item, dict):
                    continue
                title = _text(item.get("title")) or _text(item.get("query"))
                if not title:
                    continue
                # ponytail: 'related' rows are persisted with NO reader
                # today (neither /serp-intel nor /run/{id} show them) --
                # they are declared input for F4 (A4 expansion of
                # sources / related-query analysis). Trigger: first real
                # consumer; if F4 does not use them, stop capturing them
                # (chore F2-close).
                features.append({
                    "feature_type": "related",
                    "seed_query": term,
                    "content": {"title": title, "url": _text(item.get("url"))},
                })

            # AI Overview: plausible keys (no real confirmed sample) --
            # see docstring. May come as a dict with text+sources, or as
            # a bare string.
            aio = page.get("aiOverview") or page.get("aiOverviewText") or page.get("aio")
            aio_text = ""
            aio_sources: list = []
            if isinstance(aio, dict):
                aio_text = _text(aio.get("text")) or _text(aio.get("content")) or _text(aio.get("snippet"))
                aio_sources = _iter(aio.get("sources")) or _iter(aio.get("references"))
            elif isinstance(aio, str):
                aio_text = aio.strip()
            if aio_text:
                features.append({
                    "feature_type": "aio",
                    "seed_query": term,
                    "content": {"text": aio_text, "sources": aio_sources},
                })

            # Knowledge Panel: same idea, no real sample -- captures if
            # it appears.
            # TODO(A2): keys (knowledgeGraph/knowledgePanel) unconfirmed
            # against a real actor payload -- same finding as aio above;
            # review/adjust once a real sample is available.
            kp = page.get("knowledgeGraph") or page.get("knowledgePanel")
            if isinstance(kp, dict) and kp:
                features.append({
                    "feature_type": "knowledge_panel",
                    "seed_query": term,
                    "content": kp,
                })

        if unknown_keys:
            print(f"[Apify] unknown page keys in the features payload (1.2): "
                  f"{sorted(unknown_keys)} -- check whether the actor carries a new SERP feature.")

        return features


def _feature_summary(rows: list[dict]) -> dict[str, Any]:
    """F1 summary (serp.base.ProviderRun.features) from the legacy rows:
    what the google actor payload exposes today -- PAA count, AI Overview
    and Knowledge Panel presence. Local pack / video / sitelinks are not
    in this actor's payload, so they are absent (not False)."""
    kinds = [r.get("feature_type") for r in rows]
    return {"people_also_ask": kinds.count("paa"), "ai_overview": "aio" in kinds,
            "knowledge_panel": "knowledge_panel" in kinds}
