# tests/test_nav.py
"""App shell: top bar (brand, Project ▸ Subject switcher, stored-results search,
Assistant, alerts bell, account menu) and a rail with exactly three items
(bottom tab bar on phones). dashboard.nav is DB-free, so most of this is pure."""
from pathlib import Path

import pytest

from dashboard import nav


def test_rail_has_exactly_three_destinations():
    assert [(i.label, i.path) for i in nav.NAV] == [
        ("Overview", "/"), ("Results", "/results"), ("Settings", "/settings")]


@pytest.mark.parametrize("path,key", [
    ("/", "overview"), ("/analysis", "overview"), ("/results", "results"), ("/run/42", "results"),
    ("/diff", "results"), ("/notes", "results"), ("/job/abc", "results"),
    ("/settings", "settings"), ("/users", "settings"), ("/setup", "settings"),
])
def test_resolve(path, key):
    assert nav.resolve(path).key == key


def test_resolve_unknown_path_highlights_nothing():
    assert nav.resolve("/does-not-exist") is None


def test_rail_marks_current_item():
    html = nav.render_rail("/notes")
    assert "class='rail'" in html
    assert html.count("aria-current='page'") == 1
    assert "href='/results' aria-current='page'" in html


def test_topbar_contents():
    html = nav.render_topbar(project="Demo", subject="Jordan", alerts=3, username="alex", auth=True)
    assert "Demo ▸" in html and "<b>Jordan</b>" in html and "data-popover='/ui/switcher'" in html
    assert "action='/search-text'" in html and "placeholder='Find in stored results'" in html
    assert "data-panel='/assistant/panel'" in html and "aria-controls='assistant-panel'" in html
    assert "data-popover='/ui/alerts'" in html and ">3</span>" in html and "Alerts, 3 open" in html
    assert "data-popover='#acct-menu'" in html
    # Account menu: Settings, theme, Help -> onboarding, Sign out (auth on only)
    assert "<template id='acct-menu'>" in html and "data-theme-toggle" in html
    assert "href='/onboarding'" in html and "href='/logout'" in html
    assert "href='/logout'" not in nav.render_topbar(subject="x", auth=False)
    assert "dotbadge" not in nav.render_topbar(subject="x", alerts=0)


def test_labels_are_escaped():
    html = nav.render_topbar(project="A & B", subject="<script>")
    assert "A &amp; B" in html and "&lt;script&gt;" in html and "<script>" not in html


def test_icons_are_decorative_svg_not_emoji():
    html = nav.render_rail("/") + nav.render_topbar(subject="x", alerts=1, auth=True)
    assert "aria-hidden='true'" in html
    for emoji in ("🏠", "🔍", "📝", "🔔", "📄", "⚙", "🧭", "🌟", "🌓", "🚪"):
        assert emoji not in html


def test_static_assets_served(client):
    css = client.get("/static/style.css")
    js = client.get("/static/app.js")
    assert css.status_code == 200 and "--bg" in css.text and "@media (max-width: 640px)" in css.text
    assert js.status_code == 200
    for api in ("SM.openModal", "SM.openPanel", "SM.popover", "SM.toast", "SM.close",
                "X-SM-Fragment", "data-sm-submit", "data-sm-close"):
        assert api in js.text, api
    # Older native dialogs keep working.
    assert "data-dialog" in js.text and "showModal" in js.text


def test_active_row_styles_and_script(client):
    css = client.get("/static/style.css").text
    js = client.get("/static/app.js").text
    assert "tr.is-active" in css and "tr:focus-within" in css
    assert "prefers-reduced-motion" in css
    assert "is-active" in js and "focusin" in js


@pytest.mark.parametrize("path", nav.all_paths())
def test_every_nav_route_renders_shell_with_active_item(admin_client, path):
    resp = admin_client.get(path)
    assert resp.status_code == 200, path
    assert "class='rail'" in resp.text and "class='top'" in resp.text, path
    assert f"href='{path}' aria-current='page'" in resp.text, path
    assert "/static/app.js" in resp.text and "id='sm-layer'" in resp.text, path
    assert "id='assistant-panel'" in resp.text, path


def test_old_sidebar_and_tab_bars_are_gone(client):
    for path in ("/", "/settings", "/timeline", "/domains"):
        text = client.get(path).text
        assert "class='sidebar'" not in text and "class='tabbar'" not in text, path
        assert "class='rail'" in text, path


def test_legacy_and_jinja_pages_share_the_shell(client):
    legacy = client.get("/domains").text           # page()
    jinja = client.get("/timeline").text            # templates/timeline.html
    for marker in ("class='rail'", "class='top'", "/static/app.js", "id='confirm-modal'"):
        assert marker in legacy and marker in jinja, marker


def test_public_pages_have_no_shell(client):
    resp = client.get("/login")
    assert "class='rail'" not in resp.text and "class='top'" not in resp.text


def test_translation_layer_is_gone():
    src = Path(__file__).resolve().parents[1] / "src"
    offenders = []
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for needle in ("_TRANSLATIONS", "_TITLES", "def _t(", "_t("):
            if needle in text:
                offenders.append(f"{py.relative_to(src)}: {needle}")
    assert offenders == [], offenders
