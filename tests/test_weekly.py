"""Tests for item 8.1: report.weekly.run() is importable and accepts an
injected Database -- smoke with stubs (no network: Apify/email/Excel
are monkeypatched; only the repo's own real orchestration stays exposed:
run_multi_country/classify/db.save_*/alerts/monitors)."""
from pathlib import Path

from database.models import Database
from processing import ai_classifier
from report import weekly


class _FakeRunner:
    """Replaces ApifyRunner: .search() does no network, returns an outcome
    with zero results (enough to test that run() orchestrates without
    exploding)."""

    def search(self, queries, country_code, language_code, max_pages, engines=None, device=None):
        # 1.1: run_multi_country now reads outcome["engine_runs"] (one per
        # engine) -- this stub simulates a single engine ("google").
        outcome = {
            "run_id": f"run-{country_code}",
            "queries": queries,
            "country_code": country_code,
            "status": "completed",
            "results": [],
        }
        outcome["engine_runs"] = [{**outcome, "engine": "google", "error": None}]
        return outcome


class _FakeRunnerWithHomonym:
    """Like _FakeRunner, but returns 2 results per country: one positive
    (known domain) and one negative that later gets marked same_person='no'
    (2.3, H2) -- to test that the weekly email excludes namesakes."""

    def search(self, queries, country_code, language_code, max_pages, engines=None, device=None):
        q = queries[0]
        # 1.1: run_multi_country now reads outcome["engine_runs"] (one per
        # engine) -- this stub simulates a single engine ("google").
        outcome = {
            "run_id": f"run-{country_code}",
            "queries": queries,
            "country_code": country_code,
            "status": "completed",
            "results": [
                {"query": q, "url": f"https://jordanashworth.example/{country_code}",
                 "title": "t", "description": "d", "displayed_url": "jordanashworth.example",
                 "position": 1, "country_code": country_code, "domain": "jordanashworth.example"},
                {"query": q, "url": f"https://scam.example/{country_code}",
                 "title": "t2", "description": "d2", "displayed_url": "scam.example",
                 "position": 2, "country_code": country_code, "domain": "scam.example"},
            ],
        }
        outcome["engine_runs"] = [{**outcome, "engine": "google", "error": None}]
        return outcome


class _FakeRunnerReplacesDomain:
    """Like _FakeRunner, but returns 1 result for jordanashworth.example (its
    classification doesn't matter here: the diff only looks at URLs) -- to
    build a real diff (entered/exited) against a previous run seeded by
    hand with ANOTHER domain (4.2, "Changes vs previous run" block)."""

    def search(self, queries, country_code, language_code, max_pages, engines=None, device=None):
        q = queries[0]
        # 1.1: run_multi_country now reads outcome["engine_runs"] (one per
        # engine) -- this stub simulates a single engine ("google").
        outcome = {
            "run_id": f"new-{country_code}",
            "queries": queries,
            "country_code": country_code,
            "status": "completed",
            "results": [
                {"query": q, "url": f"https://jordanashworth.example/{country_code}",
                 "title": "t", "description": "d", "displayed_url": "jordanashworth.example",
                 "position": 1, "country_code": country_code, "domain": "jordanashworth.example"},
            ],
        }
        outcome["engine_runs"] = [{**outcome, "engine": "google", "error": None}]
        return outcome


def _set_scraped_date(db, url, date_str):
    conn = db._connect()
    conn.execute(
        "UPDATE search_results SET scraped_at = ? WHERE url = ?",
        (f"{date_str}T00:00:00", url),
    )
    conn.commit()
    conn.close()


def _stub_externals(monkeypatch, tmp_path=None):
    """No network: ApifyRunner, the Excel exporter (doesn't write a real
    .xlsx into the repo), and email. email_sender reads the REAL repo's
    .env (is_configured() would be True in this dev environment) -- it's
    stubbed directly, just monkeypatch.delenv isn't enough (see the
    equivalent finding in tests/conftest.py)."""
    monkeypatch.setattr(weekly, "ApifyRunner", lambda: _FakeRunner())
    monkeypatch.setattr(weekly, "build_workbook", lambda db, path, **kw: None)
    monkeypatch.setattr(weekly.email_sender, "is_configured", lambda: False)
    if tmp_path is not None:
        monkeypatch.setattr(weekly, "project_root", tmp_path)


def test_run_orchestrates_with_stubs_no_network(tmp_path, monkeypatch):
    db = Database(db_path=str(tmp_path / "t.db"))
    _stub_externals(monkeypatch, tmp_path)

    result = weekly.run(db=db, query="Test Query", countries="us,ca", pages=1)

    assert result["query"] == "Test Query"
    assert result["countries"] == ["us", "ca"]
    assert result["aggregate"]["countries_ok"] == 2
    assert result["aggregate"]["countries_error"] == 0
    # One run per country was saved (FakeRunner) in the injected temporary DB.
    assert len(db.list_runs()) == 2


