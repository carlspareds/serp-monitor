"""Tests for FIX 1 point 1a (post-F4 "hardening" audit): limits on
/search and /search_multi that previously didn't exist on the Python
side -- pages without a clamp, engines/country(ies) without a whitelist,
and no limit on concurrent jobs (dashboard.jobs.JobManager).

TDD, no network: get_runner is monkeypatched to a _FakeRunner that never
touches Apify -- these tests verify the GUARD, not the save pipeline
(already covered by tests/test_multi_engine.py).
"""
import re

import pytest
from conftest import _wait_for_job

import dashboard.main as main_module
from apify.actors import CONFIGURED_ENGINES
from dashboard.jobs import _MAX_CONCURRENT_JOBS


class _FakeRunner:
    """Captures the kwargs .search() was called with -- to verify that
    main.py's guard already cleaned up pages/engines before reaching
    here."""

    def __init__(self):
        self.calls = []

    def search(self, queries, country_code, language_code, max_pages, engines=None, device=None):
        self.calls.append({"max_pages": max_pages, "engines": engines, "device": device})
        return {
            "run_id": "run-fake", "queries": queries, "country_code": country_code,
            "status": "completed", "results": [], "features": [],
            "engine_runs": [{
                "engine": "google", "device": device or "desktop", "run_id": "run-fake",
                "status": "completed", "country_code": country_code, "queries": queries,
                "results": [], "features": [], "error": None,
            }],
            "warnings": [],
        }


@pytest.fixture
def fake_runner(monkeypatch):
    runner = _FakeRunner()
    monkeypatch.setattr(main_module, "get_runner", lambda: runner)
    return runner


# --------------------------------------------------------------------- #
# 1a: pure helpers (clamp/whitelist)
# --------------------------------------------------------------------- #
def test_clamp_pages_within_range_unchanged():
    assert main_module._clamp_pages(2) == 2


def test_clamp_pages_above_max_clamps_to_3():
    assert main_module._clamp_pages(999) == 3


def test_clamp_pages_below_min_clamps_to_1():
    assert main_module._clamp_pages(0) == 1
    assert main_module._clamp_pages(-5) == 1


def test_clamp_pages_non_numeric_falls_back_to_1():
    assert main_module._clamp_pages("not-a-number") == 1


def test_split_valid_engines_keeps_only_configured():
    valid, invalid = main_module._split_valid_engines(["google", "news", "bogus"])
    assert valid == ["google"]
    assert set(invalid) == {"news", "bogus"}


def test_split_valid_engines_all_configured():
    valid, invalid = main_module._split_valid_engines(["google", "bing"])
    assert set(valid) == {"google", "bing"}
    assert invalid == []


def test_configured_engines_is_google_and_bing_only():
    assert CONFIGURED_ENGINES == frozenset({"google", "bing"})


def test_split_valid_countries_keeps_only_whitelisted():
    valid, invalid = main_module._split_valid_countries(["us", "xx", "gb"])
    assert valid == ["us", "gb"]
    assert invalid == ["xx"]


# --------------------------------------------------------------------- #
# 1a: POST /search -- pages clamp + engines/country whitelist
# --------------------------------------------------------------------- #
def test_post_search_clamps_pages_above_3(client, fake_runner):
    resp = client.post("/search", data={"query": "Q", "country": "us", "pages": 50})
    m = re.search(r"/job/([0-9a-f]+)", resp.text)
    job = _wait_for_job(main_module, m.group(1))
    assert job["status"] == "done"
    assert fake_runner.calls[0]["max_pages"] == 3


def test_post_search_clamps_pages_below_1(client, fake_runner):
    resp = client.post("/search", data={"query": "Q", "country": "us", "pages": 0})
    m = re.search(r"/job/([0-9a-f]+)", resp.text)
    job = _wait_for_job(main_module, m.group(1))
    assert job["status"] == "done"
    assert fake_runner.calls[0]["max_pages"] == 1


def test_post_search_rejects_only_unconfigured_engines_with_clear_message(client, fake_runner):
    resp = client.post("/search", data={"query": "Q", "country": "us", "pages": 1,
                                        "engines": ["news"]})
    assert resp.status_code == 400   # the dialog again, with the reason
    assert "/job/" not in resp.text
    assert fake_runner.calls == []
    assert "engine" in resp.text.lower()


