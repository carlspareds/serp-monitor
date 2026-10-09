"""Tests for item 1.4: Apify costs (cost_usd/compute_units) + retries
with backoff in ApifyRunner.search(), monthly spend + budget in
/settings and `apify_budget` alert with monthly dedupe.

TDD, no network: ApifyClient is replaced by a controllable stub
(_FakeClient) and time.sleep is monkeypatched -- it never actually sleeps.
"""
import pytest

from apify import runner as runner_module
from apify.client import ApifyError
from apify.runner import ApifyRunner

SUMMARY = {"total": 1, "positive": 0, "negative": 0, "neutral": 1}


class _FakeClient:
    """Replaces ApifyClient: start_actor always 'launches' fine; wait_for_run
    consumes a sequence of results/exceptions, one per attempt
    (simulating a transient ApifyError that resolves on a later
    attempt, or a final one that isn't retried)."""

    def __init__(self, wait_sequence, dataset=None, start_id="run-x"):
        self._wait_sequence = list(wait_sequence)
        self.dataset = dataset if dataset is not None else []
        self.start_id = start_id
        self.start_calls = 0
        self.wait_calls = 0

    def start_actor(self, actor_id, input_data):
        self.start_calls += 1
        return {"id": self.start_id}

    def wait_for_run(self, run_id, max_wait=300, on_status=None):
        self.wait_calls += 1
        item = self._wait_sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get_dataset_items(self, dataset_id):
        return self.dataset


from conftest import make_db as _db


# --------------------------------------------------------------------- #
# 1) Column migration in `runs`
# --------------------------------------------------------------------- #
def test_runs_table_has_cost_columns(tmp_path):
    db = _db(tmp_path)
    conn = db._connect()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    conn.close()
    assert {"cost_usd", "compute_units", "retries"} <= cols


# --------------------------------------------------------------------- #
# 2) save_run persists cost_usd/compute_units/retries
# --------------------------------------------------------------------- #
def test_save_run_persists_cost_fields(tmp_path):
    db = _db(tmp_path)
    db.save_run("run-1", ["q"], "us", "completed", SUMMARY,
                cost_usd=0.25, compute_units=0.1, retries=2)

    row = db.get_run("run-1")

    assert row["cost_usd"] == 0.25
    assert row["compute_units"] == 0.1
    assert row["retries"] == 2


def test_save_run_defaults_cost_to_null_and_retries_to_zero(tmp_path):
    db = _db(tmp_path)
    db.save_run("run-2", ["q"], "us", "completed", SUMMARY)

    row = db.get_run("run-2")

    assert row["cost_usd"] is None
    assert row["compute_units"] is None
    assert row["retries"] == 0


# --------------------------------------------------------------------- #
# 3) apify_month_cost(): accumulated spend for the current UTC month
# --------------------------------------------------------------------- #
def test_apify_month_cost_sums_current_month_only(tmp_path):
    db = _db(tmp_path)
    db.save_run("run-a", ["q"], "us", "completed", SUMMARY, cost_usd=1.5, compute_units=0.2)
    db.save_run("run-b", ["q"], "ca", "completed", SUMMARY, cost_usd=2.5, compute_units=0.3)
    # run-b: backdated to a past month (outside the current accumulation).
    conn = db._connect()
    conn.execute(
        "UPDATE runs SET created_at = ? WHERE apify_run_id = ?",
        ("2000-01-15T00:00:00", "run-b"),
    )
    conn.commit()
    conn.close()

    stats = db.apify_month_cost()

    assert stats["cost_usd"] == 1.5
    assert stats["runs"] == 1
    assert abs(stats["compute_units"] - 0.2) < 1e-9


def test_apify_month_cost_ignores_runs_without_known_cost(tmp_path):
    db = _db(tmp_path)
    db.save_run("run-a", ["q"], "us", "completed", SUMMARY)  # no cost (old actor)

    stats = db.apify_month_cost()

    assert stats["cost_usd"] == 0
    assert stats["runs"] == 0


# --------------------------------------------------------------------- #
# 4) apify_budget alert with monthly dedupe
# --------------------------------------------------------------------- #
def test_save_run_over_budget_creates_alert_once(tmp_path):
    db = _db(tmp_path)
    db.set_setting("apify_monthly_budget", "1.00")

    db.save_run("run-1", ["q"], "us", "completed", SUMMARY, cost_usd=0.80)
    assert db.list_alerts() == []  # still under budget

    db.save_run("run-2", ["q"], "us", "completed", SUMMARY, cost_usd=0.50)
    alerts = db.list_alerts()
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "apify_budget"
    # 7.1: add_alert normalizes legacy severity -- 'high' becomes 'critical'.
    assert alerts[0]["severity"] == "critical"

    # A third run that still exceeds the budget does NOT duplicate the alert.
    db.save_run("run-3", ["q"], "us", "completed", SUMMARY, cost_usd=5.0)
    assert len(db.list_alerts()) == 1


def test_save_run_without_budget_configured_never_alerts(tmp_path):
    db = _db(tmp_path)
    db.save_run("run-1", ["q"], "us", "completed", SUMMARY, cost_usd=1000.0)

    assert db.list_alerts() == []


