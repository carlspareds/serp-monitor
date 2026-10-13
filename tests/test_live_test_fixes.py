"""Fixes from the live test of SERP Monitor (OpenCode routing, plain provider
errors, AI provider health, AI failures during searches, assistant eval
outcomes, job header, review de-duplication, Settings layout/confirm/audit,
report file names, mobile wrapping, scheduler-off notice, docstrings and
Telegram errors).

No real network: requests.post is always replaced by a fake.
"""
import importlib.util
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests

from database.models import Database
from processing import ai_classifier, ai_status, llm

ROOT = Path(__file__).resolve().parent.parent
SECRET = "sk-live-fix-SECRET-987"
_LLM_VARS = ("LLM_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL",
             "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")
OPENCODE = "https://opencode.ai/zen/go/v1"
SUBSCRIPTION_403 = json.dumps({"error": {"type": "CreditsError",
                                         "message": "An active subscription is required."}})
MISSING_SESSION_400 = json.dumps({"error": {
    "type": "MissingSessionID",
    "message": "Request is missing x-opencode-session and cannot be routed efficiently"}})


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch):
    for var in _LLM_VARS:
        monkeypatch.setenv(var, "")
    yield


class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text if text or payload is None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _ok(content="ok"):
    return FakeResp(200, {"choices": [{"message": {"role": "assistant", "content": content},
                                       "finish_reason": "stop"}]})


@pytest.fixture
def posts(monkeypatch):
    class Box(list):
        response = _ok()

    box = Box()

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        box.append({"url": url, "json": json, "headers": dict(headers or {})})
        resp = box.response
        if isinstance(resp, list):
            resp = resp.pop(0) if len(resp) > 1 else resp[0]
        if isinstance(resp, Exception):
            raise resp
        return resp

    monkeypatch.setattr(requests, "post", fake_post)
    return box


def _opencode_env(monkeypatch, base=OPENCODE):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_BASE_URL", base)
    monkeypatch.setenv("OPENAI_MODEL", "kimi-k2")
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)


# --------------------------------------------------------------------- #
# 1. OpenCode routing header
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("base,expected", [
    ("https://opencode.ai/zen/go/v1", True),
    ("https://api.opencode.ai/v1", True),
    ("https://notopencode.ai/v1", False),
    ("https://opencode.ai.evil.example/v1", False),
    ("https://api.openai.com/v1", False),
    ("http://localhost:11434/v1", False),
])
def test_is_opencode_host_match(base, expected):
    assert llm.is_opencode(base) is expected


def test_call_openai_sends_session_header_only_to_opencode(monkeypatch, posts):
    _opencode_env(monkeypatch)
    assert llm.call_openai("hi")[0]
    sid = posts[-1]["headers"]["x-opencode-session"]
    uuid.UUID(sid)  # a UUID, never the raw key/cookie
    # Other hosts: no header at all.
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    llm.call_openai("hi")
    assert "x-opencode-session" not in posts[-1]["headers"]


def test_session_scope_is_stable_per_key_and_distinct_between_keys(monkeypatch, posts):
    _opencode_env(monkeypatch)
    with llm.session_scope("run:a"):
        llm.call_openai("1")
        llm.call_openai("2")
    with llm.session_scope("run:b"):
        llm.call_openai("3")
    with llm.session_scope("run:a"):
        llm.call_openai("4")
    llm.call_openai("5")
    llm.call_openai("6")
    ids = [p["headers"]["x-opencode-session"] for p in posts]
    assert ids[0] == ids[1] == ids[3]
    assert ids[2] != ids[0]
    assert ids[4] == ids[5] == llm._PROCESS_SESSION_ID  # outside any scope: per process
    assert "run:a" not in ids[0]


def test_assistant_sends_one_session_per_chat(monkeypatch, posts):
    from assistant import harness
    from assistant.harness import AutoModel, OpenAIChat
    _opencode_env(monkeypatch)
    posts.response = _ok("Hello.")
    model = AutoModel(OpenAIChat(OPENCODE, "kimi-k2", SECRET))
    ctx = _ctx()
    for key in ("chat-1", "chat-1", "chat-2"):
        harness.run_turn(model, ctx, [{"role": "user", "content": "hi"}], session_key=key,
                         overview={})
    ids = [p["headers"]["x-opencode-session"] for p in posts]
    assert ids[0] == ids[1] != ids[2]
    # Another host never gets it.
    posts.clear()
    harness.run_turn(AutoModel(OpenAIChat("http://llm.test/v1", "m")), ctx,
                     [{"role": "user", "content": "hi"}], session_key="chat-1", overview={})
    assert "x-opencode-session" not in posts[0]["headers"]


