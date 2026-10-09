"""Multi-subject background work, notifications, exports and API.

Scheduled jobs iterate the scheduled subjects (active, non-demo), every alert
names its "Project ▸ Subject", the digest groups by subject, exports carry the
subject they were built for, and /api/v1 takes a `subject` parameter with
pinned keys. No network: fake runner, stubbed senders.
"""
import re
from pathlib import Path

import openpyxl
import pytest

from database.models import Database
from notify import dispatcher
from report import weekly

_NEW_KEY_RE = re.compile(r"id='new-api-key'[^>]*>([^<]+)</code>")


class _FakeRunner:
    """One neutral result per country, tagged with the searched query (neutral:
    no evidence capture, no alert -- nothing touches the network)."""

    def __init__(self, calls):
        self.calls = calls

    def search(self, queries, country_code, language_code, max_pages, engines=None, device=None):
        q = queries[0]
        self.calls.append((q, country_code))
        slug = re.sub(r"[^a-z]", "", q.lower())
        outcome = {
            "run_id": f"run-{slug}-{country_code}", "queries": queries,
            "country_code": country_code, "status": "completed",
            "results": [{"query": q, "url": f"https://dir.example/{slug}", "title": "Profile",
                         "description": "directory entry", "displayed_url": "dir.example",
                         "position": 1, "country_code": country_code, "domain": "dir.example"}],
        }
        outcome["engine_runs"] = [{**outcome, "engine": "google", "error": None}]
        return outcome


@pytest.fixture
def three_subjects(tmp_path):
    """Subject 1 (Alice Main, project "My project"), subject 2 (Bob Second, project
    Acme) and a demo subject. Returns (db, s2, demo)."""
    db = Database(db_path=str(tmp_path / "t.db"))
    db.update_subject(1, name="Alice Main", queries=["Alice Main"], countries=["us"])
    acme = db.add_client("Acme")
    s2 = db.add_subject("Bob Second", countries=["gb"], client_id=acme)
    demo = db.add_subject("Demo Person", countries=["us"], is_demo=True)
    return db, s2, demo


def _stub_weekly(monkeypatch, tmp_path, calls, email=False, sent=None):
    monkeypatch.setattr(weekly, "project_root", tmp_path)
    monkeypatch.setattr(weekly, "ApifyRunner", lambda: _FakeRunner(calls))

    def fake_wb(db, path, **kw):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"xlsx:" + (kw.get("subject_name") or "").encode())

    monkeypatch.setattr(weekly, "build_workbook", fake_wb)
    monkeypatch.setattr(weekly.email_sender, "is_configured", lambda: email)
    if sent is not None:
        monkeypatch.setattr(weekly.email_sender, "send_email",
                            lambda subject, body, **k: sent.append((subject, body)) or (True, "ok"))


# --------------------------------------------------------------------- #
# Scheduler: per-subject iteration
# --------------------------------------------------------------------- #
def test_scheduled_subjects_exclude_demo_inactive_and_archived(three_subjects):
    db, s2, _demo = three_subjects
    assert [s["id"] for s in db.scheduled_subjects()] == [1, s2]
    gone = db.add_subject("Gone Person", countries=["us"])
    db.deactivate_subject(gone)
    archived_project = db.add_client("Old client")
    db.add_subject("Archived Person", countries=["us"], client_id=archived_project)
    db.set_client_active(archived_project, False)
    assert [s["id"] for s in db.scheduled_subjects()] == [1, s2]


def test_weekly_job_runs_once_per_non_demo_subject(three_subjects, tmp_path, monkeypatch):
    import scheduler.jobs  # noqa: F401 -- populates JOBS
    from scheduler.core import JOBS

    db, s2, demo = three_subjects
    calls, sent = [], []
    _stub_weekly(monkeypatch, tmp_path, calls, email=True, sent=sent)

    results = JOBS["weekly_report"]["func"](db)

    assert sorted(results) == [1, s2]
    assert sorted(calls) == [("Alice Main", "us"), ("Bob Second", "gb")]
    # Each subject's runs live in that subject only; the demo got nothing.
    assert [r["country_code"] for r in db.for_subject(1).list_runs()] == ["us"]
    assert [r["country_code"] for r in db.for_subject(s2).list_runs()] == ["gb"]
    assert db.for_subject(demo).list_runs() == []
    # One email per subject, titled with "Project ▸ Subject".
    subjects = sorted(s for s, _b in sent)
    assert subjects[0].startswith("Weekly SEO report — Acme ▸ Bob Second — ")
    assert subjects[1].startswith("Weekly SEO report — My project ▸ Alice Main — ")
    # The workbook is stored per subject with the Reports-modal metadata.
    from export import reports as store
    exports = tmp_path / "data" / "exports"
    # Report files print "Project › Subject" (the PDF font has no ▸).
    for sid, label in ((1, "My project › Alice Main"), (s2, "Acme › Bob Second")):
        files = store.list_reports(exports, sid)
        assert len(files) == 1
        meta = files[0]
        assert meta["file"].startswith("weekly_") and meta["source"] == "weekly"
        assert meta["emailed"] is True and meta["subject_id"] == sid
        assert (exports / str(sid) / meta["file"]).read_bytes() == b"xlsx:" + label.encode()
        assert meta["subject"] == label
    assert store.list_reports(exports, demo) == []


