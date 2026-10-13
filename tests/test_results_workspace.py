"""Results page (/results) and the dialogs / side panels opened from it:
Runs, Reports, Domains, the fragment contract of every /ui/ route, the old
URLs that now redirect to an overlay, and the assistant panel wiring.

Conventions under test (docs/ui-architecture.md): a fragment request
(X-SM-Fragment: 1) gets the bare overlay; a normal request gets the same
content in a full page; old pages answer 303 to /results?panel=... or
?modal=..., which static/app.js opens on load.
"""
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parent.parent
FRAG = {"X-SM-Fragment": "1"}


def _frag(client, path, **params):
    return client.get(path, params=params, headers=FRAG)


# --------------------------------------------------------------------- #
# Results page
# --------------------------------------------------------------------- #
def test_results_page_header_toolbar_and_filter_bar(client):
    text = client.get("/results").text
    assert "<h1>Results</h1>" in text
    assert "5 results in the latest run" in text
    for needle in ("href='/ui/search' data-modal", "href='/ui/review' data-modal",
                   "href='/ui/compare' data-modal", "href='/ui/runs' data-panel",
                   "data-popover='#results-more'", "href='/ui/timeline' data-panel",
                   "href='/ui/evidence' data-panel", "href='/ui/ai-checks?next=",
                   "href='/ui/reports' data-modal", "href='/ui/notes' data-panel"):
        assert needle in text, needle
    assert "/ui/domains" not in text          # Domains live in Settings → Projects and subjects
    # one-row filter bar: country, run date, label chips, More filters
    assert "aria-label='Country'" in text and "aria-label='Run date'" in text
    for label in ("All", "Positive", "Negative", "Neutral"):
        assert f">{label}</a>" in text
    assert "data-popover='#more-filters'" in text
    for f in ("Include same-name results", "Severity 3 or higher", "Has a note",
              "Only new since last run"):
        assert f in text
    # the old big filter box and tab bar are gone
    assert "<b>Classification:</b>" not in text and "<h2>Filters</h2>" not in text


def test_results_table_columns_pills_and_row_menu(client):
    text = client.get("/results").text
    for th in ("#", "Result", "Label", "Severity", "Notes"):
        assert f">{th}" in text
    assert "class='pill negative'" in text and "class='pill positive'" in text
    # a row menu per row, with the mockup's actions
    assert text.count("data-popover='#rm-") == 5
    assert "Add or edit note" in text and "Change label…" in text
    assert "Show on timeline" in text and "Open page in new tab" in text
    # evidence snapshot only on negative rows
    assert text.count("Save evidence snapshot") == 2


def test_results_label_chip_filters_and_empty_state(client):
    text = client.get("/results", params={"classification": "negative"}).text
    assert text.count("data-popover='#rm-") == 2
    assert "aria-pressed='true'>Negative</a>" in text
    assert "Showing 2 results of 5." in text
    none = client.get("/results", params={"has_note": "1"}).text
    assert "No results match these filters." in none


def test_results_needs_review_tag_and_note_tag(client, seeded_db):
    row = seeded_db.get_results_filtered(classification="neutral", limit=1)[0]
    conn = seeded_db._connect()
    conn.execute("UPDATE search_results SET review_status='pending' WHERE id=?", (row["id"],))
    conn.commit()
    conn.close()
    seeded_db.set_note(row["url"], "watch this")
    text = client.get("/results").text
    assert "needs review</span>" in text
    assert ">Open</span>" in text            # note status tag
    only_notes = client.get("/results", params={"has_note": "1"}).text
    assert only_notes.count("data-popover='#rm-") == 1


def test_results_sorting_by_label(client):
    text = client.get("/results", params={"sort_by": "classification"}).text
    first_neg = text.index("class='pill negative'")
    first_pos = text.index("class='pill positive'")
    assert first_neg < first_pos
    assert "aria-sort='ascending'" in text


def test_results_only_new_since_last_run(client, seeded_db):
    seeded_db.save_run(apify_run_id="run-older", queries=["q"], country_code="us",
                       status="completed", summary={"total": 1, "positive": 1, "negative": 0, "neutral": 0})
    seeded_db.save_results([{"query": "q", "url": "https://jordanashworth.example/", "title": "t",
                             "description": "d", "displayed_url": "x", "position": 1,
                             "country_code": "us", "domain": "jordanashworth.example",
                             "classification": "positive"}], run_id="run-older")
    conn = seeded_db._connect()
    conn.execute("UPDATE search_results SET scraped_at='2020-01-01T00:00:00' WHERE run_id='run-older'")
    conn.commit()
    conn.close()
    text = client.get("/results").text
    assert "tag-new" in text
    new_only = client.get("/results", params={"new_only": "1"}).text
    assert new_only.count("data-popover='#rm-") == text.count("tag tag-new")


