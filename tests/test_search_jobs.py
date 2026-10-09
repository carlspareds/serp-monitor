"""Live search progress (/job/{id} + /job/{id}/status) and the JobManager
steps/snapshot/prune machinery. No network: get_runner is monkeypatched to
fakes that never touch Apify."""
import re
import time
from pathlib import Path

import pytest
from conftest import _wait_for_job

import dashboard.main as main_module
from apify.actors import ENGINE_NOT_CONFIGURED_MSG
from dashboard.jobs import JobManager

ROOT = Path(__file__).resolve().parent.parent


def _engine_run(engine, country, run_id, error=None):
    return {
        "engine": engine, "device": "desktop", "run_id": None if error else run_id,
        "status": "ERROR" if error else "SUCCEEDED", "country_code": country,
        "queries": ["Q"], "results": [], "features": [], "cost_usd": None,
        "compute_units": None, "retries": 0, "error": error,
    }


class _ProgressRunner:
    """Calls on_engine per engine like ApifyRunner does; bing is 'not configured'."""

    def __init__(self):
        self.calls = []

    def search(self, queries, country_code, language_code, max_pages, engines=None,
               device=None, on_engine=None):
        self.calls.append({"country": country_code, "engines": engines,
                           "on_engine": on_engine is not None})
        runs = []
        for engine in engines or ["google"]:
            if engine == "bing":
                msg = f"bing: {ENGINE_NOT_CONFIGURED_MSG}"
                if on_engine:
                    on_engine(engine, "skipped", message=msg)
                runs.append(_engine_run(engine, country_code, None, error=msg))
                continue
            if on_engine:
                on_engine(engine, "launching")
                on_engine(engine, "running", apify_status="READY", apify_elapsed_s=0)
                on_engine(engine, "running", apify_status="SUCCEEDED", apify_elapsed_s=5)
            runs.append(_engine_run(engine, country_code, f"run-{country_code}-{engine}"))
        ok = [r for r in runs if not r["error"]]
        return {
            "run_id": ok[0]["run_id"], "status": "SUCCEEDED", "country_code": country_code,
            "queries": queries, "results": [], "features": [], "cost_usd": None,
            "compute_units": None, "retries": 0, "engine_runs": runs,
            "warnings": [r["error"] for r in runs if r["error"]],
        }


@pytest.fixture
def progress_runner(monkeypatch):
    runner = _ProgressRunner()
    monkeypatch.setattr(main_module, "get_runner", lambda: runner)
    return runner


@pytest.fixture
def both_engines(monkeypatch):
    """Lets 'bing' pass the form whitelist so the skipped path is exercised."""
    import dashboard.ui_helpers as uh
    monkeypatch.setattr(uh, "CONFIGURED_ENGINES", {"google", "bing"})
    return ["google", "bing"]


def _job_id(resp) -> str:
    m = re.search(r"/job/([0-9a-f]+)", resp.text)
    assert m, resp.text[:500]
    return m.group(1)


def _strip_noscript(html: str) -> str:
    return re.sub(r"<noscript>.*?</noscript>", "", html, flags=re.DOTALL | re.IGNORECASE)


