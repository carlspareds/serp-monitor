"""Live test with a REASONING model (OpenCode Go, deepseek-v4-flash via the
OpenAI-compatible API) -- regressions for what it exposed:

1. empty content + finish_reason=length is a failure (never ok + marker),
   reasoning-safe output budgets, one doubled retry, max_completion_tokens
   and reasoning_effort fallbacks (remembered per server+model);
2. "database is locked" in the AI relabel (write transaction held open
   while the AI layer wrote on a second connection);
3. honest AI relabel counts;
4. a short AI reason stored with AI labels and shown by explain_row;
5. "Run AI checks" (same-name + severity) on stored results;
6. a failed AI summary never reaches the PDF;
7. the assistant answers after explain_result; acknowledged alerts are not
   counted as open.

No real network: requests.post is always replaced by a fake.
"""
import sqlite3

import pytest
import requests

from database.models import Database
from processing import ai_classifier, ai_status, llm

_LLM_VARS = ("LLM_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL", "OPENAI_API_KEY",
             "OPENAI_BASE_URL", "OPENAI_MODEL", "AI_REASONING_EFFORT")
OPENCODE = "https://opencode.ai/zen/go/v1"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _LLM_VARS:
        monkeypatch.delenv(var, raising=False)
    yield
    ai_classifier.set_cache_db(None)


