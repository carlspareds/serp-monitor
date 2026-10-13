"""Tests for item 2.3: automatic is_same_person in the pipeline.

Acceptance (§2.3): "A run with obvious
namesakes marks them 'no' without intervention; stats and Excel exclude
them; the toggle shows them." The deep dive also asks to exclude 'no' from
ALERTS and exports -- see the alerts.py/weekly.py section below.

TDD, no network: direct Database for DB/migration/aggregates, and a stub
of _call_gemini / is_same_person_batch (pattern from test_ai_cache.py/
test_review_queue.py) for the AI. Covers: real batch (PARTIAL JSON array,
no per-URL fallback), per-URL inheritance that prioritizes the human
verdict (H3), unsure->review queue, excluding 'no' from stats/exports/alerts,
toggle in /results, "Not this person" button (fixes ALL rows for the URL
and is immutable), context-sensitive identity cache (H8), and Gemini
turned off -> NULL without breaking.
"""
import functools

from conftest import DEMO_POSITIVE_DOMAINS
from conftest import make_db as _db

from database.models import Database, url_hash
from processing import ai_classifier
from processing.classifier import classify_batch
from processing.enrich import _known_same_person, annotate_same_person

# config/positive_domains.py ships an empty global list (multi-subject phase 1): these
# tests classify the demo subject's owned domains, so the list is passed explicitly.
# An explicit positive_domains=... in a call still overrides it.
classify_batch = functools.partial(classify_batch, positive_domains=DEMO_POSITIVE_DOMAINS)


def _row(db: Database, url: str):
    conn = db._connect()
    row = conn.execute("SELECT * FROM search_results WHERE url = ?", (url,)).fetchone()
    conn.close()
    return row


def _rows(db: Database, url: str) -> list:
    conn = db._connect()
    rows = conn.execute("SELECT * FROM search_results WHERE url = ? ORDER BY id", (url,)).fetchall()
    conn.close()
    return rows


