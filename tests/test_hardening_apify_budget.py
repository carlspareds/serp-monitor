"""FIX 1 point 1e (hardening audit): Database._check_apify_budget was
evaluated AFTER saving the run -- the spend had already happened. This
file covers the new pre-flight in ApifyRunner.search: if the runner is
built with `db` and the monthly budget is already exceeded, search()
raises SerpProviderError BEFORE launching any actor (every provider's
spend counts, see serp.runner.SerpRunner._check_budget).

TDD, no network: same pattern as tests/test_apify_costs.py (_FakeClient
stub, never touches real Apify) -- here it's enough that
start_actor/wait_for_run are NEVER called when the pre-flight cuts it off.
"""
import pytest
from conftest import make_db as _db

from apify.runner import ApifyRunner
from serp.base import SerpProviderError


class _FakeClient:
    """start_actor would launch best-effort if called -- the budget-cutoff
    tests assert it is NEVER reached."""

    def __init__(self):
        self.start_calls = 0

    def start_actor(self, actor_id, input_data):
        self.start_calls += 1
        return {"id": "run-should-not-happen"}

    def wait_for_run(self, run_id, max_wait=300, on_status=None):
        raise AssertionError("should never reach waiting for a run -- the budget already cut it off")

    def get_dataset_items(self, dataset_id):
        raise AssertionError("should never reach downloading the dataset")


def test_search_without_db_never_preflights_budget(tmp_path):
    """Legacy caller (no `db`, e.g. report.weekly): no
    behavior change -- it doesn't even try to read a budget."""
    client = _FakeClient()
    runner = ApifyRunner(client=client)
    assert runner.db is None
    # The search doesn't need to succeed -- it's enough that it does NOT
    # raise due to budget (it would raise via wait_for_run, which is
    # exactly what confirms the pre-flight didn't cut it off first).
    with pytest.raises(AssertionError):
        runner.search(["q"], country_code="us")
    assert client.start_calls == 1


def test_search_preflight_blocks_when_budget_already_exceeded(tmp_path):
    db = _db(tmp_path)
    db.set_setting("apify_monthly_budget", "5.00")
    db.save_run(apify_run_id="run-spent-1", queries=["q"], country_code="us",
                status="completed", summary={"total": 0, "positive": 0, "negative": 0, "neutral": 0},
                cost_usd=6.00)

    client = _FakeClient()
    runner = ApifyRunner(client=client, db=db)

    with pytest.raises(SerpProviderError, match=r"budget"):
        runner.search(["q"], country_code="us")

    assert client.start_calls == 0  # it never got to launch the actor


def test_search_preflight_allows_when_budget_not_yet_exceeded(tmp_path):
    db = _db(tmp_path)
    db.set_setting("apify_monthly_budget", "50.00")
    db.save_run(apify_run_id="run-spent-2", queries=["q"], country_code="us",
                status="completed", summary={"total": 0, "positive": 0, "negative": 0, "neutral": 0},
                cost_usd=1.00)

    client = _FakeClient()
    runner = ApifyRunner(client=client, db=db)

    # Under budget -> the pre-flight lets it through; it gets to
    # wait_for_run (which this stub fails on purpose, proof that it
    # DID try to launch).
    with pytest.raises(AssertionError):
        runner.search(["q"], country_code="us")
    assert client.start_calls == 1


def test_search_preflight_no_budget_configured_never_blocks(tmp_path):
    db = _db(tmp_path)  # no apify_monthly_budget -> "0" by default
    client = _FakeClient()
    runner = ApifyRunner(client=client, db=db)

    with pytest.raises(AssertionError):
        runner.search(["q"], country_code="us")
    assert client.start_calls == 1


def test_search_override_budget_forces_through(tmp_path):
    db = _db(tmp_path)
    db.set_setting("apify_monthly_budget", "5.00")
    db.save_run(apify_run_id="run-spent-3", queries=["q"], country_code="us",
                status="completed", summary={"total": 0, "positive": 0, "negative": 0, "neutral": 0},
                cost_usd=100.00)

    client = _FakeClient()
    runner = ApifyRunner(client=client, db=db)

    with pytest.raises(AssertionError):
        runner.search(["q"], country_code="us", override_budget=True)
    assert client.start_calls == 1


def test_check_budget_unreadable_setting_never_blocks(tmp_path):
    """Best-effort: a corrupt setting must not block a real search."""
    db = _db(tmp_path)
    db.set_setting("apify_monthly_budget", "not-a-number")
    client = _FakeClient()
    runner = ApifyRunner(client=client, db=db)

    with pytest.raises(AssertionError):
        runner.search(["q"], country_code="us")
    assert client.start_calls == 1
