"""
Background job manager
================================
Lets searches (which block on the Apify API) be launched in a separate
thread, so the dashboard responds instantly and the progress page polls
GET /job/{id}/status until the job finishes.

It's a simple in-memory manager (enough for a single uvicorn process).
Each job is a dict with: id, type, status, progress[] (free-text log),
steps[] (one per country x engine, ordered), result, error, started_at,
finished_at, seen_by (viewer keys that have seen the finished job, see
mark_seen and GET /jobs/active). Jobs are lost on restart -- the job page
says so on 404.

`meta` carries who/what: query, countries, engines, subject_id,
subject_name and the owner (started_by = username or "local", session =
a short hash of the session cookie; dashboard.job_status.owner_meta).
"""

import copy
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

# Post-F4 hardening (FIX 1 item 1a): a low-memory host can't
# handle N concurrent Apify searches (each is a separate daemon thread,
# see run_in_thread) -- before this fix there was no cap at all, a handful
# of overlapping POST /search_multi could launch jobs without limit. 2
# jobs running at once is enough margin for small hosts without
# blocking normal use (a single search + a multi-country one). Callers
# (dashboard.main::run_search/search_multi) check
# `running_count() >= _MAX_CONCURRENT_JOBS` BEFORE jobs.create() and
# return a readable error instead of launching the thread.
_MAX_CONCURRENT_JOBS = 2

# Retention of FINISHED jobs (running jobs are never pruned): at most the
# last _KEEP_FINISHED, and none older than _KEEP_FINISHED_SECONDS.
_KEEP_FINISHED = 50
_KEEP_FINISHED_SECONDS = 24 * 3600

# Step states, in lifecycle order. Terminal ones never go back to active.
STEP_STATES = ("pending", "launching", "running", "saving", "done", "skipped", "error")
TERMINAL_STEP_STATES = frozenset({"done", "skipped", "error"})

# `stage` refines a 'saving' step: what the post-search pipeline is doing now
# (report.multi_country.persist_engine_run on_stage: same_person, save,
# severity, evidence) -- dashboard.job_status turns it into a sentence.
_STEP_FIELDS = ("state", "apify_status", "apify_elapsed_s", "summary", "message", "stage")


def _utc_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def step_key(country: str, engine: str) -> str:
    return f"{country}:{engine}"


def _new_step(country: str, engine: str) -> dict[str, Any]:
    return {"key": step_key(country, engine), "country": country, "engine": engine,
            "state": "pending", "apify_status": None, "apify_elapsed_s": None,
            "summary": None, "message": None, "stage": None}


