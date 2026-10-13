"""Fixes from the final live AI test.

1. AI Test button / _call_llm budgets are reasoning-safe; one card wording.
2. A successful AI check clears the stale "AI checks skipped" run note.
3. Human verdicts always win, per page (normalized URL), across runs.
4. The assistant can't claim a card / opened page / saved item it didn't make.
5. Eval helpers (negations, hallucinated-claim cases).
6. Bell = open alerts only; acknowledged counted apart.
7. PDF text only uses glyphs the PDF font has.
8. Compare hides namesakes on both sides.
No real network: every AI call is stubbed.
"""
import importlib.util
import io
import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from database.models import Database, normalize_url, url_hash
from processing import ai_checks, ai_classifier, llm

ROOT = Path(__file__).resolve().parent.parent
FRAG = {"X-SM-Fragment": "1"}


def _row(url, cls="neutral", conf=0.9, country="us", **extra):
    out = {"query": "q", "url": url, "title": "t", "description": "", "position": 1,
           "country_code": country, "domain": url.split("/")[2], "classification": cls,
           "confidence": conf, "classified_by": "ai"}
    out.update(extra)
    return out


def _run(db, run_id, rows, country="us"):
    db.save_run(apify_run_id=run_id, queries=["q"], country_code=country, status="completed",
                summary={"total": len(rows), "positive": 0, "negative": 0, "neutral": len(rows)})
    db.save_results(rows, run_id=run_id)


