"""Tests for item 3.2 (Phase 2): evidence vault (HTML snapshots with
hash and timestamp).

Acceptance (§3.2): "A new negative
automatically generates HTML+PNG with recorded hashes; the evidence
opens from /results; the files travel in the backup."

PLATFORM ADAPTATION (documented in processing/evidence.py and
database/models.py::_init_db): local PNG via Playwright/Chromium was
DROPPED (too heavy for low-memory hosts). This suite covers the MVP: HTML snapshot
with hash, automatic trigger on new non-namesake negatives, and
button/page/download from /results.

TDD, no network: common.net.safe_urlopen from processing.evidence is
ALWAYS monkeypatched (FIX 2, hardening audit -- see the docstring of
processing.evidence: capture_url moved from requests.get to
safe_urlopen, the shared anti-SSRF guard).
"""
from conftest import _wait_for_job
from conftest import make_db as _db

from database.models import url_hash
from processing import evidence as evidence_module
from processing.evidence import AUTO_CAPTURE_LIMIT, capture_new_negatives, capture_url

RUN_ID = "run-test-1"  # matches conftest.RUN_ID (fixture seeded_db)


def _stub_get(monkeypatch, content=b"<html>ok</html>", status=200):
    """Replaces common.net.safe_urlopen (imported in processing.evidence
    as `safe_urlopen`) -- SAME real contract: truncates to max_bytes during
    the "download" (see test_capture_url_truncates_oversized_body_and_
    annotates_notes, which depends on this)."""
    captured = {}

    def fake(url, *, timeout, max_bytes, headers=None):
        captured["url"] = url
        captured["timeout"] = timeout
        captured["max_bytes"] = max_bytes
        captured["headers"] = headers
        return status, content[:max_bytes]

    monkeypatch.setattr(evidence_module, "safe_urlopen", fake)
    return captured


# --------------------------------------------------------------------- #
# Schema: evidence table + index
# --------------------------------------------------------------------- #
def test_evidence_table_exists_with_expected_columns(tmp_path):
    db = _db(tmp_path)
    conn = db._connect()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(evidence)").fetchall()}
    conn.close()
    assert {"id", "url", "url_hash", "captured_at", "html_path", "png_path",
            "sha256_html", "sha256_png", "trigger", "result_id", "notes"} <= cols


def test_evidence_url_hash_index_exists(tmp_path):
    db = _db(tmp_path)
    conn = db._connect()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    conn.close()
    assert "idx_evidence_url_hash" in names


# --------------------------------------------------------------------- #
# db methods: add_evidence / has_evidence / evidence_for / get_evidence_by_id
# --------------------------------------------------------------------- #
def test_add_and_fetch_evidence_round_trip(tmp_path):
    db = _db(tmp_path)
    h = url_hash("https://scam.example/x")
    eid = db.add_evidence(url="https://scam.example/x", url_hash=h,
                          captured_at="2026-07-15T10:00:00", html_path="/tmp/x.html",
                          sha256_html="a" * 64, trigger="auto_negative", result_id=42)

    assert db.has_evidence(h) is True
    rows = db.evidence_for(h)
    assert len(rows) == 1
    assert rows[0]["id"] == eid
    assert rows[0]["trigger"] == "auto_negative"
    assert rows[0]["result_id"] == 42
    assert rows[0]["png_path"] is None
    assert rows[0]["sha256_png"] is None

    fetched = db.get_evidence_by_id(eid)
    assert fetched["url"] == "https://scam.example/x"


def test_has_evidence_false_when_no_rows(tmp_path):
    db = _db(tmp_path)
    assert db.has_evidence(url_hash("https://never-captured.example.com/")) is False


def test_evidence_for_orders_most_recent_first(tmp_path):
    db = _db(tmp_path)
    h = url_hash("https://scam.example/x")
    id1 = db.add_evidence(url="https://scam.example/x", url_hash=h, captured_at="t1",
                          html_path="/a.html", sha256_html="a" * 64, trigger="manual")
    id2 = db.add_evidence(url="https://scam.example/x", url_hash=h, captured_at="t2",
                          html_path="/b.html", sha256_html="b" * 64, trigger="manual")
    rows = db.evidence_for(h)
    assert [r["id"] for r in rows] == [id2, id1]


def test_get_evidence_by_id_unknown_returns_none(tmp_path):
    db = _db(tmp_path)
    assert db.get_evidence_by_id(999) is None


