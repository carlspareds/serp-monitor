"""Tests for item 2.2: 1-5 severity + thematic taxonomy for negatives.

TDD, no network (_call_gemini stub): column migration, V2 prompt's
defensive parsing (severity clamp, closed topic taxonomy),
annotate_severity (only non-namesake negatives with severity IS NULL,
best-effort on AI failure), /results (pill + order + min_severity
filter), and the severity-weighted threshold alert (fallback + NULL
counts as 3).
"""
from conftest import make_db as _db

from database.models import Database
from processing import ai_classifier
from processing.severity import annotate_severity


def _neg(url: str, **overrides) -> dict:
    base = {
        "query": "q", "url": url, "title": "t", "description": "d",
        "displayed_url": url, "position": 1, "country_code": "us",
        "domain": "scam.example", "classification": "negative",
    }
    base.update(overrides)
    return base


def _row(db: Database, url: str):
    conn = db._connect()
    row = conn.execute("SELECT * FROM search_results WHERE url = ?", (url,)).fetchone()
    conn.close()
    return row


# --------------------------------------------------------------------- #
# Migration (new columns)
# --------------------------------------------------------------------- #
def test_severity_and_topic_columns_default_null_for_legacy_rows(tmp_path):
    db_path = str(tmp_path / "t.db")
    db = Database(db_path=db_path)
    conn = db._connect()
    conn.execute(
        "INSERT INTO search_results (run_id, url, classification) VALUES (?, ?, ?)",
        ("legacy-run", "https://example.com/legacy", "negative"),
    )
    conn.commit()
    conn.close()

    db2 = Database(db_path=db_path)  # re-runs the incremental migration
    row = _row(db2, "https://example.com/legacy")
    assert row["severity"] is None
    assert row["topic"] is None


def test_save_results_leaves_severity_and_topic_null(tmp_path):
    """save_results does NOT call AI (same principle as same_person) -- a
    freshly saved row always enters with severity/topic NULL."""
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db.save_results([_neg("https://scam.example/fresh")], run_id="r1")

    row = _row(db, "https://scam.example/fresh")
    assert row["severity"] is None
    assert row["topic"] is None


# --------------------------------------------------------------------- #
# V2 prompt's defensive parsing (clamp + closed taxonomy)
# --------------------------------------------------------------------- #
def test_parse_analyze_negative_clamps_severity_out_of_range():
    high = ai_classifier._parse_analyze_negative_json(
        '{"severity": 9, "topic": "legal", "action": "a", "same": "yes"}'
    )
    low = ai_classifier._parse_analyze_negative_json(
        '{"severity": 0, "topic": "legal", "action": "a", "same": "yes"}'
    )
    assert high["severity"] == 5
    assert low["severity"] == 1


def test_parse_analyze_negative_unknown_topic_falls_back_to_other():
    parsed = ai_classifier._parse_analyze_negative_json(
        '{"severity": 3, "topic": "sports", "action": "a", "same": "yes"}'
    )
    assert parsed["topic"] == "other"
    assert parsed["category"] == "other"  # backward-compat alias


def test_parse_analyze_negative_missing_severity_returns_empty():
    """Without a parseable `severity`, the candidate is discarded (M4) -- with
    no other JSON object in the text, the result is {} (total failure, it
    does not clamp an invented value)."""
    parsed = ai_classifier._parse_analyze_negative_json(
        '{"topic": "legal", "action": "a", "same": "yes"}'
    )
    assert parsed == {}


def test_parse_analyze_negative_valid_json_roundtrip():
    parsed = ai_classifier._parse_analyze_negative_json(
        '{"severity": 4, "topic": "financial", "action": "remove page", "same": "yes"}'
    )
    assert parsed == {
        "topic": "financial", "category": "financial", "severity": 4,
        "action": "remove page", "same": "yes",
    }


def test_parse_analyze_negative_float_severity_truncates_not_discards():
    """F5 (review 2.2): previously, a float severity ("4.5") discarded the
    WHOLE object (topic/action/same included) even though the docstring
    promised clamping -- now it is truncated (int(float(...))), without
    losing the rest of the verdict."""
    parsed = ai_classifier._parse_analyze_negative_json(
        '{"severity": 4.5, "topic": "legal", "action": "a", "same": "yes"}'
    )
    assert parsed["severity"] == 4
    assert parsed["topic"] == "legal"
    assert parsed["action"] == "a"