def _rows(db, url_part):
    return [r for r in db.get_results_filtered(limit=500, include_homonyms=True)
            if url_part in (r.get("url") or "")]


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "maintenance" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------- #
# 1. Reasoning-safe AI test
# --------------------------------------------------------------------- #
def test_call_llm_default_and_floor_are_reasoning_safe(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    seen = []
    monkeypatch.setattr(ai_classifier, "_call_gemini",
                        lambda prompt, max_tokens=0, temperature=0.0, lower=True:
                        (seen.append(max_tokens), (True, "ok"))[1])
    ai_classifier._call_llm("x")
    ai_classifier._call_llm("x", max_tokens=10)
    assert min(seen) >= llm.SHORT_OUTPUT_TOKENS == 1024


def test_settings_ai_ping_asks_for_a_reasoning_safe_budget(monkeypatch):
    from dashboard.routers import integrations
    seen = {}

    def fake(prompt, max_tokens=None, **kw):
        seen["max_tokens"] = max_tokens
        return True, "ok"

    monkeypatch.setattr(ai_classifier, "_call_llm", fake)
    assert integrations._ai_ping({"GEMINI_API_KEY": "gk-123456"}) == (True, "ok")
    assert seen["max_tokens"] >= llm.SHORT_OUTPUT_TOKENS


def test_working_card_wording():
    from dashboard import integrations as integ
    assert integ.working_text("2026-10-09T14:03:00") == "Working · tested 9 Oct 14:03 UTC"
    assert integ.working_text("") == "Working"


# --------------------------------------------------------------------- #
# 2. Stale run banner
# --------------------------------------------------------------------- #
def test_successful_ai_checks_clear_the_run_note(seeded_db, monkeypatch):
    from conftest import RUN_ID
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    seeded_db.set_run_ai_note(RUN_ID, "AI checks skipped: The model returned no answer.")
    monkeypatch.setattr(ai_classifier, "is_same_person_batch", lambda recs, ctx="": ["yes"] * len(recs))
    monkeypatch.setattr(ai_classifier, "analyze_negative", lambda r: {"severity": 3, "topic": "other"})
    res = ai_checks.run_ai_checks(seeded_db, limit=50)
    assert res["notes_cleared"] == 1
    assert not seeded_db.get_run(RUN_ID).get("ai_note")


def test_failed_ai_checks_replace_the_note_with_the_new_reason(seeded_db, monkeypatch):
    from conftest import RUN_ID
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    seeded_db.set_run_ai_note(RUN_ID, "AI checks skipped: old reason")

    def batch(recs, ctx=""):
        from processing import ai_status
        ai_status.record(False, "The AI provider rejected the API key (HTTP 401).")
        return [None] * len(recs)

    monkeypatch.setattr(ai_classifier, "is_same_person_batch", batch)
    monkeypatch.setattr(ai_classifier, "analyze_negative", lambda r: {})
    ai_checks.run_ai_checks(seeded_db, limit=50)
    note = seeded_db.get_run(RUN_ID)["ai_note"]
    assert note.startswith("AI checks skipped: ") and "old reason" not in note


def test_relabel_with_ai_clears_the_note_when_nothing_is_left(client, seeded_db, monkeypatch):
    from conftest import RUN_ID
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    conn = sqlite3.connect(seeded_db.db_path)
    conn.execute("UPDATE search_results SET same_person='yes', severity=2, topic='other'")
    conn.commit()
    conn.close()
    seeded_db.set_run_ai_note(RUN_ID, "AI checks skipped: old reason")
    monkeypatch.setattr(ai_classifier, "classify_with_ai_confidence",
                        lambda rec: {"classification": "positive", "confidence": 0.9,
                                     "classified_by": "ai"})
    client.post("/domains/reclassify-ai", data={"next": "/results"})
    assert not seeded_db.get_run(RUN_ID).get("ai_note")


# --------------------------------------------------------------------- #
# 3a. URL identity
# --------------------------------------------------------------------- #
def test_normalize_url_strips_tracking_params_and_fragment_only():
    base = normalize_url("https://example.com/a?id=7&page=2")
    for extra in ("srsltid=AfmBOoq", "utm_source=x&utm_medium=y", "gclid=1", "fbclid=2",
                  "mc_cid=3&mc_eid=4", "_hsenc=5&_hsmi=6", "ref_src=twsrc", "igshid=7"):
        assert normalize_url(f"https://example.com/a/?{extra}&page=2&id=7#frag") == base
    assert normalize_url("https://example.com/a?id=8") != base       # meaningful params kept
    from database.base import strip_tracking
    assert strip_tracking("https://Ex.com/a?b=2&srsltid=x&a=1#f") == "https://Ex.com/a?b=2&a=1"
    assert strip_tracking("https://ex.com/a?q=1&z=2") == "https://ex.com/a?q=1&z=2"


def test_new_results_are_stored_without_tracking_params():
    from apify.actors import _normalize_url
    assert _normalize_url("https://Site.example/p/?srsltid=AfmB&x=1") == "https://site.example/p?x=1"


def test_startup_recomputes_url_keys_when_normalization_changes(tmp_path):
    path = str(tmp_path / "k.db")
    db = Database(db_path=path)
    _run(db, "r1", [_row("https://a.example/p?srsltid=abc")])
    conn = sqlite3.connect(path)
    conn.execute("UPDATE search_results SET url_key = 'stale'")
    conn.execute("UPDATE settings SET value = '1' WHERE key = 'url_key_version'")
    conn.commit()
    conn.close()
    Database(db_path=path)
    conn = sqlite3.connect(path)
    key = conn.execute("SELECT url_key FROM search_results").fetchone()[0]
    conn.close()
    assert key == url_hash("https://a.example/p")


# --------------------------------------------------------------------- #
# 3b. Human verdicts always win, per page
# --------------------------------------------------------------------- #
def test_human_label_propagates_to_same_page_in_other_runs(tmp_path):
    db = Database(db_path=str(tmp_path / "h.db"))
    _run(db, "r1", [_row("https://a.example/p?srsltid=one", cls="negative")])
    _run(db, "r2", [_row("https://a.example/p?srsltid=two", cls="negative")])
    _run(db, "r3", [_row("https://a.example/p/?utm_source=x", cls="negative", country="gb")], "gb")
    first = _rows(db, "a.example")[-1]
    db.set_review_verdict(first["id"], "positive")
    rows = _rows(db, "a.example")
    assert len(rows) == 3
    assert all(r["classification"] == "positive" and r["classified_by"] == "human" for r in rows)
    # Run counts follow.
    assert db.get_run("r2")["positive"] == 1


def test_latest_human_verdict_wins(tmp_path):
    db = Database(db_path=str(tmp_path / "h.db"))
    _run(db, "r1", [_row("https://a.example/p?srsltid=1")])
    _run(db, "r2", [_row("https://a.example/p?srsltid=2")])
    old, new = sorted(_rows(db, "a.example"), key=lambda r: r["id"])
    db.set_review_verdict(new["id"], "negative")
    db.set_review_verdict(old["id"], "positive")      # the latest human verdict
    assert {r["classification"] for r in _rows(db, "a.example")} == {"positive"}


def test_new_run_inherits_the_page_verdict_and_not_this_person(tmp_path):
    db = Database(db_path=str(tmp_path / "h.db"))
    _run(db, "r1", [_row("https://a.example/p?srsltid=1", cls="negative")])
    rid = _rows(db, "a.example")[0]["id"]
    db.set_same_person_verdict(rid)
    _run(db, "r2", [_row("https://a.example/p?srsltid=2", cls="negative", same_person="yes")])
    new = next(r for r in _rows(db, "a.example") if r["run_id"] == "r2")
    assert new["classified_by"] == "human" and new["same_person"] == "no"


def test_relabel_never_overwrites_a_page_a_person_labelled(tmp_path):
    db = Database(db_path=str(tmp_path / "h.db"))
    _run(db, "r1", [_row("https://a.example/p?srsltid=1")])
    _run(db, "r2", [_row("https://a.example/p?srsltid=2")])
    old = min(_rows(db, "a.example"), key=lambda r: r["id"])
    conn = sqlite3.connect(db.db_path)        # a legacy human row that never propagated
    conn.execute("UPDATE search_results SET classification='positive', classified_by='human', "
                 "confidence=1.0 WHERE id=?", (old["id"],))
    conn.commit()
    conn.close()
    asked = []
    db.reclassify_all(lambda rec: (asked.append(rec["url"]),
                                   {"classification": "negative", "confidence": 0.9,
                                    "classified_by": "ai"})[1])
    assert not any("a.example" in u for u in asked)
    assert {r["classification"] for r in _rows(db, "a.example")} == {"positive"}


def test_run_ai_checks_applies_human_verdict_instead_of_asking(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    db = Database(db_path=str(tmp_path / "h.db"))
    _run(db, "r1", [_row("https://a.example/p?srsltid=1", cls="negative")])
    rid = _rows(db, "a.example")[0]["id"]
    db.set_same_person_verdict(rid)
    conn = sqlite3.connect(db.db_path)       # an older AI row on the same page
    conn.execute("UPDATE search_results SET same_person = NULL")
    conn.commit()
    conn.close()
    _run(db, "r2", [_row("https://a.example/p?srsltid=2", cls="negative")])
    conn = sqlite3.connect(db.db_path)
    conn.execute("UPDATE search_results SET classified_by='ai', same_person=NULL WHERE run_id='r2'")
    conn.execute("UPDATE search_results SET same_person='no' WHERE run_id='r1'")
    conn.commit()
    conn.close()
    asked = []
    monkeypatch.setattr(ai_classifier, "is_same_person_batch",
                        lambda recs, ctx="": (asked.extend(recs), ["yes"] * len(recs))[1])
    monkeypatch.setattr(ai_classifier, "analyze_negative", lambda r: {"severity": 3, "topic": "other"})
    res = ai_checks.run_ai_checks(db, limit=50)
    assert res["human_applied"] >= 1 and not asked
    new = next(r for r in _rows(db, "a.example") if r["run_id"] == "r2")
    assert new["same_person"] == "no" and new["classified_by"] == "human"


def test_review_queue_hides_pages_with_a_human_verdict(tmp_path):
    db = Database(db_path=str(tmp_path / "h.db"))
    _run(db, "r1", [_row("https://a.example/p?srsltid=1", conf=0.95)])
    _run(db, "r2", [_row("https://a.example/p?srsltid=2", conf=0.1)])
    conn = sqlite3.connect(db.db_path)       # legacy: human row, other spelling, not propagated
    conn.execute("UPDATE search_results SET classified_by='human' WHERE run_id='r1'")
    conn.execute("UPDATE search_results SET review_status='pending' WHERE run_id='r2'")
    conn.commit()
    conn.close()
    assert db.count_pending_review() == 0 and db.pending_review() == []


# --------------------------------------------------------------------- #
# 3c. Repair scripts
# --------------------------------------------------------------------- #
def _legacy_db(tmp_path):
    path = str(tmp_path / "legacy.db")
    db = Database(db_path=path)
    _run(db, "r1", [_row("https://a.example/p?srsltid=1", cls="negative")])
    _run(db, "r2", [_row("https://a.example/p?srsltid=2", cls="negative")])
    conn = sqlite3.connect(path)
    conn.execute("UPDATE search_results SET classification='positive', classified_by='human', "
                 "human_at='2026-10-01T10:00:00' WHERE run_id='r1'")
    conn.commit()
    conn.close()
    return path


def test_repair_human_verdicts_dry_run_then_apply(tmp_path):
    mod = _load_script("repair_human_verdicts")
    path = _legacy_db(tmp_path)
    assert mod.main(["--db", path, "--dry-run"]) == 0
    db = Database(db_path=path)
    assert [r["classification"] for r in _rows(db, "a.example") if r["run_id"] == "r2"] == ["negative"]
    assert mod.main(["--db", path]) == 0
    assert {r["classification"] for r in _rows(db, "a.example")} == {"positive"}
    assert list(Path(path).parent.glob("legacy.db.pre-verdicts-*.bak"))
    out = io.StringIO()
    assert mod.repair(db, path, dry_run=True, out=out) == 0     # idempotent


def test_repair_url_identity_merges_lifecycle_rows(tmp_path):
    mod = _load_script("repair_url_identity")
    path = str(tmp_path / "u.db")
    Database(db_path=path)
    conn = sqlite3.connect(path)
    for url, first, last, best in (("https://a.example/p?srsltid=1", "2026-09-01", "2026-09-08", 4),
                                   ("https://a.example/p?srsltid=2", "2026-09-15", "2026-09-22", 2)):
        conn.execute("INSERT INTO url_lifecycle (url_hash, url, country, engine, first_seen, last_seen, "
                     "best_position, last_position, classification, status, subject_id) "
                     "VALUES (?, ?, 'us', 'google', ?, ?, ?, ?, 'negative', 'active', 1)",
                     (f"old-{best}", url, first, last, best, best))
    conn.execute("INSERT INTO evidence (url, url_hash, captured_at) VALUES (?, 'old', '2026-09-01')",
                 ("https://a.example/p?utm_source=x",))
    conn.commit()
    conn.close()
    assert mod.main(["--db", path]) == 0                        # dry run: nothing written
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM url_lifecycle").fetchone()[0] == 2
    conn.close()
    assert mod.main(["--db", path, "--apply"]) == 0
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT url_hash, first_seen, last_seen, best_position FROM url_lifecycle").fetchall()
    ev = conn.execute("SELECT url_hash FROM evidence").fetchone()[0]
    conn.close()
    assert rows == [(url_hash("https://a.example/p"), "2026-09-01", "2026-09-22", 2)]
    assert ev == url_hash("https://a.example/p")
    conn = sqlite3.connect(path)
    assert mod.plan_is_empty(mod.build_plan(conn))               # idempotent
    conn.close()


# --------------------------------------------------------------------- #
# 3d. AI checks dialog: no subject description
# --------------------------------------------------------------------- #
def test_ai_checks_dialog_warns_without_a_subject_description(client, seeded_db, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gk-123456")
    seeded_db.set_tracked_subject(context="")
    text = client.get("/ui/ai-checks", headers=FRAG).text
    assert "data-ai-no-context" in text
    assert "Add a one-line description so the AI can tell this person from others" in text
    assert "/ui/subject/1/edit" in text
    seeded_db.set_tracked_subject(context="Jordan Ashworth, the London architect")
    assert "data-ai-no-context" not in client.get("/ui/ai-checks", headers=FRAG).text


# --------------------------------------------------------------------- #
# 4. Assistant claims vs events
# --------------------------------------------------------------------- #
def _ctx(db, role="operator"):
    from assistant import tools
    return tools.ToolContext(db=db.for_subject(1), subject_id=1, role=role, user="op")


def test_prompt_no_longer_teaches_the_prepared_below_phrase():
    from assistant import prompts
    assert "confirm or cancel below" not in prompts.RULES
    assert "CALL the tool" in prompts.RULES and "above your reply" in prompts.RULES


@pytest.mark.parametrize("text,kinds", [
    ("I've prepared the report. Confirm or cancel below.", {"confirm"}),
    ("He preparado el informe; confírmalo abajo.", {"confirm"}),
    ("I've opened the New search dialog for you.", {"open"}),
    ("The note has been saved.", {"done"}),
    ("It's not saved yet.", set()),
    ("It isn't saved.", set()),
    ("I can prepare a report if you want.", set()),
    ("Three results moved up.", set()),
])
def test_claim_detection(text, kinds):
    from assistant import harness
    assert harness.claimed_kinds(text) == kinds


def test_unbacked_claim_gets_one_corrective_reprompt(seeded_db):
    from assistant import harness
    from assistant.fakes import ScriptedModel, call, say
    from assistant.pending import PendingStore
    model = ScriptedModel([say("I've prepared the PDF report. Confirm or cancel below."),
                           call("create_report", kind="pdf"), say("Confirm above to create it.")])
    res = harness.run_turn(model, _ctx(seeded_db), [{"role": "user", "content": "PDF report please"}],
                           session_key="s", store=PendingStore())
    assert [e["tool"] for e in res.events if e["type"] == "confirm"] == ["create_report"]
    assert res.reply == "Confirm above to create it."
    assert harness.CORRECTIVE_NOTE in model.calls[1]["history"][-1]["content"]


def test_persistent_claim_is_replaced_and_a_fallback_offered(seeded_db):
    from assistant import harness
    from assistant.fakes import ScriptedModel, say
    from assistant.pending import PendingStore
    model = ScriptedModel([say("I've opened the New search dialog for you."), say("Opened it for you.")])
    res = harness.run_turn(model, _ctx(seeded_db), [{"role": "user", "content": "Open a new search"}],
                           session_key="s", store=PendingStore())
    intents = [e for e in res.events if e["type"] == "intent"]
    assert intents and intents[0]["target"] == "search" and intents[0]["fallback"] is True
    assert "I couldn't prepare that." in res.reply and "Try the button: New search" in res.reply
    assert "opened" not in res.reply.lower()


def test_fallback_report_is_only_a_proposal(seeded_db):
    from assistant import harness
    from assistant.fakes import ScriptedModel, say
    from assistant.pending import PendingStore
    store = PendingStore()
    model = ScriptedModel([say("I've created the report."), say("The report has been created.")])
    res = harness.run_turn(model, _ctx(seeded_db), [{"role": "user", "content": "Make a CSV report"}],
                           session_key="s", store=store)
    [card] = [e for e in res.events if e["type"] == "confirm"]
    assert card["tool"] == "create_report" and store.get(card["id"]).args["kind"] == "csv"
    assert not list(Path(seeded_db.db_path).parent.glob("**/*.csv"))      # nothing executed
    assert "created" not in res.reply.lower()


def test_no_fallback_when_nothing_obvious(seeded_db):
    from assistant import harness
    from assistant.fakes import ScriptedModel, say
    from assistant.pending import PendingStore
    model = ScriptedModel([say("I've saved it."), say("I've saved it.")])
    res = harness.run_turn(model, _ctx(seeded_db), [{"role": "user", "content": "do the thing"}],
                           session_key="s", store=PendingStore())
    assert res.reply == "I couldn't prepare that." and not res.events


def _js():
    return (ROOT / "static" / "app.js").read_text(encoding="utf-8")


def test_app_js_persists_the_turn_before_running_live_intents():
    js = _js()
    send = js[js.index("    function send(text) {"):js.index("    function decide(cardEl, confirm)")]
    assert send.index("save();") < send.index("runLive();")
    # Live intents are queued, never run while the turn is being rendered.
    intent = js[js.index("    function intent(entry, live) {"):js.index("    // Cards built from tool results")]
    assert "liveQueue.push(go)" in intent and "go();" not in intent.replace("liveQueue.push(go)", "")
    assert "!ev.fallback" in intent


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_markdown_lite_headings_and_tables_are_escaped():
    js = _js()
    esc_start = js.index("function esc(")
    esc_src = js[esc_start:js.index("\n  }", esc_start) + 4]
    md_src = js[js.index("  function mdInline("):js.index("  SM.md = md;")]
    prog = (esc_src + md_src + "; process.stdout.write(md(" + json.dumps(
        "# Title\n| A | B |\n|---|---|\n| <i>1</i> | [x](/results) |\n| y | [z](https://evil.example) |")
        + "));")
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30,
                         check=False).stdout
    assert '<p class="asst-h"><b>Title</b></p>' in out
    assert "<th>A</th><th>B</th>" in out and "&lt;i&gt;1&lt;/i&gt;" in out
    assert '<a href="/results" data-asst-link>x</a>' in out and "https://evil.example" not in re.findall(
        r'href="([^"]+)"', out)


# --------------------------------------------------------------------- #
# 5. Eval helpers
# --------------------------------------------------------------------- #
def _eval_module():
    spec = importlib.util.spec_from_file_location("assistant_eval_run", ROOT / "evals" / "assistant" / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_eval_claimed_done_ignores_negations():
    mod = _eval_module()
    assert mod.claimed_done("I've saved the note.")
    assert not mod.claimed_done("I've drafted the note; it's not saved yet.")
    assert not mod.claimed_done("It isn't saved until you confirm.")


def test_eval_cases_cover_hallucinated_claims():
    cases = {c["id"]: c for c in json.loads((ROOT / "evals" / "assistant" / "cases.json").read_text())["cases"]}
    for cid in ("claim_report", "claim_note", "claim_search"):
        assert cases[cid]["expect"]["type"] == "claim_backed"
    assert cases["review_help"]["expect"]["tools_any"] == ["list_review_queue", "open_review"]


# --------------------------------------------------------------------- #
# 6. Bell = open alerts
# --------------------------------------------------------------------- #
def _alerts(db):
    for i in range(3):
        db.add_alert("negative_new", "warning", "us", f"Alert {i}", f"k{i}")
    ids = [a["id"] for a in db.open_alerts()]
    db.ack_alert(ids[0])
    return ids


def test_bell_counts_only_open_alerts_and_groups_acknowledged(client, seeded_db):
    _alerts(seeded_db)
    assert seeded_db.count_open_alerts() == 2
    page = client.get("/results").text
    assert "Alerts, 2 open" in page
    pop = client.get("/ui/alerts", headers=FRAG).text
    assert "data-sm-alerts-count='2'" in pop and "Acknowledged (1)" in pop


def test_pdf_period_line_says_open_and_acknowledged(seeded_db, tmp_path, monkeypatch):
    from export import pdf_report
    texts = []
    real = pdf_report.Paragraph
    monkeypatch.setattr(pdf_report, "Paragraph", lambda t, s, *a, **k: (texts.append(t), real(t, s))[1])
    pdf_report.build_pdf(seeded_db, str(tmp_path / "r.pdf"), period={
        "label": "Last week", "new_negatives": [], "wins": [], "open_alerts": 2,
        "acknowledged_alerts": 1})
    assert any("Alerts: 2 open, 1 acknowledged." in t for t in texts)


# --------------------------------------------------------------------- #
# 7. PDF glyphs
# --------------------------------------------------------------------- #
def test_pdf_safe_keeps_only_cp1252_glyphs():
    from export.pdf_report import pdf_safe
    assert pdf_safe("Demo ▸ Jordan") == "Demo › Jordan"
    assert pdf_safe("a – b — c … d · e é") == "a – b — c … d · e é"
    assert pdf_safe("≥ 3 → ok ✓") == ">= 3 › ok OK"
    for ch in pdf_safe("名前 ▸ ★"):
        ch.encode("cp1252")


def test_pdf_header_with_project_separator_builds(seeded_db, tmp_path):
    from export.pdf_report import build_pdf
    out = tmp_path / "r.pdf"
    build_pdf(seeded_db, str(out), subject_name="Demo ▸ Jordan Ashworth")
    assert out.stat().st_size > 0


# --------------------------------------------------------------------- #
# 8. Compare hides namesakes on both sides
# --------------------------------------------------------------------- #
def test_compare_hides_pages_marked_not_this_person_on_both_sides(tmp_path):
    from processing.diff import run_diff
    db = Database(db_path=str(tmp_path / "c.db"))
    _run(db, "ra", [_row("https://a.example/p", same_person="yes"), _row("https://b.example/q")])
    _run(db, "rb", [_row("https://a.example/p", same_person="no"), _row("https://b.example/q")])
    conn = sqlite3.connect(db.db_path)        # AI verdict differs per run (no human involved)
    conn.execute("UPDATE search_results SET same_person='yes' WHERE run_id='ra' AND url LIKE '%a.example%'")
    conn.execute("UPDATE search_results SET same_person='no' WHERE run_id='rb' AND url LIKE '%a.example%'")
    conn.commit()
    conn.close()
    diff = run_diff(db, "ra", "rb")
    assert diff["exited"] == [] and diff["entered"] == []
    assert diff["hidden_same_name"] == 1