def test_post_search_filters_invalid_engine_keeps_valid(client, fake_runner):
    resp = client.post("/search", data={"query": "Q", "country": "us", "pages": 1,
                                        "engines": ["google", "bogus"]})
    m = re.search(r"/job/([0-9a-f]+)", resp.text)
    job = _wait_for_job(main_module, m.group(1))
    assert job["status"] == "done"
    assert fake_runner.calls[0]["engines"] == ["google"]


def test_post_search_rejects_invalid_country(client, fake_runner):
    resp = client.post("/search", data={"query": "Q", "country": "atlantis", "pages": 1})
    assert resp.status_code == 400   # the dialog again, with the reason
    assert "/job/" not in resp.text
    assert fake_runner.calls == []


# --------------------------------------------------------------------- #
# 1a: POST /search_multi -- same guard + countries whitelist
# --------------------------------------------------------------------- #
def test_post_search_multi_clamps_pages(client, fake_runner):
    resp = client.post("/search_multi", data={"query": "Q", "countries": ["us"], "pages": 50})
    m = re.search(r"/job/([0-9a-f]+)", resp.text)
    job = _wait_for_job(main_module, m.group(1))
    assert job["status"] == "done"
    assert fake_runner.calls[0]["max_pages"] == 3


def test_post_search_multi_filters_invalid_countries_keeps_valid(client, fake_runner):
    resp = client.post("/search_multi", data={"query": "Q", "countries": ["us", "xx"], "pages": 1})
    m = re.search(r"/job/([0-9a-f]+)", resp.text)
    job = _wait_for_job(main_module, m.group(1))
    assert job["status"] == "done"
    # a single valid country -> a single run
    assert len(fake_runner.calls) == 1


def test_post_search_multi_rejects_when_all_countries_invalid(client, fake_runner):
    resp = client.post("/search_multi", data={"query": "Q", "countries": ["xx", "yy"], "pages": 1})
    assert resp.status_code == 400   # the dialog again, with the reason
    assert "/job/" not in resp.text
    assert fake_runner.calls == []


def test_post_search_multi_rejects_unconfigured_engines(client, fake_runner):
    resp = client.post("/search_multi", data={"query": "Q", "countries": ["us"], "pages": 1,
                                               "engines": ["youtube"]})
    assert resp.status_code == 400   # the dialog again, with the reason
    assert "/job/" not in resp.text
    assert fake_runner.calls == []


# --------------------------------------------------------------------- #
# 1a: concurrent jobs cap (dashboard.jobs._MAX_CONCURRENT_JOBS)
# --------------------------------------------------------------------- #
def test_search_rejects_when_max_concurrent_jobs_running(client, fake_runner):
    # Simulates _MAX_CONCURRENT_JOBS jobs ALREADY in progress (status='running'
    # by default after create(), see dashboard.jobs.JobManager.create)
    # without needing a real long-running execution.
    for _ in range(_MAX_CONCURRENT_JOBS):
        main_module.jobs.create("single", {})

    resp = client.post("/search", data={"query": "Q", "country": "us", "pages": 1})

    assert resp.status_code == 400   # the dialog again, with the reason
    assert "/job/" not in resp.text
    assert fake_runner.calls == []


def test_search_multi_rejects_when_max_concurrent_jobs_running(client, fake_runner):
    for _ in range(_MAX_CONCURRENT_JOBS):
        main_module.jobs.create("single", {})

    resp = client.post("/search_multi", data={"query": "Q", "countries": ["us"], "pages": 1})

    assert resp.status_code == 400   # the dialog again, with the reason
    assert "/job/" not in resp.text
    assert fake_runner.calls == []


def test_search_allowed_when_below_concurrency_limit(client, fake_runner):
    main_module.jobs.create("single", {})  # 1 running, limit is 2

    resp = client.post("/search", data={"query": "Q", "country": "us", "pages": 1})
    m = re.search(r"/job/([0-9a-f]+)", resp.text)
    assert m, resp.text
    job = _wait_for_job(main_module, m.group(1))
    assert job["status"] == "done"


# --------------------------------------------------------------------- #
# JobManager.running_count (unit test, no HTTP client)
# --------------------------------------------------------------------- #
def test_job_manager_running_count_counts_only_running():
    from dashboard.jobs import JobManager
    jm = JobManager()
    a = jm.create("single", {})
    b = jm.create("single", {})
    assert jm.running_count() == 2
    jm.finish(a, {"ok": True})
    assert jm.running_count() == 1
    jm.fail(b, "boom")
    assert jm.running_count() == 0