class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def chat(content, finish="stop", tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return FakeResp(200, {"choices": [{"message": msg, "finish_reason": finish}]})


def bad_request(message):
    import json
    return FakeResp(400, text=json.dumps({"error": {"type": "invalid_request_error",
                                                    "message": message}}))


@pytest.fixture
def posts(monkeypatch):
    """requests.post replaced: answers from `posts.queue` in order (the
    last one repeats); every request is recorded."""
    class Box(list):
        queue: list

    box = Box()
    box.queue = []

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        box.append({"url": url, "json": dict(json or {}), "headers": headers or {}})
        if not box.queue:
            raise AssertionError("unexpected request")
        item = box.queue.pop(0) if len(box.queue) > 1 else box.queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(requests, "post", fake_post)
    return box


@pytest.fixture
def opencode(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", OPENCODE)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key-123456")
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-v4-flash")


def _tokens(call):
    return call["json"].get("max_tokens", call["json"].get("max_completion_tokens"))


# --------------------------------------------------------------------- #
# 1a/1c. Empty content is a failure; one doubled retry on length
# --------------------------------------------------------------------- #
def test_empty_content_with_length_retries_once_then_fails(opencode, posts, tmp_path):
    db = Database(db_path=str(tmp_path / "s.db"))
    ai_classifier.set_cache_db(db)
    posts.queue = [chat("", finish="length")]
    ok, out = ai_classifier._call_llm("x", max_tokens=1024)
    assert ok is False
    assert out == llm.EMPTY_ANSWER
    assert "truncated" not in out
    assert [_tokens(c) for c in posts] == [1024, 2048]      # one retry, doubled
    status = ai_status.status(db)
    assert status["ok"] is False and "returned no answer" in status["reason"]


def test_retry_with_bigger_budget_can_succeed(opencode, posts):
    posts.queue = [chat(None, finish="length"), chat("Positive")]
    assert ai_classifier._call_llm("x", max_tokens=1024) == (True, "positive")
    assert [_tokens(c) for c in posts] == [1024, 2048]


def test_retry_budget_is_capped(opencode, posts):
    posts.queue = [chat("", finish="length")]
    ok, _ = llm.call_openai("x", max_tokens=6000)
    assert not ok
    assert [_tokens(c) for c in posts] == [6000, llm.MAX_OUTPUT_TOKENS_CAP]
    posts.clear()
    llm.call_openai("x", max_tokens=llm.MAX_OUTPUT_TOKENS_CAP)
    assert len(posts) == 1                                   # already at the cap: no retry


def test_empty_content_with_stop_fails_without_retry(opencode, posts):
    posts.queue = [chat("   ")]
    assert ai_classifier._call_llm("x") == (False, llm.EMPTY_ANSWER)
    assert len(posts) == 1


def test_truncated_text_never_carries_a_marker(opencode, posts):
    posts.queue = [chat("Partial", finish="length")]
    ok, out = ai_classifier._call_llm("x", max_tokens=1024, lower=False)
    assert (ok, out) == (True, "Partial")


def test_gemini_max_tokens_without_parts_retries_then_fails(monkeypatch, posts):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    posts.queue = [FakeResp(200, {"candidates": [{"content": {"role": "model"},
                                                  "finishReason": "MAX_TOKENS"}]})]
    ok, out = ai_classifier._call_llm("x", max_tokens=1024)
    assert (ok, out) == (False, llm.EMPTY_ANSWER)
    assert [c["json"]["generationConfig"]["maxOutputTokens"] for c in posts] == [1024, 2048]


def test_gemini_ignores_thought_parts(monkeypatch, posts):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    posts.queue = [FakeResp(200, {"candidates": [{"content": {"parts": [
        {"text": "thinking...", "thought": True}, {"text": "Neutral"}]}, "finishReason": "STOP"}]})]
    assert ai_classifier._call_llm("x") == (True, "neutral")


# --------------------------------------------------------------------- #
# 1b. Reasoning-safe budgets for every caller
# --------------------------------------------------------------------- #
def test_every_ai_caller_asks_for_a_reasoning_safe_budget(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    seen = []

    def fake(prompt, max_tokens=10, temperature=0.0, lower=True):
        seen.append(max_tokens)
        return True, '{"label": "neutral", "confidence": 0.9, "severity": 2, "topic": "legal"}'

    monkeypatch.setattr(ai_classifier, "_call_gemini", fake)
    rec = {"url": "https://x.example/a", "title": "t", "description": "d"}
    ai_classifier.ping()
    ai_classifier.classify_with_ai_confidence(rec)
    ai_classifier.analyze_negative(rec)
    ai_classifier.is_same_person(rec, "ctx")
    ai_classifier.is_same_person_batch([rec], "ctx")
    ai_classifier.executive_summary("data")
    assert len(seen) == 6
    assert min(seen[:4]) >= llm.SHORT_OUTPUT_TOKENS
    assert seen[4] >= llm.SHORT_OUTPUT_TOKENS
    assert seen[5] >= llm.LONG_OUTPUT_TOKENS


def test_assistant_budget_is_reasoning_safe():
    from assistant import harness
    assert harness.MAX_OUTPUT_TOKENS >= llm.LONG_OUTPUT_TOKENS


# --------------------------------------------------------------------- #
# 1b. max_completion_tokens + reasoning_effort fallbacks
# --------------------------------------------------------------------- #
def test_max_completion_tokens_fallback_is_remembered(opencode, posts, monkeypatch):
    monkeypatch.setenv("AI_REASONING_EFFORT", "off")
    posts.queue = [bad_request("Unsupported parameter: 'max_tokens' is not supported with this "
                               "model. Use 'max_completion_tokens' instead."), chat("ok")]
    assert llm.call_openai("hi") == (True, "ok")
    assert "max_tokens" in posts[0]["json"]
    assert "max_tokens" not in posts[1]["json"] and posts[1]["json"]["max_completion_tokens"] == llm.SHORT_OUTPUT_TOKENS
    posts.clear()
    posts.queue = [chat("ok")]
    llm.call_openai("again", max_tokens=33)
    [call] = posts                                           # remembered: one request
    assert call["json"]["max_completion_tokens"] == 33 and "max_tokens" not in call["json"]


def test_max_tokens_range_error_is_not_a_param_switch(opencode, posts, monkeypatch):
    monkeypatch.setenv("AI_REASONING_EFFORT", "off")
    posts.queue = [bad_request("Invalid max_tokens value, the valid range is [1, 8192]")]
    ok, out = llm.call_openai("hi", max_tokens=9999)
    assert not ok and "HTTP 400" in out
    assert len(posts) == 1


def test_reasoning_effort_sent_then_dropped_and_remembered(opencode, posts):
    posts.queue = [bad_request("Unrecognized request argument supplied: reasoning_effort"), chat("ok")]
    assert llm.call_openai("hi") == (True, "ok")
    assert posts[0]["json"]["reasoning_effort"] == "low"
    assert "reasoning_effort" not in posts[1]["json"]
    posts.clear()
    posts.queue = [chat("ok")]
    llm.call_openai("again")
    [call] = posts
    assert "reasoning_effort" not in call["json"]


def test_reasoning_effort_not_remembered_when_the_400_was_about_something_else(opencode, posts):
    posts.queue = [bad_request("messages: field required")]
    ok, _ = llm.call_openai("hi")
    assert not ok
    assert len(posts) == 2 and "reasoning_effort" not in posts[1]["json"]
    posts.clear()
    llm.call_openai("again")
    assert posts[0]["json"]["reasoning_effort"] == "low"     # still tried next time


def test_reasoning_effort_can_be_turned_off_or_changed(opencode, posts, monkeypatch):
    posts.queue = [chat("ok")]
    monkeypatch.setenv("AI_REASONING_EFFORT", "off")
    llm.call_openai("hi")
    monkeypatch.setenv("AI_REASONING_EFFORT", "medium")
    llm.call_openai("hi")
    assert "reasoning_effort" not in posts[0]["json"]
    assert posts[1]["json"]["reasoning_effort"] == "medium"


# --------------------------------------------------------------------- #
# 1d. ping succeeds only with real text
# --------------------------------------------------------------------- #
def test_ping_fails_on_empty_answer(opencode, posts):
    posts.queue = [chat("", finish="length")]
    ok, msg = ai_classifier.ping()
    assert ok is False and msg == llm.EMPTY_ANSWER
    assert _tokens(posts[0]) >= llm.SHORT_OUTPUT_TOKENS


def test_ping_succeeds_with_text(opencode, posts):
    posts.queue = [chat("ok")]
    ok, msg = ai_classifier.ping()
    assert ok and "deepseek-v4-flash" in msg


# --------------------------------------------------------------------- #
# Assistant adapters: empty/length + request fallbacks
# --------------------------------------------------------------------- #
def test_assistant_openai_empty_length_retries_then_model_error(opencode, posts):
    from assistant.harness import ModelError, OpenAIChat
    posts.queue = [chat(None, finish="length")]
    model = OpenAIChat(OPENCODE, "deepseek-v4-flash", "k", max_tokens=2048)
    with pytest.raises(ModelError, match="returned no answer"):
        model.complete("sys", [{"role": "user", "content": "hi"}], [])
    assert [_tokens(c) for c in posts] == [2048, 4096]


def test_assistant_openai_retry_success_and_effort_fallback(opencode, posts):
    from assistant.harness import OpenAIChat
    posts.queue = [bad_request("reasoning_effort is not supported"), chat("", finish="length"),
                   chat("Here is the answer.")]
    model = OpenAIChat(OPENCODE, "deepseek-v4-flash", "k", max_tokens=2048)
    turn = model.complete("sys", [{"role": "user", "content": "hi"}], [])
    assert turn.text == "Here is the answer."
    assert "reasoning_effort" in posts[0]["json"]
    assert all("reasoning_effort" not in c["json"] for c in posts[1:])


def test_assistant_openai_tools_400_still_switches_to_json_mode(opencode, posts):
    """A 400 about tools isn't mistaken for a reasoning_effort problem."""
    from assistant.harness import _JSON_MODE_IDENTS, AutoModel, OpenAIChat
    _JSON_MODE_IDENTS.clear()
    posts.queue = [bad_request("tools are not supported for this model"), chat('{"reply": "ok"}')]
    model = AutoModel(OpenAIChat(OPENCODE, "m-tools", "k"))
    turn = model.complete("sys", [{"role": "user", "content": "hi"}],
                          [{"name": "x", "description": "d", "parameters": {"type": "object"}}])
    assert turn.text == "ok"
    assert "tools" in posts[0]["json"] and "tools" not in posts[1]["json"]
    _JSON_MODE_IDENTS.clear()


def test_assistant_gemini_max_tokens_retry(monkeypatch, posts):
    from assistant.harness import GeminiChat, ModelError
    posts.queue = [FakeResp(200, {"candidates": [{"content": {}, "finishReason": "MAX_TOKENS"}]})]
    model = GeminiChat("gemini-2.5-flash", "gk-123456", max_tokens=2048)
    with pytest.raises(ModelError, match="returned no answer"):
        model.complete("sys", [{"role": "user", "content": "hi"}], [])
    assert [c["json"]["generationConfig"]["maxOutputTokens"] for c in posts] == [2048, 4096]


# --------------------------------------------------------------------- #
# 2. "database is locked" in reclassify_all
# --------------------------------------------------------------------- #
def test_open_write_transaction_locks_a_second_connection(tmp_path):
    """The mechanism behind the bug: while one connection holds a write
    transaction, a write on a second connection fails with 'locked'."""
    db = Database(db_path=str(tmp_path / "lock.db"))
    a = sqlite3.connect(db.db_path)
    a.execute("PRAGMA busy_timeout=0")
    a.execute("UPDATE search_results SET title = title")
    a.execute("INSERT INTO settings (key, value) VALUES ('x', '1')")   # opens the write txn
    b = sqlite3.connect(db.db_path, timeout=0)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        b.execute("INSERT INTO settings (key, value) VALUES ('y', '1')")
    a.rollback()
    a.close()
    b.close()


def test_reclassify_all_does_not_hold_a_transaction_during_classify(seeded_db, monkeypatch):
    """classify_fn writes on ANOTHER connection (like the AI verdict cache
    and the AI status do). Before the fix the second row's write waited
    busy_timeout and failed with 'database is locked'."""
    real_connect = seeded_db._connect

    def fast_connect():
        conn = real_connect()
        conn.execute("PRAGMA busy_timeout=200")             # fail fast if it regresses
        return conn

    monkeypatch.setattr(seeded_db, "_connect", fast_connect)
    writes = []

    def classify_fn(rec):
        seeded_db.set_cached_verdict("h" + rec["url"], "classify", "v", {"verdict": "x"})
        seeded_db.set_setting("ai_provider_status", rec["url"])
        writes.append(rec["url"])
        return {"classification": "negative", "confidence": 0.9, "classified_by": "ai",
                "reason": "Mentions a lawsuit."}

    changed = seeded_db.reclassify_all(classify_fn, recompute_pending=True)
    assert len(writes) == 5
    assert changed == 3                                     # 2 positive + 1 neutral -> negative
    rows = seeded_db.get_results_filtered(limit=50, include_homonyms=True)
    assert {r["classification"] for r in rows} == {"negative"}
    assert {r["ai_reason"] for r in rows} == {"Mentions a lawsuit."}


def test_reclassify_all_never_overwrites_a_row_labelled_meanwhile(seeded_db):
    target = next(r for r in seeded_db.get_results_filtered(limit=50) if r["domain"] == "directory.example")

    def classify_fn(rec):
        if rec["url"] == target["url"]:
            seeded_db.set_review_verdict(target["id"], "positive")   # a person, mid-pass
        return {"classification": "negative", "confidence": 0.9, "classified_by": "ai"}

    seeded_db.reclassify_all(classify_fn)
    row = next(r for r in seeded_db.get_results_filtered(limit=50) if r["id"] == target["id"])
    assert (row["classification"], row["classified_by"]) == ("positive", "human")


# --------------------------------------------------------------------- #
# 3 + 4. AI relabel route: honest counts, stored reason
# --------------------------------------------------------------------- #
def _add_neutral_rows(db, n):
    rows = [{"query": "Jordan Ashworth", "url": f"https://misc{i}.example/p", "title": f"Page {i}",
             "description": "Something.", "position": 10 + i, "country_code": "us",
             "domain": f"misc{i}.example", "classification": "neutral", "confidence": 0.5,
             "classified_by": "rule"} for i in range(n)]
    db.save_results(rows, run_id="run-test-1")


def test_ai_relabel_reports_failures_honestly(client, seeded_db, monkeypatch):
    _add_neutral_rows(seeded_db, 3)                         # + directory.example = 4 neutrals
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    answers = iter([
        (True, '{"label": "negative", "confidence": 0.9, "reason": "Reports a fraud case."}'),
        (True, ""),                                          # reasoning model, empty answer
        (False, "The AI provider is rate limiting requests or the quota is used up (HTTP 429)."),
        (True, '{"label": "positive", "confidence": 0.8, "reason": "Official profile."}'),
    ])
    monkeypatch.setattr(ai_classifier, "_call_gemini", lambda *a, **k: next(answers))
    r = client.post("/domains/reclassify-ai", data={"next": "/settings#domains"})
    assert r.status_code == 200
    assert "Relabelled 2 of 4 results with AI" in r.text
    assert "2 calls failed:" in r.text
    import json
    [audit] = seeded_db.list_audit(action="domains_reclassify_ai")
    params = json.loads(audit["params_json"])
    assert params["ai_checked"] == 4 and params["ai_labelled"] == 2 and params["ai_failed"] == 2
    reasons = {r["ai_reason"] for r in seeded_db.get_results_filtered(limit=50, include_homonyms=True)
               if r["classified_by"] == "ai"}
    assert reasons == {"Reports a fraud case.", "Official profile."}


def test_classify_reason_is_short_and_old_cache_still_works(tmp_path, monkeypatch):
    db = Database(db_path=str(tmp_path / "c.db"))
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    long_reason = "Because " + "very " * 60 + "bad."
    monkeypatch.setattr(ai_classifier, "_call_gemini", lambda *a, **k: (
        True, '{"label": "Negative", "confidence": 0.7, "reason": "' + long_reason + '"}'))
    out = ai_classifier.classify_with_ai_confidence({"url": "https://a.example/x", "title": "t"})
    assert out["classification"] == "negative" and len(out["reason"]) <= 140
    # A verdict cached before reasons existed: no reason, no crash.
    version = ai_classifier._cache_version(ai_classifier._subject_version("classify"))
    from database.models import url_hash
    db.set_cached_verdict(url_hash("https://b.example/y"), "classify", version,
                          {"verdict": {"classification": "positive", "confidence": 0.9}})
    old = ai_classifier.classify_with_ai_confidence({"url": "https://b.example/y", "title": "t"})
    assert old == {"classification": "positive", "confidence": 0.9, "classified_by": "ai"}


def test_explain_row_shows_the_ai_reason(seeded_db):
    from assistant.tools import explain_row
    row = {"classification": "negative", "classified_by": "ai", "confidence": 0.8,
           "ai_reason": "Reports a fraud lawsuit."}
    assert "AI's reason: Reports a fraud lawsuit." in explain_row(seeded_db, row)["why"]
    row.pop("ai_reason")
    assert "reason" not in explain_row(seeded_db, row)["why"]


# --------------------------------------------------------------------- #
# 5. Run AI checks on stored results
# --------------------------------------------------------------------- #
def test_run_ai_checks_route_counts_and_audits(client, seeded_db, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")

    def batch(records, context=""):
        return ["no" if "directory" in (r.get("url") or "") else
                (None if "news.example" in (r.get("url") or "") else "yes") for r in records]

    monkeypatch.setattr(ai_classifier, "is_same_person_batch", batch)
    monkeypatch.setattr(ai_classifier, "analyze_negative", lambda r: (
        {"severity": 4, "topic": "legal"} if "scam" in r["url"] else {}))
    r = client.post("/ai/annotate", data={"next": "/settings#domains"})
    assert r.status_code == 200
    assert "Same-name: 4 of 5 results checked" in r.text
    assert "severity: 1 of 2 negatives rated" in r.text
    assert "2 could not be checked" in r.text
    rows = {x["domain"]: x for x in seeded_db.get_results_filtered(limit=50, include_homonyms=True)}
    assert rows["directory.example"]["same_person"] == "no"
    assert rows["news.example"]["same_person"] is None
    assert rows["scam.example"]["severity"] == 4 and rows["scam.example"]["topic"] == "legal"
    assert any(a["action"] == "ai_annotate" for a in seeded_db.list_audit())


def test_run_ai_checks_caps_and_skips_done_rows(seeded_db, monkeypatch):
    from processing import ai_checks
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    asked = []
    monkeypatch.setattr(ai_classifier, "is_same_person_batch",
                        lambda records, context="": (asked.extend(records), ["yes"] * len(records))[1])
    monkeypatch.setattr(ai_classifier, "analyze_negative", lambda r: {"severity": 2, "topic": "other"})
    res = ai_checks.run_ai_checks(seeded_db, limit=2)
    assert len(asked) == 2 and res["same_remaining"] == 3
    assert res["severity_checked"] == 2 and res["severity_set"] == 2
    msg, kind = ai_checks.summary_message(res)
    assert "more left" in msg and kind == "ok"
    asked.clear()
    res = ai_checks.run_ai_checks(seeded_db, limit=50)
    assert len(asked) == 3 and res["severity_checked"] == 0   # done rows are not re-asked


def test_run_ai_checks_needs_a_provider(client):
    r = client.post("/ai/annotate", data={"next": "/settings#domains"})
    assert "need an AI provider" in r.text


# --------------------------------------------------------------------- #
# 6. PDF: never an empty/failed AI summary
# --------------------------------------------------------------------- #
def _pdf_texts(monkeypatch):
    from export import pdf_report
    texts = []
    real = pdf_report.Paragraph

    def spy(text, *a, **k):
        texts.append(str(text))
        return real(text, *a, **k)

    monkeypatch.setattr(pdf_report, "Paragraph", spy)
    return texts


def test_pdf_omits_failed_ai_summary_and_adds_a_note(seeded_db, tmp_path, monkeypatch):
    from export.pdf_report import build_pdf
    texts = _pdf_texts(monkeypatch)
    build_pdf(seeded_db, str(tmp_path / "r.pdf"), ai_summary="",
              ai_summary_note=llm.EMPTY_ANSWER)
    assert not any("Executive summary (AI)" in t for t in texts)
    assert any(t.startswith("AI summary unavailable: The model returned no answer") for t in texts)


def test_pdf_strips_a_legacy_truncation_marker(seeded_db, tmp_path, monkeypatch):
    from export.pdf_report import build_pdf
    texts = _pdf_texts(monkeypatch)
    build_pdf(seeded_db, str(tmp_path / "r.pdf"),
              ai_summary="\n\n_(truncated: hit the output token limit)_")
    assert not any("Executive summary (AI)" in t or "truncated" in t for t in texts)


def test_export_pdf_route_with_reasoning_model_failure(client, monkeypatch):
    from export import pdf_report
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    monkeypatch.setattr(ai_classifier, "_call_gemini", lambda *a, **k: (True, "  "))
    seen = {}
    real = pdf_report.build_pdf

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)

    from dashboard.routers import export as export_router
    monkeypatch.setattr(export_router, "build_pdf", spy)
    r = client.get("/export/pdf")
    assert r.status_code == 200
    assert seen["ai_summary"] == "" and seen["ai_summary_note"] == llm.EMPTY_ANSWER


# --------------------------------------------------------------------- #
# 7. Assistant: answer after explain_result; open vs acknowledged alerts
# --------------------------------------------------------------------- #
def _ctx(db, role="operator"):
    from assistant import tools
    return tools.ToolContext(db=db.for_subject(1), subject_id=1, role=role, user="op")


def test_assistant_appends_explanation_after_navigation_only_reply(seeded_db):
    from assistant import harness
    from assistant.fakes import ScriptedModel, call, say
    from assistant.pending import PendingStore
    row = next(r for r in seeded_db.get_results_filtered(limit=50) if r["domain"] == "scam.example")
    model = ScriptedModel([call("explain_result", result_id=row["id"]), call("open_review"),
                           say("I've opened the review queue for you.")])
    res = harness.run_turn(model, _ctx(seeded_db), [{"role": "user", "content": "Why is it negative?"}],
                           session_key="s", store=PendingStore())
    assert res.reply.startswith("I've opened the review queue for you.")
    assert "is labelled negative" in res.reply
    assert "negative list" in res.reply                     # the rule's plain reason


def test_assistant_keeps_a_real_answer_untouched(seeded_db):
    from assistant import harness
    from assistant.fakes import ScriptedModel, call, say
    from assistant.pending import PendingStore
    row = next(r for r in seeded_db.get_results_filtered(limit=50) if r["domain"] == "scam.example")
    answer = "It's negative because scam.example is on your negative list."
    model = ScriptedModel([call("explain_result", result_id=row["id"]), say(answer)])
    res = harness.run_turn(model, _ctx(seeded_db), [{"role": "user", "content": "Why?"}],
                           session_key="s", store=PendingStore())
    assert res.reply == answer


def test_system_prompt_tells_the_model_to_answer_from_read_tools():
    from assistant import prompts
    assert "ANSWER THE QUESTION" in prompts.RULES and "explain_result" in prompts.RULES


def test_overview_and_greeting_do_not_count_acknowledged_alerts_as_open(seeded_db):
    from assistant import suggest, tools
    ids = [seeded_db.add_alert("new_negative", "warning", "us", f"Alert {i}", f"k{i}")
           for i in range(5)]
    seeded_db.ack_alert(ids[0])
    ctx = _ctx(seeded_db)
    ov = tools.get_overview(ctx, {})
    assert ov["open_alerts"]["count"] == 4 and ov["open_alerts"]["acknowledged"] == 1
    greeting = suggest.greeting(ctx, ov)
    assert "4 open alert(s), plus 1 acknowledged" in greeting
    assert "5 open" not in greeting
    chips = suggest.compute(ctx, ov)
    assert any(c["label"] == "Go through the 4 open alert(s)" for c in chips)
