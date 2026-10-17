"""SerpRunner: one search, several engines, on top of the active provider.

The callers (dashboard search_runner, report.weekly, report.multi_country,
the assistant) keep calling `runner.search(queries, country_code, ...)`
and reading the same outcome dict the Apify-only runner returned:

    {
        "run_id":   str,       # run_id of the "primary" engine (first successful)
        "status":   str,
        "country_code": str,
        "queries":  list[str],
        "results":  list[dict],  # rows from ALL successful engines, each tagged
                                 # with "engine" and "device" (serp.base.SerpItem)
        "features": list[dict],  # legacy SERP-feature rows of the google run
        "cost_usd": float | None,       # sum of the successful engines
        "compute_units": float | None,  # sum (Apify only; None elsewhere)
        "retries":  int,
        "engine_runs": list[dict],  # one per requested engine, see below
        "warnings": list[str],      # skipped/failed engines, readable text
    }

Each "engine_runs" entry is "the outcome of one run" the saving pipeline
persists as ONE DB run (report.multi_country.persist_engine_run):
engine/device/run_id/status/country_code/queries/results/features/cost_usd/
compute_units/retries/error, plus `provider` (which adapter ran it, saved
in runs.provider) and `serp_features` (ProviderRun.features, for F1).

Per engine, best-effort: an engine the provider cannot search, or whose
run fails after the provider's own retries, is skipped with the reason in
"warnings" and its entry's "error"; SerpProviderError is raised only if
NO requested engine produced results (the original error as-is when a
single engine was requested, so its message is not wrapped twice).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from serp import registry
from serp.base import (
    DEVICES,
    ENGINE_NOT_CONFIGURED_MSG,
    ENGINES,
    SerpProvider,
    SerpProviderError,
)

NO_PROVIDER_MSG = ("No SERP provider configured. Add an Apify or Serper.dev key in "
                   "Settings → Search.")


class SerpRunner:
    def __init__(self, provider: SerpProvider | None = None, db=None):
        # `provider` injected (tests, the ApifyRunner alias) or resolved from
        # the configuration at construction (dashboard.main.get_runner builds
        # one per key/provider change). `db` (optional) enables the budget
        # pre-flight; without it there is no budget to check.
        self.provider = provider if provider is not None else registry.get_provider()
        self.db = db

    @property
    def provider_name(self) -> str:
        return self.provider.name if self.provider is not None else ""

    def check_credentials(self) -> tuple[bool, str]:
        if self.provider is None:
            return False, NO_PROVIDER_MSG
        return self.provider.check_credentials()

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
        """Runs `queries` (one provider search per query and engine) in
        ONE country on ONE device; see the module docstring for the result.

        `device` is clamped to DEVICES, then to what the provider renders
        for each engine: an engine without the requested device runs on
        desktop, its rows are tagged with the REAL device, and a notice
        goes to "warnings" (same rule as before for bing/mobile).

        Budget pre-flight: with `db` and a monthly budget (setting
        apify_monthly_budget > 0) already exceeded by this month's spend
        across every provider, SerpProviderError is raised BEFORE anything
        is launched; `override_budget=True` skips the cutoff.

        `on_engine(engine, state, **info)` reports progress: "launching",
        "running" (info: apify_status/apify_elapsed_s, only from providers
        that poll), "skipped" or "error" (info: message). `on_status(status,
        elapsed)` keeps its legacy 2-arg shape. Callback failures never
        break the search."""
        if self.provider is None:
            raise SerpProviderError(NO_PROVIDER_MSG)
        self._check_budget(override_budget)
        provider = self.provider

        def _emit(engine: str, state: str, **info) -> None:
            if on_engine is None:
                return
            try:
                on_engine(engine, state, **info)
            except Exception as exc:  # noqa: BLE001 -- progress is best-effort
                print(f"[SERP] on_engine callback failed: {exc}")

        requested = list(dict.fromkeys(engines or ["google"]))
        device = device if device in DEVICES else "desktop"
        engine_runs: list[dict[str, Any]] = []
        warnings: list[str] = []
        total_retries = 0
        last_exc: SerpProviderError | None = None

        for engine in requested:
            if engine not in ENGINES:
                msg = f"{engine}: unknown engine"
            elif engine not in provider.engines:
                msg = f"{engine}: {ENGINE_NOT_CONFIGURED_MSG} ({provider.name})"
            else:
                msg = ""
            if msg:
                warnings.append(msg)
                _emit(engine, "skipped", message=msg)
                engine_runs.append(self._failed_engine_run(engine, country_code, queries, msg, device))
                continue

            effective_device = device if device in provider.engines[engine] else "desktop"
            if effective_device != device:
                warnings.append(f"{engine}: device ignored (no device support, desktop was used)")

            status_cb = on_status
            if on_engine is not None:
                def status_cb(status, elapsed, _engine=engine):
                    if on_status:
                        on_status(status, elapsed)
                    _emit(_engine, "running", apify_status=status, apify_elapsed_s=elapsed)
            _emit(engine, "launching")
            try:
                runs = [provider.search(q, country_code, engine, effective_device,
                                        language=language_code, pages=max_pages,
                                        per_page=results_per_page, max_wait=max_wait,
                                        on_status=status_cb) for q in queries]
            except SerpProviderError as exc:
                last_exc = exc
                msg = f"{engine}: {exc}"
                warnings.append(msg)
                _emit(engine, "error", message=msg)
                engine_runs.append(
                    self._failed_engine_run(engine, country_code, queries, msg, effective_device))
                continue

            results = [row for run in runs for row in run.items]
            for row in results:
                row["engine"] = engine
                row["device"] = effective_device
            costs = [run.cost_usd for run in runs if run.cost_usd is not None]
            cus = [run.compute_units for run in runs if run.compute_units is not None]
            retries = sum(run.retries for run in runs)
            total_retries += retries
            engine_runs.append({
                "engine": engine,
                "device": effective_device,
                "provider": provider.name,
                "run_id": runs[0].run_id,
                "status": runs[0].status,
                "country_code": country_code,
                "queries": queries,
                "results": results,
                "features": [row for run in runs for row in run.feature_rows],
                "serp_features": runs[0].features,
                "cost_usd": sum(costs) if costs else None,
                "compute_units": sum(cus) if cus else None,
                "retries": retries,
                "error": None,
            })

        successful = [er for er in engine_runs if er.get("error") is None]
        if not successful:
            if len(requested) == 1 and last_exc is not None:
                raise last_exc
            combined = "; ".join(warnings) or "no engine produced results"
            raise SerpProviderError(
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
    def _check_budget(self, override_budget: bool) -> None:
        """Budget pre-flight -- see search(). Without `self.db` or with
        `override_budget=True`, no-op. Any failure reading settings/DB is
        swallowed (best-effort): an unreadable budget must not block a
        real search."""
        if self.db is None or override_budget:
            return
        try:
            budget = float(self.db.get_setting("apify_monthly_budget", "0") or "0")
        except (ValueError, TypeError):
            return
        if budget <= 0:
            return
        try:
            spent = self.db.serp_month_cost()["cost_usd"]
        except Exception:  # noqa: BLE001 -- best-effort, see docstring
            return
        if spent >= budget:
            raise SerpProviderError(
                f"SERP monthly budget exceeded: spent ${spent:.2f} >= budget "
                f"${budget:.2f}. Search blocked (pass override_budget=True to force)."
            )

    @staticmethod
    def _failed_engine_run(engine: str, country_code: str, queries: list[str], error_msg: str,
                           device: str = "desktop") -> dict[str, Any]:
        """engine_runs entry for a skipped or failed engine: the same shape
        as a successful one, with "error" set, so the pipeline can report
        it without special cases."""
        return {
            "engine": engine, "device": device, "provider": "", "run_id": None,
            "status": "ERROR", "country_code": country_code, "queries": queries,
            "results": [], "features": [], "serp_features": {}, "cost_usd": None,
            "compute_units": None, "retries": 0, "error": error_msg,
        }