def test_one_failing_subject_does_not_stop_the_others(three_subjects, tmp_path, monkeypatch):
    db, s2, _demo = three_subjects
    calls = []
    _stub_weekly(monkeypatch, tmp_path, calls)
    real_run = weekly.run

    def flaky(db=None, **kw):
        if db._effective_subject_id() == 1:
            raise RuntimeError("apify down")
        return real_run(db=db, **kw)

    monkeypatch.setattr(weekly, "run", flaky)
    with pytest.raises(RuntimeError, match="subject"):
        weekly.run_scheduled(db)
    assert [r["country_code"] for r in db.for_subject(s2).list_runs()] == ["gb"]


def test_weekly_run_sets_the_subject_context_for_ai_code(three_subjects, tmp_path, monkeypatch):
    """Code reading the ContextVar (ai_classifier.subject_name via the injected
    singleton) sees the subject being processed, not subject 1."""
    from database.base import current_subject_id

    db, s2, _demo = three_subjects
    seen = []
    monkeypatch.setattr(weekly, "run", lambda db=None, **kw: seen.append(
        (db._effective_subject_id(), current_subject_id())))
    weekly.run_scheduled(db)
    assert seen == [(1, 1), (s2, s2)]


def test_alerts_check_job_creates_alerts_per_subject(three_subjects, tmp_path, monkeypatch):
    db, s2, demo = three_subjects
    calls = []
    _stub_weekly(monkeypatch, tmp_path, calls)
    # Two runs per subject (old date with nothing negative, new one negative).
    for sid in (1, s2, demo):
        sdb = db.for_subject(sid)
        for run_id, date, cls in ((f"old-{sid}", "2026-01-01", "neutral"),
                                  (f"new-{sid}", "2026-02-01", "negative")):
            sdb.save_run(apify_run_id=run_id, queries=["q"], country_code="us", status="completed",
                         summary={"total": 1, "positive": 0, "negative": int(cls == "negative"),
                                  "neutral": int(cls == "neutral")})
            sdb.save_results([{"query": "q", "url": "https://bad.example/x", "title": "t",
                               "description": "d", "displayed_url": "bad.example", "position": 1,
                               "country_code": "us", "domain": "bad.example",
                               "classification": cls}], run_id=run_id)
            conn = sdb._connect()
            conn.execute("UPDATE search_results SET scraped_at = ? WHERE run_id = ?",
                         (f"{date}T00:00:00", run_id))
            conn.commit()
            conn.close()

    out = weekly.run_alert_checks_scheduled(db)

    assert sorted(out) == [1, s2]
    for sid in (1, s2):
        kinds = sorted(a["kind"] for a in db.for_subject(sid).list_alerts(status="all"))
        assert "negative_new" in kinds
    assert db.for_subject(demo).list_alerts(status="all") == []
    # Dedupe is per subject: the SAME dedupe key exists once in each subject.
    keys = {sid: {a["dedupe_key"] for a in db.for_subject(sid).list_alerts(status="all")}
            for sid in (1, s2)}
    assert keys[1] & keys[s2]
    # Running again creates nothing new anywhere.
    again = weekly.run_alert_checks_scheduled(db)
    assert all(r["new_alerts"] == [] for r in again.values())


def test_purge_checks_run_deployment_wide(three_subjects):
    from scheduler.jobs import purge_checks

    db, s2, _demo = three_subjects
    for sid in (1, s2):
        sdb = db.for_subject(sid)
        aid = sdb.add_alert("threshold", "warning", "US", "old", f"old-{sid}")
        sdb.resolve_alert(aid, by="t")
        conn = sdb._connect()
        conn.execute("UPDATE alerts SET resolved_at = '2000-01-01T00:00:00' WHERE id = ?", (aid,))
        conn.commit()
        conn.close()
    assert purge_checks(db)["alerts_resolved"] == 2


