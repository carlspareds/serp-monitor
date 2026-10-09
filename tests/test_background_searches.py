"""Background searches: the activity sentence of a job (dashboard.job_status),
the stages reported while saving (report.multi_country.persist_engine_run),
GET /jobs/active + POST /jobs/{id}/seen (scoping by subject and role, seen
handling, finished retention), the single status box of the job panel/page,
the top-bar chip, Runs → "Running now" and the static JS contract.

No network: jobs are inserted into a fresh JobManager directly; the one route
that launches a search uses a fake runner."""
import re
import time
from pathlib import Path

import pytest
from conftest import _wait_for_job

import dashboard.main as main_module
from dashboard import job_status as js
from dashboard.jobs import JobManager

ROOT = Path(__file__).resolve().parent.parent
_FRAG = {"X-SM-Fragment": "1"}


@pytest.fixture
def jobs(monkeypatch):
    jm = JobManager()
    monkeypatch.setattr(main_module, "jobs", jm)
    return jm


def _job(jm, sid=1, countries=("us", "gb"), engines=("google",), name="Jordan Ashworth", **meta):
    return jm.create("multi", {"query": name, "countries": list(countries), "engines": list(engines),
                               "subject_id": sid, "subject_name": name, "started_by": "local", **meta},
                     steps=[(c, e) for c in countries for e in engines])


def _snap(steps, status="running", error=None):
    return {"status": status, "error": error, "steps": [
        {"key": f"{c}:{e}", "country": c, "engine": e, "state": st, "apify_status": a,
         "apify_elapsed_s": el, "summary": sm, "message": None, "stage": stage}
        for c, e, st, a, el, sm, stage in steps]}


def _login(client, db, username, role, client_id=None):
    db.add_user(username, f"{username}-pass", role=role, client_id=client_id)
    r = client.post("/login", data={"username": username, "password": f"{username}-pass"},
                    follow_redirects=False)
    client.cookies.set("sid", r.cookies["sid"])


# --------------------------------------------------------------------- #
# Activity sentence, one per stage
# --------------------------------------------------------------------- #
def test_activity_starting():
    assert js.activity_sentence(_snap([("us", "google", "pending", None, None, None, None)])) == "Starting search…"
    assert js.activity_sentence({"status": "running", "steps": []}) == "Starting search…"


def test_activity_searching_names_engine_country_and_position():
    snap = _snap([("us", "google", "done", None, None, {"total": 3}, None),
                  ("gb", "google", "launching", None, None, None, None),
                  ("fr", "google", "pending", None, None, None, None)])
    assert js.activity_sentence(snap) == "Searching Google in the United Kingdom (2 of 3)…"


def test_activity_between_steps_names_the_next_one():
    snap = _snap([("us", "google", "done", None, None, {"total": 3}, None),
                  ("global", "bing", "pending", None, None, None, None)])
    assert js.activity_sentence(snap) == "Searching Bing worldwide (2 of 2)…"


def test_activity_waiting_for_apify_shows_its_timer():
    snap = _snap([("gb", "google", "running", "RUNNING", 35, None, None)])
    assert js.activity_sentence(snap) == "Waiting for Apify (Google, United Kingdom)… 0:35"
    snap = _snap([("gb", "google", "running", "SUCCEEDED", 61, None, None)])
    assert js.activity_sentence(snap) == "Fetching results (Google, United Kingdom)…"


@pytest.mark.parametrize("stage,sentence", [
    (None, "Saving results…"),
    ("classify", "Saving results…"),
    ("same_person", "Checking same-name results with AI…"),
    ("save", "Saving results…"),
    ("severity", "Rating the negatives with AI…"),
    ("evidence", "Capturing evidence for new negatives…"),
])
def test_activity_saving_stages(stage, sentence):
    snap = _snap([("us", "google", "saving", "SUCCEEDED", 40, None, stage),
                  ("gb", "google", "running", "RUNNING", 5, None, None)])
    assert js.activity_sentence(snap) == sentence    # saving wins over a later running step


def test_activity_finishing_up_when_every_step_ended():
    snap = _snap([("us", "google", "done", None, None, {"total": 1}, None)])
    assert js.activity_sentence(snap) == "Finishing up…"