# --------------------------------------------------------------------- #
# PRG: POST -> 303 /job/{id}
# --------------------------------------------------------------------- #
def test_post_search_redirects_303_to_job(client, progress_runner):
    resp = client.post("/search", data={"query": "Q", "country": "us"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert re.fullmatch(r"/job/[0-9a-f]+", resp.headers["location"])
    _wait_for_job(main_module, resp.headers["location"].rsplit("/", 1)[1])


def test_post_search_multi_redirects_303_to_job(client, progress_runner):
    resp = client.post("/search_multi", data={"query": "Q", "countries": ["us", "gb"]},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/job/")
    _wait_for_job(main_module, resp.headers["location"].rsplit("/", 1)[1])


def test_followed_redirect_keeps_job_id_in_markup(client, progress_runner):
    resp = client.post("/search", data={"query": "Q", "country": "us"})
    assert resp.status_code == 200
    jid = _job_id(resp)
    assert main_module.jobs.get(jid) is not None
    _wait_for_job(main_module, jid)


# --------------------------------------------------------------------- #
# Running page markup
# --------------------------------------------------------------------- #
def test_running_page_polls_via_js_not_meta_refresh(client):
    jobs = main_module.jobs
    jid = jobs.create("multi", {"query": "Jordan", "countries": ["us", "gb"],
                                "engines": ["google"]},
                      steps=[("us", "google"), ("gb", "google")])
    jobs.set_step(jid, "us:google", state="running", apify_status="RUNNING", apify_elapsed_s=15)
    resp = client.get(f"/job/{jid}")
    assert resp.status_code == 200
    text = resp.text
    assert f"data-job-status-url='/job/{jid}/status'" in text
    assert "http-equiv" not in _strip_noscript(text)
    assert re.search(r"<noscript><meta http-equiv='refresh' content='5'></noscript>", text)
    assert "aria-live='polite'" in text
    assert "role='progressbar'" in text
    assert "data-step-key='us:google'" in text and "data-step-key='gb:google'" in text
    assert "Apify RUNNING · 15s" in text
    assert f"href='/job/{jid}'" in text  # manual Refresh link
    assert "Jordan" in text and "US, GB" in text


def test_unknown_job_page_is_404(client):
    resp = client.get("/job/deadbeef0000")
    assert resp.status_code == 404
    assert "no longer available" in resp.text


# --------------------------------------------------------------------- #
# /job/{id}/status JSON
# --------------------------------------------------------------------- #
_KEYS = {"id", "kind", "status", "query", "started_at", "server_now", "elapsed_s", "steps",
         "done_steps", "total_steps", "log", "error", "redirect", "view_url", "done_note"}
_STEP_KEYS = {"key", "country", "engine", "state", "apify_status", "apify_elapsed_s",
              "summary", "message"}


def test_status_running_shape_and_no_store(client):
    jobs = main_module.jobs
    jid = jobs.create("single", {"query": "Q", "country": "us", "engines": ["google", "bing"]},
                      steps=[("us", "google"), ("us", "bing")])
    jobs.set_step(jid, "us:bing", state="skipped", message="bing: not configured")
    jobs.add_progress(jid, "Searching in us…")
    resp = client.get(f"/job/{jid}/status")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "no-store"
    d = resp.json()
    assert set(d) == _KEYS
    assert d["id"] == jid and d["kind"] == "single" and d["status"] == "running"
    assert d["query"] == "Q"
    assert d["done_steps"] == 1 and d["total_steps"] == 2
    assert [s["key"] for s in d["steps"]] == ["us:google", "us:bing"]
    assert all(set(s) == _STEP_KEYS for s in d["steps"])
    assert d["steps"][0]["state"] == "pending" and d["steps"][0]["summary"] is None
    assert d["log"] == ["Searching in us…"]
    assert d["redirect"] is None and d["error"] is None
    assert d["server_now"] >= d["started_at"] and d["elapsed_s"] >= 0


def test_status_error_shape(client):
    jobs = main_module.jobs
    jid = jobs.create("single", {"query": "Q", "country": "us"}, steps=[("us", "google")])
    jobs.fail_open_steps(jid, "No Apify API key configured.")
    jobs.fail(jid, "No Apify API key configured.")
    d = client.get(f"/job/{jid}/status").json()
    assert d["status"] == "error"
    assert d["error"] == "No Apify API key configured."
    assert d["redirect"] is None
    assert d["steps"][0]["state"] == "error"
    assert d["done_steps"] == d["total_steps"] == 1


def test_status_unknown_job_404_json(client):
    resp = client.get("/job/0123456789ab/status")
    assert resp.status_code == 404
    assert resp.headers["cache-control"] == "no-store"
    assert resp.json()["error"] == "not_found"


def test_status_elapsed_freezes_when_finished(client):
    jobs = main_module.jobs
    jid = jobs.create("single", {"query": "Q"})
    jobs.fail(jid, "boom")
    with jobs._lock:
        jobs._jobs[jid]["started_at"] -= 30
        jobs._jobs[jid]["finished_at"] = jobs._jobs[jid]["started_at"] + 12
    d = client.get(f"/job/{jid}/status").json()
    assert d["elapsed_s"] == 12


# --------------------------------------------------------------------- #
# redirect logic
# --------------------------------------------------------------------- #
def test_redirect_single_goes_to_run(client):
    jobs = main_module.jobs
    jid = jobs.create("single", {"query": "Q"})
    jobs.finish(jid, {"kind": "single", "run_id": "run-1", "country": "us", "query": "Q",
                      "summary": {"total": 0, "positive": 0, "negative": 0, "neutral": 0},
                      "results": []})
    d = client.get(f"/job/{jid}/status").json()
    assert d["status"] == "done" and d["redirect"] == "/run/run-1"


def test_redirect_multi_one_country_goes_to_run(client):
    jobs = main_module.jobs
    jid = jobs.create("multi", {"query": "Q"})
    jobs.finish(jid, {"kind": "multi", "query": "Q", "agg": {}, "outcomes": [
        {"country": "us", "run_id": "run-us", "status": "SUCCEEDED", "summary": {}, "error": None},
    ]})
    assert client.get(f"/job/{jid}/status").json()["redirect"] == "/run/run-us"


def test_redirect_multi_several_countries_goes_to_job_summary(client):
    jobs = main_module.jobs
    jid = jobs.create("multi", {"query": "Q"})
    jobs.finish(jid, {"kind": "multi", "query": "Q",
                      "agg": {"countries_ok": 1, "countries_error": 1}, "outcomes": [
        {"country": "us", "run_id": "run-us", "status": "SUCCEEDED",
         "summary": {"positive": 1, "negative": 0, "neutral": 2, "total": 3}, "error": None},
        {"country": "gb", "run_id": None, "status": "ERROR", "summary": {}, "error": "boom"},
    ]})
    d = client.get(f"/job/{jid}/status").json()
    assert d["kind"] == "multi" and d["redirect"] == f"/job/{jid}"
    page = client.get(f"/job/{jid}").text
    assert "Multi-country report completed" in page
    assert "/run/run-us" in page and "ERROR: boom" in page
    assert "data-job-status-url" not in page  # done page doesn't poll (no redirect loop)


def test_error_page_is_server_rendered_with_link_back(client):
    jobs = main_module.jobs
    jid = jobs.create("single", {"query": "Q", "country": "us"}, steps=[("us", "google")])
    jobs.fail(jid, "No Apify API key configured.")
    text = client.get(f"/job/{jid}").text
    assert "The search failed" in text and "No Apify API key configured." in text
    assert "href='/search_page'" in text
    assert "data-job-status-url" not in text


# --------------------------------------------------------------------- #
# Callback wiring through the real routes
# --------------------------------------------------------------------- #
def test_single_flow_updates_steps_from_on_engine(client, progress_runner, both_engines):
    resp = client.post("/search", data={"query": "Q", "country": "us",
                                        "engines": both_engines})
    jid = _job_id(resp)
    job = _wait_for_job(main_module, jid)
    assert job["status"] == "done", job["error"]
    assert progress_runner.calls[0]["on_engine"] is True
    steps = {s["key"]: s for s in job["steps"]}
    assert list(steps) == ["us:google", "us:bing"]
    g = steps["us:google"]
    assert g["state"] == "done" and g["apify_status"] == "SUCCEEDED"
    assert g["apify_elapsed_s"] == 5
    assert g["summary"] == {"total": 0, "positive": 0, "negative": 0, "neutral": 0}
    assert steps["us:bing"]["state"] == "skipped"
    assert ENGINE_NOT_CONFIGURED_MSG in steps["us:bing"]["message"]
    d = client.get(f"/job/{jid}/status").json()
    assert d["redirect"] == "/run/run-us-google"
    assert d["done_steps"] == d["total_steps"] == 2


def test_multi_flow_updates_steps_per_country(client, progress_runner, both_engines):
    resp = client.post("/search_multi", data={"query": "Q", "countries": ["us", "gb"],
                                              "engines": both_engines})
    jid = _job_id(resp)
    job = _wait_for_job(main_module, jid)
    assert job["status"] == "done", job["error"]
    keys = [s["key"] for s in job["steps"]]
    assert keys == ["us:google", "us:bing", "gb:google", "gb:bing"]
    by_key = {s["key"]: s for s in job["steps"]}
    for c in ("us", "gb"):
        assert by_key[f"{c}:google"]["state"] == "done"
        assert by_key[f"{c}:google"]["apify_status"] == "SUCCEEDED"
        assert by_key[f"{c}:bing"]["state"] == "skipped"
    assert client.get(f"/job/{jid}/status").json()["redirect"] == f"/job/{jid}"


def test_legacy_runner_without_on_engine_still_works(client, monkeypatch):
    """Runners with the narrow pre-progress signature get no callback."""
    class _Legacy:
        def search(self, queries, country_code, language_code, max_pages, engines=None,
                   device=None):
            return {"run_id": "run-l", "status": "SUCCEEDED", "country_code": country_code,
                    "queries": queries, "results": [], "features": [],
                    "engine_runs": [_engine_run("google", country_code, "run-l")],
                    "warnings": []}
    monkeypatch.setattr(main_module, "get_runner", lambda: _Legacy())
    jid = _job_id(client.post("/search", data={"query": "Q", "country": "us"}))
    job = _wait_for_job(main_module, jid)
    assert job["status"] == "done"
    assert job["steps"][0]["state"] == "done"


def test_multi_country_failure_marks_country_steps_error(client, monkeypatch):
    from apify.client import ApifyError

    class _Failing(_ProgressRunner):
        def search(self, queries, country_code, **kw):
            if country_code == "gb":
                raise ApifyError("gb exploded")
            return super().search(queries, country_code, **kw)
    monkeypatch.setattr(main_module, "get_runner", lambda: _Failing())
    jid = _job_id(client.post("/search_multi", data={"query": "Q", "countries": ["us", "gb"]}))
    job = _wait_for_job(main_module, jid)
    by_key = {s["key"]: s for s in job["steps"]}
    assert by_key["us:google"]["state"] == "done"
    assert by_key["gb:google"]["state"] == "error"
    assert by_key["gb:google"]["message"] == "gb exploded"


def test_runner_search_tags_on_status_with_engine():
    """ApifyRunner wraps the client's (status, elapsed) polls per engine and
    still calls the legacy 2-arg on_status (scripts/smoke_apify.py)."""
    from apify.runner import ApifyRunner

    class _Client:
        def start_actor(self, actor_id, input_data):
            return {"id": "r1"}

        def wait_for_run(self, run_id, max_wait=300, on_status=None):
            on_status("RUNNING", 5)
            on_status("SUCCEEDED", 10)
            return {"id": run_id, "status": "SUCCEEDED", "defaultDatasetId": "d1"}

        def get_dataset_items(self, dataset_id, **kw):
            return []

        def __getattr__(self, name):  # cost lookups etc.: not needed here
            return lambda *a, **k: {}

    legacy, events = [], []
    ApifyRunner(client=_Client()).search(
        queries=["Q"], engines=["google"],
        on_status=lambda s, e: legacy.append((s, e)),
        on_engine=lambda eng, state, **info: events.append((eng, state, info)),
    )
    assert legacy == [("RUNNING", 5), ("SUCCEEDED", 10)]
    assert events[0] == ("google", "launching", {})
    assert ("google", "running", {"apify_status": "RUNNING", "apify_elapsed_s": 5}) in events


# --------------------------------------------------------------------- #
# JobManager: snapshot / prune / set_step
# --------------------------------------------------------------------- #
def test_snapshot_and_get_are_deep_copies():
    jm = JobManager()
    jid = jm.create("single", {"query": "Q"}, steps=[("us", "google")])
    jm.add_progress(jid, "a")
    snap = jm.snapshot(jid)
    snap["progress"].append("tampered")
    snap["steps"][0]["state"] = "done"
    got = jm.get(jid)
    got["progress"].append("tampered")
    got["steps"][0]["state"] = "done"
    fresh = jm.snapshot(jid)
    assert fresh["progress"] == ["a"]
    assert fresh["steps"][0]["state"] == "pending"
    import json
    json.dumps(fresh)  # JSON-safe


def test_set_step_does_not_revive_terminal_step():
    jm = JobManager()
    jid = jm.create("single", {}, steps=[("us", "google")])
    jm.set_step(jid, "us:google", state="done", summary={"total": 1})
    jm.set_step(jid, "us:google", state="running", apify_status="RUNNING")
    step = jm.snapshot(jid)["steps"][0]
    assert step["state"] == "done" and step["apify_status"] is None


def test_prune_keeps_running_and_last_50_finished():
    jm = JobManager()
    running = jm.create("single", {})
    finished = []
    for i in range(60):
        jid = jm.create("single", {})
        jm.finish(jid, {})
        with jm._lock:
            jm._jobs[jid]["finished_at"] = time.time() - 600 + i  # increasing recency
        finished.append(jid)
    jm.prune()
    assert jm.get(running) is not None
    kept = [j for j in finished if jm.get(j) is not None]
    assert kept == finished[-50:]


def test_prune_drops_finished_older_than_24h_and_runs_on_create():
    jm = JobManager()
    old = jm.create("single", {})
    jm.fail(old, "x")
    old_running = jm.create("single", {})
    with jm._lock:
        jm._jobs[old]["finished_at"] = time.time() - 25 * 3600
        jm._jobs[old_running]["started_at"] = time.time() - 25 * 3600
    jm.create("single", {})  # create() prunes
    assert jm.get(old) is None
    assert jm.get(old_running) is not None


# --------------------------------------------------------------------- #
# Static: the front-end polls instead of reloading
# --------------------------------------------------------------------- #
def test_app_js_polls_job_status_and_drops_blocking_overlay():
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "data-job-status-url" in js
    assert "document.hidden" in js and "location.replace" in js
    assert "getElementById('overlay')" not in js
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    assert ".job-steps" in css and ".job-progress-bar" in css


# --------------------------------------------------------------------- #
# New search modal (/ui/search) and the progress side panel (/ui/job/{id})
# --------------------------------------------------------------------- #
_FRAG = {"X-SM-Fragment": "1"}


def test_search_modal_preselects_the_active_subject_and_lists_enabled_engines(client, seeded_db):
    other = seeded_db.add_subject("Second Person", countries=["gb"])
    text = client.get("/ui/search", headers=_FRAG).text
    assert "<html" not in text and "New search" in text
    assert "<option value='1' selected>" in text
    assert f"<option value='{other}'>" in text and "New subject…" in text
    assert "coming soon" not in text.lower()
    assert "action='/search_multi'" in text and "data-sm-submit" in text
    assert "20 to 90 seconds" in text
    # Another subject preselected via ?subject= (and its own countries ticked)
    text2 = client.get("/ui/search", params={"subject": other}, headers=_FRAG).text
    assert f"<option value='{other}' selected>" in text2
    assert "value='gb' checked" in text2


def test_search_modal_without_apify_key_is_disabled_with_a_settings_link(client, monkeypatch):
    monkeypatch.delenv("APIFY_API_KEY", raising=False)
    text = client.get("/ui/search", headers=_FRAG).text
    assert "Needs an Apify key" in text and "href='/settings#search'" in text
    assert "class='btn primary' data-busy-label='Starting…' disabled" in text


def test_search_modal_submit_opens_the_progress_panel(client, progress_runner):
    resp = client.post("/search_multi", data={"query": "Q", "countries": ["us"], "engines": ["google"]},
                       headers=_FRAG)
    assert resp.status_code == 200, resp.text
    panel = resp.headers["X-SM-Panel"]
    assert re.fullmatch(r"/ui/job/[0-9a-f]+", panel)
    jid = panel.rsplit("/", 1)[1]
    job = _wait_for_job(main_module, jid)
    assert job["meta"]["query"] == "Q" and job["meta"]["subject_id"] == 1
    text = client.get(panel, headers=_FRAG).text
    assert "Searching" in text and "data-job-view" in text and "View results" in text
    d = client.get(f"/job/{jid}/status").json()
    assert d["view_url"].startswith("/subject/switch?id=1&next=")
    assert d["done_note"].startswith("Done.")


def test_search_modal_validation_error_stays_in_the_dialog(client, progress_runner):
    resp = client.post("/search_multi", data={"countries": []}, headers=_FRAG)
    assert resp.status_code == 400 and "Pick at least one country." in resp.text
    assert "<html" not in resp.text


def test_running_job_panel_polls_and_offers_run_in_background(client):
    jobs = main_module.jobs
    jid = jobs.create("multi", {"query": "Jordan", "countries": ["us"], "engines": ["google"],
                                "subject_name": "Jordan Ashworth"}, steps=[("us", "google")])
    text = client.get(f"/ui/job/{jid}", headers=_FRAG).text
    assert f"data-job-status-url='/job/{jid}/status'" in text
    assert "Run in background" in text and "Searching Jordan Ashworth" in text
    assert "data-job-view hidden" in text
    assert client.get("/ui/job/000000000000", headers=_FRAG).status_code == 404


class _OneResultRunner(_ProgressRunner):
    def search(self, queries, country_code, language_code, max_pages, engines=None,
               device=None, on_engine=None):
        out = super().search(queries, country_code, language_code, max_pages, engines, device, on_engine)
        for er in out["engine_runs"]:
            er["results"] = [{"query": queries[0], "url": f"https://scam.example/{country_code}",
                              "title": "Fraud complaints", "description": "scam fraud",
                              "displayed_url": "scam.example", "position": 1,
                              "country_code": country_code, "domain": "scam.example"}]
        return out


def test_search_job_writes_everything_under_the_submitted_subject(client, seeded_db, monkeypatch):
    """The subject is captured at submit time and carried into the worker
    thread: runs, results and URL lifecycle rows belong to it, not to the
    active subject of the request (1) nor to the default."""
    import sqlite3
    other = seeded_db.add_subject("Second Person", countries=["gb"])
    runner = _OneResultRunner()
    monkeypatch.setattr(main_module, "get_runner", lambda: runner)
    resp = client.post("/search_multi", data={"subject_id": str(other), "countries": ["gb"]},
                       headers=_FRAG)
    jid = resp.headers["X-SM-Panel"].rsplit("/", 1)[1]
    job = _wait_for_job(main_module, jid)
    assert job["status"] == "done", job["error"]
    assert job["meta"]["subject_id"] == other and job["meta"]["query"] == "Second Person"
    conn = sqlite3.connect(seeded_db.db_path)
    try:
        runs = conn.execute("SELECT subject_id FROM runs WHERE apify_run_id = 'run-gb-google'").fetchall()
        results = conn.execute("SELECT subject_id FROM search_results WHERE run_id = 'run-gb-google'").fetchall()
        lifecycle = conn.execute("SELECT subject_id FROM url_lifecycle WHERE url LIKE 'https://scam.example/gb%'").fetchall()
    finally:
        conn.close()
    assert runs == [(other,)]
    assert results and {r[0] for r in results} == {other}
    assert lifecycle and {r[0] for r in lifecycle} == {other}
    assert seeded_db.for_subject(1).get_run("run-gb-google") is None
    assert job["result"]["subject_id"] == other
    d = client.get(f"/job/{jid}/status").json()
    assert d["view_url"].startswith(f"/subject/switch?id={other}&next=")


def test_search_for_a_subject_the_viewer_cannot_see_is_refused(client, progress_runner):
    resp = client.post("/search_multi", data={"subject_id": "999", "countries": ["us"]},
                       headers=_FRAG)
    assert resp.status_code == 400 and "That subject isn&#x27;t available." in resp.text
    assert progress_runner.calls == []


def test_old_search_page_redirects_to_the_modal(client):
    resp = client.get("/search_page", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/results?modal=/ui/search"


def test_new_subject_from_the_search_modal_comes_back_preselected(client, seeded_db):
    resp = client.post("/subjects/add", headers=_FRAG, data={
        "name": "Brand New", "type": "person", "query": "Brand New", "countries": ["us"],
        "project_id": "1", "next": "/results?modal=/ui/search%3Fsubject%3D__new_subject__"})
    sid = next(s["id"] for s in seeded_db.list_subjects() if s["name"] == "Brand New")
    assert resp.headers["X-SM-Redirect"] == f"/results?modal=/ui/search%3Fsubject%3D{sid}"


def test_app_js_opens_the_job_panel_and_offers_view_results():
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "X-SM-Panel" in js and "data-job-view" in js and "view_url" in js
