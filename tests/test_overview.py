"""Overview (/): the active subject's latest run per country, KPIs with deltas,
inline-SVG trend, share of voice + Wins, "what changed" (run_diff), review
nudge and empty states. /analysis folded into it (redirect)."""
from conftest import RUN_ID, _reload_app

from dashboard.routers.home import trend_svg
from database.models import Database


def _r(url, domain, pos, cls, country="us"):
    return {"query": "q", "url": url, "title": f"t {domain}", "description": "d",
            "displayed_url": domain, "position": pos, "country_code": country,
            "domain": domain, "classification": cls}


def _client_for(tmp_path, monkeypatch, seed):
    db_path = str(tmp_path / "ov.db")
    seed(Database(db_path=db_path))
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")
    from fastapi.testclient import TestClient
    return TestClient(_reload_app().app)


def test_overview_single_run(client, seeded_db):
    text = client.get("/").text
    for label in ("Positive", "Negative", "Open alerts", "Wins (90 days)", "Trend",
                  "Share of voice", "Wins", "What changed"):
        assert label in text, label
    assert "40%" in text            # 2 of 5 positive
    assert "First run" in text and "This is the first run for US" in text
    assert "<svg viewBox='0 0 560 190'" in text and "var(--pos)" in text
    assert "latest run" in text
    # One home per feature: New search and Reports live on Results.
    main = text[text.index("<main"):text.index("</main>")]
    assert "href='/ui/search'" not in main and "href='/ui/reports'" not in main


def test_overview_changes_between_two_runs(client, seeded_db):
    seeded_db.save_run(apify_run_id="run-2", queries=["q"], country_code="us", status="completed",
                       summary={"total": 3, "positive": 1, "negative": 1, "neutral": 1})
    seeded_db.save_results([
        _r("https://jordanashworth.example/about", "jordanashworth.example", 3, "positive"),
        _r("https://scam.example/article", "scam.example", 1, "negative"),
        _r("https://newcomer.example/x", "newcomer.example", 2, "neutral"),
    ], run_id="run-2")
    text = client.get("/").text
    assert "What changed since" in text
    assert "<b>newcomer.example</b> entered at position 2" in text
    assert "<b>news.example</b> left the results" in text
    assert "<b>scam.example</b> moved up 2 positions to 1" in text
    assert f"/ui/compare?a={RUN_ID}&amp;b=run-2" in text
    assert "pts vs" in text


def test_overview_review_nudge(client, seeded_db):
    conn = seeded_db._connect()
    conn.execute("UPDATE search_results SET review_status = 'pending' WHERE position = 5")
    conn.commit()
    conn.close()
    text = client.get("/").text
    assert "1 result needs your review." in text and "href='/ui/review'" in text


def test_overview_country_selector_only_with_several_countries(client, seeded_db):
    assert "name='country'" not in client.get("/").text
    seeded_db.save_run(apify_run_id="run-gb", queries=["q"], country_code="gb", status="completed",
                       summary={"total": 1, "positive": 1, "negative": 0, "neutral": 0})
    seeded_db.save_results([_r("https://a.example/", "a.example", 1, "positive", "gb")], run_id="run-gb")
    text = client.get("/?country=gb").text
    assert "name='country'" in text and "<option value='gb' selected>" in text
    assert "100%" in text


def test_analysis_redirects_to_overview(client):
    r = client.get("/analysis?country=us", follow_redirects=False)
    assert r.status_code in (303, 307) and r.headers["location"] == "/?country=us"


def test_overview_is_scoped_to_the_active_subject(client, seeded_db):
    sid = seeded_db.add_subject("Someone Else", countries=["us"])
    r = client.get(f"/subject/switch?id={sid}&next=/", follow_redirects=False)
    client.cookies.set("subject", r.cookies["subject"])
    text = client.get("/").text
    assert "No searches yet for Someone Else" in text
    assert "jordanashworth.example" not in text


def test_fresh_install_empty_state_points_to_the_wizard(tmp_path, monkeypatch):
    client = _client_for(tmp_path, monkeypatch, lambda db: db.set_setting("wizard_completed", "1"))
    text = client.get("/").text
    assert "Open the setup wizard" in text and "href='/setup'" in text


def test_trend_svg_points_labels_and_endpoints():
    svg = trend_svg([("1 Sep", 40, 40), ("8 Sep", 60, 20), ("15 Sep", 43, 29)], "us")
    assert svg.count("<circle") == 2 and "1 Sep" in svg and "15 Sep" in svg
    assert "Latest: 43% positive, 29% negative." in svg and "class='sr-only'" in svg
    assert "<circle" in trend_svg([("1 Sep", 50, 10)])
    assert "No runs yet" in trend_svg([])