# --------------------------------------------------------------------- #
# Notifications: "Project ▸ Subject: " prefix, digest grouped by subject
# --------------------------------------------------------------------- #
def test_alert_messages_are_prefixed_with_project_and_subject(three_subjects, monkeypatch):
    db, s2, _demo = three_subjects
    db.set_routing("critical", "telegram")
    db.set_routing("warning", "email")
    sent, mails = [], []
    monkeypatch.setattr(dispatcher, "send_via", lambda d, ch, text, **k: sent.append(text) or (True, ""))
    monkeypatch.setattr(dispatcher.email_sender, "is_configured", lambda: True)
    monkeypatch.setattr(dispatcher.email_sender, "send_email",
                        lambda subject, body, **k: mails.append((subject, body)) or (True, "ok"))

    dispatcher.emit_alert(db.for_subject(s2), "threshold", "critical", "GB", "3 negatives", "k1")
    dispatcher.emit_alert(db.for_subject(1), "negative_new", "warning", "US", "new neg", "k2")

    assert sent == ["Acme ▸ Bob Second: CRITICAL · threshold · GB · 3 negatives"]
    assert mails == [("My project ▸ Alice Main: negative_new", "My project ▸ Alice Main: new neg")]


def test_deployment_alerts_have_no_subject_prefix(three_subjects, monkeypatch):
    db, _s2, _demo = three_subjects
    db.set_routing("warning", "telegram")
    sent = []
    monkeypatch.setattr(dispatcher, "send_via", lambda d, ch, text, **k: sent.append(text) or (True, ""))
    dispatcher._dispatch_now(db, {"id": 9, "kind": "job_missed", "severity": "warning",
                                  "country": "", "message": "weekly_report missed"})
    assert sent == ["WARNING · job_missed · weekly_report missed"]


def test_digest_is_one_email_with_a_section_per_subject(three_subjects, monkeypatch):
    db, s2, demo = three_subjects
    db.for_subject(1).add_alert("url_gone", "info", "US", "alice info", "d1")
    db.for_subject(s2).add_alert("url_gone", "info", "GB", "bob info 1", "d2")
    db.for_subject(s2).add_alert("url_gone", "info", "GB", "bob info 2", "d3")
    db.for_subject(demo).add_alert("url_gone", "info", "US", "demo info", "d4")
    mails = []
    monkeypatch.setattr(dispatcher.email_sender, "send_email",
                        lambda subject, body, **k: mails.append((subject, body)) or (True, "ok"))

    assert dispatcher.send_info_digest(db) is True

    assert len(mails) == 1
    subject, body = mails[0]
    assert subject == "SEO info digest (3)"
    alice = body.index("<h3>My project ▸ Alice Main (1)</h3>")
    bob = body.index("<h3>Acme ▸ Bob Second (2)</h3>")
    assert alice < body.index("alice info") < bob < body.index("bob info 1")
    assert "demo info" not in body
    assert dispatcher.send_info_digest(db) is False      # already digested


# --------------------------------------------------------------------- #
# Exports carry their subject
# --------------------------------------------------------------------- #
def test_workbook_title_is_the_subject_it_was_built_for(three_subjects, tmp_path):
    from export.excel_exporter import build_workbook

    db, s2, _demo = three_subjects
    out = tmp_path / "b.xlsx"
    build_workbook(db.for_subject(s2), str(out))
    assert openpyxl.load_workbook(out)["SUM"]["A1"].value == "SERP MONITOR  ·  Acme › Bob Second"
    build_workbook(db.for_subject(1), str(out), subject_name="My project ▸ Alice Main")
    assert openpyxl.load_workbook(out)["SUM"]["A1"].value == "SERP MONITOR  ·  My project › Alice Main"


