"""Assistant HTTP API (src/assistant/router.py) through the real app: the
disabled state, chat with a scripted model, the confirmation protocol
(execute once, audit, cancel, expiry, session binding, roles), CSRF, rate
limiting, the panel fragment, and that no secret reaches a response."""
import json

import pytest
from conftest import _wait_for_job

from assistant import harness, pending, router
from assistant.fakes import ScriptedModel, call, say

SECRET = "sk-test-API-SECRET-987654321"


@pytest.fixture(autouse=True)
def _clean_state():
    pending.STORE.clear()
    router._rate.clear()
    yield
    pending.STORE.clear()
    router._rate.clear()


@pytest.fixture
def ai_on(monkeypatch):
    """Configure an OpenAI-compatible provider (never called: build_model is
    replaced by a scripted model per test)."""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("OPENAI_MODEL", "fake-model")
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)


def script(monkeypatch, *turns):
    model = ScriptedModel(list(turns))
    monkeypatch.setattr(harness, "build_model", lambda: model)
    return model


def chat(client, text, **kw):
    return client.post("/assistant/chat", json={"messages": [{"role": "user", "content": text}]}, **kw)


def propose_domain(client, monkeypatch, domain="example.com"):
    script(monkeypatch, call("add_domain", domain=domain, list="positive"), say("Prepared."))
    resp = chat(client, f"add {domain} to my positive domains")
    assert resp.status_code == 200, resp.text
    return next(e for e in resp.json()["events"] if e["type"] == "confirm")


def audit_rows(seeded_db, action):
    return [r for r in seeded_db.list_audit(limit=200) if r["action"] == action]


# --------------------------------------------------------------------- #
# Disabled state
# --------------------------------------------------------------------- #
def test_disabled_without_provider(client):
    st = client.get("/assistant/status").json()
    assert st["enabled"] is False and "Settings → AI provider" in st["message"]
    resp = chat(client, "hello")
    assert resp.status_code == 503 and resp.json()["error"] == "assistant_disabled"
    panel = client.get("/assistant/panel").text
    assert "data-assistant-disabled" in panel and "href='/settings#ai'" in panel
    assert "data-assistant-composer" not in panel


def test_status_enabled_has_no_secret(client, ai_on):
    resp = client.get("/assistant/status")
    st = resp.json()
    assert st["enabled"] is True and st["provider"] == {
        "provider": "openai", "model": "fake-model", "host": "llm.test"}
    assert st["role"] == "admin" and st["can_act"] is True and st["tool_mode"] == "native"
    assert SECRET not in resp.text


# --------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------- #
def test_chat_read_tool_flow(client, ai_on, monkeypatch):
    model = script(monkeypatch, call("list_runs", limit=3), say("You have 1 run."))
    resp = chat(client, "how many runs?")
    body = resp.json()
    assert resp.status_code == 200
    assert body["reply"] == "You have 1 run."
    assert body["messages"][-1] == {"role": "assistant", "content": "You have 1 run."}
    assert body["events"][0]["type"] == "tool" and body["events"][0]["name"] == "list_runs"
    assert isinstance(body["suggestions"], list) and all("prompt" in s for s in body["suggestions"])
    assert "Active subject right now" in model.calls[0]["system"]


def test_chat_bad_body(client, ai_on, monkeypatch):
    script(monkeypatch)
    assert client.post("/assistant/chat", json={"nope": 1}).status_code == 400
    assert client.post("/assistant/chat", content=b"not json",
                       headers={"content-type": "application/json"}).status_code == 400


def test_rate_limit(client, ai_on, monkeypatch):
    monkeypatch.setattr(router, "RATE_LIMIT_TURNS", 2)
    script(monkeypatch, say("a"), say("b"), say("c"))
    assert chat(client, "1").status_code == 200
    assert chat(client, "2").status_code == 200
    resp = chat(client, "3")
    assert resp.status_code == 429 and resp.json()["error"] == "rate_limited"


