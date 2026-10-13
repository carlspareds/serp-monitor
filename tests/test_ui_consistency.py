"""One home per feature, and the assistant/overlay stacking contract.

* Domains live in Settings → Projects and subjects (the active subject's
  <details id='domains'>): no Domains section, nav item, Results menu entry
  or dialog; the old URLs redirect there.
* "Relabel with AI" / "Run AI checks" live in Results → More → AI checks.
* The Overview has no Reports / New search buttons; it keeps single shortcuts.
* The setup wizard and onboarding reuse the Settings dialogs (no second form).
* static/app.js: SM stacks overlays with increasing z-index, and the assistant
  minimizes (small screens) when it opens something.
"""
import os
import re
from pathlib import Path

import pytest

FRAG = {"X-SM-Fragment": "1"}
APP_JS = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
CSS = (Path(__file__).resolve().parent.parent / "static" / "style.css").read_text(encoding="utf-8")


@pytest.fixture
def no_ai(monkeypatch):
    for name in ("LLM_PROVIDER", "GEMINI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL"):
        monkeypatch.setenv(name, "")


@pytest.fixture
def ai_on(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://llm.test/v1")
    monkeypatch.setenv("OPENAI_MODEL", "fake-model")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-consistency-1234")  # gitleaks:allow (fake test value)


# --------------------------------------------------------------------- #
# Domains: one home
# --------------------------------------------------------------------- #
def test_settings_has_no_domains_section_or_nav_item(client):
    html = client.get("/settings").text
    assert "<section id='domains'" not in html
    assert "href='#domains'" not in html.split("<div class='sgroup'>")[0]   # not in the section nav
    projects = html[html.index("<section id='projects'"):html.index("<section id='search'")]
    # The active subject's Domains area is inside Projects and subjects.
    assert "<details class='subj-domains' id='domains'>" in projects
    assert "action='/domains/add-any'" in projects and "action='/domains/reclassify'" in projects
    assert html.count("id='domains'") == 1
    # The AI actions moved to Results: no copy left in Settings.
    assert "/domains/reclassify-ai" not in html and "/ai/annotate" not in html


def test_other_subjects_link_to_their_domains_by_switching(client, seeded_db):
    sid = seeded_db.add_subject("Other Person", countries=["us"], client_id=1, queries=["Other Person"])
    html = client.get("/settings").text
    assert f"/subject/switch?id={sid}&amp;next=/settings%23domains" in html


@pytest.mark.parametrize("path", ["/domains", "/ui/domains"])
def test_old_domains_urls_redirect_to_the_single_home(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings#domains"


def test_old_domains_dialog_as_fragment_points_home(client):
    r = client.get("/ui/domains", headers=FRAG)
    assert r.status_code == 200 and r.headers["X-SM-Redirect"] == "/settings#domains"
    assert "/domains/add-any" not in r.text          # no second manager


def test_results_more_menu_has_ai_checks_not_domains(client):
    html = client.get("/results").text
    menu = html[html.index("<template id='results-more'>"):]
    menu = menu[:menu.index("</template>")]
    assert "/ui/domains" not in menu and ">Domains<" not in menu
    assert "/ui/ai-checks?next=" in menu and "AI checks…" in menu and "/ui/reports" in menu


def test_row_menu_marks_domains_and_asks_the_assistant(client, seeded_db):
    html = client.get("/results").text
    assert "data-domain-mark='positive'" in html and "data-domain-mark='negative'" in html
    assert "Mark this domain as mine" in html and "Mark this domain as negative" in html
    assert "data-assistant-ask=" in html and "Ask the assistant about this" in html
    assert "data-result-url=" in html and "data-label=" in html


def test_row_menu_shows_the_ai_reason(client, seeded_db):
    import sqlite3
    rid = seeded_db.get_results_filtered(limit=1)[0]["id"]
    conn = sqlite3.connect(seeded_db.db_path)
    conn.execute("UPDATE search_results SET classified_by='ai', ai_reason='Complaint about a refund' "
                 "WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    html = client.get("/results").text
    assert "data-label-source='ai'" in html and "Complaint about a refund" in html


def test_mark_domain_from_row_menu_writes_the_same_list(client, seeded_db):
    r = client.post("/domains/add-any", data={"domain": "rowmenu.example", "list": "negative",
                                              "next": "/results"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/results")
    assert "rowmenu.example" in seeded_db.list_negative_domains()


# --------------------------------------------------------------------- #
# AI checks dialog (Results)
# --------------------------------------------------------------------- #
def test_ai_checks_dialog_without_provider_is_disabled(client, no_ai):
    r = client.get("/ui/ai-checks", headers=FRAG)
    assert r.status_code == 200
    assert "data-ai-checks-off" in r.text and "href='/settings#ai'" in r.text
    assert r.text.count("disabled aria-disabled='true'") == 2
    assert "action='/domains/reclassify-ai'" in r.text and "action='/ai/annotate'" in r.text


def test_ai_checks_dialog_explains_both_actions_with_counts(client, ai_on, seeded_db):
    r = client.get("/ui/ai-checks?next=/results%3Fcountry%3Dus", headers=FRAG)
    text = r.text
    assert "data-ai-checks-off" not in text and "disabled aria-disabled" not in text
    assert "Relabel with AI" in text and "Run AI checks" in text
    assert "Up to <b>30</b> per check per click" in text
    assert re.search(r"<b>\d+ results? (is|are)</b> neutral right now", text)
    assert "without a same-name check" in text and "without a severity" in text
    assert "value='/results?country=us'" in text


def test_ai_actions_default_back_to_results(client, no_ai):
    r = client.post("/ai/annotate", data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/results")
    r = client.post("/domains/reclassify-ai", data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/results")


# --------------------------------------------------------------------- #
# Overview: single shortcuts only
# --------------------------------------------------------------------- #
def _main(html: str) -> str:
    return html[html.index("<main"):html.index("</main>")]


def test_overview_has_no_reports_or_new_search(client, seeded_db):
    main = _main(client.get("/").text)
    assert "/ui/reports" not in main and "Reports</a>" not in main
    assert "/ui/search" not in main and "New search" not in main


def test_overview_review_nudge_is_a_plain_link_and_compare_is_one_button(client, seeded_db):
    from processing.diff import run_diff  # noqa: F401 -- same diff the card uses
    seeded_db.save_run(apify_run_id="run-test-2", queries=["Jordan Ashworth"], country_code="us",
                       status="completed", summary={"total": 1, "positive": 1, "negative": 0, "neutral": 0})
    seeded_db.save_results([{"url": "https://new.example/a", "title": "New", "description": "",
                             "position": 1, "domain": "new.example", "classification": "neutral",
                             "confidence": 0.5}], run_id="run-test-2")
    main = _main(client.get("/").text)
    assert main.count("/ui/compare") == 1 and "Open compare" in main
    if "/ui/review" in main:
        assert main.count("/ui/review") == 1
        assert not re.search(r"class='btn[^']*' href='/ui/review'", main)


def test_overview_without_runs_has_one_run_first_search_button(client, seeded_db):
    sid = seeded_db.add_subject("Fresh Subject", countries=["us"], client_id=1, queries=["Fresh Subject"])
    client.get(f"/subject/switch?id={sid}&next=/")
    main = _main(client.get("/").text)
    assert main.count("/ui/search") == 1 and "Run first search" in main and "/ui/reports" not in main


# --------------------------------------------------------------------- #
# No duplicate forms: wizard and onboarding reuse the Settings dialogs
# --------------------------------------------------------------------- #
def test_wizard_apify_step_uses_the_settings_dialog(client, monkeypatch):
    monkeypatch.delenv("APIFY_API_KEY", raising=False)
    html = client.get("/setup?step=apify").text
    assert "/ui/integration/apify" in html
    assert "name='apikey'" not in html and "action='/setup/apify'" not in html
    assert "/setup/apify?skip=1" in html


def test_onboarding_has_one_search_shortcut_and_names_the_domains_home(client):
    html = client.get("/onboarding").text
    assert html.count('href="/search_page"') == 1
    assert "Settings → Projects and subjects → Domains" in html
    assert "Settings → Domains<" not in html


# --------------------------------------------------------------------- #
# AI provider dialog: reasoning effort + empty-answer hint
# --------------------------------------------------------------------- #
@pytest.fixture
def env_file(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV_FILE", str(tmp_path / ".env"))
    for name in ("LLM_PROVIDER", "GEMINI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL",
                 "AI_REASONING_EFFORT"):
        monkeypatch.setenv(name, "")
    (tmp_path / ".env").write_text("")
    return tmp_path / ".env"


def test_ai_dialog_has_reasoning_effort_and_saves_it(client, env_file):
    html = client.get("/ui/integration/ai?provider=openai&preset=ollama", headers=FRAG).text
    assert "name='reasoning_effort'" in html
    for v in ("low", "medium", "high", "off"):
        assert f"<option value='{v}'" in html
    form = {"provider": "openai", "preset": "ollama", "openai_base_url": "http://localhost:11434/v1",
            "openai_key": "", "openai_model": "llama3.1", "reasoning_effort": "high"}
    client.post("/settings/ai", data=form)
    assert os.getenv("AI_REASONING_EFFORT") == "high"
    assert "AI_REASONING_EFFORT=high" in env_file.read_text()
    # The default (low) never writes the variable; a bad value is refused.
    r = client.post("/settings/ai", headers=FRAG, data={**form, "reasoning_effort": "extreme"})
    assert r.status_code == 400 and "Reasoning effort must be" in r.text
    assert os.getenv("AI_REASONING_EFFORT") == "high"


def test_ai_test_failure_with_empty_answer_shows_the_hint(client, env_file, monkeypatch):
    from dashboard.routers import integrations as router_integ
    from processing import llm
    monkeypatch.setattr(router_integ, "_ai_ping", lambda values: (False, llm.EMPTY_ANSWER))
    r = client.post("/settings/integration/ai/test", headers=FRAG,
                    data={"provider": "openai", "preset": "ollama", "openai_base_url": "",
                          "openai_key": "", "openai_model": "deepseek-r1"})
    assert "Test failed" in r.text and "data-reasoning-hint" in r.text
    assert llm.REASONING_HINT in r.text


# --------------------------------------------------------------------- #
# static/app.js: stacking + assistant (static checks; no browser in CI)
# --------------------------------------------------------------------- #
def test_sm_assigns_increasing_z_index_from_the_stack():
    assert re.search(r"function restack\(\)\s*\{", APP_JS)
    assert "stack[i].scrim.style.zIndex = String(Z_BASE + 2 * i)" in APP_JS
    assert "stack[i].el.style.zIndex = String(Z_BASE + 2 * i + 1)" in APP_JS
    # Every push and every removal restacks; popovers sit above the stack.
    assert re.search(r"stack\.push\(entry\);\s*restack\(\);", APP_JS)
    assert re.search(r"stack\.splice\(i, 1\)\[0\];[\s\S]{0,400}restack\(\);", APP_JS)
    assert "el.style.zIndex = String(Z_BASE + 2 * stack.length + 2)" in APP_JS
    # Newest overlay owns Escape/Tab: both read the top of the stack.
    assert "SM.close(stack[stack.length - 1].el)" in APP_JS
    assert "var top = stack[stack.length - 1].el;" in APP_JS


def test_closing_an_overlay_returns_focus_sensibly():
    block = APP_JS[APP_JS.index("SM.close = function"):APP_JS.index("SM.closeAll = function")]
    assert "visible(e.opener)" in block and "focusFirst(stack[stack.length - 1].el)" in block
    assert "SM.assistant.focusIfDocked()" in block


def test_assistant_is_not_an_sm_overlay_and_minimizes_on_open():
    assert "var A = SM.assistant = (function ()" in APP_JS
    # The assistant opens overlays through handoff(), which minimizes the sheet.
    assert re.search(r"function handoff\(label\)\s*\{\s*if \(state === 'open' && !docked\(\)\) minimize\(", APP_JS)
    assert re.search(r"document\.addEventListener\('sm:open', function \(ev\) \{\s*if \(state === 'open' && !docked\(\)\) minimize\(", APP_JS)
    assert "function openOverlay(kind, url, title, wide) {\n      A.handoff(title);" in APP_JS
    assert "DOCK_MQ = window.matchMedia ? window.matchMedia('(min-width: 1024px)')" in APP_JS
    # Shortcuts and Escape.
    assert "e.key === '/' && !typing" in APP_JS and "(e.ctrlKey || e.metaKey)" in APP_JS
    assert "SM.assistant.escape(e)" in APP_JS
    # The dock narrows the page; overlays centre over the remaining area.
    assert "html.asst-docked .shell { margin-right: var(--asst-w); }" in CSS
    assert "html.asst-docked .modal {\n  left: calc((100vw - var(--asst-w)) / 2);" in CSS
    assert ".assistant-dock {\n  position: fixed;\n  z-index: 50;" in CSS
    assert "z-index: 75;" in CSS        # the bubble stays below the overlay scrims (80+)


def test_shell_has_the_assistant_dock_not_a_modal_panel(client):
    html = client.get("/results").text
    assert "<aside class='assistant-dock' id='assistant-panel' role='complementary'" in html
    assert "data-assistant-toggle" in html


def test_assistant_markdown_links_are_in_app_only():
    md = APP_JS[APP_JS.index("function mdInline(text)"):APP_JS.index("function md(text)")]
    # esc() runs first; only /path links (not //host) become anchors.
    assert md.lstrip().startswith("function mdInline(text) {\n    return esc(text)")
    assert r"\((\/(?!\/)" in md