def test_pdf_subtitle_and_countries_follow_the_subject(three_subjects, tmp_path, monkeypatch):
    from export import pdf_report

    db, s2, _demo = three_subjects
    sdb = db.for_subject(s2)
    for c in ("gb", "fr"):
        sdb.save_run(apify_run_id=f"r-{c}", queries=["q"], country_code=c, status="completed",
                     summary={"total": 0, "positive": 0, "negative": 0, "neutral": 0})
    seen = {}
    real = pdf_report._cover_elements

    def spy(db_, branding, sty, **kw):
        el, latest = real(db_, branding, sty, **kw)
        seen.update(subtitle=kw["subtitle"], countries=[r["country_code"] for r in latest])
        return el, latest

    monkeypatch.setattr(pdf_report, "_cover_elements", spy)
    pdf_report.build_pdf(sdb, str(tmp_path / "r.pdf"), countries=["gb"])
    assert seen["subtitle"].startswith("Acme › Bob Second &middot; Executive report")
    assert seen["countries"] == ["gb"]


def test_report_store_titles_use_project_and_subject(three_subjects, tmp_path, monkeypatch):
    from export import excel_exporter, reports

    db, s2, _demo = three_subjects
    got = {}
    monkeypatch.setattr(excel_exporter, "build_workbook",
                        lambda d, p, **kw: got.update(kw) or Path(p).write_bytes(b"x"))
    meta = reports.create_report(db.for_subject(s2), tmp_path, s2, "xlsx")
    assert got["subject_name"] == "Acme › Bob Second"
    assert meta["subject_id"] == s2 and (tmp_path / str(s2) / meta["file"]).is_file()


def test_legacy_flat_report_files_move_to_subject_1(tmp_path):
    from export import reports

    (tmp_path / "weekly_2026-01-05.xlsx").write_bytes(b"old")
    (tmp_path / "notes.txt").write_text("not a report")
    (tmp_path / "2").mkdir()
    assert reports.adopt_legacy_files(tmp_path) == 1
    assert reports.adopt_legacy_files(tmp_path) == 0
    files = reports.list_reports(tmp_path, 1)
    assert [f["file"] for f in files] == ["weekly_2026-01-05.xlsx"]
    assert files[0]["title"] == "Weekly report" and files[0]["source"] == "weekly"
    assert (tmp_path / "notes.txt").is_file()


# --------------------------------------------------------------------- #
# API: subject param, pinned keys, backward compatibility
# --------------------------------------------------------------------- #
def _key(client, scope="read", subject_id=""):
    r = client.post("/settings/api-keys", data={"name": "k", "scope": scope, "subject_id": subject_id})
    assert r.status_code == 200
    return _NEW_KEY_RE.search(r.text).group(1)