def test_csrf_cross_site_post_rejected(client, ai_on, monkeypatch):
    script(monkeypatch, say("x"))
    assert chat(client, "hi", headers={"origin": "http://evil.example"}).status_code == 403
    assert chat(client, "hi", headers={"origin": "http://testserver"}).status_code == 200


def test_no_secret_in_any_response(client, ai_on, monkeypatch):
    monkeypatch.setenv("APIFY_API_KEY", "apify_api_HIDDEN_555666")
    script(monkeypatch, call("get_setup_status"), say(f"key={SECRET}"))
    texts = [chat(client, "show me my keys").text, client.get("/assistant/status").text,
             client.get("/assistant/panel").text, client.get("/assistant/suggestions").text]
    for t in texts:
        assert SECRET not in t and "apify_api_HIDDEN_555666" not in t
    assert "<secret>" in texts[0]


# --------------------------------------------------------------------- #
# Confirmation protocol
# --------------------------------------------------------------------- #
def test_action_waits_for_confirm_then_executes_once(client, ai_on, monkeypatch, seeded_db):
    card = propose_domain(client, monkeypatch)
    assert card["title"] == "Add example.com to the positive domains?"
    assert card["cost_note"] and card["details"] and card["id"]
    assert "example.com" not in seeded_db.list_positive_domains()
    assert audit_rows(seeded_db, "assistant_action") == []

    resp = client.post("/assistant/confirm", json={"id": card["id"]})
    body = resp.json()
    assert resp.status_code == 200 and body["ok"] is True and body["already"] is False
    assert "example.com added" in body["message"]
    assert body["events"][0]["type"] == "intent" and body["events"][0]["url"] == "/settings#domains"
    assert "example.com" in seeded_db.list_positive_domains()
    rows = audit_rows(seeded_db, "assistant_action")
    assert len(rows) == 1
    params = json.loads(rows[0]["params_json"])
    assert params["tool"] == "add_domain" and params["ok"] is True and params["subject_id"] == 1
    assert params["args"] == {"domain": "example.com", "list": "positive"}
    assert rows[0]["resource_type"] == "domain" and rows[0]["resource_id"] == "example.com"

    again = client.post("/assistant/confirm", json={"id": card["id"]})
    assert again.status_code == 200 and again.json()["already"] is True
    assert len(audit_rows(seeded_db, "assistant_action")) == 1      # idempotent
    assert client.post("/assistant/cancel", json={"id": card["id"]}).status_code == 409


def test_cancel_changes_nothing_and_is_audited(client, ai_on, monkeypatch, seeded_db):
    card = propose_domain(client, monkeypatch, "cancel.example")
    resp = client.post("/assistant/cancel", json={"id": card["id"]})
    assert resp.status_code == 200 and "Nothing was changed" in resp.json()["message"]
    assert "cancel.example" not in seeded_db.list_positive_domains()
    assert len(audit_rows(seeded_db, "assistant_action_cancelled")) == 1
    assert client.post("/assistant/confirm", json={"id": card["id"]}).status_code == 409
    assert "cancel.example" not in seeded_db.list_positive_domains()


def test_unknown_and_expired_ids_404(client, ai_on, monkeypatch, seeded_db):
    assert client.post("/assistant/confirm", json={"id": "nope"}).status_code == 404
    assert client.post("/assistant/cancel", json={}).status_code == 404
    card = propose_domain(client, monkeypatch, "late.example")
    pending.STORE.get(card["id"]).created_at -= pending.TTL_SECONDS + 1
    resp = client.post("/assistant/confirm", json={"id": card["id"]})
    assert resp.status_code == 404 and resp.json()["error"] == "expired"
    assert "late.example" not in seeded_db.list_positive_domains()


def test_other_session_gets_403(client, ai_on, monkeypatch, seeded_db):
    card = propose_domain(client, monkeypatch, "mine.example")
    client.cookies.clear()                      # a different browser/session
    assert client.post("/assistant/confirm", json={"id": card["id"]}).status_code == 403
    assert client.post("/assistant/cancel", json={"id": card["id"]}).status_code == 403
    assert "mine.example" not in seeded_db.list_positive_domains()