def test_run_accepts_countries_as_list(tmp_path, monkeypatch):
    db = Database(db_path=str(tmp_path / "t.db"))
    _stub_externals(monkeypatch, tmp_path)

    result = weekly.run(db=db, query="Q", countries=["gb"], pages=1)

    assert result["countries"] == ["gb"]


def test_run_falls_back_to_settings_when_query_and_countries_omitted(tmp_path, monkeypatch):
    db = Database(db_path=str(tmp_path / "t.db"))
    db.set_setting("report_query", "Settings Query")
    db.set_setting("report_countries", "de")
    _stub_externals(monkeypatch, tmp_path)

    result = weekly.run(db=db)

    assert result["query"] == "Settings Query"
    assert result["countries"] == ["de"]


def test_run_alert_checks_returns_new_alerts_and_check_results(tmp_path):
    db = Database(db_path=str(tmp_path / "t.db"))
    # Clean DB: no runs to compare, no overdue notes -> both checks are no-op.
    checks = weekly.run_alert_checks(db)

    assert checks["error"] is None
    assert checks["new_alerts"] == []
    assert set(checks["checks"]) == {"notes_overdue"}
    for m in checks["checks"].values():
        assert m["new"] == 0


def test_email_body_figures_exclude_homonyms(tmp_path, monkeypatch):
    """H2 (arbitration, 2.3): the email BODY is computed from
    db.latest_run_per_country() (excludes same_person='no') filtered to
    countries_list, AFTER run_multi_country -- not from the raw
    aggregate_summary (which DOES count namesakes)."""
    db = Database(db_path=str(tmp_path / "t.db"))
    # Positive domains are per subject (multi-subject phase 1): the subject owns
    # jordanashworth.example in its DB list, the global config list is empty.
    db.add_positive_domain("jordanashworth.example")
    # The email attaches the generated .xlsx (open(out, "rb")) -- this test
    # DOES reach that branch (email_sender.is_configured=True), unlike
    # _stub_externals. project_root points at tmp_path so that .xlsx
    # (stub, content irrelevant) gets written outside the real repo.
    monkeypatch.setattr(weekly, "project_root", tmp_path)
    monkeypatch.setattr(weekly, "ApifyRunner", lambda: _FakeRunnerWithHomonym())

    def _fake_build_workbook(db, path, **kw):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"fake-xlsx")

    monkeypatch.setattr(weekly, "build_workbook", _fake_build_workbook)
    monkeypatch.setattr(weekly.email_sender, "is_configured", lambda: True)
    # Real is_same_person_batch (no network): the positive is "same", the
    # negative (scam.example) is a namesake ("no").
    monkeypatch.setattr(
        ai_classifier, "is_same_person_batch",
        lambda records, context="": [
            "no" if "scam.example" in (r.get("url") or "") else "yes" for r in records
        ],
    )
    captured = {}

    def fake_send_email(subject, html_body, to=None, attachments=None):
        captured["html"] = html_body
        return True, "ok"

    monkeypatch.setattr(weekly.email_sender, "send_email", fake_send_email)

    weekly.run(db=db, query="Q", countries="us", pages=1)

    assert "html" in captured
    # The negative (namesake) ends up excluded: 0 negatives, 1 positive in the body.
    assert "Positive: 1" in captured["html"]
    assert "Negative: 0" in captured["html"]


def test_email_includes_changes_vs_previous_run_block(tmp_path, monkeypatch):
    """4.2: the weekly email includes a "Changes vs previous run" block
    per country, built with processing.diff.run_diff -- same source as
    _detect_new_negatives and the /diff page (ponytail: no duplicated
    diff logic)."""
    db = Database(db_path=str(tmp_path / "t.db"))
    monkeypatch.setattr(weekly, "project_root", tmp_path)

    # Previous run (old date) for 'us': one published negative.
    db.save_run(apify_run_id="old-us", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db.save_results([
        {"query": "q", "url": "https://scam.example/us",
         "title": "t", "description": "d", "displayed_url": "scam.example",
         "position": 1, "country_code": "us", "domain": "scam.example",
         "classification": "negative"},
    ], run_id="old-us")
    _set_scraped_date(db, "https://scam.example/us", "2025-01-01")

    monkeypatch.setattr(weekly, "ApifyRunner", lambda: _FakeRunnerReplacesDomain())

    def _fake_build_workbook(db, path, **kw):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"fake-xlsx")

    monkeypatch.setattr(weekly, "build_workbook", _fake_build_workbook)
    monkeypatch.setattr(weekly.email_sender, "is_configured", lambda: True)
    captured = {}

    def fake_send_email(subject, html_body, to=None, attachments=None):
        captured["html"] = html_body
        return True, "ok"

    monkeypatch.setattr(weekly.email_sender, "send_email", fake_send_email)

    weekly.run(db=db, query="Q", countries="us", pages=1)

    assert "html" in captured
    assert "Changes vs previous run" in captured["html"]
    # The new run (jordanashworth.example) entered; scam.example exited.
    assert "US" in captured["html"]
    assert "+1 entered" in captured["html"]
    assert "-1 exited" in captured["html"]
