"""SECURITY.md items 4, 7, 8: row-level scoping per project/subject, role
enforcement on every route, and tamper-proof ids/roles/redirects.

Two projects: A (the default project, subject 1, seeded by conftest) and B
(subject "Beta Person"). The attackers are a read-only Viewer of project A
and an *operator tied to project A* (restricted to A's subjects but allowed
to write) -- the strongest non-admin. Every route family is tried against
B's rows; B's data must neither show up nor change."""
import re
import sqlite3
from pathlib import Path

import pytest
from conftest import app_routes, sign_in

ADMIN_PW = "correct horse battery"
B_MARK = "secret-b.example"
B_NOTE = "B-private-note-text"


@pytest.fixture
def two_projects(seeded_db, tmp_path):
    db = seeded_db
    pid_b = db.add_client("Client B")
    sid_b = db.add_subject("Beta Person", countries=["us"], client_id=pid_b)
    b = db.for_subject(sid_b)
    b.save_run(apify_run_id="run-b", queries=["Beta Person"], country_code="us",
               status="completed", summary={"total": 1, "positive": 0, "negative": 1,
                                            "neutral": 0})
    b.save_results([{"query": "Beta Person", "url": f"https://{B_MARK}/x", "title": "B title",
                     "description": "B snippet", "displayed_url": B_MARK, "position": 1,
                     "country_code": "us", "domain": B_MARK, "classification": "negative"}],
                   run_id="run-b", subject_id=sid_b)
    b_result = b.get_results_filtered(country_code="us", limit=5)[0]
    b.set_note(f"https://{B_MARK}/x", B_NOTE)
    b_note = b.list_notes()[0]
    b_alert = b.add_alert("negative_threshold", "warning", "us", f"B alert {B_MARK}",
                          dedupe_key="b-alert", subject_id=sid_b)
    b.add_event("2026-01-01", f"B event {B_MARK}")
    b_event = b.list_events()[0]
    vault = Path(db.db_path).parent / "evidence"
    vault.mkdir(exist_ok=True)
    html = vault / "b.html"
    html.write_text(f"<html>{B_MARK}</html>")
    b_ev = b.add_evidence(f"https://{B_MARK}/x", "h" * 64, "2026-01-01T00:00:00", str(html),
                          "0" * 64, "manual")
    exports = Path(db.db_path).parent / "exports" / str(sid_b)
    exports.mkdir(parents=True, exist_ok=True)
    (exports / "beta-report.csv").write_text(f"url\nhttps://{B_MARK}/x\n")
    db.add_user("viewer_a", "viewer-a-password", role="client_viewer", client_id=1)
    db.add_user("op_a", "op-a-password-1", role="operator", client_id=1)
    db.add_user("boss", ADMIN_PW, role="admin")
    return {"sid_b": sid_b, "pid_b": pid_b, "result": b_result["id"], "note": b_note["id"],
            "alert": b_alert, "event": b_event["id"], "evidence": b_ev, "db": b}


def _snapshot(db_path: str, sid: int) -> list:
    conn = sqlite3.connect(db_path)
    try:
        out = []
        for table in ("search_results", "url_notes", "alerts", "events", "runs", "evidence"):
            out.append(conn.execute(f"SELECT * FROM {table} WHERE subject_id = ? ORDER BY 1",  # noqa: S608 -- fixed table names
                                    (sid,)).fetchall())
        out.append(conn.execute("SELECT * FROM subjects WHERE id = ?", (sid,)).fetchall())
        return out
    finally:
        conn.close()