def _ctx(db=None):
    from assistant import tools
    from dashboard.jobs import JobManager
    return tools.ToolContext(db=db, subject_id=1, role="operator", user="t", jobs=JobManager(),
                             exports_dir=Path("/nonexistent"))


# --------------------------------------------------------------------- #
# 2. Provider errors -> plain messages
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("status,body,base,expected", [
    (403, SUBSCRIPTION_403, OPENCODE,
     ("OpenCode rejected the request: an active OpenCode Go subscription is required for this "
      "model. Check your plan or choose another model/provider in Settings → AI provider.")),
    (401, '{"error": {"message": "Invalid API key"}}', "https://api.openai.com/v1",
     "The AI provider rejected the API key (HTTP 401). Check Settings → AI provider."),
    (429, '{"error": {"message": "slow down"}}', "", "rate limiting"),
    (418, '{"error": {"message": "teapot"}}', "", "The AI provider returned an error (HTTP 418)."),
    (400, MISSING_SESSION_400, OPENCODE, "OpenCode couldn't route the request"),
    (500, "<html>oops</html>", "", "The AI provider had a server error (HTTP 500)."),
])
def test_describe_http_error_is_plain(status, body, base, expected):
    msg = llm.describe_http_error(status, body, base)
    assert expected in msg
    assert "{" not in msg and "<html" not in msg
    for raw in ("CreditsError", "MissingSessionID", "teapot", "slow down"):
        assert raw not in msg


def test_call_openai_failure_has_no_raw_json(monkeypatch, posts):
    _opencode_env(monkeypatch)
    posts.response = FakeResp(403, text=SUBSCRIPTION_403)
    ok, out = llm.call_openai("hi")
    assert not ok and out.startswith("OpenCode rejected the request")
    assert "{" not in out and "CreditsError" not in out


def test_assistant_reply_never_shows_provider_json(monkeypatch, posts):
    from assistant import harness
    from assistant.harness import AutoModel, OpenAIChat
    _opencode_env(monkeypatch)
    posts.response = FakeResp(403, text=SUBSCRIPTION_403)
    res = harness.run_turn(AutoModel(OpenAIChat(OPENCODE, "kimi-k2", SECRET)), _ctx(),
                           [{"role": "user", "content": "hi"}], session_key="k", overview={})
    assert res.stopped == "model_error"
    assert "active OpenCode Go subscription is required" in res.reply
    assert "{" not in res.reply and SECRET not in res.reply


def test_gemini_http_error_is_plain(monkeypatch, posts):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    posts.response = FakeResp(403, text='{"error": {"code": 403, "status": "PERMISSION_DENIED"}}')
    ok, out = ai_classifier._call_gemini("x")
    assert not ok and out.startswith("The AI provider refused access (HTTP 403)")
    assert "{" not in out