def test_activity_done_sentence_counts_results_countries_and_new_negatives():
    snap = _snap([("us", "google", "done", None, None, {"total": 4, "negative": 1, "new_negatives": 1}, None),
                  ("gb", "google", "done", None, None, {"total": 3, "negative": 0, "new_negatives": 0}, None),
                  ("fr", "google", "done", None, None, {"total": 0}, None)], status="done")
    assert js.activity_sentence(snap) == "Done: 7 results in 3 countries, 1 new negative"
    one = _snap([("us", "google", "done", None, None, {"total": 1}, None),
                 ("gb", "google", "error", None, None, None, None)], status="done")
    assert js.activity_sentence(one) == "Done: 1 result in 1 country, no new negatives (1 country failed)"


def test_activity_done_without_steps_uses_the_result():
    result = {"kind": "single", "summary": {"total": 2, "negative": 1}, "new_negatives": 2}
    assert js.activity_sentence({"status": "done", "steps": []}, result) == \
        "Done: 2 results in 1 country, 2 new negatives"


def test_activity_error_is_plain():
    snap = _snap([], status="error", error="No Apify API key configured.\nTraceback (most recent call last)")
    assert js.activity_sentence(snap) == "Search failed: No Apify API key configured."


def test_scope_line_is_one_line():
    assert js.scope_line({"countries": ["us", "es", "gb"], "engines": ["google"]}) == \
        "Countries: US, ES, GB · Engines: Google"


# --------------------------------------------------------------------- #
# persist_engine_run reports its stages (saving / AI checks / evidence)
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("ai_on,expected", [
    (True, ["classify", "same_person", "save", "severity", "evidence"]),
    (False, ["classify", "save", "evidence"]),
])
def test_persist_engine_run_reports_stages_and_new_negatives(seeded_db, monkeypatch, ai_on, expected):
    from report import multi_country as mc
    monkeypatch.setattr(mc, "_ai_on", lambda: ai_on)
    monkeypatch.setattr(mc, "annotate_same_person", lambda results, db: results)
    monkeypatch.setattr(mc, "annotate_severity", lambda db, run_id: None)
    monkeypatch.setattr(mc, "capture_new_negatives", lambda db, run_id: [{"id": 1}, {"id": 2}])
    stages = []
    run = {"run_id": "run-stage-1", "queries": ["Q"], "country_code": "us", "status": "SUCCEEDED",
           "engine": "google", "device": "desktop", "results": [
               {"query": "Q", "url": "https://scam.example/a", "title": "Fraud", "description": "scam",
                "displayed_url": "scam.example", "position": 1, "country_code": "us",
                "domain": "scam.example"}]}
    out = mc.persist_engine_run(seeded_db, run, on_stage=stages.append)
    assert stages == expected
    assert out["new_negatives"] == 2 and out["summary"]["total"] == 1


def test_a_failing_stage_callback_never_breaks_the_save(seeded_db, monkeypatch):
    from report import multi_country as mc
    monkeypatch.setattr(mc, "capture_new_negatives", lambda db, run_id: [])

    def boom(_name):
        raise RuntimeError("ui gone")
    run = {"run_id": "run-stage-2", "queries": ["Q"], "country_code": "us", "status": "SUCCEEDED",
           "engine": "google", "device": "desktop", "results": []}
    assert mc.persist_engine_run(seeded_db, run, on_stage=boom)["new_negatives"] == 0


def test_job_steps_carry_the_stage(jobs):
    jid = _job(jobs, countries=("us",))
    jobs.set_step(jid, "us:google", state="saving", stage="same_person")
    snap = jobs.snapshot(jid)
    assert snap["steps"][0]["stage"] == "same_person"
    assert js.activity_sentence(snap) == "Checking same-name results with AI…"


# --------------------------------------------------------------------- #
# GET /jobs/active
# --------------------------------------------------------------------- #
def test_active_lists_running_job_of_the_active_subject(client, jobs):
    jid = _job(jobs)
    jobs.set_step(jid, "us:google", state="running", apify_status="RUNNING", apify_elapsed_s=12)
    resp = client.get("/jobs/active")
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    d = resp.json()
    assert d["running"] == 1 and d["other_count"] == 0 and d["subject_id"] == 1
    j = d["jobs"][0]
    assert j["id"] == jid and j["status"] == "running" and j["subject_name"] == "Jordan Ashworth"
    assert j["activity"] == "Waiting for Apify (Google, United States)… 0:12"
    assert j["done_steps"] == 0 and j["total_steps"] == 2 and j["elapsed_s"] >= 0
    assert j["summary"] is None and j["error"] is None and j["finished_at"] is None
    assert {"query", "redirect", "view_url", "started_by", "seen", "started_at"} <= set(j)