def test_save_run_without_cost_does_not_check_budget(tmp_path):
    """A run with no known cost (old actor) must not trigger the
    budget check even if it had already been exceeded before."""
    db = _db(tmp_path)
    db.set_setting("apify_monthly_budget", "1.00")
    db.save_run("run-1", ["q"], "us", "completed", SUMMARY, cost_usd=5.0)
    assert len(db.list_alerts()) == 1

    db.mark_alerts_seen()
    db.save_run("run-2", ["q"], "us", "completed", SUMMARY)  # no cost_usd
    # The budget is not re-evaluated: there's still only one alert.
    assert len(db.list_alerts()) == 1


# --------------------------------------------------------------------- #
# 5) ApifyRunner.search(): cost capture + retry with backoff
# --------------------------------------------------------------------- #
def test_search_captures_cost_fields_from_final_run(monkeypatch):
    client = _FakeClient(wait_sequence=[
        {"status": "SUCCEEDED", "defaultDatasetId": "ds1",
         "usageTotalUsd": 0.1234, "stats": {"computeUnits": 0.05}},
    ], dataset=[{"searchQuery": {"term": "q"}, "organicResults": []}])
    r = ApifyRunner(client=client)

    out = r.search(["q"], country_code="us")

    assert out["cost_usd"] == 0.1234
    assert out["compute_units"] == 0.05
    assert out["retries"] == 0


def test_search_cost_fields_null_when_actor_omits_them(monkeypatch):
    """Old actor that doesn't report usageTotalUsd/stats: NULL, no error."""
    client = _FakeClient(wait_sequence=[
        {"status": "SUCCEEDED", "defaultDatasetId": "ds1"},
    ], dataset=[])
    r = ApifyRunner(client=client)

    out = r.search(["q"], country_code="us")

    assert out["cost_usd"] is None
    assert out["compute_units"] is None


def test_search_retries_wait_on_same_run_without_relaunching(monkeypatch):
    """Regression from review 1.4 HIGH: a transient failure AFTER launching
    (timeout/5xx while waiting) retries the WAIT on the SAME run —
    relaunching the actor would leave the original run alive and billing on Apify."""
    sleeps = []
    monkeypatch.setattr(runner_module.time, "sleep", lambda s: sleeps.append(s))
    client = _FakeClient(wait_sequence=[
        ApifyError("HTTP 500 from Apify: boom"),
        ApifyError("Run run-x did not finish in 300s (last status: RUNNING)"),
        {"status": "SUCCEEDED", "defaultDatasetId": "ds1",
         "usageTotalUsd": 0.12, "stats": {"computeUnits": 0.05}},
    ], dataset=[{"searchQuery": {"term": "q"}, "organicResults": []}])
    r = ApifyRunner(client=client)

    out = r.search(["q"], country_code="us")

    assert out["retries"] == 2
    assert out["status"] == "SUCCEEDED"
    assert out["cost_usd"] == 0.12
    assert sleeps == [30, 60]  # 30*2^0, 30*2^1 -- backoff without actually sleeping
    assert client.start_calls == 1  # ONE single actor launched: zero double spend
    assert client.wait_calls == 3  # the wait WAS retried


def test_search_retries_launch_when_start_actor_fails(monkeypatch):
    """LAUNCH phase: if start_actor fails transiently, relaunching is
    safe (there's no run billing yet)."""
    sleeps = []
    monkeypatch.setattr(runner_module.time, "sleep", lambda s: sleeps.append(s))
    client = _FakeClient(wait_sequence=[
        {"status": "SUCCEEDED", "defaultDatasetId": "ds1",
         "usageTotalUsd": 0.05, "stats": {"computeUnits": 0.02}},
    ], dataset=[{"searchQuery": {"term": "q"}, "organicResults": []}])
    boom = {"left": 2}
    original_start = client.start_actor

    def flaky_start(actor_id, input_data):
        if boom["left"] > 0:
            boom["left"] -= 1
            client.start_calls += 1
            raise ApifyError("Request to Apify failed after 3 attempts: connection timed out")
        return original_start(actor_id, input_data)

    client.start_actor = flaky_start
    r = ApifyRunner(client=client)

    out = r.search(["q"], country_code="us")

    assert out["retries"] == 2
    assert client.start_calls == 3  # 2 launch failures + 1 good one
    assert client.wait_calls == 1


def test_search_does_not_retry_4xx_even_with_timeout_in_body(monkeypatch):
    """Regression from review 1.4 #3: an HTTP 400 whose body mentions
    'timeout' (invalid input) must NOT be classified as transient."""
    calls = {"n": 0}
    monkeypatch.setattr(runner_module.time, "sleep", lambda s: calls.__setitem__("n", calls["n"] + 1))
    client = _FakeClient(wait_sequence=[
        ApifyError('HTTP 400 from Apify: {"error": "invalid input field timeout"}'),
    ])
    r = ApifyRunner(client=client)

    with pytest.raises(ApifyError):
        r.search(["q"], country_code="us")

    assert calls["n"] == 0  # never slept: not retryable