# --------------------------------------------------------------------- #
# 3. AI provider card status
# --------------------------------------------------------------------- #
def test_ai_card_says_not_tested_until_a_test_or_call(client, seeded_db, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    text = client.get("/settings").text
    card = text[text.index("<section id='ai'"):text.index("<section id='notifications'")]
    assert "Not tested yet" in card and "Connected" not in card


def test_ai_test_button_stores_and_shows_the_result(client, seeded_db, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    monkeypatch.setattr(ai_classifier, "_call_gemini", lambda *a, **k: (True, "ok"))
    client.post("/settings/ai-test")
    card = client.get("/settings").text
    card = card[card.index("<section id='ai'"):card.index("<section id='notifications'")]
    assert ">Working</span>" in card and "Working · tested " in card
    monkeypatch.setattr(ai_classifier, "_call_gemini",
                        lambda *a, **k: (False, "The AI provider rejected the API key (HTTP 401)."))
    client.post("/settings/ai-test")
    text = client.get("/settings").text
    card = text[text.index("<section id='ai'"):text.index("<section id='notifications'")]
    assert "Failing" in card and "rejected the API key" in card
    # Another model = another configuration: back to "not tested".
    monkeypatch.setenv("GEMINI_MODEL", "gemini-other")
    text = client.get("/settings").text
    card = text[text.index("<section id='ai'"):text.index("<section id='notifications'")]
    assert "Not tested yet" in card


def test_real_calls_update_the_status_with_throttled_writes(tmp_path, monkeypatch):
    db = Database(db_path=str(tmp_path / "s.db"))
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    ai_classifier.set_cache_db(db)
    try:
        monkeypatch.setattr(ai_classifier, "_call_gemini", lambda *a, **k: (False, "down"))
        ai_classifier._call_llm("x")
        assert ai_status.status(db)["ok"] is False
        # Same result again within the window: no rewrite.
        db.set_setting(ai_status.SETTING_KEY, json.dumps({**json.loads(
            db.get_setting(ai_status.SETTING_KEY)), "at": "marker"}))
        ai_classifier._call_llm("x")
        assert ai_status.status(db)["at"] == "marker"
        # A change (now working) is written at once.
        monkeypatch.setattr(ai_classifier, "_call_gemini", lambda *a, **k: (True, "positive"))
        ai_classifier._call_llm("x")
        assert ai_status.status(db)["ok"] is True
    finally:
        ai_classifier.set_cache_db(None)


def test_not_configured_is_not_recorded(tmp_path):
    db = Database(db_path=str(tmp_path / "s.db"))
    ai_status.record(False, "No AI provider configured.", force=True, db=db)
    assert db.get_setting(ai_status.SETTING_KEY, "") == ""


# --------------------------------------------------------------------- #
# 4. AI failures during searches are recorded, the search completes
# --------------------------------------------------------------------- #
_NEUTRAL = [{"query": "Jordan Ashworth", "url": f"https://dir{i}.example/x", "title": "Profile",
             "description": "Directory entry.", "displayed_url": f"dir{i}.example",
             "position": i + 1, "country_code": "us", "domain": f"dir{i}.example"}
            for i in range(3)]


class _FakeRunner:
    def search(self, queries, country_code, language_code, max_pages=1, engines=None,
               device="desktop"):
        run_id = f"run-{country_code}"
        return {"run_id": run_id, "status": "SUCCEEDED", "warnings": [], "engine_runs": [{
            "run_id": run_id, "queries": queries, "country_code": country_code,
            "status": "SUCCEEDED", "results": [dict(r) for r in _NEUTRAL], "engine": "google",
            "device": device, "error": None}]}


def test_failed_ai_steps_add_a_run_note_and_the_search_completes(tmp_path, monkeypatch):
    from report.multi_country import run_multi_country
    db = Database(db_path=str(tmp_path / "s.db"))
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    reason = "The AI provider is rate limiting requests or the quota is used up (HTTP 429)."
    monkeypatch.setattr(ai_classifier, "_call_gemini", lambda *a, **k: (False, reason))
    ai_classifier.set_cache_db(db)
    events = []
    try:
        outcomes = run_multi_country(_FakeRunner(), db, "Jordan Ashworth", ["us"],
                                     on_event=events.append)
    finally:
        ai_classifier.set_cache_db(None)
    assert outcomes[0]["error"] is None and outcomes[0]["summary"]["total"] == 3
    note = f"AI checks skipped: {reason}"
    assert outcomes[0]["ai_note"] == note
    assert db.get_run("run-us")["ai_note"] == note
    assert f"us: {note}" in events
    assert ai_status.status(db)["ok"] is False  # provider card -> Failing


def test_working_ai_leaves_no_note(tmp_path, monkeypatch):
    from report.multi_country import run_multi_country
    db = Database(db_path=str(tmp_path / "s.db"))
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    monkeypatch.setattr(ai_classifier, "_call_gemini",
                        lambda *a, **k: (True, '[{"i": 1, "same": "yes"}]'))
    ai_classifier.set_cache_db(db)
    try:
        outcomes = run_multi_country(_FakeRunner(), db, "Jordan Ashworth", ["us"])
    finally:
        ai_classifier.set_cache_db(None)
    assert outcomes[0]["ai_note"] == "" and not db.get_run("run-us")["ai_note"]


def test_results_and_overview_show_the_run_note(client, seeded_db):
    from conftest import RUN_ID
    seeded_db.set_run_ai_note(RUN_ID, "AI checks skipped: The AI provider had a server error.")
    for url in ("/results?country=us", "/?country=us"):
        text = client.get(url).text
        assert "data-ai-note" in text and "AI checks skipped: The AI provider had" in text, url


def test_job_done_note_and_log_mention_skipped_ai_checks():
    from dashboard.routers.search_runner import _done_note
    job = {"result": {"kind": "single", "summary": {"total": 3}, "ai_note": "AI checks skipped: x."}}
    assert _done_note(job).endswith("AI checks skipped: x.")
    job = {"result": {"kind": "multi", "agg": {"total": 3},
                      "outcomes": [{"ai_note": ""}, {"ai_note": "AI checks skipped: y"}]}}
    assert _done_note(job).endswith("AI checks skipped: y.")


def test_ai_relabel_reports_provider_failure(client, seeded_db, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    monkeypatch.setattr(ai_classifier, "_call_gemini",
                        lambda *a, **k: (False, "The AI provider rejected the API key (HTTP 401)."))
    r = client.post("/domains/reclassify-ai", data={"next": "/settings#domains"})
    assert "1 call failed: The AI provider rejected the API key" in r.text


# --------------------------------------------------------------------- #
# 5. Assistant evals: provider errors are their own outcome
# --------------------------------------------------------------------- #
def _eval_module():
    spec = importlib.util.spec_from_file_location("assistant_eval_run",
                                                  ROOT / "evals" / "assistant" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_eval_model_error_is_an_error_not_a_refusal():
    from assistant.harness import TurnResult
    ev = _eval_module()
    case = {"id": "r1", "expect": {"type": "refuse"}}
    err = TurnResult(reply="Sorry, I couldn't get an answer from the AI provider. Rate limited.",
                     error="Rate limited.", stopped="model_error")
    row = ev.evaluate(case, err, 0)
    assert row["outcome"] == "error" and row["pass"] is False
    genuine = TurnResult(reply="Sorry, I can't delete other users' data.")
    row = ev.evaluate(case, genuine, 0)
    assert row["outcome"] == "pass" and row["pass"] is True
    summary = ev.summarize([{**ev.evaluate(case, err, 0), "mode": "live"}])
    assert summary["model_errors"] == 1 and summary["passed"] == 0


# --------------------------------------------------------------------- #
# 6. Job panel status updates from the status JSON (the "Step x of y"
# header became the activity sentence of the single status box)
# --------------------------------------------------------------------- #
def test_job_status_sentence_is_live(client):
    import dashboard.main as main_module
    jobs = main_module.jobs
    jid = jobs.create("single", {"query": "q", "countries": ["us"], "engines": ["google"]},
                      steps=[("us", "google")])
    text = client.get(f"/ui/job/{jid}").text
    assert "<p class='job-activity' data-job-activity>Starting search…</p>" in text
    assert "aria-valuetext='0 of 1 steps finished'" in text
    js = (ROOT / "static" / "app.js").read_text()
    assert "q('[data-job-activity]')" in js and "if (d.activity) setActivity(d.activity);" in js


# --------------------------------------------------------------------- #
# 7. Review queue: one entry per URL
# --------------------------------------------------------------------- #
def _pending(url, country="us", engine="google"):
    return {"query": "q", "url": url, "title": "t", "description": "", "position": 1,
            "country_code": country, "domain": url.split("/")[2], "classification": "neutral",
            "confidence": 0.1, "engine": engine}


def test_review_queue_counts_each_url_once(tmp_path):
    db = Database(db_path=str(tmp_path / "r.db"))
    db.save_results([_pending("https://a.example/1"), _pending("https://b.example/1")], run_id="r1")
    db.save_results([_pending("https://a.example/1"), _pending("https://c.example/1")], run_id="r2")
    queue = db.pending_review()
    assert db.count_pending_review() == len(queue) == 3
    urls = [r["url"] for r in queue]
    assert sorted(urls) == ["https://a.example/1", "https://b.example/1", "https://c.example/1"]
    a = next(r for r in queue if r["url"] == "https://a.example/1")
    assert a["run_id"] == "r2"  # the newest row
    # Other country = another decision.
    db.save_results([_pending("https://a.example/1", country="gb")], run_id="r3")
    assert db.count_pending_review() == 4
    # A human verdict on the newest row settles the PAGE: the older duplicate and
    # the other country's row leave the queue too (human verdicts win per page).
    db.set_review_verdict(a["id"], "positive")
    assert "https://a.example/1" not in [r["url"] for r in db.pending_review()]
    assert db.count_pending_review() == 2


# --------------------------------------------------------------------- #
# 8, 9, 13. Layout CSS
# --------------------------------------------------------------------- #
def _css() -> str:
    return (ROOT / "static" / "style.css").read_text()


def _rule(css: str, selector: str) -> str:
    start = css.index(selector + " {")
    return css[start:css.index("}", start)]


def test_settings_sections_cannot_widen_the_column():
    css = _css()
    assert "grid-template-columns: minmax(0, 1fr)" in _rule(css, ".sgroup > section")
    assert "min-width: 0" in _rule(css, ".sgroup .card")
    assert "overflow-wrap: anywhere" in _rule(css, ".sgroup td.wrap-any")


def test_audit_table_cells_wrap(client, seeded_db):
    seeded_db.add_audit(actor_type="user", actor_id="x", action="settings_ai_save",
                        params={"long": "x" * 300})
    text = client.get("/settings").text
    assert "<td class='muted wrap-any'>" in text
    audit = text[text.index("<section id='audit'"):text.index("<section id='backup'")]
    assert "class='table-wrap'" in audit


def test_popover_items_are_left_aligned():
    css = _css()
    block = css[css.index(".pop .item,\n.pop button.item {"):]
    block = block[:block.index("}")]
    assert "justify-content: flex-start" in block


def test_domains_do_not_break_mid_word(client):
    css = _css()
    rule = _rule(css, "td .rdom.dom")
    assert "white-space: nowrap" in rule and "text-overflow: ellipsis" in rule
    assert "anywhere" not in _rule(css, ".rdom")
    text = client.get("/results?country=us").text
    assert re.search(r"<div class='rdom dom' title='scam\.example'>scam\.example</div>", text)


def test_assistant_placeholder_fits_on_mobile():
    from assistant import router
    src = Path(router.__file__).read_text()
    m = re.search(r"placeholder='([^']+)'></textarea>", src)
    assert m and len(m.group(1)) <= 32
    mobile = _css()[_css().index("@media (max-width: 640px) {\n  .hide-sm"):]
    assert ".assistant-composer textarea { font-size: 16px; }" in mobile


# --------------------------------------------------------------------- #
# 10. No native confirm()
# --------------------------------------------------------------------- #
def test_no_native_confirm_anywhere():
    pattern = re.compile(r"(^|[^A-Za-z_.])confirm\(")
    for folder, exts in (("src", (".py",)), ("static", (".js",)), ("templates", (".html",))):
        for path in (ROOT / folder).rglob("*"):
            if path.suffix in exts:
                for line in path.read_text(encoding="utf-8").splitlines():
                    code = line.split("//")[0] if path.suffix == ".js" else line
                    assert not pattern.search(code), f"{path}: {line.strip()}"


def test_api_key_revoke_uses_the_confirm_modal(client, seeded_db):
    seeded_db.add_api_key(name="n8n", scopes="read")
    text = client.get("/settings").text
    api = text[text.index("<section id='api'"):text.index("<section id='audit'")]
    assert "data-sm-confirm=" in api and "data-confirm-label='Revoke key'" in api
    assert "onsubmit" not in api


# --------------------------------------------------------------------- #
# 11. Test buttons are audited (no secrets)
# --------------------------------------------------------------------- #
def test_test_buttons_write_audit_entries(client, seeded_db, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    monkeypatch.setattr(ai_classifier, "_call_gemini",
                        lambda *a, **k: (False, f"The AI provider rejected the API key {SECRET}"))
    client.post("/settings/ai-test")
    row = seeded_db.list_audit(action="settings_ai_test")[0]
    params = json.loads(row["params_json"])
    assert params["result"] == "failed" and "rejected the API key" in params["reason"]
    assert SECRET not in row["params_json"]

    import dashboard.routers.settings as settings_module

    class _Client:
        def check_account(self):
            return {"ok": True, "username": "me"}

    class _Runner:
        client = _Client()

    monkeypatch.setattr(settings_module, "_get_runner", lambda: _Runner())
    client.post("/settings/apify-test")
    assert json.loads(seeded_db.list_audit(action="settings_apify_test")[0]["params_json"]) \
        == {"result": "ok", "reason": ""}

    client.post("/settings/email-test")
    assert json.loads(seeded_db.list_audit(action="settings_email_test")[0]["params_json"])[
        "result"] == "failed"
    client.post("/settings/notify-test", data={"channel": "slack"})
    params = json.loads(seeded_db.list_audit(action="settings_notify_test")[0]["params_json"])
    assert params["result"] == "failed" and params["channel"] == "slack"


# --------------------------------------------------------------------- #
# 12. Report file names
# --------------------------------------------------------------------- #
def test_report_file_names_include_project_and_subject(tmp_path):
    from export import reports
    db = Database(db_path=str(tmp_path / "n.db"))
    stem = reports.csv_name_stem(db, 1)
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    pdf = reports.report_path(db, tmp_path, 1, "pdf")
    assert pdf.name == f"{stem}-summary-{day}.pdf"
    pdf.write_bytes(b"%PDF")
    assert reports.report_path(db, tmp_path, 1, "pdf").name == f"{stem}-summary-{day}-2.pdf"
    assert reports.report_path(db, tmp_path, 1, "xlsx").name == f"{stem}-workbook-{day}.xlsx"
    for kind in ("pdf", "xlsx", "csv"):
        assert reports.safe_name(reports.report_path(db, tmp_path, 1, kind).name)


def test_pdf_download_name(client, seeded_db):
    from export import reports
    r = client.get("/export/pdf")
    assert r.status_code == 200
    stem = reports.csv_name_stem(seeded_db, 1)
    assert f"{stem}-summary-" in r.headers["content-disposition"]


# --------------------------------------------------------------------- #
# 14. Scheduler turned off
# --------------------------------------------------------------------- #
def test_scheduling_explains_disabled_scheduler_and_lists_schedules(client, monkeypatch):
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")
    text = client.get("/settings").text
    card = text[text.index("<section id='scheduling'"):text.index("<section id='users'")]
    assert "The scheduler is turned off (DISABLE_SCHEDULER is set), so no jobs run." in card
    assert "No jobs registered" not in card
    for name in ("Weekly report", "Alert checks", "Watchdog"):
        assert f"<b>{name}</b>" in card
    assert "/settings/cron/toggle" not in card and "/ui/settings/schedule/" not in card


# --------------------------------------------------------------------- #
# 15. Passwords are hashed by the DB layer
# --------------------------------------------------------------------- #
def test_add_user_takes_a_plain_password(tmp_path):
    db = Database(db_path=str(tmp_path / "u.db"))
    uid = db.add_user("ana", "plain-pass-1", role="operator")
    assert db.authenticate_user("ana", "plain-pass-1")
    db.set_user_password(uid, "plain-pass-2")
    assert db.authenticate_user("ana", "plain-pass-2")
    assert "PLAIN-TEXT" in Database.add_user.__doc__
    assert "ALREADY hashed" not in Database.add_user.__doc__
    assert "PLAIN-TEXT" in Database.set_user_password.__doc__


# --------------------------------------------------------------------- #
# 16. Telegram errors are actionable
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("detail,needle", [
    ('HTTP 400: {"ok":false,"error_code":400,"description":"Bad Request: chat not found"}',
     ("Telegram couldn't find this chat. Open your bot in Telegram, press Start (or send any "
      "message), then check the chat id (Settings → Notifications → Telegram).")),
    ('HTTP 401: {"ok":false,"error_code":401,"description":"Unauthorized"}',
     "Telegram rejected the bot token (HTTP 401)"),
    ('HTTP 403: {"ok":false,"error_code":403,"description":"Forbidden: bot was blocked by the user"}',
     "The bot was blocked by this Telegram user (HTTP 403)"),
    ('HTTP 403: {"ok":false,"description":"Forbidden: bot is not a member of the channel chat"}',
     "Add the bot to the group or channel"),
    ("Error: dns fail", "Error: dns fail"),
])
def test_telegram_errors_are_actionable(detail, needle):
    from notify.telegram import describe_error
    out = describe_error(detail)
    assert needle in out and "{" not in out


def test_telegram_test_button_shows_the_actionable_message(client, seeded_db, monkeypatch):
    import notify.base as notify_base
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret-token")
    seeded_db.set_setting("telegram_chat_id", "42")
    body = '{"ok":false,"error_code":400,"description":"Bad Request: chat not found"}'
    monkeypatch.setattr(notify_base.requests, "post", lambda *a, **k: FakeResp(400, text=body))
    r = client.post("/settings/notify-test", data={"channel": "telegram"})
    assert "Telegram couldn&#x27;t find this chat" in r.text or "Telegram couldn't find this chat" in r.text
    assert "chat not found" not in r.text
