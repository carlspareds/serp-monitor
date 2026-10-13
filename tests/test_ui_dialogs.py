"""Modals (<dialog>) and empty states."""
from dashboard import ui


def test_overlay_markup_has_title_close_body_and_footer():
    html = ui.overlay("New <x>", "<p>b</p>", "<button>ok</button>")
    assert "<h2 data-sm-title>New &lt;x&gt;</h2>" in html and "data-sm-close" in html
    assert "<div class='ov-body'><p>b</p></div>" in html and "<div class='ov-foot'><button>ok</button></div>" in html
    assert "ov-foot" not in ui.overlay("t", "b")


def test_dialog_markup_has_id_title_close_and_body():
    html = ui.dialog("add-user", "Add user", "<form action='/users/add'></form>")
    assert html.startswith("<dialog id='add-user'")
    assert "<h3>Add user</h3>" in html
    assert "data-close" in html
    assert "<form action='/users/add'></form>" in html
    assert html.endswith("</dialog>")


def test_dialog_opener_button():
    html = ui.opener("add-user", "Add user")
    assert html == "<button type='button' class='btn-sm' data-dialog='add-user'>＋ Add user</button>"
    ghost = ui.opener("add-api-key", "Edit", ghost=True)
    assert "class='btn-sm btn-ghost'" in ghost and "data-dialog='add-api-key'" in ghost


def test_empty_state_markup():
    html = ui.empty_state("Something", "some data", "<a href='/x'>Connect</a>")
    assert "class='empty-state'" in html
    assert "Connect Something to see some data here" in html
    assert "<a href='/x'>Connect</a>" in html


def test_helpers_escape_text():
    assert "&lt;b&gt;" in ui.dialog("x", "<b>", "")
    assert "&lt;b&gt;" in ui.empty_state("<b>", "", "")


def test_page_open_dialog_attribute(client):
    import dashboard.main as main_module
    assert "data-open-dialog='x'" in main_module.page("T", "", "/", open_dialog="x")


def test_overview_links_only_to_single_shortcuts(client, seeded_db):
    """New search and Reports have one home (Results); the Overview only keeps
    shortcuts that open the same dialogs (e.g. Open compare)."""
    text = client.get("/").text
    main = text[text.index("<main"):text.index("</main>")]
    assert "href='/ui/search'" not in main and "href='/ui/reports'" not in main
    results = client.get("/results").text
    assert "href='/ui/search' data-modal" in results and "href='/ui/reports' data-modal" in results


def test_search_validation_error_reopens_dialog(client):
    r = client.post("/search_multi", data={"query": "x", "countries": []},
                    headers={"X-SM-Fragment": "1"})
    assert r.status_code == 400                # stays inside the New search dialog
    assert "Pick at least one country." in r.text and "<html" not in r.text
