"""Dialogs: the users dialog error round-trip, and the panels that replaced
the old timeline/notes <dialog> pages (Log an event, Note)."""
import pytest  # noqa: F401  (kept for parametrised additions)


def test_timeline_and_note_panels_have_their_forms(admin_client):
    tl = admin_client.get("/ui/timeline", headers={"X-SM-Fragment": "1"}).text
    assert "Log an event" in tl and "action='/timeline/add'" in tl
    note = admin_client.get("/ui/note?url=https://example.com/a", headers={"X-SM-Fragment": "1"}).text
    assert "action='/notes/save'" in note and "https://example.com/a" in note


def test_users_add_error_comes_back_as_a_toast(admin_client):
    r = admin_client.post("/users/add", data={"username": "", "password": "",
                                               "role": "operator"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings#users"
    page = admin_client.get("/settings")
    assert "Not created: username and password are required." in page.text


def test_timeline_invalid_date_stays_in_the_panel(client):
    r = client.post("/timeline/add", data={"event_date": "not-a-date", "description": "x"},
                    headers={"X-SM-Fragment": "1"})
    assert r.status_code == 400
    assert "pick a valid date" in r.text and "value='not-a-date'" in r.text