def test_search_does_not_retry_on_401(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(runner_module.time, "sleep", lambda s: calls.__setitem__("n", calls["n"] + 1))
    client = _FakeClient(wait_sequence=[ApifyError("HTTP 401 from Apify: unauthorized")])
    r = ApifyRunner(client=client)

    with pytest.raises(ApifyError):
        r.search(["q"], country_code="us")

    assert calls["n"] == 0  # never slept: not retryable
    assert client.start_calls == 1


def test_search_does_not_retry_on_actor_failed_status(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(runner_module.time, "sleep", lambda s: calls.__setitem__("n", calls["n"] + 1))
    client = _FakeClient(wait_sequence=[{"status": "FAILED", "defaultDatasetId": "ds1"}])
    r = ApifyRunner(client=client)

    with pytest.raises(ApifyError):
        r.search(["q"], country_code="us")

    assert calls["n"] == 0
    assert client.start_calls == 1


def test_search_gives_up_after_max_wait_attempts(monkeypatch):
    sleeps = []
    monkeypatch.setattr(runner_module.time, "sleep", lambda s: sleeps.append(s))
    client = _FakeClient(wait_sequence=[
        ApifyError("HTTP 500 from Apify: e1"),
        ApifyError("HTTP 500 from Apify: e2"),
        ApifyError("HTTP 500 from Apify: e3"),
    ])
    r = ApifyRunner(client=client)

    with pytest.raises(ApifyError):
        r.search(["q"], country_code="us")

    assert sleeps == [30, 60]  # 2 backoffs between 3 attempts, none after the last one
    assert client.start_calls == 1  # the failures were WAIT failures: no relaunch
    assert client.wait_calls == 3


def test_save_run_conflict_preserves_existing_cost(tmp_path):
    """Regression from review 1.4 #4: a re-save without cost (legacy caller)
    must not overwrite the accounting already recorded."""
    db = _db(tmp_path)
    db.save_run("run-k", ["q"], "us", "completed", SUMMARY,
                cost_usd=0.4, compute_units=0.2, retries=1)
    db.save_run("run-k", ["q"], "us", "completed", SUMMARY)  # no cost

    row = db.get_run("run-k")

    assert row["cost_usd"] == 0.4
    assert row["compute_units"] == 0.2
    assert row["retries"] == 1


# --------------------------------------------------------------------- #
# 6) /settings: spend card + editable budget
# --------------------------------------------------------------------- #
def test_settings_page_shows_apify_cost_card(client):
    response = client.get("/settings")
    assert response.status_code == 200
    assert "Spend this month" in response.text


def test_settings_save_apify_budget_persists_and_shows(client):
    response = client.post("/settings/apify-budget", data={"budget": "50.5"}, follow_redirects=True)
    assert response.status_code == 200
    assert "50.50" in response.text


# --------------------------------------------------------------------- #
# 7) The budget alert is deployment-wide: once a month, whichever subject
# --------------------------------------------------------------------- #
def test_budget_alert_is_deployment_wide_and_fires_once_a_month(tmp_path):
    root = _db(tmp_path)
    root.set_setting("apify_monthly_budget", "1.00")
    s2 = root.add_subject("Bob Second", countries=["us"])
    s3 = root.add_subject("Carol Third", countries=["us"])

    alert = root.for_subject(s2).save_run("run-2", ["q"], "us", "completed", SUMMARY,
                                           cost_usd=5.0)
    assert alert is not None and alert["kind"] == "apify_budget"
    # Stored under subject 1, not under the subject whose run crossed it.
    assert [a["kind"] for a in root.for_subject(1).list_alerts()] == ["apify_budget"]
    assert root.for_subject(s2).list_alerts() == []

    # Another subject going over the same month: no second alert.
    assert root.for_subject(s3).save_run("run-3", ["q"], "us", "completed", SUMMARY,
                                          cost_usd=5.0) is None
    assert root.for_subject(s3).list_alerts() == []
    # Not even after the first one is resolved: once a month, globally.
    root.for_subject(1).resolve_alert(alert["id"])
    assert root.for_subject(s2).save_run("run-4", ["q"], "us", "completed", SUMMARY,
                                          cost_usd=1.0) is None
    assert len(root.for_all_subjects().list_alerts(status="all")) == 1
    assert root.current_budget_alert()["status"] == "resolved"


def test_budget_alert_shows_in_other_subjects_popover_and_settings(client, seeded_db):
    seeded_db.set_setting("apify_monthly_budget", "1.00")
    s2 = seeded_db.add_subject("Bob Second", countries=["us"])
    seeded_db.for_subject(s2).save_run("run-b", ["q"], "us", "completed", SUMMARY,
                                        cost_usd=5.0)
    client.cookies.set("subject", str(s2))
    pop = client.get("/ui/alerts").text
    assert "exceeds budget" in pop and "All subjects" in pop
    assert "data-sm-alerts-count='0'" in pop  # not counted in this subject's badge
    settings = client.get("/settings").text
    assert "Budget alert this month" in settings