def test_parse_analyze_negative_non_numeric_severity_string_discards_candidate():
    """"4/5" is NOT a parseable number (float("4/5") raises) -- that
    candidate IS discarded (M4); with no other JSON object in the text,
    the total result is {}."""
    parsed = ai_classifier._parse_analyze_negative_json(
        '{"severity": "4/5", "topic": "legal", "action": "a", "same": "yes"}'
    )
    assert parsed == {}


# --------------------------------------------------------------------- #
# annotate_severity (post-save pipeline)
# --------------------------------------------------------------------- #
def test_annotate_severity_sets_severity_and_topic_for_pending_negative(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(
        ai_classifier, "_call_gemini",
        lambda prompt, max_tokens=10, temperature=0.0, lower=True:
            (True, '{"severity": 5, "topic": "legal", "action": "act", "same": "yes"}'),
    )
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db.save_results([_neg("https://scam.example/pending")], run_id="r1")

    annotate_severity(db, "r1")

    row = _row(db, "https://scam.example/pending")
    assert row["severity"] == 5
    assert row["topic"] == "legal"


def test_annotate_severity_skips_non_negative(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    calls = []
    monkeypatch.setattr(
        ai_classifier, "_call_gemini",
        lambda prompt, max_tokens=10, temperature=0.0, lower=True:
            (calls.append(1), (True, '{"severity": 5, "topic": "legal", "action": "a", "same": "yes"}'))[1],
    )
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 1, "negative": 0, "neutral": 0})
    db.save_results([_neg("https://good.example/positive", classification="positive",
                          domain="good.example")], run_id="r1")

    annotate_severity(db, "r1")

    assert calls == []  # Gemini was never called for a non-negative
    row = _row(db, "https://good.example/positive")
    assert row["severity"] is None