@pytest.fixture
def two_subject_api(client, seeded_db):
    acme = seeded_db.add_client("Acme")
    s2 = seeded_db.add_subject("Bob Second", countries=["gb"], client_id=acme)
    sdb = seeded_db.for_subject(s2)
    sdb.save_run(apify_run_id="bob-run", queries=["Bob"], country_code="gb", status="completed",
                 summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    sdb.save_results([{"query": "Bob", "url": "https://bob.example/x", "title": "t",
                       "description": "d", "displayed_url": "bob.example", "position": 1,
                       "country_code": "gb", "domain": "bob.example",
                       "classification": "negative"}], run_id="bob-run")
    alert_id = sdb.add_alert("threshold", "warning", "GB", "bob alert", "bob-a")
    return s2, alert_id


def test_api_defaults_to_subject_1_and_accepts_a_subject_param(client, two_subject_api):
    s2, _aid = two_subject_api
    h = {"X-API-Key": _key(client)}
    default = client.get("/api/v1/results", headers=h).json()
    assert {r["subject_id"] for r in default} == {1} and len(default) == 5
    bob = client.get(f"/api/v1/results?subject={s2}", headers=h).json()
    assert [r["url"] for r in bob] == ["https://bob.example/x"] and bob[0]["subject_id"] == s2
    runs = client.get(f"/api/v1/runs?subject={s2}", headers=h).json()
    assert [r["apify_run_id"] for r in runs] == ["bob-run"]
    alerts = client.get(f"/api/v1/alerts?subject={s2}", headers=h).json()
    assert [a["message"] for a in alerts] == ["bob alert"] and alerts[0]["subject_id"] == s2
    stats = client.get(f"/api/v1/stats?subject={s2}", headers=h).json()
    assert stats["subject_id"] == s2 and stats["total_results"] == 1
    assert client.get("/api/v1/results?subject=999", headers=h).status_code == 404


def test_pinned_key_only_reaches_its_subject(client, two_subject_api):
    s2, _aid = two_subject_api
    h = {"X-API-Key": _key(client, subject_id=str(s2))}
    pinned = client.get("/api/v1/results", headers=h).json()
    assert [r["url"] for r in pinned] == ["https://bob.example/x"]
    assert client.get(f"/api/v1/runs?subject={s2}", headers=h).status_code == 200
    for path in ("/api/v1/results", "/api/v1/runs", "/api/v1/alerts", "/api/v1/stats"):
        assert client.get(f"{path}?subject=1", headers=h).status_code == 403, path


def test_patch_alert_is_scoped_to_the_subject(client, seeded_db, two_subject_api):
    s2, aid = two_subject_api
    h = {"X-API-Key": _key(client, scope="write:alerts")}
    body = {"status": "acknowledged"}
    # Default subject 1 doesn't own this alert: not found, untouched.
    assert client.patch(f"/api/v1/alerts/{aid}", json=body, headers=h).status_code == 404
    r = client.patch(f"/api/v1/alerts/{aid}?subject={s2}", json=body, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "acknowledged"
    assert r.json()["subject_id"] == s2
    pinned = {"X-API-Key": _key(client, scope="write:alerts", subject_id="1")}
    assert client.patch(f"/api/v1/alerts/{aid}?subject={s2}", json=body,
                        headers=pinned).status_code == 403


def test_api_key_settings_offer_all_subjects_or_one(client, seeded_db, two_subject_api):
    s2, _aid = two_subject_api
    page = client.get("/ui/settings/api-key/new", headers={"X-SM-Fragment": "1"}).text
    assert "<option value='' selected>All subjects</option>" in page
    assert f"<option value='{s2}'>Acme ▸ Bob Second</option>" in page
    _key(client, subject_id=str(s2))
    _key(client)
    rows = seeded_db.list_api_keys()
    assert sorted((r["subject_id"] or 0) for r in rows) == [0, s2]
    page = client.get("/settings").text
    assert "<td>Acme ▸ Bob Second</td>" in page
    assert "<td>All subjects</td>" in page
    bad = client.post("/settings/api-keys", data={"name": "x", "scope": "read", "subject_id": "999"})
    assert "unknown subject" in bad.text and len(seeded_db.list_api_keys()) == 2


def test_openapi_documents_the_subject_param(client):
    spec = client.get("/openapi.json").json()
    for path in ("/api/v1/results", "/api/v1/runs", "/api/v1/alerts", "/api/v1/stats"):
        params = {p["name"] for p in spec["paths"][path]["get"]["parameters"]}
        assert "subject" in params, path
    patch = spec["paths"]["/api/v1/alerts/{alert_id}"]["patch"]
    assert "subject" in {p["name"] for p in patch["parameters"]}


# --------------------------------------------------------------------- #
# Viewer (read-only, one project)
# --------------------------------------------------------------------- #
def _login(client, db, username, role, client_id=None):
    db.add_user(username, f"{username}-pass", role=role, client_id=client_id)
    r = client.post("/login", data={"username": username, "password": f"{username}-pass"},
                    follow_redirects=False)
    client.cookies.set("sid", r.cookies["sid"])


def test_viewer_is_limited_to_their_project(client, seeded_db, tmp_path, monkeypatch):
    from dashboard import deps
    from export import reports

    monkeypatch.setattr(deps, "exports_dir", tmp_path)
    acme = seeded_db.add_client("Acme")
    s2 = seeded_db.add_subject("Bob Second", countries=["gb"], client_id=acme)
    (tmp_path / "1").mkdir()
    (tmp_path / "1" / "workbook_x1.xlsx").write_bytes(b"x")
    reports.register_file(tmp_path, 1, tmp_path / "1" / "workbook_x1.xlsx", "xlsx")
    _login(client, seeded_db, "viewer", "client_viewer", client_id=acme)
    # Subject 1's report and data are out of reach.
    assert client.get("/reports/1/workbook_x1.xlsx").status_code == 404
    client.cookies.set("subject", "1")
    assert "jordanashworth.example/about" not in client.get("/results").text
    # Read-only: no writes, no API keys, no searches.
    assert client.post("/settings/api-keys", data={"name": "x"}).status_code == 403
    assert client.post("/search_multi", data={"countries": ["gb"]}).status_code == 403
    assert seeded_db.list_api_keys() == []
    assert s2


def test_viewer_role_is_labelled_read_only(admin_client):
    assert "Viewer (read-only)" in admin_client.get("/settings").text


def test_default_subject_listing_never_includes_another_subjects_alert(client, two_subject_api):
    _s2, aid = two_subject_api
    h = {"X-API-Key": _key(client)}
    assert all(a["id"] != aid for a in client.get("/api/v1/alerts", headers=h).json())