def _result(url: str, **overrides) -> dict:
    base = {
        "query": "q", "url": url, "title": "t", "description": "d",
        "displayed_url": url, "position": 1, "country_code": "us",
        "domain": "random-directory.example",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------- #
# Column (migration)
# --------------------------------------------------------------------- #
def test_same_person_column_defaults_null_for_legacy_rows(tmp_path):
    db_path = str(tmp_path / "t.db")
    db = Database(db_path=db_path)
    conn = db._connect()
    conn.execute(
        "INSERT INTO search_results (run_id, url, classification) VALUES (?, ?, ?)",
        ("legacy-run", "https://example.com/legacy", "neutral"),
    )
    conn.commit()
    conn.close()

    db2 = Database(db_path=db_path)  # re-run migration
    row = _row(db2, "https://example.com/legacy")
    assert row["same_person"] is None


# --------------------------------------------------------------------- #
# Real batch: PARTIAL JSON array (H7/H9), no per-URL fallback
# --------------------------------------------------------------------- #
def test_is_same_person_batch_parses_json_array_in_order(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    def fake_call_gemini(prompt, max_tokens=10, temperature=0.0, lower=True):
        return True, (
            '[{"i": 1, "same": "yes"}, {"i": 2, "same": "no"}, {"i": 3, "same": "unsure"}]'
        )

    monkeypatch.setattr(ai_classifier, "_call_gemini", fake_call_gemini)
    records = [
        {"url": "https://a.example/1", "title": "t1", "description": "d1"},
        {"url": "https://a.example/2", "title": "t2", "description": "d2"},
        {"url": "https://a.example/3", "title": "t3", "description": "d3"},
    ]

    result = ai_classifier.is_same_person_batch(records)
    assert result == ["yes", "no", "unsure"]


def test_is_same_person_batch_caches_each_valid_url_individually(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    calls = []

    def fake_call_gemini(prompt, max_tokens=10, temperature=0.0, lower=True):
        calls.append(prompt)
        return True, '[{"i": 1, "same": "no"}]'

    monkeypatch.setattr(ai_classifier, "_call_gemini", fake_call_gemini)
    record = {"url": "https://a.example/cache-me", "title": "t", "description": "d"}

    ai_classifier.is_same_person_batch([record])
    # A later per-URL hit (is_same_person) reuses what the batch decided.
    assert ai_classifier.is_same_person(record) == "no"
    assert len(calls) == 1  # is_same_person didn't call Gemini again


def test_is_same_person_batch_returns_all_none_when_json_malformed(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(
        ai_classifier, "_call_gemini",
        lambda prompt, max_tokens=10, temperature=0.0, lower=True: (True, "not json at all"),
    )
    records = [{"url": "https://a.example/x", "title": "t", "description": "d"}]

    assert ai_classifier.is_same_person_batch(records) == [None]


def test_is_same_person_batch_returns_partial_when_item_count_mismatch(tmp_path, monkeypatch):
    """H7/H9: the model returned only 1 verdict for 2 requested records --
    PARTIAL: the resolved position is used, the missing one stays None
    (retried on the next run, no per-URL fallback right now)."""
    db = _db(tmp_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr(
        ai_classifier, "_call_gemini",
        lambda prompt, max_tokens=10, temperature=0.0, lower=True: (True, '[{"i": 1, "same": "yes"}]'),
    )
    records = [
        {"url": "https://a.example/1", "title": "t", "description": "d"},
        {"url": "https://a.example/2", "title": "t", "description": "d"},
    ]

    assert ai_classifier.is_same_person_batch(records) == ["yes", None]


def test_is_same_person_batch_returns_all_none_without_gemini_configured(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    records = [{"url": "https://a.example/x", "title": "t", "description": "d"}]

    assert ai_classifier.is_same_person_batch(records) == [None]


def test_annotate_same_person_leaves_unresolved_item_as_none_no_fallback(tmp_path, monkeypatch):
    """H7/H9: no per-URL fallback -- an item the batch didn't resolve stays
    None in the result (it's not re-asked per-URL in the same run)."""
    db = _db(tmp_path)
    monkeypatch.setattr(
        ai_classifier, "is_same_person_batch", lambda records, context="": [None] * len(records)
    )

    def boom(*a, **kw):
        raise AssertionError("per-URL fallback should not exist")

    monkeypatch.setattr(ai_classifier, "is_same_person", boom)

    results = [_result("https://a.example/unresolved-item")]
    annotate_same_person(results, db)

    assert results[0]["same_person"] is None


# --------------------------------------------------------------------- #
# Per-URL inheritance: ALWAYS prioritizes the human verdict (H3)
# --------------------------------------------------------------------- #
def test_known_same_person_lookup_uses_url_hash_normalization(tmp_path):
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    db.save_results(
        [_result("https://Example.COM/foo?utm_source=x", same_person="no")], run_id="r1"
    )

    known = _known_same_person(db)
    assert known[url_hash("https://example.com/foo")] == "no"


def test_known_same_person_prefers_human_even_when_it_is_the_oldest_row(tmp_path):
    """H3: two rows for the same URL in conflict -- the human one (OLDEST
    row, lower id) must win over a more recent AI row."""
    db = _db(tmp_path)
    url = "https://random-directory.example/conflict"
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    # Row 1 (lower id, OLDEST): human, says 'no'.
    db.save_results(
        [_result(url, same_person="no", classified_by="human")], run_id="r1"
    )
    # Row 2 (higher id, more recent): AI, says 'unsure' -- in practice
    # this shouldn't happen via annotate_same_person (which inherits), but
    # _known_same_person must still be robust against conflicting data.
    db.save_results(
        [_result(url, same_person="unsure", classified_by="ai")], run_id="r1"
    )

    known = _known_same_person(db)
    assert known[url_hash(url)] == "no"


def test_annotate_same_person_known_url_inherits_without_calling_gemini(tmp_path, monkeypatch):
    db = _db(tmp_path)
    url = "https://random-directory.example/known"
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    db.save_results([_result(url, same_person="no")], run_id="r1")

    def boom(*a, **kw):
        raise AssertionError("Gemini should not be called for an already-known URL")

    monkeypatch.setattr(ai_classifier, "is_same_person_batch", boom)

    results = [_result(url)]
    annotate_same_person(results, db)

    assert results[0]["same_person"] == "no"


def test_annotate_same_person_new_url_goes_through_batch(tmp_path, monkeypatch):
    db = _db(tmp_path)
    calls = []

    def fake_batch(records, context=""):
        calls.append(len(records))
        return ["yes" for _ in records]

    monkeypatch.setattr(ai_classifier, "is_same_person_batch", fake_batch)

    results = [_result("https://a.example/brand-new")]
    annotate_same_person(results, db)

    assert results[0]["same_person"] == "yes"
    assert calls == [1]


def test_annotate_same_person_splits_into_batches_of_ten(tmp_path, monkeypatch):
    """DECLINED lowering _BATCH_SIZE (arbitration): pages>1 in /search_page
    makes 30+ results per country reachable from the UI -- the chunking
    isn't speculative, it stays."""
    db = _db(tmp_path)
    calls = []

    def fake_batch(records, context=""):
        calls.append(len(records))
        return ["yes" for _ in records]

    monkeypatch.setattr(ai_classifier, "is_same_person_batch", fake_batch)

    results = [_result(f"https://a.example/{i}") for i in range(12)]
    annotate_same_person(results, db)

    assert calls == [10, 2]
    assert all(r["same_person"] == "yes" for r in results)


# --------------------------------------------------------------------- #
# Gemini turned off -> NULL without breaking
# --------------------------------------------------------------------- #
def test_annotate_same_person_without_gemini_leaves_new_urls_null(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(ai_classifier, "is_configured", lambda: False)

    results = [_result("https://a.example/no-gemini")]
    annotate_same_person(results, db)

    assert results[0]["same_person"] is None


def test_annotate_same_person_without_gemini_still_inherits_known(tmp_path, monkeypatch):
    """Gemini being off must not break inheritance -- it's just a DB read."""
    db = _db(tmp_path)
    url = "https://random-directory.example/known2"
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    db.save_results([_result(url, same_person="unsure")], run_id="r1")
    monkeypatch.setattr(ai_classifier, "is_configured", lambda: False)

    results = [_result(url)]
    annotate_same_person(results, db)

    assert results[0]["same_person"] == "unsure"


def test_save_results_persists_null_same_person_without_error(tmp_path):
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    db.save_results(classify_batch([_result("https://a.example/null-ok")]), run_id="r1")

    row = _row(db, "https://a.example/null-ok")
    assert row["same_person"] is None


# --------------------------------------------------------------------- #
# Context (H18): the empty->default fallback lives ONLY in enrich.py
# --------------------------------------------------------------------- #
def test_annotate_same_person_falls_back_to_setting_then_default(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ai_classifier.set_cache_db(db)
    seen = {}

    def fake_batch(records, context=""):
        seen["context"] = context
        return ["yes" for _ in records]

    monkeypatch.setattr(ai_classifier, "is_same_person_batch", fake_batch)
    default_context = ai_classifier.default_subject_context()

    annotate_same_person([_result("https://a.example/ctx-default")], db)
    assert seen["context"] == default_context

    db.set_setting("subject_context", "Custom Bio")
    annotate_same_person([_result("https://a.example/ctx-setting")], db)
    assert seen["context"] == "Custom Bio"

    # H18: saving explicit "" (via /settings) reverts to the default here.
    db.set_setting("subject_context", "")
    annotate_same_person([_result("https://a.example/ctx-empty-reverts")], db)
    assert seen["context"] == default_context


# --------------------------------------------------------------------- #
# H8: the identity cache is sensitive to context (subject's bio)
# --------------------------------------------------------------------- #
def test_is_same_person_cache_key_changes_with_context(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    calls = []

    def fake_call_gemini(prompt, max_tokens=10, temperature=0.0, lower=True):
        calls.append(prompt)
        return True, "no"

    monkeypatch.setattr(ai_classifier, "_call_gemini", fake_call_gemini)
    record = {"url": "https://a.example/context-sensitive", "title": "t", "description": "d"}

    ai_classifier.is_same_person(record, context="Bio A")
    ai_classifier.is_same_person(record, context="Bio A")  # hit, same context
    assert len(calls) == 1

    ai_classifier.is_same_person(record, context="Bio B")  # miss: different context
    assert len(calls) == 2


def test_is_same_person_batch_cache_key_changes_with_context(tmp_path, monkeypatch):
    """H8: changing the bio in /settings re-evaluates non-human URLs -- at
    the cache level, a different context is a different PK, so the batch
    calls Gemini again even for the same URL."""
    db = _db(tmp_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    calls = []

    def fake_call_gemini(prompt, max_tokens=10, temperature=0.0, lower=True):
        calls.append(prompt)
        return True, '[{"i": 1, "same": "no"}]'

    monkeypatch.setattr(ai_classifier, "_call_gemini", fake_call_gemini)
    record = {"url": "https://a.example/batch-context", "title": "t", "description": "d"}

    ai_classifier.is_same_person_batch([record], context="Bio A")
    assert ai_classifier.is_same_person(record, context="Bio A") == "no"  # hit
    assert len(calls) == 1

    ai_classifier.is_same_person(record, context="Bio B")  # miss: different PK -> new call
    assert len(calls) == 2


# --------------------------------------------------------------------- #
# unsure -> review queue
# --------------------------------------------------------------------- #
def test_unsure_verdict_enters_pending_review_queue(tmp_path):
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 1, "negative": 0, "neutral": 0})
    # confidence 1.0 (positive domain, rule): without same_person it would enter 'none'.
    results = classify_batch([
        {"domain": "jordanashworth.example", "url": "https://jordanashworth.example/x",
         "title": "", "description": "", "country_code": "us"},
    ])
    results[0]["same_person"] = "unsure"
    db.save_results(results, run_id="r1")

    assert db.count_pending_review() == 1
    row = _row(db, "https://jordanashworth.example/x")
    assert row["review_status"] == "pending"


def test_yes_no_verdicts_do_not_force_pending(tmp_path):
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 1, "negative": 0, "neutral": 0})
    results = classify_batch([
        {"domain": "jordanashworth.example", "url": "https://jordanashworth.example/y",
         "title": "", "description": "", "country_code": "us"},
    ])
    results[0]["same_person"] = "yes"
    db.save_results(results, run_id="r1")

    assert db.count_pending_review() == 0


# --------------------------------------------------------------------- #
# Integration: obvious namesakes marked 'no' without intervention
# --------------------------------------------------------------------- #
def test_full_pipeline_marks_evident_homonym_no_without_intervention(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(
        ai_classifier, "is_same_person_batch",
        lambda records, context="": ["no" for _ in records],
    )
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})

    results = classify_batch([
        {"domain": "scam.example", "url": "https://scam.example/homonym",
         "title": "Someone else entirely", "description": "different person",
         "country_code": "us"},
    ])
    results = annotate_same_person(results, db)
    db.save_results(results, run_id="r1")

    row = _row(db, "https://scam.example/homonym")
    assert row["same_person"] == "no"
    # Doesn't enter review (it's not 'unsure') nor requires human intervention.
    assert db.count_pending_review() == 0


# --------------------------------------------------------------------- #
# 'no' excluded from stats/exports by default; include_homonyms=True brings them back
# --------------------------------------------------------------------- #
def _seed_pos_and_homonym(db, run_id="r1"):
    db.save_run(apify_run_id=run_id, queries=["q"], country_code="us", status="completed",
                summary={"total": 2, "positive": 1, "negative": 1, "neutral": 0})
    results = classify_batch([
        {"domain": "jordanashworth.example", "url": "https://jordanashworth.example/real",
         "title": "", "description": "", "country_code": "us"},
        {"domain": "scam.example", "url": "https://scam.example/homonym",
         "title": "", "description": "", "country_code": "us"},
    ])
    results[0]["same_person"] = "yes"
    results[1]["same_person"] = "no"
    db.save_results(results, run_id=run_id)


def test_get_stats_excludes_no_by_default(tmp_path):
    db = _db(tmp_path)
    _seed_pos_and_homonym(db)

    stats = db.get_stats()
    assert stats["total_results"] == 1
    assert stats["by_class"]["positive"] == 1
    assert stats["by_class"]["negative"] == 0


def test_get_results_filtered_excludes_no_by_default(tmp_path):
    db = _db(tmp_path)
    _seed_pos_and_homonym(db)

    results = db.get_results_filtered(limit=100)
    urls = {r["url"] for r in results}
    assert "https://scam.example/homonym" not in urls
    assert "https://jordanashworth.example/real" in urls


def test_get_results_filtered_include_homonyms_true(tmp_path):
    db = _db(tmp_path)
    _seed_pos_and_homonym(db)

    results = db.get_results_filtered(limit=100, include_homonyms=True)
    urls = {r["url"] for r in results}
    assert "https://scam.example/homonym" in urls


def test_get_results_filtered_never_drops_unevaluated_null(tmp_path):
    """NULL (not evaluated) is not an exclusion -- only 'no' is."""
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    db.save_results(classify_batch([_result("https://a.example/unevaluated")]), run_id="r1")

    results = db.get_results_filtered(limit=100)
    assert any(r["url"] == "https://a.example/unevaluated" for r in results)


def test_latest_run_per_country_recomputes_counts_excluding_no(tmp_path):
    db = _db(tmp_path)
    _seed_pos_and_homonym(db)

    latest = db.latest_run_per_country()
    assert len(latest) == 1
    row = latest[0]
    assert row["positive"] == 1
    assert row["negative"] == 0
    assert row["total_results"] == 1


def test_get_history_pivot_excludes_no_by_default(tmp_path):
    db = _db(tmp_path)
    _seed_pos_and_homonym(db)

    pivot = db.get_history_pivot()
    urls = {row["url"] for row in pivot["rows"]}
    assert "https://scam.example/homonym" not in urls
    assert "https://jordanashworth.example/real" in urls


# --------------------------------------------------------------------- #
# H1: list_runs()/get_run() match latest_run_per_country (homepage
# "Recent runs" and the /run/{id} header); the /run/{id} TABLE stays raw.
# --------------------------------------------------------------------- #
def test_list_runs_counts_match_latest_run_per_country(tmp_path):
    db = _db(tmp_path)
    _seed_pos_and_homonym(db, run_id="r1")

    runs = db.list_runs()
    assert len(runs) == 1
    assert runs[0]["positive"] == 1
    assert runs[0]["negative"] == 0
    assert runs[0]["total_results"] == 1


def test_get_run_counts_match_latest_run_per_country(tmp_path):
    db = _db(tmp_path)
    _seed_pos_and_homonym(db, run_id="r1")

    run = db.get_run("r1")
    assert run["positive"] == 1
    assert run["negative"] == 0
    assert run["total_results"] == 1


def test_get_run_unknown_id_returns_none(tmp_path):
    db = _db(tmp_path)
    assert db.get_run("does-not-exist") is None


def test_get_results_by_run_stays_raw_drill_down(tmp_path):
    """The /run/{id} TABLE (get_results_by_run) is NOT filtered -- only the
    header summary is (get_run/_filtered_run_counts)."""
    db = _db(tmp_path)
    _seed_pos_and_homonym(db, run_id="r1")

    raw = db.get_results_by_run("r1")
    urls = {r["url"] for r in raw}
    assert "https://scam.example/homonym" in urls  # still there, unfiltered


# --------------------------------------------------------------------- #
# App: toggle in /results (H14: strict parsing)
# --------------------------------------------------------------------- #
def _seed_client_homonym(seeded_db):
    seeded_db.save_run(apify_run_id="r-homonym", queries=["q"], country_code="us",
                       status="completed",
                       summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    results = classify_batch([
        {"domain": "scam.example", "url": "https://scam.example/client-homonym",
         "title": "Client homonym item", "description": "", "country_code": "us"},
    ])
    results[0]["same_person"] = "no"
    seeded_db.save_results(results, run_id="r-homonym")


def test_results_page_excludes_homonym_by_default(client, seeded_db):
    _seed_client_homonym(seeded_db)
    response = client.get("/results?country=us")
    assert response.status_code == 200
    assert "client-homonym" not in response.text


def test_results_page_toggle_include_homonyms_shows_it(client, seeded_db):
    _seed_client_homonym(seeded_db)
    response = client.get("/results?country=us&include_homonyms=1")
    assert response.status_code == 200
    assert "client-homonym" in response.text


def test_results_page_toggle_rejects_zero_and_false_as_off(client, seeded_db):
    """H14: strict parsing -- "0"/"false" must NOT be read as "include"."""
    _seed_client_homonym(seeded_db)
    for off_value in ("0", "false"):
        response = client.get(f"/results?country=us&include_homonyms={off_value}")
        assert response.status_code == 200
        assert "client-homonym" not in response.text


def test_results_page_toggle_shows_identity_pill_only_when_on(client, seeded_db):
    """H13: the "same-name" pill only appears when the toggle brings the
    namesake back (if it were off, the row wouldn't even appear)."""
    _seed_client_homonym(seeded_db)
    off = client.get("/results?country=us")
    on = client.get("/results?country=us&include_homonyms=1")
    assert ">same-name</span>" not in off.text
    assert ">same-name</span>" in on.text


# --------------------------------------------------------------------- #
# App: "Not this person" button (2.3, closes B14 from 2.1) -- H4/H6
# --------------------------------------------------------------------- #
def test_set_same_person_verdict_fixes_no_human_confidence_and_corrected(tmp_path):
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    url = "https://scam.example/needs-review"
    db.save_results(classify_batch([
        {"domain": "scam.example", "url": url, "title": "", "description": "",
         "country_code": "us"},
    ]), run_id="r1")
    row_id = _row(db, url)["id"]

    result = db.set_same_person_verdict(row_id)

    assert result == url  # H4 (arbitration): returns the URL (str), not a dict
    stored = _row(db, url)
    assert stored["same_person"] == "no"
    assert stored["classified_by"] == "human"
    assert stored["review_status"] == "corrected"
    assert stored["confidence"] == 1.0  # H6


def test_set_same_person_verdict_unknown_id_returns_none(tmp_path):
    db = _db(tmp_path)
    assert db.set_same_person_verdict(999) is None


def test_set_same_person_verdict_propagates_to_all_rows_of_that_url(tmp_path):
    """H4: not just the clicked row -- ALL observations (different runs)
    of the same URL end up marked, and get_stats() drops."""
    db = _db(tmp_path)
    url = "https://scam.example/repeat-homonym"
    for i, run_id in enumerate(("r1", "r2")):
        db.save_run(apify_run_id=run_id, queries=["q"], country_code="us", status="completed",
                    summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
        db.save_results(classify_batch([
            {"domain": "scam.example", "url": url, "title": f"item {i}", "description": "",
             "country_code": "us"},
        ]), run_id=run_id)

    assert db.get_stats()["total_results"] == 2  # both rows, not evaluated yet
    first_id = _rows(db, url)[0]["id"]

    db.set_same_person_verdict(first_id)

    both = _rows(db, url)
    assert len(both) == 2
    assert all(r["same_person"] == "no" for r in both)
    assert all(r["classified_by"] == "human" for r in both)
    assert db.get_stats()["total_results"] == 0  # BOTH end up excluded


def test_not_this_person_endpoint_fixes_and_audits(client, seeded_db):
    seeded_db.save_run(apify_run_id="r-npt", queries=["q"], country_code="us",
                       status="completed",
                       summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    url = "https://scam.example/not-this-person"
    seeded_db.save_results(classify_batch([
        {"domain": "scam.example", "url": url, "title": "Not this person item",
         "description": "", "country_code": "us"},
    ]), run_id="r-npt")
    row_id = _row(seeded_db, url)["id"]

    response = client.post(f"/review/{row_id}/not-this-person")
    assert response.status_code == 200

    stored = _row(seeded_db, url)
    assert stored["same_person"] == "no"
    assert stored["classified_by"] == "human"
    assert stored["confidence"] == 1.0

    audited = seeded_db.list_audit(action="review_same_person")
    assert len(audited) == 1
    assert audited[0]["resource_id"] == url


def test_not_this_person_endpoint_unknown_id_404(client):
    response = client.post("/review/999999/not-this-person")
    assert response.status_code == 404


# --------------------------------------------------------------------- #
# H15: /backup/csv includes namesakes + same_person column
# --------------------------------------------------------------------- #
def test_backup_csv_includes_homonyms_and_same_person_column(client, seeded_db):
    seeded_db.save_run(apify_run_id="r-csv", queries=["q"], country_code="us",
                       status="completed",
                       summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    results = classify_batch([
        {"domain": "scam.example", "url": "https://scam.example/csv-homonym",
         "title": "CSV homonym item", "description": "", "country_code": "us"},
    ])
    results[0]["same_person"] = "no"
    seeded_db.save_results(results, run_id="r-csv")

    response = client.get("/backup/csv")

    assert response.status_code == 200
    assert "csv-homonym" in response.text  # H15: the backup does NOT exclude
    assert "same_person" in response.text.splitlines()[0]  # column in the header


# --------------------------------------------------------------------- #
# H16/H17: alerts and weekly.py's new-negatives detector exclude
# same_person='no' (deep dive §2.3: "excluding the 'no' from stats, ALERTS
# and exports").
# --------------------------------------------------------------------- #
def _set_scraped_date(db, url, date_str):
    conn = db._connect()
    conn.execute(
        "UPDATE search_results SET scraped_at = ? WHERE url = ?",
        (f"{date_str}T00:00:00", url),
    )
    conn.commit()
    conn.close()


def test_generate_alerts_excludes_homonym_from_threshold(tmp_path):
    from processing import alerts as alerts_engine

    db = _db(tmp_path)
    db.set_setting("ALERT_NEG_THRESHOLD", "1")
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    results = classify_batch([
        {"domain": "scam.example", "url": "https://scam.example/alert-homonym",
         "title": "", "description": "", "country_code": "us"},
    ])
    results[0]["same_person"] = "no"
    db.save_results(results, run_id="r1")

    msgs = alerts_engine.generate_alerts(db)

    # The only negative is an excluded namesake -- no threshold, no alert.
    assert msgs == []


def test_generate_alerts_excludes_homonym_from_new_negative_detection(tmp_path):
    from processing import alerts as alerts_engine

    db = _db(tmp_path)
    url = "https://scam.example/new-negative-homonym"
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    db.save_results([_result(url, domain="random-directory.example", classification="neutral",
                             confidence=1.0, classified_by="rule")], run_id="r1")
    _set_scraped_date(db, url, "2025-01-01")

    db.save_run(apify_run_id="r2", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    results = classify_batch([
        {"domain": "scam.example", "url": url, "title": "", "description": "",
         "country_code": "us"},
    ])
    results[0]["same_person"] = "no"
    db.save_results(results, run_id="r2")
    _set_scraped_date(db, url, "2025-01-08")

    msgs = alerts_engine.generate_alerts(db)

    assert msgs == []  # the "new negative" is an excluded namesake


def test_weekly_detect_new_negatives_excludes_homonyms(tmp_path):
    from report.weekly import _detect_new_negatives

    db = _db(tmp_path)
    url = "https://scam.example/weekly-homonym"
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    db.save_results([_result(url, domain="random-directory.example", classification="neutral",
                             confidence=1.0, classified_by="rule")], run_id="r1")
    _set_scraped_date(db, url, "2025-01-01")

    db.save_run(apify_run_id="r2", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    results = classify_batch([
        {"domain": "scam.example", "url": url, "title": "", "description": "",
         "country_code": "us"},
    ])
    results[0]["same_person"] = "no"
    db.save_results(results, run_id="r2")
    _set_scraped_date(db, url, "2025-01-08")

    assert _detect_new_negatives(db, ["us"]) == []