# --------------------------------------------------------------------- #
# capture_url: best-effort network (ai_url_summary pattern), file + hash
# --------------------------------------------------------------------- #
def test_capture_url_saves_file_hash_and_row(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _stub_get(monkeypatch, content=b"<html>captured</html>")

    row = capture_url(db, "https://scam.example/article", trigger="manual", result_id=7)

    assert row is not None
    assert row["trigger"] == "manual"
    assert row["result_id"] == 7
    assert row["url_hash"] == url_hash("https://scam.example/article")
    assert len(row["sha256_html"]) == 64

    from pathlib import Path
    saved = Path(row["html_path"])
    assert saved.is_file()
    assert saved.read_bytes() == b"<html>captured</html>"
    assert saved.suffix == ".html"
    # data/evidence/{yyyy}/{mm}/{sha256[:16]}.html
    assert saved.parent.parent.parent.name == "evidence"

    assert db.has_evidence(row["url_hash"]) is True


def test_capture_url_ssl_or_network_failure_is_swallowed_best_effort(tmp_path, monkeypatch, capsys):
    """FIX 2 (hardening audit), tradeoff documented in processing.
    evidence: the "retry with verify=False" fallback is REMOVED when
    migrating to safe_urlopen (it doesn't distinguish SSLError from any
    other network failure, see its docstring) -- a site with broken TLS is
    now treated like any other best-effort failure: no capture, no
    exception, logged."""
    db = _db(tmp_path)
    monkeypatch.setattr(evidence_module, "safe_urlopen", lambda *a, **k: (None, b""))

    row = capture_url(db, "https://ssl-broken.example.com/", trigger="manual")

    assert row is None
    assert "capture blocked or failed" in capsys.readouterr().out
    assert db.has_evidence(url_hash("https://ssl-broken.example.com/")) is False


def test_capture_url_network_failure_returns_none_and_never_raises(tmp_path, monkeypatch, capsys):
    db = _db(tmp_path)

    def boom(*a, **k):
        raise ConnectionError("no network")
    monkeypatch.setattr(evidence_module, "safe_urlopen", boom)

    row = capture_url(db, "https://unreachable.example.com/", trigger="manual")

    assert row is None
    assert "capture failed" in capsys.readouterr().out


def test_capture_url_http_error_status_returns_none(tmp_path, monkeypatch, capsys):
    db = _db(tmp_path)
    _stub_get(monkeypatch, content=b"", status=404)

    row = capture_url(db, "https://gone.example.com/", trigger="manual")

    assert row is None
    assert "capture failed" in capsys.readouterr().out


def test_capture_url_truncates_oversized_body_and_annotates_notes(tmp_path, monkeypatch):
    db = _db(tmp_path)
    big = b"x" * (evidence_module._MAX_BYTES + 500)
    _stub_get(monkeypatch, content=big)

    row = capture_url(db, "https://huge.example.com/", trigger="manual")

    from pathlib import Path
    saved_bytes = Path(row["html_path"]).read_bytes()
    assert len(saved_bytes) == evidence_module._MAX_BYTES
    assert "truncat" in row["notes"].lower()
    assert len(row["sha256_html"]) == 64


def test_capture_url_sends_realistic_user_agent(tmp_path, monkeypatch):
    db = _db(tmp_path)
    captured = _stub_get(monkeypatch)

    capture_url(db, "https://scam.example/x", trigger="manual")

    assert "Mozilla" in captured["headers"]["User-Agent"]
    assert captured["timeout"] == 20
    assert captured["max_bytes"] == evidence_module._MAX_BYTES


# --------------------------------------------------------------------- #
# FIX 2 (hardening audit): real anti-SSRF -- capture_url does NOT monkeypatch
# safe_urlopen here, it exercises the REAL guard from common.net (127.0.0.1
# resolves locally, no real network).
# --------------------------------------------------------------------- #
def test_capture_url_rejects_private_ip_target_via_real_ssrf_guard(tmp_path, capsys):
    db = _db(tmp_path)

    row = capture_url(db, "http://127.0.0.1/secret", trigger="manual")

    assert row is None
    assert db.has_evidence(url_hash("http://127.0.0.1/secret")) is False
    assert "capture blocked or failed" in capsys.readouterr().out


def test_capture_url_rejects_non_http_scheme_via_real_ssrf_guard(tmp_path):
    db = _db(tmp_path)

    row = capture_url(db, "file:///etc/passwd", trigger="manual")

    assert row is None


# --------------------------------------------------------------------- #
# capture_new_negatives: automatic post-run trigger
# --------------------------------------------------------------------- #
def _seed_run_with_results(db, rows, run_id=RUN_ID):
    db.save_run(apify_run_id=run_id, queries=["q"], country_code="us", status="completed",
                summary={"total": len(rows), "positive": 0, "negative": 0, "neutral": 0})
    db.save_results(rows, run_id=run_id)


def test_capture_new_negatives_only_captures_new_non_homonym_negatives(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _stub_get(monkeypatch)
    _seed_run_with_results(db, [
        {"url": "https://neg1.example.com/", "title": "t", "description": "d",
         "classification": "negative", "country_code": "us", "domain": "neg1.example.com"},
        {"url": "https://neg2-homonym.example.com/", "title": "t", "description": "d",
         "classification": "negative", "country_code": "us", "domain": "neg2-homonym.example.com",
         "same_person": "no"},
        {"url": "https://pos1.example.com/", "title": "t", "description": "d",
         "classification": "positive", "country_code": "us", "domain": "pos1.example.com"},
    ])

    captured = capture_new_negatives(db, RUN_ID)

    assert [c["url"] for c in captured] == ["https://neg1.example.com/"]
    assert db.has_evidence(url_hash("https://neg1.example.com/")) is True
    assert db.has_evidence(url_hash("https://neg2-homonym.example.com/")) is False
    assert db.has_evidence(url_hash("https://pos1.example.com/")) is False


def test_capture_new_negatives_does_not_recapture_existing_evidence(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _stub_get(monkeypatch)
    _seed_run_with_results(db, [
        {"url": "https://neg1.example.com/", "title": "t", "description": "d",
         "classification": "negative", "country_code": "us", "domain": "neg1.example.com"},
    ])

    first = capture_new_negatives(db, RUN_ID)
    second = capture_new_negatives(db, RUN_ID)

    assert len(first) == 1
    assert len(second) == 0
    assert len(db.evidence_for(url_hash("https://neg1.example.com/"))) == 1


def test_capture_new_negatives_respects_auto_capture_limit(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _stub_get(monkeypatch)
    rows = [
        {"url": f"https://neg{i}.example.com/", "title": "t", "description": "d",
         "classification": "negative", "country_code": "us", "domain": f"neg{i}.example.com"}
        for i in range(AUTO_CAPTURE_LIMIT + 5)
    ]
    _seed_run_with_results(db, rows)

    captured = capture_new_negatives(db, RUN_ID)

    assert len(captured) == AUTO_CAPTURE_LIMIT


def test_capture_new_negatives_unknown_run_returns_empty(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _stub_get(monkeypatch)
    assert capture_new_negatives(db, "no-such-run") == []


def test_capture_new_negatives_result_id_flows_to_evidence_row(tmp_path, monkeypatch):
    db = _db(tmp_path)
    _stub_get(monkeypatch)
    _seed_run_with_results(db, [
        {"url": "https://neg1.example.com/", "title": "t", "description": "d",
         "classification": "negative", "country_code": "us", "domain": "neg1.example.com"},
    ])
    result_id = db.get_results_by_run(RUN_ID)[0]["id"]

    captured = capture_new_negatives(db, RUN_ID)

    assert captured[0]["result_id"] == result_id


class _BoomDB:
    """H2: get_results_by_run always
    raises -- capture_new_negatives must never propagate it."""

    def get_results_by_run(self, run_id):
        raise RuntimeError("boom: db no disponible")


def test_capture_new_negatives_never_raises_even_if_db_read_fails(capsys):
    assert capture_new_negatives(_BoomDB(), "r1") == []
    assert "capture_new_negatives" in capsys.readouterr().out


# --------------------------------------------------------------------- #
# Post-run chokepoints: main.run_search (POST /search) and
# report/multi_country.run_multi_country
# --------------------------------------------------------------------- #
def test_run_multi_country_triggers_auto_capture_for_new_negatives(tmp_path, monkeypatch):
    from report.multi_country import run_multi_country

    db = _db(tmp_path)
    _stub_get(monkeypatch)

    class _FakeRunner:
        def search(self, queries, country_code, language_code, max_pages, engines=None, device=None):
            # 1.1: run_multi_country now reads outcome["engine_runs"] (one
            # per engine) -- this stub simulates a single engine ("google"),
            # same pattern as ApifyRunner.search with engines=["google"].
            outcome = {
                "run_id": f"run-{country_code}", "queries": queries,
                "country_code": country_code, "status": "completed",
                "results": [{
                    "query": queries[0], "url": "https://scam.example/mc",
                    "title": "t", "description": "d", "position": 1,
                    "country_code": country_code, "domain": "scam.example",
                }],
                "features": [],
            }
            outcome["engine_runs"] = [{**outcome, "engine": "google", "error": None}]
            return outcome

    run_multi_country(_FakeRunner(), db, "Q", ["us"],
                       negative_domains=["scam.example"], max_pages=1)

    assert db.has_evidence(url_hash("https://scam.example/mc")) is True
    rows = db.evidence_for(url_hash("https://scam.example/mc"))
    assert rows[0]["trigger"] == "auto_negative"


def test_post_search_triggers_auto_capture_via_chokepoint(client, seeded_db, monkeypatch):
    import re

    import dashboard.main as main_module

    class _FakeRunner:
        def search(self, queries, country_code, language_code, max_pages, engines=None, device=None):
            # 1.1: run_search now reads outcome["engine_runs"] (one per
            # engine) -- this stub simulates a single engine ("google").
            outcome = {
                "run_id": "run-evidence-chokepoint-1", "queries": queries,
                "country_code": country_code, "status": "completed",
                "results": [{
                    "query": queries[0], "url": "https://scam.example/chokepoint",
                    "title": "t", "description": "d", "position": 1,
                    "country_code": country_code, "domain": "scam.example",
                }],
                "features": [],
            }
            outcome["engine_runs"] = [{**outcome, "engine": "google", "error": None}]
            return outcome

    monkeypatch.setattr(main_module, "get_runner", lambda: _FakeRunner())
    _stub_get(monkeypatch)

    resp = client.post("/search", data={"query": "Chokepoint Q", "country": "us", "pages": 1})
    assert resp.status_code == 200
    m = re.search(r"/job/([0-9a-f]+)", resp.text)
    assert m, resp.text
    job_id = m.group(1)

    # Poll the JobManager directly (the run_in_thread daemon thread runs
    # concurrently with the TestClient).
    # Shared helper (conftest._wait_for_job, chore F3-close top-5 #5).
    job = _wait_for_job(main_module, job_id)
    assert job["status"] == "done", job

    assert seeded_db.has_evidence(url_hash("https://scam.example/chokepoint")) is True


# --------------------------------------------------------------------- #
# Router: GET /evidence, POST /evidence/capture (audited C24), GET
# /evidence/{id}/download
# --------------------------------------------------------------------- #
def test_evidence_panel_without_url_hash_is_empty(client):
    resp = client.get("/ui/evidence", headers={"X-SM-Fragment": "1"})
    assert resp.status_code == 200
    assert "No snapshots yet." in resp.text


def test_old_evidence_page_redirects_to_the_panel(client):
    resp = client.get("/evidence", params={"url_hash": "abc"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/results?panel=/ui/evidence%3Furl_hash%3Dabc"


def test_evidence_page_without_url_hash_lists_every_snapshot(client, seeded_db):
    """/evidence is the vault's index: with no url_hash it lists ALL
    snapshots (newest first), each linking to its per-URL view."""
    for url, captured in (("https://scam.example/one", "2026-07-15T10:00:00"),
                          ("https://other.example/two", "2026-07-16T10:00:00")):
        seeded_db.add_evidence(url=url, url_hash=url_hash(url), captured_at=captured,
                               html_path="/tmp/x.html", sha256_html="c" * 64, trigger="manual")

    resp = client.get("/ui/evidence", headers={"X-SM-Fragment": "1"})

    assert resp.status_code == 200
    assert "2 snapshots, newest first." in resp.text
    assert resp.text.index("<b class='break'>other.example/two") < resp.text.index("<b class='break'>scam.example/one")
    # per-URL filter
    assert f"<option value='{url_hash('https://scam.example/one')}'>" in resp.text
    assert "cccccccccccc…" in resp.text      # short SHA-256 in mono


def test_evidence_page_lists_snapshots_for_url_hash(client, seeded_db):
    h = url_hash("https://scam.example/article")
    seeded_db.add_evidence(url="https://scam.example/article", url_hash=h,
                           captured_at="2026-07-15T10:00:00", html_path="/tmp/x.html",
                           sha256_html="a" * 64, trigger="auto_negative")

    resp = client.get(f"/ui/evidence?url_hash={h}", headers={"X-SM-Fragment": "1"})

    assert resp.status_code == 200
    assert "scam.example" in resp.text
    assert "automatic" in resp.text  # trigger auto_negative, shown in plain words


def test_post_evidence_capture_success_redirects_and_audits(client, seeded_db, monkeypatch):
    _stub_get(monkeypatch, content=b"<html>captured via form</html>")

    resp = client.post("/evidence/capture",
                       data={"url": "https://scam.example/from-form", "result_id": 1},
                       follow_redirects=False)

    assert resp.status_code == 303  # PRG back to Results with the Evidence panel open
    h0 = url_hash("https://scam.example/from-form")
    assert resp.headers["location"] == f"/results?panel=/ui/evidence%3Furl_hash%3D{h0}"

    h = url_hash("https://scam.example/from-form")
    assert seeded_db.has_evidence(h) is True

    audited = seeded_db.list_audit(action="evidence_capture")
    assert len(audited) == 1
    assert audited[0]["resource_id"] == "https://scam.example/from-form"
    assert '"trigger": "manual"' in audited[0]["params_json"]


def test_post_evidence_capture_failure_returns_502_and_does_not_audit(client, seeded_db, monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("no network")
    monkeypatch.setattr(evidence_module, "safe_urlopen", boom)

    resp = client.post("/evidence/capture", data={"url": "https://unreachable.example.com/",
                                                  "next": "/results?country=us"},
                       follow_redirects=False)

    # PRG back to the page it came from, with a friendly error toast (no raw error).
    assert resp.status_code == 303 and resp.headers["location"] == "/results?country=us"
    page = client.get("/results?country=us").text
    assert "Couldn&#x27;t save a snapshot: the site didn&#x27;t answer" in page
    assert "flash-error" in page
    assert seeded_db.list_audit(action="evidence_capture") == []


def test_download_existing_evidence_serves_html(client, seeded_db, monkeypatch):
    _stub_get(monkeypatch, content=b"<html>downloadable</html>")
    row = capture_url(seeded_db, "https://scam.example/dl", trigger="manual")

    resp = client.get(f"/evidence/{row['id']}/download")

    assert resp.status_code == 200
    assert resp.content == b"<html>downloadable</html>"
    # Review 3.2 H1: third-party content must never be rendered under this
    # origin -- downloads as octet-stream with attachment disposition.
    assert "application/octet-stream" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]


def test_download_path_outside_vault_is_404(client, seeded_db, tmp_path):
    # Review 3.2 H2: a row whose html_path points outside data/evidence
    # (corruption/future bug) is never served, even if the file exists.
    outside = tmp_path / "leaked-secret.html"
    outside.write_text("<html>outside the vault</html>")
    h = url_hash("https://scam.example/outside")
    eid = seeded_db.add_evidence(url="https://scam.example/outside", url_hash=h,
                                 captured_at="2026-07-15T10:00:00",
                                 html_path=str(outside),
                                 sha256_html="b" * 64, trigger="manual")

    resp = client.get(f"/evidence/{eid}/download")

    assert resp.status_code == 404


def test_download_unknown_evidence_id_is_404(client):
    resp = client.get("/evidence/999999/download")
    assert resp.status_code == 404


def test_download_evidence_missing_file_on_disk_is_404(client, seeded_db):
    h = url_hash("https://scam.example/gone")
    eid = seeded_db.add_evidence(url="https://scam.example/gone", url_hash=h,
                                 captured_at="2026-07-15T10:00:00",
                                 html_path="/tmp/does-not-exist-evidence-file.html",
                                 sha256_html="a" * 64, trigger="manual")

    resp = client.get(f"/evidence/{eid}/download")

    assert resp.status_code == 404


# --------------------------------------------------------------------- #
# /results: "Capture" button (negative rows) vs "N snapshots" link per row
# --------------------------------------------------------------------- #
def test_results_page_shows_capture_button_when_no_evidence(client):
    resp = client.get("/results")
    assert resp.status_code == 200
    assert "Save evidence snapshot</button>" in resp.text
    assert "/evidence/capture" in resp.text


def test_results_page_offers_capture_only_on_negative_rows(client):
    """SAMPLE_RESULTS has 2 negatives without evidence: one Capture form each,
    none for the positive/neutral rows."""
    resp = client.get("/results")
    assert resp.text.count("action='/evidence/capture'") == 2


def test_results_page_shows_snapshot_link_when_evidence_exists(client, seeded_db):
    # SAMPLE_RESULTS from conftest.py includes "https://scam.example/article"
    # (negative) -- we capture evidence for that exact URL.
    h = url_hash("https://scam.example/article")
    seeded_db.add_evidence(url="https://scam.example/article", url_hash=h,
                           captured_at="2026-07-15T10:00:00", html_path="/tmp/x.html",
                           sha256_html="a" * 64, trigger="auto_negative")

    resp = client.get("/results")

    assert resp.status_code == 200
    assert f"/evidence?url_hash={h}" in resp.text
    assert "snapshot" in resp.text