# --------------------------------------------------------------------- #
# Item 4: reads of subject B by project A's users
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("who,pw", [("viewer_a", "viewer-a-password"), ("op_a", "op-a-password-1")])
def test_project_a_users_cannot_read_subject_b(client, strict_auth, seeded_db, two_projects,
                                               who, pw):
    t = two_projects
    sign_in(client, seeded_db, who, pw)
    client.cookies.set("subject", str(t["sid_b"]))       # tampered cookie
    sid_b = t["sid_b"]
    reads = [
        "/", "/results", f"/results?subject={sid_b}", "/ui/runs", "/ui/run/run-b", "/run/run-b",
        "/ui/review", f"/ui/review?id={t['result']}", "/ui/notes", f"/ui/note?url=https://{B_MARK}/x",
        "/ui/alerts", "/ui/timeline", "/ui/evidence", "/ui/compare?country=us",
        "/search-text?q=Beta", "/search-text?q=title", "/ui/switcher", "/jobs/active",
        f"/ui/reports?subject_id={sid_b}&tab=prev", f"/ui/search?subject={sid_b}",
        f"/ui/subject/{sid_b}/edit", "/assistant/status", "/assistant/suggestions",
        "/assistant/panel", "/settings",
    ]
    for path in reads:
        resp = client.get(path, headers={"X-SM-Fragment": "1"}, follow_redirects=True)
        assert B_NOTE not in resp.text and "Beta Person" not in resp.text, path
        if "url=" not in path:      # a URL the viewer typed may be echoed back
            assert B_MARK not in resp.text, path
    for path in (f"/evidence/{t['evidence']}/download", f"/reports/{sid_b}/beta-report.csv",
                 "/assistant/reports/beta-report.csv"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code in (403, 404), path
        assert B_MARK not in resp.text, path


def test_api_key_pinned_to_a_cannot_read_b(client, seeded_db, two_projects):
    _id, key = seeded_db.add_api_key("a-only", "admin", subject_id=1)
    h = {"X-API-Key": key}
    sid_b = two_projects["sid_b"]
    for path in ("/api/v1/results", "/api/v1/runs", "/api/v1/alerts", "/api/v1/stats"):
        assert client.get(path, params={"subject": sid_b}, headers=h).status_code == 403
        assert B_MARK not in client.get(path, headers=h).text
    resp = client.patch(f"/api/v1/alerts/{two_projects['alert']}", json={"status": "resolved"},
                        headers=h)
    assert resp.status_code == 404
    assert two_projects["db"].get_alert(two_projects["alert"])["status"] == "open"


def test_jobs_of_subject_b_are_invisible(client, strict_auth, seeded_db, two_projects):
    from dashboard import main
    job_id = main.jobs.create("single", meta={"subject_id": two_projects["sid_b"],
                                              "query": "Beta Person"})
    sign_in(client, seeded_db, "op_a", "op-a-password-1")
    assert client.get(f"/job/{job_id}/status").status_code == 404
    assert client.get(f"/ui/job/{job_id}", headers={"X-SM-Fragment": "1"}).status_code in (403, 404)
    assert job_id not in client.get("/jobs/active").text
    assert client.post(f"/jobs/{job_id}/seen").status_code == 404


def test_assistant_tools_are_scoped_to_the_active_subject(seeded_db, two_projects, tmp_path):
    from assistant import tools
    ctx = tools.ToolContext(db=seeded_db.for_subject(1), subject_id=1, role="admin",
                            user="op_a", exports_dir=tmp_path)
    rid = two_projects["result"]
    for name, args in (("explain_result", {"result_id": rid}),
                       ("set_result_label", {"result_id": rid, "label": "positive"}),
                       ("acknowledge_alert", {"alert_id": two_projects["alert"]})):
        tool = tools.get_tool(name)
        with pytest.raises(tools.ToolError):
            (tool.prepare or tool.executor)(ctx, tools.validate_args(tool, args))
        with pytest.raises(tools.ToolError):
            tool.executor(ctx, tools.validate_args(tool, args))
    for name in ("list_results", "list_alerts", "list_notes", "list_runs", "list_reports"):
        tool = tools.get_tool(name)
        out = str(tool.executor(ctx, tools.validate_args(tool, {})))
        assert B_MARK not in out and B_NOTE not in out, name


# --------------------------------------------------------------------- #
# Item 4: writes to subject B by project A's operator
# --------------------------------------------------------------------- #
def test_restricted_operator_cannot_modify_subject_b(client, strict_auth, seeded_db, two_projects):
    t = two_projects
    before = _snapshot(seeded_db.db_path, t["sid_b"])
    sign_in(client, seeded_db, "op_a", "op-a-password-1")
    client.cookies.set("subject", str(t["sid_b"]))
    sid_b = t["sid_b"]
    attempts = [
        (f"/review/{t['result']}/verdict", {"classification": "positive"}),
        (f"/review/{t['result']}/not-this-person", {}),
        ("/notes/status", {"note_id": t["note"], "to_status": "resolved"}),
        ("/alerts/ack", {"alert_id": t["alert"]}),
        ("/alerts/resolve", {"alert_id": t["alert"]}),
        ("/alerts/snooze", {"alert_id": t["alert"], "until": "7d"}),
        ("/timeline/delete", {"event_id": t["event"]}),
        (f"/subjects/{sid_b}/edit", {"name": "Hijacked", "type": "person", "countries": "us"}),
        (f"/subjects/{sid_b}/deactivate", {}),
        (f"/projects/{t['pid_b']}/rename", {"name": "Hijacked"}),
        (f"/projects/{t['pid_b']}/archive", {}),
        ("/run/run-b/delete", {}),
        ("/reports/create", {"kind": "csv", "subject_id": sid_b, "period": "30d"}),
        ("/search", {"country": "us", "subject_id": sid_b, "query": "Beta Person"}),
        ("/domains/add", {"domain": "evil.example", "subject_id": sid_b}),
        ("/assistant/confirm", {"id": "whatever"}),
    ]
    for path, data in attempts:
        resp = client.post(path, data=data, follow_redirects=False)
        assert resp.status_code < 500, path
        assert B_MARK not in resp.text, path
    assert _snapshot(seeded_db.db_path, t["sid_b"]) == before
    exports = Path(seeded_db.db_path).parent / "exports" / str(sid_b)
    assert sorted(p.name for p in exports.iterdir()) == ["beta-report.csv"]
    assert seeded_db.get_client(t["pid_b"])["name"] == "Client B"


# --------------------------------------------------------------------- #
# Item 7: every route requires auth; write routes reject viewers
# --------------------------------------------------------------------- #
_PUBLIC = {"/login", "/logout", "/health", "/onboarding", "/robots.txt", "/setup/admin"}
# POSTs a read-only account may make (its own UI state only).
_VIEWER_WRITES = {"/assistant/chat", "/assistant/stop", "/jobs/{job_id}/seen", "/logout"}
_ADMIN_ONLY_GETS = {"/backup/db", "/backup/csv", "/audit/export.csv"}


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def _routes(app):
    for r in app_routes(app):
        for m in sorted(r.methods - {"HEAD"}):
            yield m, r.path


def test_route_inventory_is_known(client):
    """A new route must be classified here (public? viewer-writable?)."""
    paths = {p for _m, p in _routes(client.app)}
    assert _PUBLIC - {"/logout"} <= paths
    assert len(paths) > 100


def test_every_non_public_route_requires_a_session(client, strict_auth, seeded_db):
    seeded_db.add_user("boss", ADMIN_PW, role="admin")
    for method, path in _routes(client.app):
        if path in _PUBLIC:
            continue
        resp = client.request(method, _concrete(path), follow_redirects=False)
        if path.startswith("/api/v1/"):
            assert resp.status_code in (401, 307), (method, path, resp.status_code)
            continue
        assert resp.status_code in (303, 401), (method, path, resp.status_code)
        if resp.status_code == 303:
            assert resp.headers["location"] == "/login", (method, path)


def test_write_routes_reject_viewers(client, strict_auth, seeded_db):
    sign_in(client, seeded_db, "viewer", "viewer-password1", role="client_viewer",
            client_id=1)
    for method, path in _routes(client.app):
        if method == "GET" or path in _PUBLIC or path in _VIEWER_WRITES or path.startswith("/api/v1/"):
            continue
        resp = client.request(method, _concrete(path), follow_redirects=False)
        ok = resp.status_code == 403 or (path.startswith("/users/")
                                         and resp.headers.get("location") == "/")
        assert ok, (method, path, resp.status_code)


def test_admin_only_routes_reject_operators(client, strict_auth, seeded_db):
    sign_in(client, seeded_db, "op", "operator-password", role="operator")
    for method, path in _routes(client.app):
        admin_area = (path.startswith(("/settings", "/backup/", "/audit/"))
                      and (method != "GET" or path in _ADMIN_ONLY_GETS)) or (
            method != "GET" and path.startswith("/setup/") and path != "/setup/admin")
        if not admin_area:
            continue
        resp = client.request(method, _concrete(path), follow_redirects=False)
        assert resp.status_code == 403, (method, path, resp.status_code)
    for path in ("/users/add", "/users/toggle", "/users/password"):
        resp = client.post(path, data={"username": "x", "password": "y" * 14, "role": "admin",
                                       "user_id": "1", "active": "0"}, follow_redirects=False)
        assert resp.headers.get("location") == "/", path
    assert [u["username"] for u in seeded_db.list_users()] == ["op"]


def test_viewer_cannot_read_admin_data(client, strict_auth, seeded_db):
    sign_in(client, seeded_db, "viewer", "viewer-password1", role="client_viewer", client_id=1)
    for path in _ADMIN_ONLY_GETS:
        assert client.get(path, follow_redirects=False).status_code == 403, path
    settings = client.get("/settings").text
    assert "API keys" not in settings or "/settings/api-keys" not in settings


# --------------------------------------------------------------------- #
# Item 8: tampering with ids, roles, owners, redirects
# --------------------------------------------------------------------- #
def test_role_and_owner_fields_are_not_trusted(client, strict_auth, seeded_db):
    sign_in(client, seeded_db, "boss", ADMIN_PW)
    client.post("/users/add", data={"username": "sneaky", "password": "sneaky-password",
                                    "role": "superadmin"})
    assert seeded_db.get_user_by_username("sneaky") is None
    client.post("/users/add", data={"username": "op2", "password": "op2-password-x",
                                    "role": "operator", "client_id": "999", "active": "0",
                                    "password_hash": "x"})
    op2 = seeded_db.get_user_by_username("op2")
    assert op2["role"] == "operator" and op2["client_id"] is None and op2["active"] == 1


def test_viewer_cannot_escalate_through_forms(client, strict_auth, seeded_db):
    sign_in(client, seeded_db, "viewer", "viewer-password1", role="client_viewer", client_id=1)
    for data in ({"role": "admin"}, {"subject_id": "1", "role": "admin"}):
        resp = client.post("/users/add", data={"username": "x", "password": "p" * 14, **data},
                           follow_redirects=False)
        assert resp.status_code == 403 or resp.headers.get("location") == "/"
    assert seeded_db.get_user_by_username("x") is None
    v = seeded_db.get_user_by_username("viewer")
    assert v["role"] == "client_viewer"


@pytest.mark.parametrize("bad", ["https://evil.example/", "//evil.example/x", "/\\evil.example",
                                 "javascript:alert(1)", "\\\\evil.example", " //evil.example"])
def test_next_parameters_only_redirect_locally(client, seeded_db, bad):
    for path, data in (("/alerts/seen", {"next": bad}), ("/alerts/check", {"next": bad}),
                       ("/evidence/capture", {"url": "https://a.example", "next": bad})):
        resp = client.post(path, data=data, follow_redirects=False)
        loc = resp.headers.get("location") or resp.headers.get("x-sm-redirect") or ""
        assert loc.startswith("/") and not loc.startswith(("//", "/\\")), (path, loc)
        assert "evil.example" not in loc, (path, loc)
    resp = client.get("/subject/switch", params={"id": 1, "next": bad}, follow_redirects=False)
    assert "evil.example" not in resp.headers["location"]


def test_run_ids_of_other_subjects_are_not_found(client, seeded_db, two_projects):
    assert B_MARK not in client.get("/ui/run/run-b", headers={"X-SM-Fragment": "1"}).text
    resp = client.get("/ui/compare", params={"a": "run-b", "b": "run-b", "country": "us"},
                      headers={"X-SM-Fragment": "1"})
    assert B_MARK not in resp.text


def test_subject_cookie_tampering_falls_back(client, strict_auth, seeded_db, two_projects):
    sign_in(client, seeded_db, "viewer_a", "viewer-a-password")
    for value in (str(two_projects["sid_b"]), "-1", "0", "1 OR 1=1", "99999"):
        client.cookies.set("subject", value)
        text = client.get("/results").text
        assert B_MARK not in text and client.get("/").status_code == 200