def test_subject_switch_blocks_confirm(client, ai_on, monkeypatch, seeded_db):
    card = propose_domain(client, monkeypatch, "switch.example")
    pending.STORE.get(card["id"]).subject_id = 2
    assert client.post("/assistant/confirm", json={"id": card["id"]}).status_code == 409


def test_failed_action_is_audited(client, ai_on, monkeypatch, seeded_db):
    alert_id = seeded_db.add_alert("new_negative", "warning", "us", "msg", dedupe_key="k1")
    script(monkeypatch, call("acknowledge_alert", alert_id=alert_id), say("ok"))
    card = next(e for e in chat(client, "ack it").json()["events"] if e["type"] == "confirm")
    # the alert disappears before the user confirms
    monkeypatch.setattr(seeded_db.__class__, "get_alert", lambda self, i: None)
    body = client.post("/assistant/confirm", json={"id": card["id"]}).json()
    assert body["ok"] is False and "Couldn't do it" in body["message"]
    params = json.loads(audit_rows(seeded_db, "assistant_action")[0]["params_json"])
    assert params["ok"] is False


# --------------------------------------------------------------------- #
# Roles (auth enabled)
# --------------------------------------------------------------------- #
def _login(client, seeded_db, username, role):
    seeded_db.add_user(username, f"{username}-password", role=role)
    resp = client.post("/login", data={"username": username, "password": f"{username}-password"},
                       follow_redirects=False)
    sid = resp.cookies["sid"]
    client.cookies.clear()
    client.cookies.set("sid", sid)
    return sid


def test_viewer_can_chat_but_not_act(client, ai_on, monkeypatch, seeded_db):
    sid = _login(client, seeded_db, "vera", "client_viewer")
    model = script(monkeypatch, call("add_domain", domain="v.example", list="positive"),
                   say("An operator has to do that."))
    resp = chat(client, "add v.example")
    assert resp.status_code == 200, resp.text[:300]
    assert not [e for e in resp.json()["events"] if e["type"] == "confirm"]
    assert "add_domain" not in model.calls[0]["specs"]
    assert client.get("/assistant/status").json()["can_act"] is False
    # a pending action somehow bound to this session still needs operator+
    from starlette.requests import Request

    from assistant.router import session_key
    key, _ = session_key(Request({"type": "http", "headers": [
        (b"cookie", f"sid={sid}".encode())]}))
    forged = pending.STORE.add(tool="add_domain", args={"domain": "v.example", "list": "positive"},
                               session_key=key, user="vera", subject_id=1, title="t", details=[],
                               cost_note="")
    assert client.post("/assistant/confirm", json={"id": forged.id}).status_code == 403
    assert client.post("/assistant/cancel", json={"id": forged.id}).status_code == 403
    assert "v.example" not in seeded_db.list_positive_domains()


def test_operator_can_confirm(client, ai_on, monkeypatch, seeded_db):
    _login(client, seeded_db, "otto", "operator")
    card = propose_domain(client, monkeypatch, "otto.example")
    assert client.post("/assistant/confirm", json={"id": card["id"]}).json()["ok"] is True
    row = audit_rows(seeded_db, "assistant_action")[0]
    assert row["actor_id"] == "otto"


# --------------------------------------------------------------------- #
# Paid / file-producing actions
# --------------------------------------------------------------------- #
class _FakeRunner:
    def __init__(self):
        self.calls = []

    def search(self, queries, country_code, language_code, max_pages, engines=None,
               device=None, on_engine=None):
        self.calls.append(country_code)
        run = {"engine": "google", "device": "desktop", "run_id": f"asst-{country_code}",
               "status": "SUCCEEDED", "country_code": country_code, "queries": queries,
               "results": [], "features": [], "cost_usd": None, "compute_units": None,
               "retries": 0, "error": None}
        return {"run_id": run["run_id"], "status": "SUCCEEDED", "country_code": country_code,
                "queries": queries, "results": [], "features": [], "cost_usd": None,
                "compute_units": None, "retries": 0, "engine_runs": [run], "warnings": []}