def test_annotate_severity_skips_homonym_same_person_no(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    calls = []
    monkeypatch.setattr(
        ai_classifier, "_call_gemini",
        lambda prompt, max_tokens=10, temperature=0.0, lower=True:
            (calls.append(1), (True, '{"severity": 5, "topic": "legal", "action": "a", "same": "yes"}'))[1],
    )
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db.save_results([_neg("https://scam.example/homonym", same_person="no")], run_id="r1")

    annotate_severity(db, "r1")

    assert calls == []
    row = _row(db, "https://scam.example/homonym")
    assert row["severity"] is None


def test_annotate_severity_skips_rows_with_severity_already_set(tmp_path, monkeypatch):
    """severity IS NULL is the pending condition -- a row that's already
    computed (from an earlier observation of the same URL, inherited or
    not) doesn't pay for another AI call."""
    db = _db(tmp_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    calls = []
    monkeypatch.setattr(
        ai_classifier, "_call_gemini",
        lambda prompt, max_tokens=10, temperature=0.0, lower=True:
            (calls.append(1), (True, '{"severity": 5, "topic": "legal", "action": "a", "same": "yes"}'))[1],
    )
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db.save_results([_neg("https://scam.example/already")], run_id="r1")
    conn = db._connect()
    conn.execute("UPDATE search_results SET severity=2, topic='opinion' WHERE url=?",
                ("https://scam.example/already",))
    conn.commit()
    conn.close()

    annotate_severity(db, "r1")

    assert calls == []
    row = _row(db, "https://scam.example/already")
    assert row["severity"] == 2  # untouched


def test_annotate_severity_best_effort_on_ai_not_configured(tmp_path, monkeypatch):
    """Without GEMINI_API_KEY (or a prior cache): analyze_negative returns
    empty, the row stays NULL, and the run does NOT break (doesn't raise).

    Explicit delenv (not just "don't set it"): the repo's real .env can
    carry GEMINI_API_KEY -- without this, this test would depend on the
    environment and could attempt real network (forbidden, see brief 2.2
    hard rule #3)."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    db = _db(tmp_path)
    ai_classifier.set_cache_db(db)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db.save_results([_neg("https://scam.example/no-ai")], run_id="r1")

    annotate_severity(db, "r1")  # must not raise

    row = _row(db, "https://scam.example/no-ai")
    assert row["severity"] is None
    assert row["topic"] is None


def test_annotate_severity_best_effort_on_call_gemini_exception(tmp_path, monkeypatch):
    """An unexpected error (e.g. from real network, or the DB itself)
    during ONE row's analyze_negative must not bring down
    annotate_severity -- it skips that row and continues (total
    best-effort, brief 2.2)."""
    db = _db(tmp_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    def boom(prompt, max_tokens=10, temperature=0.0, lower=True):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(ai_classifier, "_call_gemini", boom)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db.save_results([_neg("https://scam.example/boom")], run_id="r1")

    annotate_severity(db, "r1")  # must not propagate the exception

    row = _row(db, "https://scam.example/boom")
    assert row["severity"] is None


# --------------------------------------------------------------------- #
# F6 (review 2.2): annotate_severity_for_result -- a human correction in
# /review that sets classification='negative' doesn't go through any run
# chokepoint, so that specific row is annotated on the spot.
# --------------------------------------------------------------------- #
def test_annotate_severity_for_result_sets_severity(tmp_path, monkeypatch):
    from processing.severity import annotate_severity_for_result

    db = _db(tmp_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(
        ai_classifier, "_call_gemini",
        lambda prompt, max_tokens=10, temperature=0.0, lower=True:
            (True, '{"severity": 4, "topic": "financial", "action": "a", "same": "yes"}'),
    )
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db.save_results([_neg("https://scam.example/f6-direct")], run_id="r1")
    result_id = _row(db, "https://scam.example/f6-direct")["id"]

    annotate_severity_for_result(db, result_id)

    row = _row(db, "https://scam.example/f6-direct")
    assert row["severity"] == 4
    assert row["topic"] == "financial"


def test_annotate_severity_for_result_noop_on_unknown_id(tmp_path):
    from processing.severity import annotate_severity_for_result

    db = _db(tmp_path)
    annotate_severity_for_result(db, 999999)  # must not raise


def test_review_verdict_negative_triggers_severity_annotation(client, seeded_db, monkeypatch):
    """F6 integration: POST /review/{id}/verdict with classification=negative
    annotates severity/topic for that specific row -- without this, it
    would stay NULL until the URL reappears in a new run (no run
    chokepoint runs for a /review correction)."""
    seeded_db.save_run(apify_run_id="r-f6", queries=["q"], country_code="us", status="completed",
                       summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    seeded_db.save_results(
        [_neg("https://random-directory.example/f6-review", classification="neutral",
             domain="random-directory.example")],
        run_id="r-f6",
    )
    result_id = _row(seeded_db, "https://random-directory.example/f6-review")["id"]

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(
        ai_classifier, "_call_gemini",
        lambda prompt, max_tokens=10, temperature=0.0, lower=True:
            (True, '{"severity": 4, "topic": "financial", "action": "a", "same": "yes"}'),
    )

    response = client.post(f"/review/{result_id}/verdict", data={"classification": "negative"})

    assert response.status_code == 200
    row = _row(seeded_db, "https://random-directory.example/f6-review")
    assert row["severity"] == 4
    assert row["topic"] == "financial"


def test_review_verdict_positive_does_not_trigger_severity_annotation(client, seeded_db, monkeypatch):
    """Only classification='negative' triggers the annotation -- confirming
    it as positive/neutral must not call Gemini at all."""
    seeded_db.save_run(apify_run_id="r-f6b", queries=["q"], country_code="us", status="completed",
                       summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    seeded_db.save_results(
        [_neg("https://random-directory.example/f6-positive", classification="neutral",
             domain="random-directory.example")],
        run_id="r-f6b",
    )
    result_id = _row(seeded_db, "https://random-directory.example/f6-positive")["id"]

    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    calls = []
    monkeypatch.setattr(
        ai_classifier, "_call_gemini",
        lambda prompt, max_tokens=10, temperature=0.0, lower=True:
            (calls.append(1), (True, '{"severity": 4, "topic": "financial", "action": "a", "same": "yes"}'))[1],
    )

    response = client.post(f"/review/{result_id}/verdict", data={"classification": "positive"})

    assert response.status_code == 200
    assert calls == []
    row = _row(seeded_db, "https://random-directory.example/f6-positive")
    assert row["severity"] is None


# --------------------------------------------------------------------- #
# get_results_filtered: min_severity + order by severity
# --------------------------------------------------------------------- #
def _seed_two_severities(db, run_id="r1"):
    db.save_run(apify_run_id=run_id, queries=["q"], country_code="us", status="completed",
                summary={"total": 2, "positive": 0, "negative": 2, "neutral": 0})
    db.save_results([
        _neg("https://scam.example/low", domain="low.com"),
        _neg("https://scam.example/high", domain="high.com"),
    ], run_id=run_id)
    db.set_result_severity(_row(db, "https://scam.example/low")["id"], 2, "opinion")
    db.set_result_severity(_row(db, "https://scam.example/high")["id"], 5, "legal")


def test_get_results_filtered_min_severity_excludes_below_threshold(tmp_path):
    db = _db(tmp_path)
    _seed_two_severities(db)

    results = db.get_results_filtered(limit=100, min_severity=4)
    urls = {r["url"] for r in results}
    assert urls == {"https://scam.example/high"}


def test_get_results_filtered_min_severity_zero_is_no_filter(tmp_path):
    db = _db(tmp_path)
    _seed_two_severities(db)

    results = db.get_results_filtered(limit=100, min_severity=0)
    urls = {r["url"] for r in results}
    assert urls == {"https://scam.example/low", "https://scam.example/high"}


def test_get_results_filtered_min_severity_counts_unscored_negative_as_3(tmp_path):
    """A negative with no computed severity (e.g. no AI provider) counts as
    _SERIES_NULL_SEVERITY (3), like alerts and the series: kept by >=3,
    dropped by >=4 -- the >=3 chip must not hide every negative."""
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db.save_results([_neg("https://scam.example/unscored")], run_id="r1")

    assert [r["url"] for r in db.get_results_filtered(limit=100, min_severity=3)] == [
        "https://scam.example/unscored"]
    assert db.get_results_filtered(limit=100, min_severity=4) == []


def test_get_results_filtered_sorts_by_severity_desc(tmp_path):
    db = _db(tmp_path)
    _seed_two_severities(db)

    results = db.get_results_filtered(limit=100, sort_by="severity", order="desc")
    assert [r["url"] for r in results] == [
        "https://scam.example/high", "https://scam.example/low",
    ]


def test_get_results_filtered_sorts_by_severity_nulls_last(tmp_path):
    db = _db(tmp_path)
    _seed_two_severities(db)
    db.save_results([_neg("https://scam.example/unscored")], run_id="r1")

    results = db.get_results_filtered(limit=100, sort_by="severity", order="asc")
    assert results[-1]["url"] == "https://scam.example/unscored"


# --------------------------------------------------------------------- #
# /results: severity pill + order + filter (HTTP client)
# --------------------------------------------------------------------- #
def test_results_page_shows_severity_pill_and_topic(client, seeded_db):
    seeded_db.save_run(apify_run_id="r-sev", queries=["q"], country_code="us",
                       status="completed",
                       summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    seeded_db.save_results(
        [_neg("https://scam.example/sev-shown", domain="sev-shown.com")],
        run_id="r-sev",
    )
    row = _row(seeded_db, "https://scam.example/sev-shown")
    seeded_db.set_result_severity(row["id"], 5, "legal")

    response = client.get("/results?country=us")

    assert response.status_code == 200
    assert "sev-shown" in response.text
    # F11 (review 2.2): the pill's EXACT markup (see _severity_pill) instead
    # of a loose ">5<" -- that substring would also match, e.g.,
    # "position 5" or any other incidental "5" in the row.
    assert "<span class='num sev' title='legal'>5 / 5</span>" in response.text


def test_results_page_severity_dash_when_unscored(client, seeded_db):
    seeded_db.save_run(apify_run_id="r-noscore", queries=["q"], country_code="us",
                       status="completed",
                       summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    seeded_db.save_results(
        [_neg("https://scam.example/noscore", domain="noscore.com")],
        run_id="r-noscore",
    )

    response = client.get("/results?country=us")

    assert response.status_code == 200
    assert "noscore" in response.text
    # F11 (review 2.2): "—" in its exact cell -- see _severity_pill; a
    # loose "—" would also match the "(no dates)"/empty date elsewhere on
    # the page.
    assert "<td class='hide-sm'><span class='muted'>—</span></td>" in response.text


def test_results_page_min_severity_filters_out_low_severity(client, seeded_db):
    seeded_db.save_run(apify_run_id="r-filter", queries=["q"], country_code="us",
                       status="completed",
                       summary={"total": 2, "positive": 0, "negative": 2, "neutral": 0})
    seeded_db.save_results([
        _neg("https://scam.example/low2", domain="lowfilter.com"),
        _neg("https://scam.example/high2", domain="highfilter.com"),
    ], run_id="r-filter")
    seeded_db.set_result_severity(_row(seeded_db, "https://scam.example/low2")["id"], 1, "other")
    seeded_db.set_result_severity(_row(seeded_db, "https://scam.example/high2")["id"], 5, "legal")

    response = client.get("/results?country=us&min_severity=4")

    assert response.status_code == 200
    assert "highfilter" in response.text
    assert "lowfilter" not in response.text


def test_results_page_sort_by_severity_link_present(client, seeded_db):
    response = client.get("/results?country=us")
    assert response.status_code == 200
    assert "sort_by=severity" in response.text


# --------------------------------------------------------------------- #
# F1 (blocking, review 2.2): min_severity typed as str -- a country/date
# resubmitted by /results's <form> with min_severity='' returned a RAW 422
# when the param was `int`. Also covers garbage values and floats from
# the address bar (must never bring down the page).
# --------------------------------------------------------------------- #
def test_results_page_min_severity_empty_string_is_200(client, seeded_db):
    response = client.get("/results?country=us&min_severity=")
    assert response.status_code == 200


def test_results_page_min_severity_non_numeric_is_200(client, seeded_db):
    response = client.get("/results?country=us&min_severity=abc")
    assert response.status_code == 200


def test_results_page_min_severity_float_string_is_200(client, seeded_db):
    response = client.get("/results?country=us&min_severity=3.5")
    assert response.status_code == 200


def test_results_page_country_switch_form_does_not_422(client, seeded_db):
    """Literally reproduces F1: the country <form> resubmits min_severity=''
    via GET (empty hidden input) when switching country -- must not break,
    and the hidden input must remain present for the next submit."""
    response = client.get("/results?country=us&min_severity=")
    assert response.status_code == 200
    assert "name='min_severity'" in response.text


# --------------------------------------------------------------------- #
# Severity-weighted threshold alert (7.x, generate_alerts)
# --------------------------------------------------------------------- #
def test_severity_threshold_default_is_five_when_unconfigured(tmp_path):
    from processing import alerts as alerts_engine

    db = _db(tmp_path)
    assert alerts_engine.severity_threshold(db) == 5


def test_severity_threshold_falls_back_to_legacy_neg_threshold(tmp_path):
    from processing import alerts as alerts_engine

    db = _db(tmp_path)
    db.set_setting("ALERT_NEG_THRESHOLD", "2")
    assert alerts_engine.severity_threshold(db) == 2


def test_severity_threshold_explicit_setting_wins_over_legacy(tmp_path):
    from processing import alerts as alerts_engine

    db = _db(tmp_path)
    db.set_setting("ALERT_NEG_THRESHOLD", "2")
    db.set_setting("ALERT_SEVERITY_THRESHOLD", "9")
    assert alerts_engine.severity_threshold(db) == 9


def test_generate_alerts_threshold_uses_severity_sum_not_count(tmp_path):
    """A single severity-5 negative triggers the default threshold (5),
    even though the count (1) is lower than it would be under the old
    count-based threshold of 5 negatives."""
    from processing import alerts as alerts_engine

    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db.save_results([_neg("https://scam.example/severe", domain="severe.com")], run_id="r1")
    db.set_result_severity(_row(db, "https://scam.example/severe")["id"], 5, "legal")

    msgs = alerts_engine.generate_alerts(db)

    assert len(msgs) == 1
    assert "severity sum 5" in msgs[0]


def test_generate_alerts_null_severity_counts_as_three(tmp_path):
    """3 negatives WITHOUT computed severity -> sum to 3*3=9, already
    above the default (5) -- documents the conservative-neutral NULL=3
    approach."""
    from processing import alerts as alerts_engine

    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 3, "positive": 0, "negative": 3, "neutral": 0})
    db.save_results([
        _neg("https://scam.example/n1", domain="n1.com"),
        _neg("https://scam.example/n2", domain="n2.com"),
        _neg("https://scam.example/n3", domain="n3.com"),
    ], run_id="r1")

    msgs = alerts_engine.generate_alerts(db)

    assert len(msgs) == 1
    assert "severity sum 9" in msgs[0]


def test_generate_alerts_below_severity_threshold_no_alert(tmp_path):
    from processing import alerts as alerts_engine

    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db.save_results([_neg("https://scam.example/mild", domain="mild.com")], run_id="r1")
    db.set_result_severity(_row(db, "https://scam.example/mild")["id"], 1, "opinion")

    msgs = alerts_engine.generate_alerts(db)

    assert msgs == []


def test_generate_alerts_threshold_dedupe_key_ignores_severity_sum_change(tmp_path):
    """F4 (blocking, review 2.2): dedupe_key is `thr:{country}:{date}` --
    WITHOUT the sum. Before, a re-run on the same day/country that changed
    sev_sum (e.g. the AI finishes annotating severity between two calls to
    generate_alerts, or a new negative gets added) generated a NEW alert
    every time -- there must be ONLY ONE threshold alert per
    country/date."""
    from processing import alerts as alerts_engine

    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db.save_results([_neg("https://scam.example/dedupe", domain="dedupe.com")], run_id="r1")
    db.set_result_severity(_row(db, "https://scam.example/dedupe")["id"], 5, "legal")

    first = alerts_engine.generate_alerts(db)
    assert len(first) == 1

    # Same country/date, but sev_sum changes from 5 to 10 (a second severe
    # negative is added) -- still >= threshold, so WITHOUT the F4 fix this
    # would generate a second alert.
    db.save_results([_neg("https://scam.example/dedupe2", domain="dedupe2.com")], run_id="r1")
    db.set_result_severity(_row(db, "https://scam.example/dedupe2")["id"], 5, "legal")

    second = alerts_engine.generate_alerts(db)
    assert second == []  # deduped -- same dedupe_key thr:US:<date>

    threshold_alerts = [a for a in db.list_alerts(limit=200, status="all") if a["kind"] == "threshold"]
    assert len(threshold_alerts) == 1


def test_generate_alerts_breakdown_sorted_by_severity_desc(tmp_path):
    """F7 (review 2.2): the message breakdown sorts by severity desc
    BEFORE the [:8] cap -- otherwise the cut was by insertion order/SERP
    position and could omit exactly the most severe negatives."""
    from processing import alerts as alerts_engine

    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 2, "positive": 0, "negative": 2, "neutral": 0})
    # high-sev is inserted FIRST (lower id) and low-sev AFTER (higher id):
    # get_results_filtered defaults to ordering by id DESC ("recent"), so
    # WITHOUT the F7 fix the positional cut would put low-domain first in
    # the message despite being the least severe -- the fix must reorder
    # by severity, not leave the query's raw order.
    db.save_results([
        _neg("https://scam.example/high-sev", domain="high-domain.com"),
        _neg("https://scam.example/low-sev", domain="low-domain.com"),
    ], run_id="r1")
    db.set_result_severity(_row(db, "https://scam.example/high-sev")["id"], 5, "legal")
    db.set_result_severity(_row(db, "https://scam.example/low-sev")["id"], 1, "opinion")
    db.set_setting("ALERT_SEVERITY_THRESHOLD", "1")  # lowers the threshold to force the alert

    msgs = alerts_engine.generate_alerts(db)

    assert len(msgs) == 1
    breakdown = msgs[0].split("Breakdown: ")[1]
    assert breakdown.index("high-domain.com=5") < breakdown.index("low-domain.com=1")


# --------------------------------------------------------------------- #
# F2/F3 (blocking, review 2.2): POST /alerts/thresholds writes
# ALERT_SEVERITY_THRESHOLD -- before, the setting had NO real producer
# (it was only read as an internal default), and the form showed/saved
# the old threshold (ALERT_NEG_THRESHOLD) without it having any real
# effect anymore in generate_alerts, silently lowering the effective
# threshold to 1.
# --------------------------------------------------------------------- #
def test_alerts_thresholds_post_saves_severity_threshold_setting(client, seeded_db):
    response = client.post("/alerts/thresholds", data={"sev": "9", "neg": "2", "pos_drop": "3.0"})
    assert response.status_code == 200

    # seeded_db and dashboard.deps.db are DIFFERENT objects pointing at the
    # same sqlite file (same pattern as the rest of the suite, see
    # tests/conftest.py -- each method opens/commits/closes its own
    # connection, so the POST's write is visible here with no extra step).
    assert seeded_db.get_setting("ALERT_SEVERITY_THRESHOLD", "") == "9"
    assert seeded_db.get_setting("ALERT_NEG_THRESHOLD", "") == "2"


def test_alerts_thresholds_post_severity_field_becomes_real_producer(client, seeded_db):
    """After saving the form ONCE, severity_threshold(db) reflects EXACTLY
    what was saved -- it no longer depends on the fallback to
    ALERT_NEG_THRESHOLD (F3: the setting has a real producer)."""
    from processing import alerts as alerts_engine

    client.post("/alerts/thresholds", data={"sev": "7", "neg": "1", "pos_drop": "3.0"})

    assert alerts_engine.severity_threshold(seeded_db) == 7


def test_settings_shows_severity_threshold_field(client, seeded_db):
    response = client.get("/settings")
    assert response.status_code == 200
    assert "add up to a harm score of" in response.text
    assert "name='sev'" in response.text
