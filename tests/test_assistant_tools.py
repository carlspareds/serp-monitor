"""Assistant tool registry (src/assistant/tools.py): schemas, read tools on
the seeded demo database, navigate intents, argument validation, subject
scoping, and that action tools' `prepare` never changes anything."""
import importlib.util
import json
import re
import shutil

import pytest
from conftest import _ROOT

from assistant import harness, tools
from database.models import Database, subject_scope

EXPECTED = {
    "read": {"get_overview", "list_changes", "list_review_queue", "explain_result", "list_alerts",
             "list_runs", "list_notes", "get_setup_status", "list_reports", "app_help",
             "list_results"},
    "navigate": {"open_review", "open_compare", "open_runs", "open_timeline", "open_evidence",
                 "open_domains", "open_reports", "open_notes", "open_alerts", "open_settings",
                 "open_result", "open_ai_checks", "open_search"},
    "action": {"start_search", "create_report", "save_note", "set_result_label",
               "acknowledge_alert", "add_domain"},
}
_TYPES = {"string", "integer", "boolean", "array", "object", "number"}


def _load_seed():
    spec = importlib.util.spec_from_file_location("seed_demo_data", _ROOT / "scripts" / "seed_demo_data.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("demo") / "demo.db")
    seed = _load_seed()
    root = Database(db_path=path)
    sid = seed.demo_subject_id(root)
    with subject_scope(sid):
        seed._seed(root.for_subject(sid), path, sid)
    return path, sid


@pytest.fixture
def demo(demo_template, tmp_path):
    src, sid = demo_template
    dst = str(tmp_path / "demo.db")
    for suffix in ("", "-wal", "-shm"):
        try:
            shutil.copy(src + suffix, dst + suffix)
        except FileNotFoundError:
            pass
    db = Database(db_path=dst).for_subject(sid)
    return tools.ToolContext(db=db, subject_id=sid, role="admin", user="tester",
                             exports_dir=tmp_path / "exports")


def _run(ctx, name, **args):
    tool = tools.get_tool(name)
    return tool.executor(ctx, tools.validate_args(tool, args))


# --------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------- #
def test_registry_has_exactly_the_specified_tools():
    by_kind = {k: {t.name for t in tools.TOOLS.values() if t.kind == k} for k in tools.KINDS}
    assert by_kind == EXPECTED


def _check_schema(schema, where):
    assert schema.get("type") in _TYPES, where
    if schema["type"] == "array":
        _check_schema(schema["items"], where + "[]")
    if "enum" in schema:
        assert schema["enum"] and all(isinstance(v, str) for v in schema["enum"]), where


@pytest.mark.parametrize("name", sorted(tools.TOOLS))
def test_tool_schema_is_valid(name):
    t = tools.TOOLS[name]
    assert re.fullmatch(r"[a-z][a-z_]{2,40}", t.name)
    assert len(t.description) > 15
    p = t.parameters
    assert p["type"] == "object" and p["additionalProperties"] is False
    assert set(p["required"]) <= set(p["properties"])
    for key, sub in p["properties"].items():
        _check_schema(sub, f"{name}.{key}")
    json.dumps(t.spec())                                   # serialisable
    g = harness.gemini_schema(p)
    assert "additionalProperties" not in json.dumps(g)
    if t.kind == "action":
        assert t.requires_confirmation and t.prepare is not None
        assert tools.ROLE_RANK[t.min_role] >= tools.ROLE_RANK["operator"]
    else:
        assert not t.requires_confirmation


def test_viewer_specs_exclude_actions_and_admin_tools():
    names = {s["name"] for s in tools.tool_specs("viewer")}
    assert not names & EXPECTED["action"]
    assert "open_settings" not in names
    assert {s["name"] for s in tools.tool_specs("admin")} == set(tools.TOOLS)


def test_validate_args_coerces_and_rejects():
    t = tools.get_tool("list_review_queue")
    assert tools.validate_args(t, {"limit": "500", "junk": 1}) == {"limit": 25}
    t = tools.get_tool("add_domain")
    with pytest.raises(tools.ToolError, match="Missing"):
        tools.validate_args(t, {"domain": "x.com"})
    with pytest.raises(tools.ToolError, match="one of"):
        tools.validate_args(t, {"domain": "x.com", "list": "grey"})
    assert tools.validate_args(t, {"domain": "x.com", "list": "Positive"})["list"] == "positive"
    t = tools.get_tool("start_search")
    assert tools.validate_args(t, {"countries": "us, gb"})["countries"] == ["us", "gb"]
    with pytest.raises(tools.ToolError):
        tools.validate_args(tools.get_tool("open_result"), {"result_id": "abc"})


@pytest.mark.parametrize("raw,expected", [
    ("Example.COM", "example.com"), ("https://www.example.com/path?x=1", "www.example.com"),
    ("blog.example.co.uk.", "blog.example.co.uk"), ("user@example.org:8080", "example.org"),
])
def test_clean_domain(raw, expected):
    assert tools.clean_domain(raw) == expected


@pytest.mark.parametrize("raw", ["", "not a domain", "-bad.com", "localhost", "a" * 70 + ".com"])
def test_clean_domain_rejects(raw):
    with pytest.raises(tools.ToolError):
        tools.clean_domain(raw)


# --------------------------------------------------------------------- #
# Read tools on the demo DB
# --------------------------------------------------------------------- #
def test_get_overview(demo):
    out = _run(demo, "get_overview")
    db = demo.db
    assert out["subject"]["name"] == "Jordan Ashworth (demo)"
    assert out["kpis"]["total_runs"] == 4
    us = next(c for c in out["latest_by_country"] if c["country"] == "US")
    assert us["latest_run"]["run_id"] == "demo-run-4"
    assert us["previous_run"]["run_id"] == "demo-run-3"
    assert "forum.example" in [e["domain"] for e in us["entered"]]
    assert out["pending_review"] == db.count_pending_review() > 0
    assert out["open_alerts"]["count"] == len(db.open_alerts())
    assert out["wins_90d"]["count"] == 1
    assert any("need review" in a for a in out["attention"])
    assert any("forum.example" in a for a in out["attention"])


def test_list_changes_default_is_latest_pair(demo):
    out = _run(demo, "list_changes")
    assert (out["run_a"]["run_id"], out["run_b"]["run_id"]) == ("demo-run-3", "demo-run-4")
    assert [e["domain"] for e in out["entered"]] == ["forum.example", "cityscene-vancouver.example"]
    moved = {m["domain"]: m["places"] for m in out["moved"]}
    assert moved["scam.example"] == -2
    assert out["compare_url"] == "/results?modal=/ui/compare%3Fa%3Ddemo-run-3%26b%3Ddemo-run-4"
    assert tools.get_tool("list_changes").summary({}, out).startswith("Used: compare runs ")


def test_list_changes_resolves_and_orders_runs(demo):
    out = _run(demo, "list_changes", run_b="demo-run-2")
    assert out["run_a"]["run_id"] == "demo-run-1"
    out = _run(demo, "list_changes", run_a="demo-run-4", run_b="demo-run-3")
    assert (out["run_a"]["run_id"], out["run_b"]["run_id"]) == ("demo-run-3", "demo-run-4")
    with pytest.raises(tools.ToolError, match="does not exist"):
        _run(demo, "list_changes", run_a="nope")
    with pytest.raises(tools.ToolError, match="No runs"):
        _run(demo, "list_changes", country="de")


def test_list_review_queue(demo):
    out = _run(demo, "list_review_queue", limit=25)
    assert out["total_pending"] == demo.db.count_pending_review() == len(out["items"])
    assert all("no rule matched" in i["why"] for i in out["items"])
    assert {i["domain"] for i in out["items"]} >= {"directory.example", "forum.example"}


def test_explain_result_rules_and_human(demo):
    scam = _run(demo, "explain_result", url="https://scam.example/jordan-ashworth-fraud")
    assert scam["label"] == "negative" and "negative list" in scam["why"]
    assert scam["has_evidence"] is True
    assert scam["note"]["status"] in ("in_progress", "open")
    court = _run(demo, "explain_result", url="https://news.example/court/ashworth-dismissed")
    assert "keyword 'court'" in court["why"]
    rid = court["result"]["result_id"]
    demo.db.set_review_verdict(rid, "positive")
    human = _run(demo, "explain_result", result_id=rid)
    assert human["labelled_by"] == "human" and "A person" in human["why"]
    with pytest.raises(tools.ToolError):
        _run(demo, "explain_result", result_id=999999)
    with pytest.raises(tools.ToolError, match="result_id or a url"):
        _run(demo, "explain_result")


def test_list_alerts_runs_notes(demo):
    out = _run(demo, "list_alerts", status="all", limit=25)
    assert out["count"] == len(demo.db.list_alerts(limit=25, status="all")) > 0
    runs = _run(demo, "list_runs", limit=2)
    assert [r["run_id"] for r in runs["items"]] == ["demo-run-4", "demo-run-3"]
    notes = _run(demo, "list_notes")
    assert notes["count"] == 2
    assert _run(demo, "list_notes", status="resolved")["count"] == 1


def test_get_setup_status_has_no_secrets(demo, monkeypatch):
    monkeypatch.setenv("APIFY_API_KEY", "apify_api_SECRETVALUE_123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:SECRET-telegram-token")
    out = _run(demo, "get_setup_status")
    dumped = json.dumps(out)
    assert "SECRETVALUE" not in dumped and "SECRET-telegram" not in dumped
    assert out["apify"]["configured"] is True
    assert out["ai_provider"]["configured"] is False
    assert any("AI provider" in m for m in out["missing"])


def test_list_reports(demo):
    out_dir = demo.exports_dir / str(demo.subject_id)     # export.reports store
    out_dir.mkdir(parents=True)
    (out_dir / "executive_report_20260101_000000.pdf").write_bytes(b"%PDF")
    (out_dir / "notes.txt").write_text("x")
    out = _run(demo, "list_reports")
    assert [f["name"] for f in out["previous_files"]] == ["executive_report_20260101_000000.pdf"]
    assert out["previous_files"][0]["download_url"] == (
        f"/reports/{demo.subject_id}/executive_report_20260101_000000.pdf")
    assert {k["kind"] for k in out["kinds"]} == {"pdf", "xlsx", "csv"}


def test_app_help(demo):
    assert "Telegram setup" in [s["title"] for s in _run(demo, "app_help", topic="telegram")["sections"]]
    assert "Review queue" in _run(demo, "app_help")["topics"]
    assert _run(demo, "app_help", topic="zzqx")["found"] is False


# --------------------------------------------------------------------- #
# Navigate
# --------------------------------------------------------------------- #
# Fallback URLs are the deep links of the new UI (the old pages only redirect).
@pytest.mark.parametrize("name,url", [
    ("open_review", "/results?modal=/ui/review"), ("open_runs", "/results?panel=/ui/runs"),
    ("open_timeline", "/results?panel=/ui/timeline"), ("open_evidence", "/results?panel=/ui/evidence"),
    ("open_domains", "/settings#domains"), ("open_reports", "/results?modal=/ui/reports"),
    ("open_notes", "/results?panel=/ui/notes"), ("open_alerts", "/?alerts=1"),
    ("open_compare", "/results?modal=/ui/compare"), ("open_ai_checks", "/results?modal=/ui/ai-checks"),
    ("open_search", "/results?modal=/ui/search"),
])
def test_navigate_intents(demo, name, url):
    intent = _run(demo, name)["intent"]
    assert intent["url"] == url and intent["target"] and intent["label"]


def test_no_navigation_url_points_at_an_old_page(demo):
    """Every fallback URL is a page that renders (no redirect hop)."""
    old = ("/review", "/runs", "/timeline", "/evidence", "/domains", "/export", "/notes",
           "/alerts", "/diff", "/run/", "/settings/", "/users", "/audit")
    urls = [tools.URL_REVIEW, tools.URL_RUNS, tools.URL_TIMELINE, tools.URL_EVIDENCE,
            tools.URL_DOMAINS, tools.URL_REPORTS, tools.URL_NOTES, tools.URL_ALERTS,
            tools.URL_AI_CHECKS, tools.URL_SEARCH, *tools._SETTINGS_SECTIONS.values()]
    for url in urls:
        assert not url.startswith(old), url
        assert "/ui/domains" not in url, url


def test_navigate_intents_point_to_the_single_homes():
    """One home per feature: Domains in Settings → Projects and subjects (not a
    Results dialog); the result-acting AI tools, reports and search on Results."""
    assert tools.URL_DOMAINS == "/settings#domains"
    assert tools._SETTINGS_SECTIONS["domains"] == "/settings#domains"
    for url in (tools.URL_REVIEW, tools.URL_REPORTS, tools.URL_AI_CHECKS, tools.URL_SEARCH,
                tools.compare_url()):
        assert url.startswith("/results?modal=/ui/"), url
    assert tools.get_tool("open_search").min_role == "operator"


def test_navigate_with_params(demo):
    i = _run(demo, "open_compare", run_a="demo-run-1", run_b="demo-run-2")["intent"]
    assert i["url"] == "/results?modal=/ui/compare%3Fa%3Ddemo-run-1%26b%3Ddemo-run-2"
    with pytest.raises(tools.ToolError):
        _run(demo, "open_compare", run_a="nope")
    assert _run(demo, "open_settings", section="telegram")["intent"]["url"] == "/settings#notifications"
    rid = demo.db.pending_review(limit=1)[0]["id"]
    i = _run(demo, "open_result", result_id=rid)["intent"]
    assert i["params"]["result_id"] == rid and i["url"].startswith("/results?panel=/ui/run/demo-run-")
    with pytest.raises(tools.ToolError):
        _run(demo, "open_result", result_id=999999)


# --------------------------------------------------------------------- #
# Actions: prepare never mutates; scoping
# --------------------------------------------------------------------- #
def test_action_prepare_has_no_side_effects(demo):
    db = demo.db
    before = (db.list_positive_domains(), db.list_notes(), db.count_pending_review(),
              [a["status"] for a in db.list_alerts(status="all", limit=100)])
    rid = db.pending_review(limit=1)[0]["id"]
    alert = db.open_alerts()[0]
    preps = [
        tools.prepare_add_domain(demo, {"domain": "example.com", "list": "positive"}),
        tools.prepare_save_note(demo, {"url": "https://scam.example/x", "text": "Please remove."}),
        tools.prepare_set_result_label(demo, {"result_id": rid, "label": "negative"}),
        tools.prepare_acknowledge_alert(demo, {"alert_id": alert["id"]}),
        tools.prepare_create_report(demo, {"kind": "csv"}),
    ]
    for p in preps:
        assert p["title"].endswith("?") and p["cost_note"] and p["details"]
    after = (db.list_positive_domains(), db.list_notes(), db.count_pending_review(),
             [a["status"] for a in db.list_alerts(status="all", limit=100)])
    assert before == after
    assert not demo.exports_dir.exists()


def test_action_executors(demo):
    db = demo.db
    tools.exec_add_domain(demo, {"domain": "Example.com", "list": "positive"})
    assert "example.com" in db.list_positive_domains()
    with pytest.raises(tools.ToolError, match="already"):
        tools.prepare_add_domain(demo, {"domain": "example.com", "list": "positive"})
    tools.exec_save_note(demo, {"url": "https://forum.example/t/ashworth-refund",
                                "text": "Draft removal request", "status": "in_progress"})
    note = next(n for n in db.list_notes() if n["url"] == "https://forum.example/t/ashworth-refund")
    assert note["note"] == "Draft removal request" and note["status"] == "in_progress"
    rid = db.pending_review(limit=1)[0]["id"]
    out = tools.exec_set_result_label(demo, {"result_id": rid, "label": "neutral"})
    assert out["message"].startswith("Label set")
    alert = db.open_alerts()[0]
    tools.exec_acknowledge_alert(demo, {"alert_id": alert["id"]})
    assert db.get_alert(alert["id"])["status"] == "acknowledged"
    with pytest.raises(tools.ToolError, match="already acknowledged"):
        tools.prepare_acknowledge_alert(demo, {"alert_id": alert["id"]})
    rep = tools.exec_create_report(demo, {"kind": "csv", "period": "all"})
    assert (demo.exports_dir / str(demo.subject_id) / rep["file"]).read_text().count("\n") > 5


def test_start_search_refused_in_demo_mode(demo, monkeypatch):
    monkeypatch.delenv("APIFY_API_KEY", raising=False)
    with pytest.raises(tools.ToolError, match="Apify"):
        tools.prepare_start_search(demo, {})


def test_tools_are_scoped_to_the_active_subject(demo):
    root = Database(db_path=demo.db.db_path)
    other = root.add_subject("Someone Else", countries=["gb"])
    ctx = tools.ToolContext(db=root.for_subject(other), subject_id=other, role="admin")
    out = _run(ctx, "get_overview")
    assert out["kpis"]["total_runs"] == 0 and out["pending_review"] == 0
    rid = demo.db.pending_review(limit=1)[0]["id"]
    with pytest.raises(tools.ToolError):
        _run(ctx, "explain_result", result_id=rid)
    with pytest.raises(tools.ToolError, match="no runs"):
        _run(ctx, "list_changes")


def test_eval_cases_reference_real_tools():
    cases = json.loads((_ROOT / "evals" / "assistant" / "cases.json").read_text())["cases"]
    assert len(cases) >= 25 and len({c["id"] for c in cases}) == len(cases)
    for c in cases:
        exp = c["expect"]
        assert exp["type"] in ("tool", "propose", "refuse", "deny", "no_execute",
                               "claim_backed"), c["id"]
        for name in exp.get("tools_any", []) + ([exp["tool"]] if "tool" in exp else []):
            assert name in tools.TOOLS, (c["id"], name)
        if exp["type"] == "propose":
            assert tools.TOOLS[exp["tool"]].kind == "action"
        for step in c["script"]:
            assert ("tool" in step and step["tool"] in tools.TOOLS) or "reply" in step, c["id"]


# --------------------------------------------------------------------- #
# Editable confirmations, cards, follow-ups, page context
# --------------------------------------------------------------------- #
def test_add_domain_texts_name_the_single_home(demo):
    prep = tools.prepare_add_domain(demo, {"domain": "mine.example", "list": "positive"})
    assert any("Settings → Projects and subjects → Domains" in d for d in prep["details"])
    out = tools.exec_add_domain(demo, {"domain": "mine.example", "list": "positive"})
    assert "Settings → Projects and subjects → Domains" in out["message"]
    assert out["intent"]["target"] == "domains" and out["intent"]["url"] == "/settings#domains"


def test_start_search_card_is_editable(demo, monkeypatch):
    monkeypatch.setenv("APIFY_API_KEY", "apify_api_test")
    prep = tools.prepare_start_search(demo, {})
    fields = {f["name"]: f for f in prep["edit"]["fields"]}
    from apify.actors import CONFIGURED_ENGINES
    assert {o["value"] for o in fields["engines"]["options"]} == set(CONFIGURED_ENGINES)
    subject_countries = demo.db.current_subject()["countries"]
    assert fields["countries"]["value"] == [c.lower() for c in subject_countries]
    est = prep["edit"]["estimate"]
    assert est["per_run"] > 0 and est["pages"] == 1


def _edit(demo, name, args, edits):
    tool = tools.get_tool(name)
    prep = tool.prepare(demo, tools.validate_args(tool, args))
    return tools.apply_edits(demo, tool, prep["normalized_args"], edits, prep.get("edit"))


def test_apply_edits_accepts_offered_values(demo, monkeypatch):
    monkeypatch.setenv("APIFY_API_KEY", "apify_api_test")
    country = demo.db.current_subject()["countries"][0].lower()
    out = _edit(demo, "start_search", {}, {"countries": [country.upper()]})
    assert out["countries"] == [country] and out["engines"] == ["google"]
    rep = _edit(demo, "create_report", {"kind": "pdf"}, {"kind": "csv", "period": "30d"})
    assert rep["kind"] == "csv" and rep["period"] == "30d"
    note = _edit(demo, "save_note", {"url": "https://scam.example/x", "text": "Draft"},
                 {"text": "Edited draft"})
    assert note["text"] == "Edited draft" and note["url"] == "https://scam.example/x"


@pytest.mark.parametrize("name,args,edits,msg", [
    ("start_search", {}, {"engines": ["yandex"]}, "Not allowed"),
    ("start_search", {}, {"countries": ["zz"]}, "Not allowed"),
    ("start_search", {}, {"countries": []}, "at least one"),
    ("start_search", {}, {"pages": 3}, "can't be changed"),
    ("start_search", {}, {"device": "mobile"}, "can't be changed"),
    ("create_report", {"kind": "pdf"}, {"kind": "docx"}, "valid"),
    ("create_report", {"kind": "pdf"}, {"countries": ["us"]}, "can't be changed"),
    ("save_note", {"url": "https://a.example/x", "text": "t"}, {"url": "https://evil.example"},
     "can't be changed"),
    ("save_note", {"url": "https://a.example/x", "text": "t"}, {"text": "   "}, "empty"),
    ("save_note", {"url": "https://a.example/x", "text": "t"}, "not a dict", "object"),
])
def test_apply_edits_rejects_disallowed_values(demo, monkeypatch, name, args, edits, msg):
    monkeypatch.setenv("APIFY_API_KEY", "apify_api_test")
    with pytest.raises(tools.ToolError, match=msg):
        _edit(demo, name, args, edits)


def test_cards_from_tool_results(demo):
    from assistant import cards
    sov = cards.from_tool("get_overview", _run(demo, "get_overview"), demo)
    assert sov["kind"] == "sov" and sov["data"]["rows"][0]["total"] > 0
    ch = cards.from_tool("list_changes", _run(demo, "list_changes"), demo)
    assert ch["kind"] == "changes" and ch["data"]["compare_url"].startswith("/results?modal=/ui/compare")
    assert ch["data"]["params"]["run_a"] and ch["data"]["params"]["run_b"]
    rq = cards.from_tool("list_review_queue", _run(demo, "list_review_queue"), demo)
    assert rq["kind"] == "review" and 1 <= len(rq["data"]["items"]) <= 3
    assert all(i["result_id"] and i["why"] for i in rq["data"]["items"])
    viewer = tools.ToolContext(db=demo.db, subject_id=demo.subject_id, role="viewer")
    rv = cards.from_tool("list_review_queue", _run(demo, "list_review_queue"), viewer)
    assert rv["kind"] == "results" and rv["data"]["can_review"] is False
    rid = demo.db.pending_review(limit=1)[0]["id"]
    ex = cards.from_tool("explain_result", _run(demo, "explain_result", result_id=rid), demo)
    assert ex["kind"] == "results" and ex["data"]["items"][0]["result_id"] == rid
    tools.exec_create_report(demo, {"kind": "csv", "period": "all"})
    rp = cards.from_tool("list_reports", _run(demo, "list_reports"), demo)
    assert rp["kind"] == "report" and rp["data"]["files"][0]["url"].startswith("/reports/")
    assert cards.from_tool("app_help", _run(demo, "app_help", topic="x"), demo) is None
    json.dumps([sov, ch, rq, ex, rp])           # JSON-safe


def test_follow_ups_come_from_tool_results(demo):
    from assistant import suggest
    changes = _run(demo, "list_changes")
    trace = [{"name": "list_changes", "executed": True, "result": changes}]
    chips = suggest.follow_ups(demo, trace, [], "what changed?")
    assert 2 <= len(chips) <= 3 and all(c["label"] and c["prompt"] for c in chips)
    queue = _run(demo, "list_review_queue")
    chips = suggest.follow_ups(demo, [{"name": "list_review_queue", "executed": True, "result": queue}], [])
    assert any("review" in c["label"].lower() for c in chips)
    # Nothing to go on: falls back to the state chips, never repeats the question.
    chips = suggest.follow_ups(demo, [], [], "Run a new search")
    assert chips and all(c["prompt"].lower() != "run a new search" for c in chips)


def test_clean_context_keeps_only_known_small_fields():
    raw = {"page": "/results", "title": "Results", "overlay": "//evil.example/x", "evil": "x" * 9,
           "result": {"id": "12", "url": "https://a.example/" + "y" * 900, "title": "T",
                      "domain": "a.example", "label": "NEGATIVE", "extra": 1},
           "filters": {"country": "us", "classification": "negative", "junk": "1"}}
    out = harness.clean_context(raw)
    assert out["page"] == "/results" and "overlay" not in out and "evil" not in out
    assert out["highlighted_result"]["result_id"] == 12 and out["highlighted_result"]["label"] == "negative"
    assert len(out["highlighted_result"]["url"]) <= 300 and "extra" not in out["highlighted_result"]
    assert out["filters"] == {"country": "us", "classification": "negative"}
    assert harness.clean_context("nope") == {} and harness.clean_context({"page": "http://x"}) == {}