def test_start_search_needs_confirm_then_runs_job(client, ai_on, monkeypatch, seeded_db):
    import dashboard.main as main_module
    monkeypatch.setenv("APIFY_API_KEY", "apify_test_key_ABCDEF")
    seeded_db.set_tracked_subject(query="Jordan Ashworth")
    runner = _FakeRunner()
    monkeypatch.setattr(main_module, "get_runner", lambda: runner)
    script(monkeypatch, call("start_search", countries=["us", "gb"]), say("Ready when you are."))
    body = chat(client, "run a search").json()
    card = next(e for e in body["events"] if e["type"] == "confirm")
    assert card["title"] == "Run this search now?" and "$" in card["cost_note"]
    assert "Countries: US, GB" in card["details"]
    assert runner.calls == []                                   # nothing ran yet
    done = client.post("/assistant/confirm", json={"id": card["id"]}).json()
    assert done["ok"] is True and done["events"][0]["url"].startswith("/job/")
    assert done["events"][0]["quiet"] is True                   # no auto-opened panel...
    job_card = next(e for e in done["events"] if e["type"] == "card")
    assert job_card["kind"] == "job"                            # ...a live job card instead
    assert job_card["data"]["job_id"] == done["result"]["job_id"]
    job = _wait_for_job(main_module, done["result"]["job_id"])
    assert job["status"] == "done" and sorted(runner.calls) == ["gb", "us"]
    assert job["meta"]["via"] == "assistant"
    assert job["meta"]["subject_id"] == 1 and job["meta"]["started_by"] == "local"
    assert json.loads(audit_rows(seeded_db, "assistant_action")[0]["params_json"])["tool"] == "start_search"


