"""Smoke tests: the app boots and the key pages respond 200.

Without AUTH_USER/AUTH_PASS in the environment the app is fail-open (see
auth_enabled() in src/dashboard/main.py), so the TestClient doesn't need
to log in.
"""
import re

import pytest

_PAGES = ["/", "/results", "/alerts", "/notes", "/timeline", "/search-text",
          "/settings", "/static/style.css", "/review",
          "/domains", "/runs", "/analysis", "/diff", "/evidence", "/export",
          "/onboarding"]



@pytest.mark.parametrize("path", _PAGES)
def test_pages_return_200(client, path):
    response = client.get(path)
    assert response.status_code == 200


# Features removed on purpose (goals, AI domain suggestions, saved
# searches, SERP snapshots, product overview + aliases): their routes are gone.
_REMOVED = [("get", "/goals"), ("post", "/goals/add"), ("post", "/goals/save"),
            ("post", "/goals/delete"), ("get", "/ai-suggest-domains"),
            ("post", "/settings/queries"), ("post", "/settings/serp-snapshots"),
            ("get", "/landing"), ("get", "/welcome"), ("get", "/boarding")]


@pytest.mark.parametrize("method,path", _REMOVED)
def test_removed_routes_are_gone(client, method, path):
    assert getattr(client, method)(path).status_code in (404, 405), path


_HREF_RE = re.compile(r'''(?:href|action)=['"]([^'"]+)['"]''')


def test_no_dead_links_on_key_pages(client, seeded_db):
    """Regression: template links/form actions that point at a route which
    no longer exists (e.g. a page removed during a refactor, with its link
    left behind elsewhere) render fine and only 404 when actually clicked
    -- nothing in a plain GET-200 smoke test catches that. This walks every
    internal href/action found on the key pages and GETs it, flagging any
    that 404. Found and fixed two real ones this way: /analysis linked to
    a removed /ai-insights page, /results linked to a removed /stories
    page -- both leftover from the AI-answer-monitor feature cut during
    extraction -- and later a third: /notes rendered an "Open case" link to
    a /cases page that never shipped.

    Matching is exact for static routes; only routes with a {param} are
    matched by prefix (a bare prefix match would let "/" swallow every
    link). Rows that only render with data (e.g. the /notes board) are
    seeded first so their links are on the page."""
    from dashboard.main import app

    seeded_db.set_note("https://example.com/linked", "note with a board row")

    paths = [r.path for r in app.routes if getattr(r, "path", None)]
    static_paths = {p for p in paths if "{" not in p}
    dynamic_prefixes = tuple(p.split("{")[0] for p in paths if "{" in p)

    found_links = set()
    for path in _PAGES:
        if path.startswith("/static/"):
            continue
        resp = client.get(path)
        if resp.status_code != 200:
            continue
        for link in _HREF_RE.findall(resp.text):
            if link.startswith(("http://", "https://", "#", "mailto:", "javascript:",
                                 "/static/")):
                continue
            if link.startswith("/"):
                found_links.add(link.split("?")[0])

    dead = []
    for link in sorted(found_links):
        if link not in static_paths and not link.startswith(dynamic_prefixes):
            # Not in the routing table at all -- almost certainly dead, but
            # confirm with a real GET before flagging it.
            resp = client.get(link, follow_redirects=False)
            if resp.status_code == 404:
                dead.append(link)

    assert dead == [], f"dead link(s) found on key pages: {dead}"


def test_results_page_reads_seeded_temp_db_not_production(client):
    """Confirms the app uses the temporary DB (DB_PATH), not data/seo_dashboard.db."""
    response = client.get("/results")
    assert response.status_code == 200
    # URL seeded in tests/conftest.py::seeded_db, doesn't exist in production.
    assert "jordanashworth.example/about" in response.text


def test_notes_save_then_appears_on_get(client):
    """Regression for the /notes/save form (9.1B): save a note via POST
    and confirm it appears on the next GET /notes, using the same
    temporary DB injected via DB_PATH (fixture `client`)."""
    response = client.post(
        "/notes/save",
        data={"url": "https://example.com/tracked", "status": "open", "note": "review this"},
    )
    assert response.status_code == 200      # PRG back to Results with the note panel

    follow_up = client.get("/ui/notes", headers={"X-SM-Fragment": "1"})
    assert follow_up.status_code == 200
    assert "https://example.com/tracked" in follow_up.text
    assert "review this" in follow_up.text