class JobManager:
    def __init__(self):
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def running_count(self) -> int:
        """How many jobs are still in 'running' state -- see _MAX_CONCURRENT_JOBS."""
        with self._lock:
            return sum(1 for j in self._jobs.values() if j["status"] == "running")

    def create(self, job_type: str, meta: dict | None = None,
               steps: list[tuple[str, str]] | None = None) -> str:
        """Registers a running job. `steps` pre-seeds the ordered step list
        as (country, engine) pairs, all 'pending'. Prunes old finished jobs."""
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        seeded = [_new_step(country, engine) for country, engine in steps or []]
        with self._lock:
            self._prune_locked(now)
            self._jobs[job_id] = {
                "id": job_id,
                "type": job_type,
                "status": "running",          # running | done | error
                "progress": [],               # list of messages (log)
                "steps": seeded,
                "result": None,               # payload when finished
                "error": None,
                "meta": meta or {},
                "started_at": now,            # epoch seconds
                "finished_at": None,
                "created_at": _utc_iso(now),  # legacy display string (naive UTC)
                "seen_by": set(),             # viewer keys (mark_seen)
            }
        return job_id

    def add_progress(self, job_id: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job["progress"].append(message)

    def set_step(self, job_id: str, key: str, **fields) -> None:
        """Updates step `key` (see step_key) of a job. Unknown fields are
        ignored; a terminal step (done/skipped/error) is not moved back to an
        active state by a late callback. Unknown keys append a new step."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            step = next((s for s in job["steps"] if s["key"] == key), None)
            if step is None:
                country, _, engine = key.partition(":")
                step = _new_step(country, engine)
                job["steps"].append(step)
            new_state = fields.get("state")
            if (step["state"] in TERMINAL_STEP_STATES
                    and new_state not in TERMINAL_STEP_STATES):
                return
            for name in _STEP_FIELDS:
                if name in fields:
                    step[name] = copy.deepcopy(fields[name])

    def fail_open_steps(self, job_id: str, message: str, country: str | None = None) -> None:
        """Marks every non-terminal step (optionally of one country) as error."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for step in job["steps"]:
                if step["state"] in TERMINAL_STEP_STATES:
                    continue
                if country is not None and step["country"] != country:
                    continue
                step["state"] = "error"
                step["message"] = message

    def finish(self, job_id: str, result: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job["status"] = "done"
                job["result"] = result
                job["finished_at"] = time.time()

    def fail(self, job_id: str, error: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job["status"] = "error"
                job["error"] = error
                job["finished_at"] = time.time()

    def mark_seen(self, job_id: str, viewer_key: str) -> bool:
        """Records that `viewer_key` has seen this job (the top-bar chip and
        the "finished" toast stop showing it to them). False if unknown."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            job["seen_by"].add(viewer_key)
            return True

    def snapshots(self) -> list[dict[str, Any]]:
        """snapshot() of every job, oldest first (GET /jobs/active)."""
        with self._lock:
            ids = sorted(self._jobs, key=lambda i: self._jobs[i]["started_at"])
        return [s for s in (self.snapshot(i) for i in ids) if s is not None]

    def get(self, job_id: str) -> dict[str, Any] | None:
        """Deep copy of the job (callers can't mutate shared state)."""
        with self._lock:
            job = self._jobs.get(job_id)
            return copy.deepcopy(job) if job else None

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        """JSON-safe deep copy of the live parts of a job (no `result`
        payload, which can hold hundreds of rows): what /job/{id}/status
        needs. Timestamps are epoch seconds (floats)."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            return {
                "id": job["id"],
                "type": job["type"],
                "status": job["status"],
                "meta": copy.deepcopy(job["meta"]),
                "progress": list(job["progress"]),
                "steps": copy.deepcopy(job["steps"]),
                "error": job["error"],
                "started_at": job["started_at"],
                "finished_at": job["finished_at"],
                "seen_by": sorted(job.get("seen_by") or ()),
            }

    def prune(self, now: float | None = None) -> int:
        """Drops finished jobs beyond the last _KEEP_FINISHED or older than
        _KEEP_FINISHED_SECONDS. Running jobs are always kept. Returns how
        many were removed."""
        with self._lock:
            return self._prune_locked(time.time() if now is None else now)

    def _prune_locked(self, now: float) -> int:
        finished = sorted(
            (j for j in self._jobs.values() if j["status"] != "running"),
            key=lambda j: j["finished_at"] or j["started_at"],
            reverse=True,
        )
        drop = [j["id"] for i, j in enumerate(finished)
                if i >= _KEEP_FINISHED
                or now - (j["finished_at"] or j["started_at"]) > _KEEP_FINISHED_SECONDS]
        for jid in drop:
            del self._jobs[jid]
        return len(drop)

    def run_in_thread(self, job_id: str, work: Callable[[str], Any]) -> None:
        """
        Runs `work(job_id)` in a thread. `work` must return the result
        payload; exceptions are caught and mark the job as error.
        """
        def _runner():
            try:
                result = work(job_id)
                self.finish(job_id, result)
            except Exception as exc:  # noqa: BLE001
                self.fail_open_steps(job_id, str(exc))
                self.fail(job_id, str(exc))

        threading.Thread(target=_runner, daemon=True).start()
