"""
Apify Runner
================================
Orchestrates one or more Apify actors from start to finish (1.1: google +
configured verticals, see apify.actors.ACTORS), for each engine:

    1. Builds the input.
    2. Launches the run (start_actor).
    3. Polls until it finishes (wait_for_run).
    4. Downloads the dataset.
    5. NORMALIZES the results with the engine's normalizer (see
       apify.actors) into flat records, ready to classify and save.

Always returns records with this normalized shape:

    {
        "query":        str,   # searched term
        "country_code": str,   # search country (us, ca, gb, ...)
        "position":     int,   # organic position on Google
        "title":        str,
        "url":          str,
        "displayed_url":str,
        "description":  str,
        "domain":       str,   # domain extracted from the URL
    }

search() also returns "features": list[dict] (1.2) -- SERP features
(PAA/related/aio/knowledge_panel) extracted DEFENSIVELY by
_extract_features (see the method's docstring), an empty list if the
payload has nothing recognizable.

Multi-engine extraction (1.1): search() accepts `engines: list[str]`
(defaults to ["google"], IDENTICAL to the behavior before this item). For
each valid and configured engine (see apify.actors.ACTORS) it launches a
run of ITS actor with the same two-phase poll/retry as before, normalizes
with the engine's normalizer and tags each row with "engine". `position`
is PER ENGINE (intra-engine: each normalizer numbers its own results,
there is no "global" position crossing engines). SERP features (1.2) are
ONLY extracted from the google run -- the verticals do not carry that
information. An engine with no configured actor or whose run fails after
exhausting retries does NOT bring down the others (best-effort per
engine, see outcome["warnings"]/outcome["engine_runs"]); ApifyError is
only raised if NO requested engine produced results.
"""

import time
from collections.abc import Callable
from typing import Any

from config.apify_config import DEFAULT_INPUT

from .actors import ACTORS, ENGINE_NOT_CONFIGURED_MSG, VALID_DEVICES
from .client import ApifyClient, ApifyError

# Retries with backoff (1.4) in TWO separate phases -- different from the
# low-level retry of ApifyClient._request (retries ONE HTTP request):
#   LAUNCH phase: if start_actor fails transiently, relaunching is safe
#     (no run is billing yet).
#   WAIT phase: if wait_for_run fails (wait timeout or 5xx during
#     polling), the run is STILL ALIVE on Apify -- the WAIT is retried on
#     the SAME run_id, the actor is NEVER relaunched (relaunching = real
#     double spend; review 1.4 HIGH finding).
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