def test_active_counts_jobs_of_other_subjects(client, seeded_db, jobs):
    other = seeded_db.add_subject("Second Person", countries=["gb"])
    _job(jobs, sid=other, name="Second Person")
    d = client.get("/jobs/active").json()
    assert d["jobs"] == [] and d["other_count"] == 1
    assert d["other_jobs"][0]["subject_name"] == "Second Person" and d["running"] == 1
    client.cookies.set("subject", str(other))
    d = client.get("/jobs/active").json()
    assert len(d["jobs"]) == 1 and d["other_count"] == 0


def test_finished_job_is_listed_until_seen(client, jobs):
    jid = _job(jobs, countries=("us",))
    jobs.set_step(jid, "us:google", state="done",
                  summary={"total": 5, "negative": 2, "new_negatives": 1})
    jobs.finish(jid, {"kind": "multi", "query": "Q", "agg": {}, "subject_id": 1, "outcomes": [
        {"country": "us", "run_id": "run-us", "status": "SUCCEEDED", "summary": {}, "error": None}]})
    j = client.get("/jobs/active").json()["jobs"][0]
    assert j["status"] == "done" and j["seen"] is False
    assert j["summary"] == {"results": 5, "negatives": 2, "new_negatives": 1, "countries": 1,
                            "failed_countries": 0}
    assert j["activity"] == "Done: 5 results in 1 country, 1 new negative"
    assert j["view_url"].startswith("/subject/switch?id=1&next=") and j["redirect"] == "/run/run-us"
    resp = client.post(f"/jobs/{jid}/seen")
    assert resp.status_code == 200 and resp.json()["ok"] is True
    assert resp.headers["cache-control"] == "no-store"
    assert client.get("/jobs/active").json()["jobs"] == []


def test_failed_job_reports_a_plain_error(client, jobs):
    jid = _job(jobs, countries=("us",))
    jobs.fail_open_steps(jid, "boom")
    jobs.fail(jid, "Every engine failed: google: boom\nmore lines")
    j = client.get("/jobs/active").json()["jobs"][0]
    assert j["status"] == "error" and j["error"] == "Every engine failed: google: boom"
    assert j["activity"] == "Search failed: Every engine failed: google: boom"


def test_finished_jobs_drop_out_after_ten_minutes(client, jobs):
    recent, old = _job(jobs), _job(jobs)
    for jid in (recent, old):
        jobs.fail(jid, "boom")
    with jobs._lock:
        jobs._jobs[old]["finished_at"] = time.time() - js.ACTIVE_FINISHED_SECONDS - 5
        jobs._jobs[recent]["finished_at"] = time.time() - js.ACTIVE_FINISHED_SECONDS + 60
    ids = [j["id"] for j in client.get("/jobs/active").json()["jobs"]]
    assert ids == [recent]


def test_seen_does_not_hide_a_running_job(client, jobs):
    jid = _job(jobs)
    assert client.post(f"/jobs/{jid}/seen").status_code == 200
    assert [j["id"] for j in client.get("/jobs/active").json()["jobs"]] == [jid]


def test_seen_unknown_or_invisible_job_is_404(client, seeded_db, jobs):
    assert client.post("/jobs/0123456789ab/seen").status_code == 404
    pid = seeded_db.add_client("Client B")
    theirs = seeded_db.add_subject("Client B Person", countries=["us"], client_id=pid)
    jid = _job(jobs, sid=1)
    _login(client, seeded_db, "vic", "client_viewer", client_id=pid)
    assert theirs
    resp = client.post(f"/jobs/{jid}/seen")
    assert resp.status_code == 404 and resp.json()["error"] == "not_found"


