"""
Apify Client Module
================================
Thin client over the Apify REST API v2.

Key points fixed relative to the previous version:
- The v2 API wraps objects in {"data": {...}}. Here it is ALWAYS
  unwrapped, so the caller receives the plain object (run, dataset, etc.).
- Retries with backoff for transient network / 5xx errors.
- Explicit timeouts on all requests.
- Clear errors (ApifyError) with the response body when it fails.
"""

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from serp.base import SerpProviderError

# ENV_FILE (when set) is the same file Settings and /setup write keys to,
# so it must also be the one loaded at startup; otherwise the repo .env.
load_dotenv(os.getenv("ENV_FILE") or Path(__file__).resolve().parents[2] / ".env")


class ApifyError(SerpProviderError):
    """Communication or business error with the Apify API. A
    SerpProviderError, so one `except` covers every provider; `retryable`
    is set by serp.apify_provider when it re-raises."""


class ApifyClient:
    # Terminal states of an Apify run (frozenset: immutable, avoids
    # RUF012 -- only used for membership testing, see wait_for_run).
    TERMINAL_STATES = frozenset({"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"})

    def __init__(
        self,
        api_key: str | None = None,
        timeout: int = 60,
        max_retries: int = 3,
    ):
        self.api_key = api_key or os.getenv("APIFY_API_KEY")
        if not self.api_key:
            raise ApifyError(
                "No Apify key configured. Add it in Settings → General (or the setup wizard)."
            )

        self.base_url = "https://api.apify.com/v2"
        self.timeout = timeout
        self.max_retries = max_retries
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------ #
    # HTTP helper with retries
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> requests.Response:
        """Performs an HTTP request with retries for transient failures."""
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.request(
                    method,
                    url,
                    headers=self.headers,
                    params=params,
                    json=json,
                    timeout=self.timeout,
                )
                # 429 / 5xx -> retryable
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise ApifyError(
                        f"HTTP {resp.status_code} from Apify: {resp.text[:300]}"
                    )
                if resp.status_code >= 400:
                    # Client error (4xx other than 429): do not retry
                    raise ApifyError(
                        f"HTTP {resp.status_code} from Apify: {resp.text[:500]}"
                    )
                return resp
            except (requests.RequestException, ApifyError) as exc:
                last_exc = exc
                # Do not retry non-transient 4xx errors (except 429)
                if isinstance(exc, ApifyError) and "HTTP 4" in str(exc) \
                        and "HTTP 429" not in str(exc):
                    raise
                if attempt < self.max_retries:
                    backoff = 2 ** (attempt - 1)
                    print(
                        f"[Apify] Attempt {attempt} failed ({exc}). "
                        f"Retrying in {backoff}s..."
                    )
                    time.sleep(backoff)
        raise ApifyError(
            f"Request to Apify failed after {self.max_retries} attempts: {last_exc}"
        )

    @staticmethod
    def _unwrap(resp: requests.Response) -> Any:
        """Unwraps the {"data": ...} envelope of the v2 API if present."""
        try:
            payload = resp.json()
        except ValueError:
            raise ApifyError(f"Non-JSON response from Apify: {resp.text[:300]}")
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    # ------------------------------------------------------------------ #
    # High-level operations
    # ------------------------------------------------------------------ #
    def start_actor(self, actor_id: str, input_data: dict[str, Any]) -> dict[str, Any]:
        """
        Launches an actor WITHOUT waiting for it to finish. Returns the run
        object (already unwrapped) with its `id`, `status`,
        `defaultDatasetId`, etc.
        """
        url = f"{self.base_url}/acts/{actor_id}/runs"
        resp = self._request("POST", url, json=input_data)
        return self._unwrap(resp)

    def get_run(self, run_id: str) -> dict[str, Any]:
        """Queries the state of a run (unwrapped object)."""
        url = f"{self.base_url}/actor-runs/{run_id}"
        resp = self._request("GET", url)
        return self._unwrap(resp)

    def wait_for_run(
        self,
        run_id: str,
        poll_interval: int = 5,
        max_wait: int = 300,
        on_status: Callable | None = None,
    ) -> dict[str, Any]:
        """
        Polls the run until it reaches a terminal state or max_wait runs out.
        Returns the final run object. Raises ApifyError if it does not
        finish in time.
        """
        elapsed = 0
        while elapsed <= max_wait:
            run = self.get_run(run_id)
            status = run.get("status")
            if on_status:
                on_status(status, elapsed)
            if status in self.TERMINAL_STATES:
                return run
            time.sleep(poll_interval)
            elapsed += poll_interval
        raise ApifyError(
            f"Run {run_id} did not finish in {max_wait}s (last known state)."
        )

    def check_account(self) -> dict[str, Any]:
        """
        Verifies the connection/credentials with Apify (GET /users/me).
        Returns {'ok': bool, 'detail': str, 'username': str|None}.
        """
        try:
            resp = self._request("GET", f"{self.base_url}/users/me")
            data = self._unwrap(resp)
            return {"ok": True, "detail": "Connection successful",
                    "username": data.get("username") or data.get("id")}
        except ApifyError as e:
            return {"ok": False, "detail": str(e), "username": None}

    def get_dataset_items(
        self, dataset_id: str, clean: bool = True
    ) -> list[dict]:
        """
        Fetches the items of a dataset. The endpoint returns a direct array
        (not wrapped in `data`).
        """
        url = f"{self.base_url}/datasets/{dataset_id}/items"
        params = {"format": "json", "clean": "true" if clean else "false"}
        resp = self._request("GET", url, params=params)
        try:
            return resp.json()
        except ValueError:
            raise ApifyError(f"Dataset {dataset_id} returned invalid JSON.")

    # Key-value store (get_key_value_record/list_key_value_store_keys):
    # RETIRED (F4 wrap-up, fix 1.3→HTML snapshot) -- its only consumer
    # was processing.screenshot._fetch_png_bytes, to read the OUTPUT of
    # the screenshot actor (apify.actors.SCREENSHOT_ACTOR, retired --
    # never produced a PNG in two real runs, see that module).