class ApifyRunner:
    def __init__(self, client: ApifyClient | None = None, db=None):
        self.client = client or ApifyClient()
        # FIX 1 item 1e (hardening audit): `db` optional -- only passed by
        # dashboard.main.get_runner (the only caller that may want the
        # budget pre-flight of _check_budget). The other constructors in
        # the repo (report.weekly, most tests) still
        # instantiate `ApifyRunner()` without `db` -- no budget to check,
        # no change in behavior.
        self.db = db

    # ------------------------------------------------------------------ #
    # Full flow in a single method
    # ------------------------------------------------------------------ #
    def search(
        self,
        queries: list[str],
        country_code: str = "us",
        language_code: str = "en",
        max_pages: int = 1,
        results_per_page: int = 10,
        max_wait: int = 300,
        on_status: Callable | None = None,
        engines: list[str] | None = None,
        device: str = "desktop",
        override_budget: bool = False,
        on_engine: Callable | None = None,
    ) -> dict[str, Any]:
        """
        Executes a complete search (1.1: on one or several engines) and
        returns a dict:

            {
                "run_id":   str,       # run_id of the "primary" engine (see below)
                "status":   str,       # status of the primary engine
                "country_code": str,
                "queries":  List[str],
                "results":  List[dict],  # from ALL successful engines, each
                                          # row with "engine" (1.1) and "device" (1.3)
                "features": List[dict],  # SERP features (1.2) -- ONLY from the
                                          # google run (verticals don't carry them)
                "cost_usd": float | None,       # sum of the successful engines (1.4)
                "compute_units": float | None,  # sum of the successful engines (1.4)
                "retries":  int,        # sum of retries of the successful engines
                "engine_runs": List[dict],  # 1 entry per requested engine, see below
                "warnings": List[str],  # skipped/failed engines, readable text
            }

        `device` (1.3, mobile/desktop split, defaults to "desktop" --
        IDENTICAL to the behavior before this item): a SINGLE device per
        call, same approach as `country_code` -- to cover "both devices"
        (acceptance 1.3) the caller launches two searches, one per device
        (same pattern as "one run per country" in
        report.multi_country.run_multi_country for several countries).
        Whitelisted against apify.actors.VALID_DEVICES -- a value outside
        that list falls back to "desktop" instead of being propagated
        unvalidated to the actor input. ONLY the google engine receives
        the device key in its input (`mobileResults`, conjectural -- see
        _engine_input); bing has no known device key, it is omitted
        (documented, brief 1.3) instead of guessing one with no basis --
        ITS rows/engine_run are tagged "device": "desktop" (the REAL
        device it rendered with, review H5) regardless of what was
        requested, with a notice in "warnings".

        `engines` (1.1, defaults to ["google"] -- IDENTICAL to the
        behavior before this item): an engine with no configured actor
        (see apify.actors.ACTORS) or whose run fails after exhausting
        retries is SKIPPED (best-effort per engine: a down engine does
        not bring down the others), recording the reason in "warnings"
        and in its "engine_runs" entry (with "error" not None). ApifyError
        is only raised if NO requested engine produced results.

        Each successful "engine_runs" entry has the SAME shape as the
        legacy (pre-1.1) single-engine outcome -- run_id/status/
        country_code/queries/results/features/cost_usd/compute_units/
        retries -- plus "engine" and "error"=None, so the caller can treat each entry
        as "the outcome of a run" and run the same saving pipeline as
        before, once per engine (see
        report.multi_country.persist_engine_run: "one DB run per
        engine"). The TOP-LEVEL `run_id`/`status` are those of the first
        successful engine in the order of `engines` (google if requested
        and successful, unless requested in a different order).

        `position` (1.1) is PER ENGINE: each normalizer numbers its own
        results: there is no "global" position crossing engines.

        Retries with backoff (1.4): per engine, same as before 1.1 --
        only transient failures are retried (network timeout, HTTP
        5xx/429, ConnectionError) up to MAX_SEARCH_ATTEMPTS times, with
        backoff RETRY_BASE_SECONDS·2^n; 401/403/404, invalid input or a
        run FAILED by the actor propagate immediately (without retrying)
        and that engine is left as failed (best-effort, see above).

        Apify budget (FIX 1 item 1e, hardening audit): if this runner was
        built with `db` (see __init__/dashboard.main.get_runner) and
        there is a configured monthly budget (setting
        apify_monthly_budget > 0) ALREADY exceeded by the month's
        accumulated spend, ApifyError is raised HERE, BEFORE launching
        any actor -- Database._check_apify_budget (the previous
        mechanism) was only evaluated AFTER saving the run, when the
        spend had ALREADY happened. `override_budget=True` skips the
        cutoff (operator button for an exceptional case: e.g. an urgent
        search the same day the budget was exceeded). Without `db`
        (legacy callers: report.weekly, most tests), there
        is no pre-flight -- behavior IDENTICAL to before this fix.

        `on_engine` (optional, progress UI): per-engine progress callback
        `on_engine(engine, state, **info)` with state "launching", "running"
        (info: apify_status, apify_elapsed_s -- the same polls `on_status`
        receives, but tagged with the engine), "skipped" or "error" (info:
        message). `on_status(status, elapsed)` keeps its legacy 2-arg shape
        (scripts/maintenance/smoke_apify.py). Callback failures never break the search.
        """
        self._check_budget(override_budget)

        def _emit(engine: str, state: str, **info) -> None:
            if on_engine is None:
                return
            try:
                on_engine(engine, state, **info)
            except Exception as exc:  # noqa: BLE001 -- progress is best-effort
                print(f"[Apify] on_engine callback failed: {exc}")
        # Dedupe preserving order -- engines=None or [] is equivalent to
        # the behavior before 1.1 (a single engine: google).
        requested = list(dict.fromkeys(engines or ["google"]))
        # 1.3: defensive whitelist -- a value outside VALID_DEVICES
        # (hand-crafted query param, manipulated form) falls back to
        # "desktop" instead of being propagated unvalidated to the actor
        # input.
        device = device if device in VALID_DEVICES else "desktop"

        base_input = self._build_input(
            queries, country_code, language_code, max_pages, results_per_page
        )

        engine_runs: list[dict[str, Any]] = []
        warnings: list[str] = []
        total_retries = 0
        # Review LOW #5: keeps the LAST real ApifyError caught (not the
        # ones for "unknown engine"/"not configured yet", which have no
        # associated exception) -- with a single requested engine, if it
        # fails, THIS exception is relaunched as-is (original message,
        # not wrapped twice); with several engines, it is chained as the
        # __cause__ of the combined ApifyError (`raise ... from
        # last_exc`) so as not to lose the original traceback in
        # logs/debugging.
        last_exc: ApifyError | None = None

        for engine in requested:
            entry = ACTORS.get(engine)
            if entry is None:
                msg = f"{engine}: unknown engine"
                warnings.append(msg)
                _emit(engine, "skipped", message=msg)
                engine_runs.append(
                    self._failed_engine_run(engine, country_code, queries, msg, device)
                )
                continue

            actor_id, normalize = entry.get("id"), entry.get("normalize")
            if not actor_id or not normalize:
                # PONYTAIL (#9): uses the shared constant from actors.py
                # -- single source of truth for the exact text (it used
                # to be a literal copy here AND in
                # actors.ENGINE_NOT_CONFIGURED_MSG, which stayed dead/
                # with no real consumer).
                msg = f"{engine}: {ENGINE_NOT_CONFIGURED_MSG}"
                warnings.append(msg)
                _emit(engine, "skipped", message=msg)
                engine_runs.append(
                    self._failed_engine_run(engine, country_code, queries, msg, device)
                )
                continue

            # 1.3 (H5, review): ONLY google supports the requested device
            # -- the rest of the engines (bing today) have no known
            # device key (see _engine_input) and ALWAYS render desktop,
            # regardless of `device`. Tagging their rows/run as "mobile"
            # when it was actually captured on desktop would be FALSE
            # evidence (it would look like a device it is not) -- it is
            # tagged with the REAL one, and a notice is added to warnings
            # (visible in /job) that the requested device was ignored for
            # this engine.
            effective_device = device if engine == "google" else "desktop"
            if engine != "google" and device == "mobile":
                warnings.append(
                    f"{engine}: device ignored (no device support, desktop was used)"
                )

            input_data = self._engine_input(engine, base_input, device)
            status_cb = on_status
            if on_engine is not None:
                def status_cb(status, elapsed, _engine=engine):
                    if on_status:
                        on_status(status, elapsed)
                    _emit(_engine, "running", apify_status=status, apify_elapsed_s=elapsed)
            _emit(engine, "launching")
            try:
                run_id, status, raw_items, cost_usd, compute_units, retries = self._run_actor(
                    engine, actor_id, input_data, max_wait, status_cb
                )
            except ApifyError as exc:
                last_exc = exc
                msg = f"{engine}: {exc}"
                warnings.append(msg)
                _emit(engine, "error", message=msg)
                engine_runs.append(
                    self._failed_engine_run(engine, country_code, queries, msg, effective_device)
                )
                continue

            total_retries += retries
            results = normalize(raw_items, country_code)
            for row in results:
                row["engine"] = engine
                row["device"] = effective_device
            features = self._extract_features(raw_items) if engine == "google" else []
            print(f"[Apify] {engine}: {len(results)} results extracted "
                  f"from {len(raw_items)} page(s).")
            engine_runs.append({
                "engine": engine,
                "device": effective_device,
                "run_id": run_id,
                "status": status,
                "country_code": country_code,
                "queries": queries,
                "results": results,
                "features": features,
                "cost_usd": cost_usd,
                "compute_units": compute_units,
                "retries": retries,
                "error": None,
            })

        successful = [er for er in engine_runs if er.get("error") is None]
        if not successful:
            # LOW #5: a single requested engine -- relaunches the
            # ORIGINAL ApifyError as-is (same message as before 1.1),
            # instead of wrapping it in the combined "All engines..."
            # message.
            if len(requested) == 1 and last_exc is not None:
                raise last_exc
            combined = "; ".join(warnings) or "no engine produced results"
            raise ApifyError(
                f"All requested engines failed ({', '.join(requested)}): {combined}"
            ) from last_exc

        primary = successful[0]
        google_run = next((er for er in successful if er["engine"] == "google"), None)
        costs = [er["cost_usd"] for er in successful if er.get("cost_usd") is not None]
        cus = [er["compute_units"] for er in successful if er.get("compute_units") is not None]

        return {
            "run_id": primary["run_id"],
            "status": primary["status"],
            "country_code": country_code,
            "queries": queries,
            "results": [row for er in successful for row in er["results"]],
            "features": google_run["features"] if google_run else [],
            "cost_usd": sum(costs) if costs else None,
            "compute_units": sum(cus) if cus else None,
            "retries": total_retries,
            "engine_runs": engine_runs,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _check_budget(self, override_budget: bool) -> None:
        """FIX 1 item 1e (hardening audit): budget pre-flight -- see
        search()'s docstring. Without `self.db` (legacy caller) or with
        `override_budget=True`, no-op. Any failure reading settings/DB is
        swallowed (best-effort, same spirit as the rest of the repo): an
        unreadable budget must not block a real search."""
        if self.db is None or override_budget:
            return
        try:
            budget = float(self.db.get_setting("apify_monthly_budget", "0") or "0")
        except (ValueError, TypeError):
            return
        if budget <= 0:
            return
        try:
            spent = self.db.apify_month_cost()["cost_usd"]
        except Exception:  # noqa: BLE001 -- best-effort, see docstring
            return
        if spent >= budget:
            raise ApifyError(
                f"Apify monthly budget exceeded: spent ${spent:.2f} >= budget "
                f"${budget:.2f}. Search blocked (pass override_budget=True to force)."
            )

    def _failed_engine_run(
        self, engine: str, country_code: str, queries: list[str], error_msg: str,
        device: str = "desktop",
    ) -> dict[str, Any]:
        """engine_runs entry (1.1) for an engine that was skipped (no
        configured actor, or unknown) or whose run failed after
        exhausting retries -- a shape recognizable by the caller
        (error not None) without breaking the rest of the pipeline
        (best-effort per engine). device (1.3): same value as the call to
        search(), for shape consistency with successful entries."""
        return {
            "engine": engine,
            "device": device,
            "run_id": None,
            "status": "ERROR",
            "country_code": country_code,
            "queries": queries,
            "results": [],
            "features": [],
            "cost_usd": None,
            "compute_units": None,
            "retries": 0,
            "error": error_msg,
        }

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
