"""Tests for item 4.2: diff between runs (entered/exited/moved/reclassified).

TDD: processing.diff.run_diff() is pure (receives an already-seeded
Database, no network) -- same discipline as processing/enrich.py. The /diff
page and the link on /runs are tested via the `client` fixture (TestClient),
same as the rest of the migrated Jinja2 pages (see tests/test_search.py).
"""

import html
import re
from urllib.parse import unquote

from conftest import make_db as _db


def _result(url, domain, position, classification, country_code="us", same_person=None):
    r = {
        "query": "q", "url": url, "title": f"title {domain}", "description": "d",
        "displayed_url": domain, "position": position, "country_code": country_code,
        "domain": domain, "classification": classification,
    }
    if same_person is not None:
        r["same_person"] = same_person
    return r


def _seed_two_runs(db, run_a="run-a", run_b="run-b", country="us"):
    """run_a (old): keep(pos1,positive), mover(pos5,neutral), exit(pos3,negative),
    reclass(pos4,neutral). run_b (new): keep(pos1,positive, no change),
    mover(pos2,neutral, rose 3), reclass(pos4,negative, reclassified),
    enter(pos3,negative, new)."""
    db.save_run(apify_run_id=run_a, queries=["q"], country_code=country, status="completed",
                summary={"total": 4, "positive": 1, "negative": 1, "neutral": 2})
    db.save_results([
        _result("https://keep.example/x", "keep.example", 1, "positive", country),
        _result("https://mover.example/x", "mover.example", 5, "neutral", country),
        _result("https://exit.example/x", "exit.example", 3, "negative", country),
        _result("https://reclass.example/x", "reclass.example", 4, "neutral", country),
    ], run_id=run_a)

    db.save_run(apify_run_id=run_b, queries=["q"], country_code=country, status="completed",
                summary={"total": 4, "positive": 1, "negative": 2, "neutral": 1})
    db.save_results([
        _result("https://keep.example/x", "keep.example", 1, "positive", country),
        _result("https://mover.example/x", "mover.example", 2, "neutral", country),
        _result("https://reclass.example/x", "reclass.example", 4, "negative", country),
        _result("https://enter.example/x", "enter.example", 3, "negative", country),
    ], run_id=run_b)


# --------------------------------------------------------------------- #
# run_diff() puro
# --------------------------------------------------------------------- #
def test_entered_are_urls_only_in_run_b(tmp_path):
    from processing.diff import run_diff

    db = _db(tmp_path)
    _seed_two_runs(db)

    diff = run_diff(db, "run-a", "run-b")
    urls = {r["url"] for r in diff["entered"]}
    assert urls == {"https://enter.example/x"}


def test_exited_are_urls_only_in_run_a(tmp_path):
    from processing.diff import run_diff

    db = _db(tmp_path)
    _seed_two_runs(db)

    diff = run_diff(db, "run-a", "run-b")
    urls = {r["url"] for r in diff["exited"]}
    assert urls == {"https://exit.example/x"}


def test_moved_delta_is_pos_a_minus_pos_b_positive_means_rose(tmp_path):
    from processing.diff import run_diff

    db = _db(tmp_path)
    _seed_two_runs(db)

    diff = run_diff(db, "run-a", "run-b")
    assert len(diff["moved"]) == 1
    result, delta = diff["moved"][0]
    assert result["url"] == "https://mover.example/x"
    assert delta == 3  # 5 (A) -> 2 (B): went up 3


def test_reclassified_reports_from_and_to(tmp_path):
    from processing.diff import run_diff

    db = _db(tmp_path)
    _seed_two_runs(db)

    diff = run_diff(db, "run-a", "run-b")
    assert len(diff["reclassified"]) == 1
    result, old, new = diff["reclassified"][0]
    assert result["url"] == "https://reclass.example/x"
    assert old == "neutral"
    assert new == "negative"


