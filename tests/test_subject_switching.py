"""Active subject (cookie `subject`, validated per request by
dashboard.context.SubjectMiddleware), the Project ▸ Subject switcher, the
projects/subjects management routes, overlay fragments and error pages."""
import html

import pytest


def _switch(client, sid, next_url="/"):
    return client.get("/subject/switch", params={"id": sid, "next": next_url},
                      follow_redirects=False)


def _login(client, db, username, role, client_id=None):
    db.add_user(username, f"{username}-pass", role=role, client_id=client_id)
    r = client.post("/login", data={"username": username, "password": f"{username}-pass"},
                    follow_redirects=False)
    client.cookies.set("sid", r.cookies["sid"])


# --------------------------------------------------------------------- #
# Switching
# --------------------------------------------------------------------- #
def test_switch_sets_a_httponly_lax_cookie_and_redirects(client, seeded_db):
    sid = seeded_db.add_subject("Acme Corp", type="company", countries=["us"])
    r = _switch(client, sid, "/results?x=1")
    assert r.status_code == 303 and r.headers["location"] == "/results?x=1"
    cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith("subject="))
    assert f"subject={sid}" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()


@pytest.mark.parametrize("bad_next", ["https://evil.example/", "//evil.example", "javascript:alert(1)"])
def test_switch_next_must_be_a_local_path(client, seeded_db, bad_next):
    sid = seeded_db.add_subject("Acme Corp", countries=["us"])
    assert _switch(client, sid, bad_next).headers["location"] == "/"


def test_switch_to_unknown_subject_is_refused(client, seeded_db):
    r = _switch(client, 999)
    assert r.status_code == 303 and "subject=" not in r.headers.get("set-cookie", "")


def test_invalid_cookie_falls_back_to_the_first_allowed_subject(client, seeded_db):
    sid = seeded_db.add_subject("Gone Soon", countries=["us"])
    seeded_db.deactivate_subject(sid)
    client.cookies.set("subject", str(sid))
    text = client.get("/results").text
    assert "Gone Soon" not in text and "jordanashworth.example/about" in text
    client.cookies.set("subject", "not-a-number")
    assert client.get("/").status_code == 200


def test_data_follows_the_active_subject(client, seeded_db):
    sid = seeded_db.add_subject("Acme Corp", countries=["us"])
    client.cookies.set("subject", str(sid))
    assert "jordanashworth.example/about" not in client.get("/results").text
    client.cookies.set("subject", "1")
    assert "jordanashworth.example/about" in client.get("/results").text


# --------------------------------------------------------------------- #
# Viewer restriction
# --------------------------------------------------------------------- #
def test_client_viewer_only_sees_subjects_of_their_project(client, seeded_db):
    pid = seeded_db.add_client("Client B")
    mine = seeded_db.add_subject("Client B Person", countries=["us"], client_id=pid)
    _login(client, seeded_db, "viewer", "client_viewer", client_id=pid)
    switcher = client.get("/ui/switcher").text
    assert "Client B Person" in switcher and "Jordan" not in switcher
    # Forbidden subject: switch refused, cookie ignored.
    assert "subject=" not in _switch(client, 1).headers.get("set-cookie", "")
    client.cookies.set("subject", "1")
    assert "jordanashworth.example/about" not in client.get("/results").text
    assert "Client B Person" in client.get("/").text
    assert mine


def test_staff_see_every_subject(client, seeded_db):
    pid = seeded_db.add_client("Client B")
    seeded_db.add_subject("Client B Person", countries=["us"], client_id=pid)
    _login(client, seeded_db, "op", "operator")
    switcher = client.get("/ui/switcher").text
    assert "Client B Person" in switcher and "Client B" in switcher
    assert "New subject" in switcher and "href='/settings#projects'" in switcher


# --------------------------------------------------------------------- #
# Demo banner follows the subject
# --------------------------------------------------------------------- #
def test_demo_banner_only_for_the_demo_subject(client, seeded_db):
    assert "demo-banner" not in client.get("/").text
    demo = seeded_db.add_subject("Demo Person", countries=["us"], is_demo=True)
    client.cookies.set("subject", str(demo))
    text = client.get("/").text
    assert "class='demo-banner'" in text and "Demo subject" in text
    banner = text.split("class='demo-banner'")[1].split("</div>")[0]
    assert "<a " not in banner          # plain text
    client.cookies.set("subject", "1")
    assert "demo-banner" not in client.get("/").text


# --------------------------------------------------------------------- #
# Switcher fragment + checkmark
# --------------------------------------------------------------------- #
def test_switcher_groups_by_project_and_marks_the_active_subject(client, seeded_db):
    pid = seeded_db.add_client("Personal")
    sid = seeded_db.add_subject("Alex Rivera", countries=["us"], client_id=pid)
    client.cookies.set("subject", str(sid))
    text = client.get("/ui/switcher", headers={"referer": "http://testserver/settings"}).text
    assert "<div class='hd'>Personal</div>" in text
    current = text.split("Alex Rivera")[1].split("</a>")[0]
    assert "class='check'" in current
    assert "next=%2Fsettings" in text