def test_create_report_csv_and_download(client, ai_on, monkeypatch, seeded_db, tmp_path):
    from dashboard import deps
    monkeypatch.setattr(deps, "exports_dir", tmp_path / "exports")
    script(monkeypatch, call("create_report", kind="csv", period="all"), say("Confirm above."))
    card = next(e for e in chat(client, "export csv").json()["events"] if e["type"] == "confirm")
    assert not (tmp_path / "exports").exists()
    done = client.post("/assistant/confirm", json={"id": card["id"]}).json()
    assert done["ok"] is True
    url = done["result"]["download_url"]
    resp = client.get(url)
    assert resp.status_code == 200 and "scam.example" in resp.text
    assert client.get("/assistant/reports/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert client.get("/assistant/reports/missing.pdf").status_code == 404


# --------------------------------------------------------------------- #
# Suggestions + panel
# --------------------------------------------------------------------- #
def test_suggestions_and_panel(client, ai_on, seeded_db):
    chips = client.get("/assistant/suggestions").json()["suggestions"]
    assert chips and all({"id", "label", "prompt"} <= set(c) for c in chips)
    assert "search" in {c["id"] for c in chips}               # admin (auth off) can act
    html = client.get("/assistant/panel").text
    for needle in ("id='assistant-panel'", "data-assistant ", "data-chat-url='/assistant/chat'",
                   "data-confirm-url='/assistant/confirm'", "id='assistant-transcript'",
                   "data-assistant-chip", "id='assistant-composer'", "data-greeting",
                   "id='assistant-confirm-template'", "data-enabled='true'"):
        assert needle in html, needle
    assert "Hi. I can see" in html


# --------------------------------------------------------------------- #
# Context, cards, follow-ups, progress/stop, editable confirmations
# --------------------------------------------------------------------- #
def test_chat_sends_page_context_to_the_model(client, ai_on, monkeypatch, seeded_db):
    rid = seeded_db.get_results_filtered(limit=1)[0]["id"]
    model = script(monkeypatch, call("explain_result", result_id=rid), say("It is neutral because…"))
    ctx = {"page": "/results", "title": "Results",
           "result": {"id": rid, "title": "Some <b>title</b>", "domain": "x.example", "label": "negative"},
           "filters": {"country": "us", "classification": "negative"}, "bogus": "ignore me"}
    resp = client.post("/assistant/chat", json={"messages": [{"role": "user", "content": "why is this one negative?"}],
                                                "context": ctx})
    assert resp.status_code == 200
    system = model.calls[0]["system"]
    assert "Current screen" in system and f'"result_id": {rid}' in system
    assert '"classification": "negative"' in system and "ignore me" not in system
    body = resp.json()
    cards = [e for e in body["events"] if e["type"] == "card"]
    assert cards and cards[0]["kind"] == "results" and cards[0]["data"]["items"][0]["result_id"] == rid
    assert 1 <= len(body["followups"]) <= 3 and body["progress"][0] == "Thinking…"


def test_chat_card_events_come_from_tool_results(client, ai_on, monkeypatch, seeded_db):
    script(monkeypatch, call("get_overview"), say("Here's the state."))
    body = chat(client, "how are we doing?").json()
    kinds = [e["kind"] for e in body["events"] if e["type"] == "card"]
    assert kinds == ["sov"]
    script(monkeypatch, say("No tools needed."))
    body = chat(client, "hello").json()
    assert not [e for e in body["events"] if e["type"] == "card"]


def test_progress_and_stop(client, ai_on, monkeypatch, seeded_db):
    turn = "turn123abc"
    seen = {}

    def peek(system, history, specs):
        seen["progress"] = client.get(f"/assistant/progress?turn={turn}").json()
        return say("done")

    script(monkeypatch, peek)
    client.get("/assistant/panel")                  # sets the per-browser assistant session
    resp = client.post("/assistant/chat", json={"messages": [{"role": "user", "content": "hi"}], "turn": turn})
    assert resp.status_code == 200
    assert seen["progress"]["step"] == "Thinking…" and seen["progress"]["done"] is False
    assert client.get(f"/assistant/progress?turn={turn}").json()["done"] is True
    # Stop: the turn ends before its next model call; nothing is proposed.
    turn2 = "turn456def"

    def stop_then_tool(system, history, specs):
        assert client.post("/assistant/stop", json={"turn": turn2}).json()["ok"] is True
        return call("add_domain", domain="stop.example", list="positive")

    script(monkeypatch, stop_then_tool, say("never"))
    body = client.post("/assistant/chat", json={"messages": [{"role": "user", "content": "add"}],
                                                "turn": turn2}).json()
    assert body["stopped"] is True and not [e for e in body["events"] if e["type"] == "confirm"]
    assert client.post("/assistant/stop", json={"turn": "x"}).status_code == 400


def _propose(client, monkeypatch, *turns):
    script(monkeypatch, *turns)
    return next(e for e in chat(client, "do it").json()["events"] if e["type"] == "confirm")


def test_confirm_with_edited_args(client, ai_on, monkeypatch, seeded_db, tmp_path):
    from dashboard import deps
    monkeypatch.setattr(deps, "exports_dir", tmp_path / "exports")
    card = _propose(client, monkeypatch, call("create_report", kind="pdf"), say("Confirm above."))
    fields = {f["name"]: f for f in card["edit"]["fields"]}
    assert {o["value"] for o in fields["kind"]["options"]} == {"pdf", "xlsx", "csv"}
    done = client.post("/assistant/confirm", json={"id": card["id"], "args": {"kind": "csv", "period": "all"}}).json()
    assert done["ok"] is True and done["edited"] is True and done["result"]["file"].endswith(".csv")
    assert any(e["type"] == "card" and e["kind"] == "report" for e in done["events"])
    params = json.loads(audit_rows(seeded_db, "assistant_action")[0]["params_json"])
    assert params["args"]["kind"] == "csv" and params["edited"] is True
    # save_note: the edited text is what gets saved
    card = _propose(client, monkeypatch, call("save_note", url="https://scam.example/x", text="Draft"),
                    say("Confirm above."))
    assert card["edit"]["fields"][0]["value"] == "Draft"
    done = client.post("/assistant/confirm", json={"id": card["id"], "args": {"text": "My edited request"}}).json()
    assert done["ok"] is True
    assert next(n for n in seeded_db.list_notes() if n["url"] == "https://scam.example/x")["note"] == "My edited request"


@pytest.mark.parametrize("edits,msg", [
    ({"engines": ["yandex"]}, "Not allowed"),
    ({"countries": ["zz"]}, "Not allowed"),
    ({"countries": []}, "at least one"),
    ({"pages": 3}, "can't be changed"),
    ("nope", "object"),
])
def test_confirm_rejects_disallowed_edits_and_keeps_it_pending(client, ai_on, monkeypatch, seeded_db,
                                                              edits, msg):
    monkeypatch.setenv("APIFY_API_KEY", "apify_test_key_ABCDEF")
    seeded_db.set_tracked_subject(query="Jordan Ashworth")
    card = _propose(client, monkeypatch, call("start_search"), say("Ready."))
    resp = client.post("/assistant/confirm", json={"id": card["id"], "args": edits})
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_args"
    assert msg in resp.json()["message"]
    assert pending.STORE.get(card["id"]).status == "pending"
    assert audit_rows(seeded_db, "assistant_action") == []


def test_viewer_cannot_confirm_edited_args(client, ai_on, monkeypatch, seeded_db):
    sid = _login(client, seeded_db, "vic", "client_viewer")
    from starlette.requests import Request

    from assistant.router import session_key
    key, _ = session_key(Request({"type": "http", "headers": [(b"cookie", f"sid={sid}".encode())]}))
    forged = pending.STORE.add(tool="create_report", args={"kind": "pdf", "period": "latest", "countries": []},
                               session_key=key, user="vic", subject_id=1, title="t", details=[], cost_note="",
                               edit={"fields": []})
    resp = client.post("/assistant/confirm", json={"id": forged.id, "args": {"kind": "csv"}})
    assert resp.status_code == 403 and pending.STORE.get(forged.id).status == "pending"


def test_review_card_verdict_uses_the_audited_endpoint(client, seeded_db):
    rid = seeded_db.get_results_filtered(limit=1)[0]["id"]
    resp = client.post(f"/review/{rid}/verdict", data={"classification": "negative", "mode": "chat"})
    assert resp.status_code == 200 and resp.json()["ok"] is True and "negative" in resp.json()["message"]
    assert audit_rows(seeded_db, "review_verdict")
    resp = client.post("/review/999999/verdict", data={"classification": "negative", "mode": "chat"})
    assert resp.status_code == 404 and resp.json()["ok"] is False


def test_status_and_panel_show_the_reasoning_hint(client, ai_on, monkeypatch, seeded_db):
    from processing import ai_status, llm
    ai_status.record(False, llm.EMPTY_ANSWER, force=True, db=seeded_db)
    st = client.get("/assistant/status").json()
    assert st["health"]["ok"] is False and st["health"]["hint"] == llm.REASONING_HINT
    html = client.get("/assistant/panel").text
    assert "data-assistant-health" in html and "Reasoning models are supported" in html
    script(monkeypatch, harness.ModelError(llm.EMPTY_ANSWER))
    body = chat(client, "hi").json()
    assert body["error"] == llm.EMPTY_ANSWER and body["hint"] == llm.REASONING_HINT


def test_panel_has_assistant_controls(client, ai_on, seeded_db):
    html = client.get("/assistant/panel").text
    for needle in ("data-assistant-clear", "data-assistant-minimize", "data-assistant-close",
                   "data-assistant-stop", "data-progress-url='/assistant/progress'",
                   "data-stop-url='/assistant/stop'"):
        assert needle in html, needle
    assert "data-sm-close" not in html          # not an SM overlay any more