def test_seen_is_rejected_cross_site(client, jobs):
    jid = _job(jobs)
    resp = client.post(f"/jobs/{jid}/seen", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_client_viewer_only_sees_jobs_of_their_subjects(client, seeded_db, jobs):
    pid = seeded_db.add_client("Client B")
    theirs = seeded_db.add_subject("Client B Person", countries=["us"], client_id=pid)
    hidden = _job(jobs, sid=1)
    mine = _job(jobs, sid=theirs, name="Client B Person")
    jobs.fail(mine, "boom")
    _login(client, seeded_db, "vic", "client_viewer", client_id=pid)
    d = client.get("/jobs/active").json()
    assert [j["id"] for j in d["jobs"]] == [mine] and d["other_count"] == 0
    # A read-only account may still mark its own view as seen.
    assert client.post(f"/jobs/{mine}/seen").status_code == 200
    assert client.get("/jobs/active").json()["jobs"] == []
    # Nor can it open the other subject's job.
    assert client.get(f"/job/{hidden}/status").status_code == 404
    assert client.get(f"/ui/job/{hidden}", headers=_FRAG).status_code == 404


def test_staff_see_jobs_of_every_subject_and_seen_is_per_user(client, seeded_db, jobs):
    pid = seeded_db.add_client("Client B")
    theirs = seeded_db.add_subject("Client B Person", countries=["us"], client_id=pid)
    jid = _job(jobs, sid=theirs, name="Client B Person")
    jobs.fail(jid, "boom")
    jobs.mark_seen(jid, "user:someone-else")
    _login(client, seeded_db, "boss", "admin")
    d = client.get("/jobs/active").json()
    assert d["other_count"] == 1 and d["other_jobs"][0]["id"] == jid


def test_search_records_who_started_it_and_the_subject(client, seeded_db, jobs, monkeypatch):
    from test_search_jobs import _ProgressRunner
    monkeypatch.setattr(main_module, "get_runner", lambda: _ProgressRunner())
    resp = client.post("/search_multi", data={"query": "Q", "countries": ["us"], "engines": ["google"]},
                       headers=_FRAG)
    assert resp.status_code == 200, resp.text[:300]
    jid = resp.headers["X-SM-Panel"].rsplit("/", 1)[1]
    job = _wait_for_job(main_module, jid)
    meta = job["meta"]
    assert meta["subject_id"] == 1 and meta["started_by"] == "local" and "session" in meta
    assert job["steps"][0]["summary"]["new_negatives"] == 0
    d = client.get(f"/job/{jid}/status").json()
    assert d["activity"].startswith("Done: ") and d["summary"]["countries"] == 1


# --------------------------------------------------------------------- #
# Job panel / page: ONE status box, Details collapsed unless a step failed
# --------------------------------------------------------------------- #
def _before_details(text: str) -> str:
    return text.split("<details class='job-details'", 1)[0]


def test_panel_renders_a_single_status_box(client, jobs):
    jid = _job(jobs, countries=("us", "gb", "fr"))
    jobs.set_step(jid, "us:google", state="launching")
    text = client.get(f"/ui/job/{jid}", headers=_FRAG).text
    assert text.count("data-job-box") == 1
    assert re.search(r"data-job-activity>Searching Google in the United States \(1 of 3\)…<", text)
    assert "Countries: US, GB, FR · Engines: Google" in text
    assert "role='progressbar'" in text and "data-job-elapsed" in text
    head = _before_details(text)
    assert "job-step" not in head and "pending" not in head          # no per-item list outside Details
    assert "<details class='job-details' data-job-details>" in text      # collapsed
    assert "data-step-key='gb:google'" in text                          # ...but there
    assert "Run in background" in text and "data-job-view hidden" in text
    assert "data-job-retry hidden" in text


def test_details_auto_expand_when_a_step_failed(client, jobs):
    jid = _job(jobs, countries=("us", "gb"))
    jobs.set_step(jid, "us:google", state="error", message="Apify run failed")
    text = client.get(f"/ui/job/{jid}", headers=_FRAG).text
    assert "<details class='job-details' data-job-details open>" in text
    assert "Apify run failed" in text


def test_failed_job_panel_offers_back_to_new_search(client, jobs):
    jid = _job(jobs, countries=("us",))
    jobs.fail_open_steps(jid, "No Apify API key configured.")
    jobs.fail(jid, "No Apify API key configured.")
    text = client.get(f"/ui/job/{jid}", headers=_FRAG).text
    assert "data-job-status-url" not in text
    assert "Search failed: No Apify API key configured." in text
    assert "job-box is-error" in text and "data-job-details open" in text
    assert re.search(r"href='/results\?modal=/ui/search' data-job-retry>Back to New search<", text)
    assert "data-job-view hidden" in text and ">Close</button>" in text


def test_opening_a_finished_job_marks_it_seen(client, jobs):
    jid = _job(jobs, countries=("us",))
    jobs.fail(jid, "boom")
    assert client.get("/jobs/active").json()["jobs"]
    client.get(f"/ui/job/{jid}", headers=_FRAG)
    assert client.get("/jobs/active").json()["jobs"] == []


def test_job_page_fallback_uses_the_same_status_box(client, jobs):
    jid = _job(jobs, countries=("us", "gb"))
    text = client.get(f"/job/{jid}").text
    assert text.count("data-job-box") == 1 and "data-job-activity>Starting search…<" in text
    assert "job-step" not in _before_details(text)
    jobs.fail_open_steps(jid, "boom")
    jobs.fail(jid, "boom")
    text = client.get(f"/job/{jid}").text
    assert "data-job-details open" in text and "Back to New search" in text


# --------------------------------------------------------------------- #
# Shell chip, Runs "Running now", assistant job card
# --------------------------------------------------------------------- #
def test_chip_markup_is_in_the_shell_next_to_the_bell(client):
    text = client.get("/").text
    chip = re.search(r"<button type='button' class='jobchip' id='jobchip' data-jobs-chip hidden[^>]*>", text)
    assert chip and "aria-describedby='jobchip-live'" in chip.group(0)
    assert "id='jobchip-live' role='status' aria-live='polite'" in text
    assert text.index("id='jobchip'") < text.index("id='bell'")
    assert "data-jobs-chip-text" in text


def test_runs_panel_lists_running_now(client, seeded_db, jobs):
    jid = _job(jobs, countries=("us",))
    other = seeded_db.add_subject("Second Person", countries=["gb"])
    hidden = _job(jobs, sid=other, name="Second Person")
    text = client.get("/ui/runs", headers=_FRAG).text
    assert "Running now" in text
    assert f"data-panel='/ui/job/{jid}'" in text and f"data-active-job='{jid}'" in text
    assert "data-active-job-activity>Starting search…<" in text
    assert hidden not in text
    jobs.fail(jid, "boom")
    assert "Running now" not in client.get("/ui/runs", headers=_FRAG).text


def test_assistant_job_card_and_quiet_intent():
    from assistant import cards
    card = cards.job_card({"job_id": "abc123", "title": "Search for Jordan"})
    assert card["kind"] == "job" and card["data"]["job_id"] == "abc123"
    assert card["data"]["activity"] == "Starting search…"
    assert cards.job_card({}) is None


# --------------------------------------------------------------------- #
# Static: chip polling contract, toasts, reduced motion
# --------------------------------------------------------------------- #
def _js():
    return (ROOT / "static" / "app.js").read_text(encoding="utf-8")


def test_app_js_polls_active_jobs_only_while_something_runs():
    js_text = _js()
    assert "fetch('/jobs/active'" in js_text
    assert "var JOBS_POLL_MS = 5000;" in js_text and "var JOBS_HIDDEN_MS = 30000;" in js_text
    schedule = js_text[js_text.index("    function schedule() {\n      if (timer) { clearTimeout(timer); timer = null; }"):]
    schedule = schedule[:schedule.index("\n    }\n")]
    assert "if (!anyRunning()) return;" in schedule
    assert "document.hidden ? JOBS_HIDDEN_MS : JOBS_POLL_MS" in schedule


def test_app_js_fetches_once_on_load_so_the_chip_survives_reloads():
    js_text = _js()
    assert "refresh();   // initial fetch on every page load" in js_text
    assert "document.getElementById('jobchip')" in js_text


def test_app_js_toasts_finished_and_failed_searches_with_actions():
    js_text = _js()
    assert "' finished: '" in js_text and "action: 'View results'" in js_text
    assert "action: 'Details'" in js_text and "'/seen'" in js_text
    assert "JOBS_TOASTED_KEY" in js_text
    assert "SM.toast = function (message, kind, opts)" in js_text


def test_app_js_job_panel_renders_activity_and_opens_details_on_error():
    js_text = _js()
    assert "d.activity" in js_text and "[data-job-activity]" in js_text
    assert "details.open = true" in js_text
    assert "[data-job-retry]" in js_text


def test_spinner_respects_reduced_motion():
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
    block = css[css.index("22. BACKGROUND SEARCHES"):]
    assert "@media (prefers-reduced-motion: reduce)" in block
    rm = block[block.index("@media (prefers-reduced-motion: reduce)"):]
    assert ".jobchip-spin" in rm[:200] and "animation: none" in rm[:300]