# --------------------------------------------------------------------- #
# Projects and subjects management (PRG)
# --------------------------------------------------------------------- #
def test_create_project_and_subject_then_switch(client, seeded_db):
    r = client.post("/projects/add", data={"name": "Acme"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings#projects"
    pid = next(c for c in seeded_db.list_clients() if c["name"] == "Acme")["id"]
    r = client.post("/subjects/add", data={
        "name": "Acme Corp", "type": "company", "query": "Acme Corporation", "aliases": "ACME, Acme Inc",
        "countries": ["us", "gb"], "context": "Maker of anvils", "project_id": str(pid), "switch": "1",
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    s = seeded_db.find_subject_by_name("Acme Corp")
    assert s["client_id"] == pid and s["type"] == "company" and s["countries"] == ["us", "gb"]
    assert s["queries"] == ["Acme Corporation"] and s["aliases"] == ["ACME", "Acme Inc"]
    assert f"subject={s['id']}" in r.headers["set-cookie"]


def test_subject_edit_without_changes_says_so(client, seeded_db):
    sid = seeded_db.add_subject("Acme Corp", countries=["us"])
    data = {"name": "Acme Corp", "type": "person", "query": "Acme Corp", "aliases": "",
            "countries": ["us"], "context": "", "project_id": "1"}
    assert "No changes to save." in client.post(f"/subjects/{sid}/edit", data=data).text
    data["context"] = "New context"
    assert "Saved Acme Corp." in client.post(f"/subjects/{sid}/edit", data=data).text
    assert seeded_db.get_subject(sid)["context"] == "New context"


def test_subject_form_errors_stay_in_the_modal(client, seeded_db):
    r = client.post("/subjects/add", data={"name": "", "countries": ["us"], "project_id": "1"},
                    headers={"X-SM-Fragment": "1"})
    assert r.status_code == 400 and "a name is required" in r.text and "<html" not in r.text
    r = client.post("/subjects/add", data={"name": "X", "countries": ["us"], "project_id": "1"},
                    headers={"X-SM-Fragment": "1"})
    assert r.status_code == 200 and r.headers["X-SM-Redirect"] == "/settings#projects"


def test_deactivate_subject_and_archive_project(client, seeded_db):
    pid = seeded_db.add_client("Old client")
    sid = seeded_db.add_subject("Old Person", countries=["us"], client_id=pid)
    client.post(f"/subjects/{sid}/deactivate")
    assert not seeded_db.get_subject(sid)["active"]
    client.post(f"/projects/{pid}/archive")
    assert not seeded_db.get_client(pid)["active"]
    assert "cannot be archived" in client.post("/projects/1/archive").text
    assert seeded_db.get_client(1)["active"]
    assert "can't be deactivated" in html.unescape(client.post("/subjects/1/deactivate").text)
    assert seeded_db.get_subject(1)["active"]


# --------------------------------------------------------------------- #
# Overlay fragments
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["/ui/search", "/ui/reports", "/ui/review", "/ui/compare",
                                  "/ui/subject/new", "/ui/project/new", "/ui/subject/1/edit",
                                  "/ui/project/1/edit"])
def test_overlay_fragments_bare_for_the_loader_and_full_page_otherwise(client, seeded_db, path):
    frag = client.get(path, headers={"X-SM-Fragment": "1"})
    assert frag.status_code == 200 and "<html" not in frag.text
    assert "class='ov-head'" in frag.text and "data-sm-close" in frag.text
    full = client.get(path)
    assert full.status_code == 200 and "<html" in full.text and "class='rail'" in full.text


def test_assistant_panel_route_exists(client):
    assert client.get("/assistant/panel", headers={"X-SM-Fragment": "1"}).status_code == 200


# --------------------------------------------------------------------- #
# Error pages
# --------------------------------------------------------------------- #
def test_browser_404_is_a_page_in_the_layout(client):
    r = client.get("/no-such-page", headers={"accept": "text/html"})
    assert r.status_code == 404 and "Page not found" in r.text and "class='rail'" in r.text


def test_api_and_job_status_errors_stay_json(client):
    assert client.get("/api/v1/nope").headers["content-type"].startswith("application/json")
    r = client.get("/job/nope/status")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")


def test_browser_validation_error_is_not_raw_json(client):
    r = client.get("/ui/subject/abc/edit", headers={"accept": "text/html"})
    assert r.status_code == 422 and "text/html" in r.headers["content-type"]
    assert '"detail"' not in r.text