def test_results_empty_state_without_runs(client, seeded_db):
    sid = seeded_db.add_subject("Nobody Yet", countries=["us"])
    client.cookies.set("subject", str(sid))
    text = client.get("/results").text
    assert "No results yet for Nobody Yet." in text and "Run a first search" in text
    assert "no runs yet" in text


def test_results_are_scoped_to_the_active_subject(client, seeded_db):
    sid = seeded_db.add_subject("Other Subject", countries=["us"])
    other = seeded_db.for_subject(sid)
    other.save_run(apify_run_id="run-other", queries=["Other"], country_code="us", status="completed",
                   summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    other.save_results([{"query": "Other", "url": "https://only-other.example/", "title": "Only other",
                         "description": "", "displayed_url": "x", "position": 1, "country_code": "us",
                         "domain": "only-other.example", "classification": "neutral"}], run_id="run-other")
    assert "only-other.example" not in client.get("/results").text
    client.cookies.set("subject", str(sid))
    text = client.get("/results").text
    assert "only-other.example" in text and "scam.example" not in text


# --------------------------------------------------------------------- #
# Fragment contract of every overlay route
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("path", [
    "/ui/search", "/ui/review", "/ui/compare", "/ui/runs", "/ui/run/run-test-1", "/ui/timeline",
    "/ui/evidence", "/ui/ai-checks", "/ui/reports", "/ui/reports?tab=prev",
    "/ui/note?url=https%3A%2F%2Fscam.example%2Farticle", "/ui/notes",
])
def test_overlay_routes_answer_fragments_and_full_pages(client, path):
    frag = client.get(path, headers=FRAG)
    assert frag.status_code == 200, path
    assert "<html" not in frag.text and "data-sm-title" in frag.text and "data-sm-close" in frag.text
    full = client.get(path)
    assert full.status_code == 200 and "<html" in full.text and "standalone-overlay" in full.text


@pytest.mark.parametrize("old,target", [
    ("/runs", "/results?panel=/ui/runs"),
    ("/run/run-test-1", "/results?panel=/ui/run/run-test-1"),
    ("/timeline", "/results?panel=/ui/timeline"),
    ("/evidence", "/results?panel=/ui/evidence"),
    ("/notes", "/results?panel=/ui/notes"),
    ("/review", "/results?modal=/ui/review"),
    ("/diff", "/results?modal=/ui/compare"),
    ("/export", "/results?modal=/ui/reports"),
    ("/domains", "/settings#domains"),
    ("/search_page", "/results?modal=/ui/search"),
])
def test_old_pages_redirect_to_their_overlay(client, old, target):
    resp = client.get(old, follow_redirects=False)
    assert resp.status_code == 303, old
    assert resp.headers["location"] == target


def test_app_js_opens_deep_linked_overlays_only_for_ui_paths():
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "params.get('panel')" in js and "params.get('modal')" in js
    assert "(ui|assistant)" in js


# --------------------------------------------------------------------- #
# Runs panel
# --------------------------------------------------------------------- #
def test_runs_panel_lists_runs_with_counts_and_actions(client):
    text = _frag(client, "/ui/runs").text
    assert "<span class='tag'>US</span>" in text
    assert "5 results · 2 positive · 2 negative · 1 neutral" in text
    assert "href='/ui/run/run-test-1' data-sm-nav" in text
    assert "Compare with previous" in text
    assert "data-sm-confirm=" in text and "action='/run/run-test-1/delete'" in text


def test_run_detail_inside_the_panel(client):
    text = _frag(client, "/ui/run/run-test-1").text
    assert "scam.example" in text and "← All runs" in text and "Show in Results" in text
    assert _frag(client, "/ui/run/nope").status_code == 404


def test_delete_run_is_admin_only_and_prg(client, seeded_db, monkeypatch):
    resp = client.post("/run/run-test-1/delete", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/results?panel=/ui/runs"
    assert seeded_db.get_run("run-test-1") is None
    assert seeded_db.list_audit(action="run_delete")


def test_operator_cannot_delete_runs(client, seeded_db, monkeypatch):
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("op", "op-password-123", role="operator")
    r = client.post("/login", data={"username": "op", "password": "op-password-123"},
                    follow_redirects=False)
    client.cookies.set("sid", r.cookies["sid"])
    assert "data-sm-confirm=" not in _frag(client, "/ui/runs").text
    resp = client.post("/run/run-test-1/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert seeded_db.get_run("run-test-1") is not None


# --------------------------------------------------------------------- #
# Domains dialog
# --------------------------------------------------------------------- #
def test_domains_home_reuses_the_manager_and_reports_relabelled_count(client, seeded_db):
    text = client.get("/settings").text
    area = text[text.index("<details class='subj-domains' id='domains'>"):]
    area = area[:area.index("</details>")]
    assert "scam.example" in area and "Apply to stored results" in area
    resp = client.post("/domains/reclassify", data={"next": "/settings#domains"},
                       follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/settings#domains"
    page = client.get("/results").text
    assert "Domains saved." in page and "relabelled" in page or "No stored result changed" in page


# --------------------------------------------------------------------- #
# Reports dialog
# --------------------------------------------------------------------- #
@pytest.fixture
def exports(tmp_path, monkeypatch):
    from dashboard import deps
    out = tmp_path / "exports"
    monkeypatch.setattr(deps, "exports_dir", out)
    return out


def test_reports_new_tab_has_purpose_tiles_details_and_a_real_preview(client, exports):
    text = _frag(client, "/ui/reports").text
    for title in ("Share with a client or manager", "Analyse in a spreadsheet",
                  "Use in scripts or other tools"):
        assert title in text
    assert "1.</b> What is it for?" in text and "2.</b> Check the details" in text
    assert "3.</b> What you'll get" in text
    for period in ("Last week", "Last 4 weeks", "Last 3 months"):
        assert period in text
    assert "Create PDF" in text and "action='/reports/create'" in text
    assert "new negative result" in text and "open alert" in text
    assert "No AI-written summary" in text            # no provider in tests: said honestly
    xlsx = _frag(client, "/ui/reports", kind="xlsx").text
    assert "The period doesn&#x27;t change the workbook" in xlsx and "Create workbook" in xlsx
    csv_ = _frag(client, "/ui/reports", kind="csv", period="week").text
    assert "5 stored results scraped in the period" in csv_


@pytest.mark.parametrize("kind,ext", [("pdf", "pdf"), ("xlsx", "xlsx"), ("csv", "csv")])
def test_reports_create_then_download_and_list(client, seeded_db, exports, kind, ext):
    resp = client.post("/reports/create", data={"kind": kind, "period": "4weeks"}, headers=FRAG)
    assert resp.status_code == 200
    assert unquote(resp.headers["X-SM-Toast"]).startswith("Report ready.")
    assert ">Download</a>" in resp.text
    files = list((exports / "1").glob(f"*.{ext}"))
    assert len(files) == 1 and (exports / "1" / (files[0].name + ".json")).is_file()
    dl = client.get(f"/reports/1/{files[0].name}")
    assert dl.status_code == 200 and len(dl.content) > 0
    prev = _frag(client, "/ui/reports", tab="prev").text
    assert files[0].name in prev and {"pdf": "PDF", "xlsx": "Excel", "csv": "CSV"}[kind] in prev
    assert seeded_db.list_audit(action=f"report_create_{kind}")


def test_csv_report_is_named_after_project_and_subject_and_respects_countries(
        client, seeded_db, exports):
    """No comment row in the CSV (header + data only); the file name carries
    the project, the subject and the date; the Countries choice filters rows."""
    import csv as _csv
    from datetime import UTC, datetime

    seeded_db.update_subject(1, name="Jane Doe")
    seeded_db.rename_client(1, "Acme Corp")
    seeded_db.save_run(apify_run_id="run-gb", queries=["Jane Doe"], country_code="gb",
                       status="completed", summary={"total": 1, "positive": 1, "negative": 0,
                                                    "neutral": 0})
    seeded_db.save_results([{"query": "Jane Doe", "url": "https://uk.example/a", "title": "UK",
                             "position": 1, "country_code": "gb", "domain": "uk.example",
                             "classification": "positive"}], run_id="run-gb")
    resp = client.post("/reports/create", data={"kind": "csv", "period": "4weeks",
                                                "country": "gb"}, headers=FRAG)
    assert resp.status_code == 200
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    files = list((exports / "1").glob("*.csv"))
    assert [f.name for f in files] == [f"acme-corp-jane-doe-results-{today}.csv"]
    rows = list(_csv.reader(files[0].read_text(encoding="utf-8").splitlines()))
    assert rows[0][0] == "run_id"                      # first line is the header
    assert {r[1] for r in rows[1:]} == {"gb"}          # country_code column: GB only
    # A second CSV the same day doesn't overwrite the first.
    client.post("/reports/create", data={"kind": "csv", "period": "4weeks"}, headers=FRAG)
    names = sorted(f.name for f in (exports / "1").glob("*.csv"))
    assert names == [f"acme-corp-jane-doe-results-{today}-2.csv",
                     f"acme-corp-jane-doe-results-{today}.csv"]
    assert client.get(f"/reports/1/{names[0]}").status_code == 200


def test_previous_reports_are_per_subject(client, seeded_db, exports):
    client.post("/reports/create", data={"kind": "csv"}, headers=FRAG)
    name = next((exports / "1").glob("*.csv")).name
    sid = seeded_db.add_subject("Other Subject", countries=["us"])
    client.cookies.set("subject", str(sid))
    assert "No reports yet." in _frag(client, "/ui/reports", tab="prev").text
    # another subject's file is not reachable by guessing the URL either way
    assert client.get(f"/reports/{sid}/{name}").status_code == 404
    assert client.get("/reports/1/..%2F..%2Fetc%2Fpasswd").status_code == 404


def test_previous_reports_show_weekly_files_sent_by_email(client, exports):
    from export import reports as store
    d = store.subject_dir(exports, 1)
    d.mkdir(parents=True)
    f = d / "weekly_2026-10-05.xlsx"
    f.write_bytes(b"PK")
    store.register_file(exports, 1, f, "xlsx", title="Weekly report", source="weekly", emailed=True)
    text = _frag(client, "/ui/reports", tab="prev").text
    assert "Weekly report" in text and "sent by email" in text and "Excel" in text


def test_read_only_viewer_cannot_create_reports(client, seeded_db, exports, monkeypatch):
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("viewer", "viewer-password-123", role="client_viewer", client_id=1)
    r = client.post("/login", data={"username": "viewer", "password": "viewer-password-123"},
                    follow_redirects=False)
    client.cookies.set("sid", r.cookies["sid"])
    text = _frag(client, "/ui/reports").text
    assert "read-only" in text and "action='/reports/create'" not in text
    assert client.post("/reports/create", data={"kind": "csv"}).status_code == 403


def test_period_filter_applies_to_csv(client, seeded_db, exports):
    from export import reports as store
    conn = seeded_db._connect()
    conn.execute("UPDATE search_results SET scraped_at='2020-01-01T00:00:00' WHERE domain='scam.example'")
    conn.commit()
    conn.close()
    meta = store.create_report(seeded_db, exports, 1, "csv", [], "week")
    body = (store.subject_dir(exports, 1) / meta["file"]).read_text()
    assert "scam.example" not in body and "jordanashworth.example" in body


# --------------------------------------------------------------------- #
# Assistant panel wiring (static check: app.js implements docs/assistant.md)
# --------------------------------------------------------------------- #
def test_app_js_wires_the_assistant_panel_contract():
    js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    for attr in ("data-assistant-transcript", "data-assistant-chips", "data-assistant-composer",
                 "data-assistant-input", "data-assistant-chip", "data-assistant-confirm-card",
                 "data-assistant-confirm", "data-assistant-cancel", "data-confirm-title",
                 "data-confirm-details", "data-confirm-note", "data-confirm-id",
                 "data-chat-url", "data-confirm-url", "data-cancel-url", "assistant-confirm-template"):
        assert attr in js, attr
    for status in ("503", "429", "404", "409", "403"):
        assert status in js
    assert "'Content-Type': 'application/json'" in js
    assert "ev.type === 'tool'" in js and "ev.type === 'intent'" in js and "ev.type === 'confirm'" in js
    assert "SM.openPanel" in js and "SM.openModal" in js


def test_assistant_panel_fragment_has_its_own_overlay_header(client):
    text = client.get("/assistant/panel", headers=FRAG).text
    assert "class='assistant-head ov-head'" in text and "data-assistant-close" in text
    assert "data-assistant-disabled" in text and "Settings → AI provider" in text