def test_diff_uses_url_hash_identity_not_raw_url(tmp_path):
    """Two URLs equivalent after normalizing (2.5: no trailing slash / no
    utm_) must be identified as the SAME entry, not as entered+exited."""
    from processing.diff import run_diff

    db = _db(tmp_path)
    db.save_run(apify_run_id="ra", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    db.save_results([_result("https://same.example/page?utm_source=x", "same.example", 1, "neutral")],
                    run_id="ra")
    db.save_run(apify_run_id="rb", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    db.save_results([_result("https://same.example/page/", "same.example", 1, "neutral")], run_id="rb")

    diff = run_diff(db, "ra", "rb")
    assert diff["entered"] == []
    assert diff["exited"] == []


def test_duplicate_url_hash_within_same_run_keeps_best_position(tmp_path):
    """C5: the SAME URL can appear twice in a run (2 different queries
    bring it in); run_diff keeps the one with the BEST position (lower
    number), and its classification is the one reported."""
    from processing.diff import run_diff

    db = _db(tmp_path)
    db.save_run(apify_run_id="ra", queries=["q"], country_code="us", status="completed",
                summary={"total": 0, "positive": 0, "negative": 0, "neutral": 0})
    db.save_run(apify_run_id="rb", queries=["q1", "q2"], country_code="us", status="completed",
                summary={"total": 2, "positive": 0, "negative": 1, "neutral": 1})
    db.save_results([
        _result("https://dup.example/x", "dup.example", 5, "neutral"),
        _result("https://dup.example/x", "dup.example", 2, "negative"),
    ], run_id="rb")

    diff = run_diff(db, "ra", "rb")
    assert len(diff["entered"]) == 1
    assert diff["entered"][0]["position"] == 2
    assert diff["entered"][0]["classification"] == "negative"


def test_diff_excludes_homonyms_same_person_no(tmp_path):
    """Consistency 2.3: same_person='no' must not appear in ANY block of the
    diff (run_diff operates on get_results_filtered, not get_results_by_run)."""
    from processing.diff import run_diff

    db = _db(tmp_path)
    db.save_run(apify_run_id="ra", queries=["q"], country_code="us", status="completed",
                summary={"total": 0, "positive": 0, "negative": 0, "neutral": 0})
    db.save_run(apify_run_id="rb", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db.save_results([
        _result("https://homonym.example/x", "homonym.example", 1, "negative", same_person="no"),
    ], run_id="rb")

    diff = run_diff(db, "ra", "rb")
    all_urls = (
        {r["url"] for r in diff["entered"]}
        | {r["url"] for r in diff["exited"]}
        | {r["url"] for r, _ in diff["moved"]}
        | {r["url"] for r, *_ in diff["reclassified"]}
    )
    assert "https://homonym.example/x" not in all_urls


def test_run_diff_on_nonexistent_runs_returns_empty_not_crash(tmp_path):
    from processing.diff import run_diff

    db = _db(tmp_path)
    diff = run_diff(db, "ghost-a", "ghost-b")
    assert diff == {"entered": [], "exited": [], "moved": [], "reclassified": [],
                    "hidden_same_name": 0}


def test_run_diff_empty_run_id_returns_empty_without_querying(tmp_path):
    """C7: early guard -- an empty/falsy run_a/run_b must not touch the DB."""
    from processing.diff import run_diff

    db = _db(tmp_path)
    assert run_diff(db, "", "rb") == {"entered": [], "exited": [], "moved": [], "reclassified": [],
                    "hidden_same_name": 0}
    assert run_diff(db, "ra", "") == {"entered": [], "exited": [], "moved": [], "reclassified": [],
                    "hidden_same_name": 0}


# --------------------------------------------------------------------- #
# /diff page
# --------------------------------------------------------------------- #
def _compare(client, **params):
    return client.get("/ui/compare", params=params, headers={"X-SM-Fragment": "1"})


def test_compare_dialog_with_explicit_a_and_b(client, seeded_db):
    _seed_two_runs(seeded_db)
    response = _compare(client, a="run-a", b="run-b")
    assert response.status_code == 200
    assert "<html" not in response.text and "data-sm-title" in response.text
    for domain in ("enter.example", "exit.example", "mover.example", "reclass.example"):
        assert domain in response.text
    # KPI tiles: 1 entered, 1 left, 1 moved
    assert re.search(r"Entered</div><div class='value num'>1<", response.text)
    assert re.search(r"Left</div><div class='value num'>1<", response.text)
    assert re.search(r"Moved</div><div class='value num'>1<", response.text)
    assert "entered at 3" in response.text and "left (was 3)" in response.text
    assert "5 → 2" in response.text


def test_compare_defaults_a_to_previous_run_of_same_country_when_only_b_given(client, seeded_db):
    _seed_two_runs(seeded_db)
    response = _compare(client, b="run-b")
    assert response.status_code == 200
    assert "enter.example" in response.text  # the same diff as run-a vs run-b


def test_compare_defaults_to_last_two_runs_of_most_recent_country_when_no_params(client, seeded_db):
    _seed_two_runs(seeded_db)
    response = _compare(client)
    assert response.status_code == 200
    assert "enter.example" in response.text


def test_compare_nonexistent_run_shows_message_not_500(client, seeded_db):
    _seed_two_runs(seeded_db)
    response = _compare(client, a="does-not-exist", b="run-b")
    assert response.status_code == 200
    assert "doesn't exist" in html.unescape(response.text)
    assert "does-not-exist" not in response.text      # no internal ids echoed back


def test_compare_single_run_shows_message(client):
    """seeded_db already brings 1 run -- nothing to compare: clear message, not 500."""
    response = _compare(client)
    assert response.status_code == 200
    assert "only one run for US" in response.text


def test_compare_swaps_reversed_a_b_and_shows_note(client, seeded_db):
    """C10: if A is newer than B they are swapped -- the diff is still A=old, B=new."""
    _seed_two_runs(seeded_db)
    response = _compare(client, a="run-b", b="run-a")  # intentionally reversed
    assert response.status_code == 200
    assert "were swapped" in response.text
    assert ("<span class='pill neutral'><span class='dot' aria-hidden='true'></span>neutral</span> → "
            "<span class='pill negative'><span class='dot' aria-hidden='true'></span>negative</span>"
            ) in response.text


def test_compare_rejects_runs_from_different_countries(client, seeded_db):
    """C11: different countries is a clear message naming both."""
    seeded_db.save_run(apify_run_id="run-us9", queries=["q"], country_code="us", status="completed",
                       summary={"total": 0, "positive": 0, "negative": 0, "neutral": 0})
    seeded_db.save_run(apify_run_id="run-gb9", queries=["q"], country_code="gb", status="completed",
                       summary={"total": 0, "positive": 0, "negative": 0, "neutral": 0})
    response = _compare(client, a="run-us9", b="run-gb9")
    assert response.status_code == 200
    assert "different countries (US and GB)" in response.text


def test_compare_links_domain_to_url_only_when_http_and_trims_title(client, seeded_db):
    seeded_db.save_run(apify_run_id="run-a2", queries=["q"], country_code="us", status="completed",
                       summary={"total": 0, "positive": 0, "negative": 0, "neutral": 0})
    seeded_db.save_run(apify_run_id="run-b2", queries=["q"], country_code="us", status="completed",
                       summary={"total": 2, "positive": 0, "negative": 0, "neutral": 2})
    long_title = "T" * 200
    seeded_db.save_results([
        {"query": "q", "url": "https://linked.example/x", "title": long_title, "description": "d",
         "displayed_url": "linked.example", "position": 1, "country_code": "us",
         "domain": "linked.example", "classification": "neutral"},
        {"query": "q", "url": "ftp://weird.example/x", "title": "t2", "description": "d2",
         "displayed_url": "weird.example", "position": 2, "country_code": "us",
         "domain": "weird.example", "classification": "neutral"},
    ], run_id="run-b2")
    response = _compare(client, a="run-a2", b="run-b2")
    assert response.status_code == 200
    assert "<a href='https://linked.example/x'" in response.text
    assert "<a href='ftp://weird.example/x'" not in response.text
    assert "weird.example" in response.text
    assert "T" * 79 + "…" in response.text
    assert long_title not in response.text


def test_runs_panel_links_to_compare_vs_previous(client, seeded_db):
    _seed_two_runs(seeded_db)
    response = client.get("/ui/runs", headers={"X-SM-Fragment": "1"})
    assert response.status_code == 200
    assert "/ui/compare?b=run-b" in response.text and "Compare with previous" in response.text


def test_old_diff_url_redirects_to_the_compare_dialog(client, seeded_db):
    response = client.get("/diff", params={"a": "run-a", "b": "run-b"}, follow_redirects=False)
    assert response.status_code == 303
    target = response.headers["location"]
    assert target.startswith("/results?modal=")
    assert unquote(target.split("modal=", 1)[1]) == "/ui/compare?a=run-a&b=run-b"


# --------------------------------------------------------------------- #
# Overview (formerly /analysis): "What changed" is built on run_diff and
# links to Compare prefilled with the country's two latest runs.
# --------------------------------------------------------------------- #
def test_overview_changes_use_run_diff_and_link_to_compare(client, seeded_db):
    _seed_two_runs(seeded_db)
    response = client.get("/", params={"country": "us"})

    assert response.status_code == 200
    assert "/ui/compare?a=run-a&amp;b=run-b" in response.text
    assert "<b>enter.example</b> entered at position 3" in response.text
    assert "<b>exit.example</b> left the results" in response.text
    assert "<b>mover.example</b> moved up 3 positions to 2" in response.text
    assert "Share of voice" in response.text


def test_compare_has_run_pickers_with_a_older_b_newer(client, seeded_db):
    _seed_two_runs(seeded_db)
    response = _compare(client)
    assert response.status_code == 200
    assert "<select id='cmp-a' name='a'>" in response.text and "<select id='cmp-b' name='b'>" in response.text
    assert "Older run (A)" in response.text and "Newer run (B)" in response.text
    assert "<option value='run-a' selected>" in response.text
    assert "<option value='run-b' selected>" in response.text
    assert "data-sm-wide" in response.text      # opens as a wide modal
