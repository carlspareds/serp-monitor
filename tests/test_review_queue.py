"""Tests for item 2.1: confidence score + human review queue.

Acceptance (§2.1): "A result with
confidence 0.5 appears in /review; correcting it fixes it as human; a
later bulk reclassification doesn't touch it."

Includes the batch arbitrated post-review (H1/H2 blocking, M3-M7 medium, B8
low): recompute_pending in both directions, human verdict inheritance
between observations of the same URL ("Sisyphus queue"), deterministic
and robust AI parsing, recomputing run counts after an individual
correction, not overwriting an AI verdict with the rules' default-neutral,
and conditional UPDATE (no churn).

TDD, no network: direct Database for DB/migration/queue, and a stub of
_call_gemini (pattern from test_ai_cache.py) for the AI.
"""
import functools
from pathlib import Path
from urllib.parse import unquote

from conftest import DEMO_POSITIVE_DOMAINS
from conftest import make_db as _db

from database.models import Database
from processing import ai_classifier
from processing.classifier import classify_batch, classify_with_confidence

# config/positive_domains.py ships an empty global list (multi-subject phase 1): these
# tests classify the demo subject's owned domains, so the list is passed explicitly.
# An explicit positive_domains=... in a call still overrides it.
classify_batch = functools.partial(classify_batch, positive_domains=DEMO_POSITIVE_DOMAINS)
classify_with_confidence = functools.partial(classify_with_confidence,
                                             positive_domains=DEMO_POSITIVE_DOMAINS)


def _row(db: Database, url: str):
    conn = db._connect()
    row = conn.execute("SELECT * FROM search_results WHERE url = ?", (url,)).fetchone()
    conn.close()
    return row


# --------------------------------------------------------------------- #
# Migration: new columns + one-shot backfill of legacy rows
# --------------------------------------------------------------------- #
def test_migration_backfills_legacy_rows_as_rule_and_none(tmp_path):
    db_path = str(tmp_path / "t.db")
    db = Database(db_path=db_path)
    conn = db._connect()
    # Simulates a row inserted before this migration: classified_by and
    # review_status missing (NULL).
    conn.execute(
        "INSERT INTO search_results (run_id, url, classification) VALUES (?, ?, ?)",
        ("legacy-run", "https://example.com/legacy", "neutral"),
    )
    conn.commit()
    conn.close()

    # Reopening the DB re-runs _init_db, including the one-shot migration.
    db2 = Database(db_path=db_path)
    conn = db2._connect()
    row = conn.execute(
        "SELECT classified_by, review_status FROM search_results WHERE url = ?",
        ("https://example.com/legacy",),
    ).fetchone()
    conn.close()
    assert row["classified_by"] == "rule"
    assert row["review_status"] == "none"
    # It is NOT marked pending retroactively (avoids flooding the queue with history).
    assert db2.count_pending_review() == 0


# --------------------------------------------------------------------- #
# classify_with_confidence (reglas)
# --------------------------------------------------------------------- #
def test_rule_match_gives_full_confidence():
    result = {"domain": "jordanashworth.example", "url": "https://jordanashworth.example/x",
              "title": "", "description": ""}
    info = classify_with_confidence(result)
    assert info == {"classification": "positive", "confidence": 1.0, "classified_by": "rule"}


def test_unknown_domain_default_neutral_has_half_confidence():
    result = {"domain": "random-directory.example", "url": "https://random-directory.example/z",
              "title": "Jordan Ashworth profile", "description": "General bio."}
    info = classify_with_confidence(result)
    assert info == {"classification": "neutral", "confidence": 0.5, "classified_by": "rule"}


def test_classify_batch_adds_confidence_and_classified_by():
    results = [{"domain": "random-directory.example", "url": "https://random-directory.example/z",
                "title": "", "description": ""}]
    out = classify_batch(results)
    assert out[0]["classification"] == "neutral"
    assert out[0]["confidence"] == 0.5
    assert out[0]["classified_by"] == "rule"


# --------------------------------------------------------------------- #
# Review queue: save_results marks pending based on review_threshold
# --------------------------------------------------------------------- #
def test_low_confidence_result_enters_pending_queue_on_save(tmp_path):
    db = _db(tmp_path)
    db.set_setting("review_threshold", "0.7")
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us",
                status="completed", summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    results = classify_batch([
        {"domain": "random-directory.example", "url": "https://random-directory.example/z",
         "title": "", "description": "", "country_code": "us"},
    ])
    db.save_results(results, run_id="r1")

    pending = db.pending_review()
    assert len(pending) == 1
    assert pending[0]["url"] == "https://random-directory.example/z"
    assert pending[0]["confidence"] == 0.5
    assert pending[0]["review_status"] == "pending"
    assert db.count_pending_review() == 1


def test_high_confidence_result_does_not_enter_queue(tmp_path):
    db = _db(tmp_path)
    db.set_setting("review_threshold", "0.7")
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us",
                status="completed", summary={"total": 1, "positive": 1, "negative": 0, "neutral": 0})
    results = classify_batch([
        {"domain": "jordanashworth.example", "url": "https://jordanashworth.example/x",
         "title": "", "description": "", "country_code": "us"},
    ])
    db.save_results(results, run_id="r1")
    assert db.count_pending_review() == 0


def test_legacy_save_without_confidence_key_does_not_enter_queue(tmp_path):
    """Legacy caller (without 'confidence' in the dict) must not flood the queue."""
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us",
                status="completed", summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    db.save_results(
        [{"url": "https://example.com/legacy-save", "classification": "neutral", "country_code": "us"}],
        run_id="r1",
    )
    assert db.count_pending_review() == 0


# --------------------------------------------------------------------- #
# H2: human verdict inheritance between observations ("Sisyphus queue")
# --------------------------------------------------------------------- #
def test_human_verdict_is_inherited_by_new_observation_of_same_url(tmp_path):
    db = _db(tmp_path)
    url = "https://random-directory.example/z"
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    db.save_results(classify_batch([
        {"domain": "random-directory.example", "url": url, "title": "", "description": "", "country_code": "us"},
    ]), run_id="r1")
    row = db.pending_review()[0]
    db.set_review_verdict(row["id"], "negative")  # fixes human in run1

    # run2: same URL observed again -- must NOT re-ask.
    db.save_run(apify_run_id="r2", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    db.save_results(classify_batch([
        {"domain": "random-directory.example", "url": url, "title": "", "description": "", "country_code": "us"},
    ]), run_id="r2")

    conn = db._connect()
    new_row = conn.execute(
        "SELECT classification, classified_by, confidence, review_status FROM search_results "
        "WHERE run_id = 'r2' AND url = ?", (url,),
    ).fetchone()
    conn.close()
    assert new_row["classification"] == "negative"
    assert new_row["classified_by"] == "human"
    assert new_row["confidence"] == 1.0
    assert new_row["review_status"] == "approved"
    assert db.count_pending_review() == 0


# --------------------------------------------------------------------- #
# AI: prompt V2 (JSON) with confidence + robust, deterministic fallback
# --------------------------------------------------------------------- #
def test_classify_with_ai_confidence_parses_json(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    def fake_call_gemini(prompt, max_tokens=10, temperature=0.0, lower=True):
        return True, '{"classification": "negative", "confidence": 0.83}'

    monkeypatch.setattr(ai_classifier, "_call_gemini", fake_call_gemini)
    record = {"url": "https://example.com/ai-json", "title": "t", "description": "d"}

    result = ai_classifier.classify_with_ai_confidence(record)
    assert result == {"classification": "negative", "confidence": 0.83, "classified_by": "ai"}
    # classify_with_ai (backward-compat, str) still returns only the label.
    assert ai_classifier.classify_with_ai(record) == "negative"


def test_classify_with_ai_confidence_falls_back_when_no_json(tmp_path, monkeypatch):
    db = _db(tmp_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    def fake_call_gemini(prompt, max_tokens=10, temperature=0.0, lower=True):
        return True, "positive"  # no JSON -- disobedient model / legacy V1 prompt

    monkeypatch.setattr(ai_classifier, "_call_gemini", fake_call_gemini)
    record = {"url": "https://example.com/ai-no-json", "title": "t", "description": "d"}

    result = ai_classifier.classify_with_ai_confidence(record)
    assert result == {"classification": "positive", "confidence": 0.6, "classified_by": "ai"}


def test_classify_with_ai_confidence_handles_legacy_string_cache(tmp_path):
    """B8: a cache entry saved as a plain string (pre-2.1, or manual
    corruption) must not blow up -- it's normalized like the single-word
    fallback."""
    from database.models import url_hash

    db = _db(tmp_path)
    ai_classifier.set_cache_db(db)
    h = url_hash("https://example.com/legacy-cache")
    db.set_cached_verdict(h, "classify", ai_classifier._subject_version("classify"),
                          {"verdict": "positive"})
    record = {"url": "https://example.com/legacy-cache", "title": "t", "description": "d"}

    result = ai_classifier.classify_with_ai_confidence(record)
    assert result == {"classification": "positive", "confidence": 0.6, "classified_by": "ai"}
    assert ai_classifier.classify_with_ai(record) == "positive"


# --------------------------------------------------------------------- #
# M3: deterministic single-word fallback
# --------------------------------------------------------------------- #
def test_word_fallback_picks_the_unique_label_present():
    # _call_gemini already returns the text in lowercase (see .lower() there);
    # _parse_classify_word always receives text pre-normalized this way.
    assert ai_classifier._parse_classify_word("this is clearly positive overall") == {
        "classification": "positive", "confidence": 0.6,
    }


def test_word_fallback_is_empty_when_no_label_present():
    assert ai_classifier._parse_classify_word("no idea here") == {}


def test_word_fallback_is_empty_when_multiple_labels_present():
    """Used to depend on a set's iteration order (non-deterministic);
    now, ambiguous text with more than one label doesn't cache garbage."""
    assert ai_classifier._parse_classify_word("could be positive or negative") == {}


# --------------------------------------------------------------------- #
# M4: greedy JSON parsing -> raw_decode from each '{', stops at the first
# valid one; confidence on a 1-100 scale normalizes to 0-1.
# --------------------------------------------------------------------- #
def test_parse_classify_json_stops_at_first_valid_object():
    text = '{"classification": "positive", "confidence": 0.9} trailing text with a stray }'
    assert ai_classifier._parse_classify_json(text) == {
        "classification": "positive", "confidence": 0.9,
    }


def test_parse_classify_json_scales_percentage_confidence():
    text = '{"classification": "negative", "confidence": 85}'
    assert ai_classifier._parse_classify_json(text) == {
        "classification": "negative", "confidence": 0.85,
    }


# --------------------------------------------------------------------- #
# Human verdict immutability (2.1) + M5 (recompute run counts)
# --------------------------------------------------------------------- #
def test_set_review_verdict_correction_marks_human_and_corrected(tmp_path):
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    url = "https://random-directory.example/z"
    db.save_results(classify_batch([
        {"domain": "random-directory.example", "url": url, "title": "", "description": "", "country_code": "us"},
    ]), run_id="r1")
    row = db.pending_review()[0]

    result = db.set_review_verdict(row["id"], "positive")

    # P5: the return value is trimmed down to what the router consumes.
    assert result == {"url": url, "previous_classification": "neutral", "review_status": "corrected"}
    stored = _row(db, url)
    assert stored["classification"] == "positive"
    assert stored["classified_by"] == "human"
    assert stored["confidence"] == 1.0
    assert stored["review_status"] == "corrected"


def test_set_review_verdict_same_classification_marks_approved(tmp_path):
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    url = "https://random-directory.example/z"
    db.save_results(classify_batch([
        {"domain": "random-directory.example", "url": url, "title": "", "description": "", "country_code": "us"},
    ]), run_id="r1")
    row = db.pending_review()[0]

    result = db.set_review_verdict(row["id"], "neutral")  # matches the current one
    assert result["review_status"] == "approved"
    assert _row(db, url)["classified_by"] == "human"


def test_set_review_verdict_unknown_id_returns_none(tmp_path):
    db = _db(tmp_path)
    assert db.set_review_verdict(999, "positive") is None


def test_set_review_verdict_recomputes_run_counts(tmp_path):
    """M5: correcting an individual row also recomputes positive/negative/
    neutral/total_results of the run it belongs to (before, only
    reclassify_all did this)."""
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    db.save_results(classify_batch([
        {"domain": "random-directory.example", "url": "https://random-directory.example/z",
         "title": "", "description": "", "country_code": "us"},
    ]), run_id="r1")
    row = db.pending_review()[0]

    db.set_review_verdict(row["id"], "negative")

    run = db.get_run("r1")
    assert run["negative"] == 1
    assert run["neutral"] == 0
    assert run["positive"] == 0
    assert run["total_results"] == 1


def test_human_verdict_is_immutable_against_rule_reclassify(tmp_path):
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    db.save_results(classify_batch([
        {"domain": "random-directory.example", "url": "https://random-directory.example/z",
         "title": "", "description": "", "country_code": "us"},
    ]), run_id="r1")
    row = db.pending_review()[0]
    db.set_review_verdict(row["id"], "negative")  # fixes human

    # A bulk reclassification (rules or AI, see test_human_verdict_is_
    # immutable... above and reclassify_all's WHERE) that would say
    # "positive" for EVERYTHING must not touch the human row.
    db.reclassify_all(lambda rec: {"classification": "positive", "confidence": 1.0, "classified_by": "rule"})

    after = _row(db, "https://random-directory.example/z")
    assert after["classification"] == "negative"
    assert after["classified_by"] == "human"


def test_reclassify_all_still_updates_non_human_rows(tmp_path):
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    db.save_results(classify_batch([
        {"domain": "random-directory.example", "url": "https://random-directory.example/z",
         "title": "", "description": "", "country_code": "us"},
    ]), run_id="r1")

    n = db.reclassify_all(lambda rec: {"classification": "positive", "confidence": 1.0, "classified_by": "rule"})
    assert n == 1
    row = _row(db, "https://random-directory.example/z")
    assert row["classification"] == "positive"
    assert row["confidence"] == 1.0
    assert row["classified_by"] == "rule"


# --------------------------------------------------------------------- #
# H1: recompute_pending moves rows in BOTH directions, and only if True
# --------------------------------------------------------------------- #
def test_recompute_pending_true_moves_rows_both_directions(tmp_path):
    db = _db(tmp_path)
    db.set_setting("review_threshold", "0.7")
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 2, "positive": 1, "negative": 0, "neutral": 1})
    db.save_results(classify_batch([
        {"domain": "random-directory.example", "url": "https://random-directory.example/a",
         "title": "", "description": "", "country_code": "us"},  # confidence 0.5 -> pending
        {"domain": "jordanashworth.example", "url": "https://jordanashworth.example/b",
         "title": "", "description": "", "country_code": "us"},  # confidence 1.0 -> none
    ]), run_id="r1")
    assert _row(db, "https://random-directory.example/a")["review_status"] == "pending"
    assert _row(db, "https://jordanashworth.example/b")["review_status"] == "none"

    def fn(rec):
        if rec["url"].endswith("/a"):
            return {"classification": "neutral", "confidence": 0.9, "classified_by": "ai"}  # goes up -> exits pending
        return {"classification": "negative", "confidence": 0.4, "classified_by": "ai"}  # goes down -> enters pending

    db.reclassify_all(fn, recompute_pending=True)

    assert _row(db, "https://random-directory.example/a")["review_status"] == "none"
    assert _row(db, "https://jordanashworth.example/b")["review_status"] == "pending"


def test_recompute_pending_false_never_touches_review_status(tmp_path):
    db = _db(tmp_path)
    db.set_setting("review_threshold", "0.7")
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 1, "negative": 0, "neutral": 0})
    db.save_results(classify_batch([
        {"domain": "jordanashworth.example", "url": "https://jordanashworth.example/c",
         "title": "", "description": "", "country_code": "us"},
    ]), run_id="r1")
    assert _row(db, "https://jordanashworth.example/c")["review_status"] == "none"

    # Confidence drops well below the threshold, but recompute_pending=False
    # (default) -- review_status is NOT touched.
    db.reclassify_all(lambda rec: {"classification": "negative", "confidence": 0.1, "classified_by": "ai"})

    assert _row(db, "https://jordanashworth.example/c")["review_status"] == "none"


# --------------------------------------------------------------------- #
# M6: a rules-ONLY pass doesn't overwrite an AI verdict with the
# default-neutral (confidence 0.5)
# --------------------------------------------------------------------- #
def test_rule_reclassify_does_not_overwrite_ai_verdict_with_default_neutral(tmp_path):
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 1, "negative": 0, "neutral": 0})
    db.save_results(classify_batch([
        {"domain": "random-directory.example", "url": "https://random-directory.example/z",
         "title": "", "description": "", "country_code": "us"},
    ]), run_id="r1")
    # Simulates an AI verdict already saved (e.g. from an earlier AI
    # reclassification) directly on the row.
    conn = db._connect()
    conn.execute(
        "UPDATE search_results SET classification='positive', confidence=0.82, "
        "classified_by='ai' WHERE url=?", ("https://random-directory.example/z",),
    )
    conn.commit()
    conn.close()

    # Rules-ONLY reclassification: with no lists, "random-directory.example"
    # matches nothing -> "default neutral" (0.5). Must not overwrite the AI.
    db.reclassify_all(
        lambda rec: classify_with_confidence(rec, positive_domains=[], negative_domains=[])
    )

    row = _row(db, "https://random-directory.example/z")
    assert row["classification"] == "positive"
    assert row["confidence"] == 0.82
    assert row["classified_by"] == "ai"


# --------------------------------------------------------------------- #
# M7: conditional UPDATE -- doesn't write if nothing changed
# --------------------------------------------------------------------- #
def test_reclassify_all_skips_update_when_nothing_changes(tmp_path, monkeypatch):
    """M7: sqlite3.Cursor/Connection are immutable types (their execute
    method can't be monkeypatched directly) -- Connection.total_changes is
    used instead (native counter of modified rows, reset to 0 on each new
    connection -- and reclassify_all opens a new connection per call),
    captured right before the connection closes, via
    Connection.set_trace_callback wrapping Database._connect."""
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 1, "positive": 1, "negative": 0, "neutral": 0})
    db.save_results(classify_batch([
        {"domain": "jordanashworth.example", "url": "https://jordanashworth.example/z",
         "title": "", "description": "", "country_code": "us"},
    ]), run_id="r1")

    original_connect = Database._connect
    last_total_changes = {}

    def traced_connect(self):
        conn = original_connect(self)
        conn.set_trace_callback(lambda _sql: last_total_changes.__setitem__("n", conn.total_changes))
        return conn

    monkeypatch.setattr(Database, "_connect", traced_connect)

    # classify_fn returns EXACTLY the same thing already stored: only the
    # `runs` row should change (unconditional count recompute), no
    # `search_results` row.
    db.reclassify_all(
        lambda rec: {"classification": "positive", "confidence": 1.0, "classified_by": "rule"}
    )
    unchanged_total = last_total_changes["n"]
    assert unchanged_total == 1  # only the runs UPDATE -- neither search_results nor its FTS5 triggers

    # With a real change, it should write (and that DOES trigger the FTS5
    # triggers on search_results, which is why we don't compare an exact number).
    db.reclassify_all(
        lambda rec: {"classification": "negative", "confidence": 1.0, "classified_by": "rule"}
    )
    assert last_total_changes["n"] > unchanged_total


# --------------------------------------------------------------------- #
# App: badge, /review page, audit (C24)
# --------------------------------------------------------------------- #
def _seed_pending(seeded_db):
    seeded_db.set_setting("review_threshold", "0.7")
    seeded_db.save_run(apify_run_id="r-pending", queries=["q"], country_code="us",
                       status="completed",
                       summary={"total": 1, "positive": 0, "negative": 0, "neutral": 1})
    seeded_db.save_results(classify_batch([
        {"domain": "random-directory.example", "url": "https://random-directory.example/pending-item",
         "title": "Pending item", "description": "", "country_code": "us"},
    ]), run_id="r-pending")


def test_pending_result_appears_in_the_review_dialog(client, seeded_db):
    _seed_pending(seeded_db)
    response = client.get("/ui/review", headers={"X-SM-Fragment": "1"})
    assert response.status_code == 200
    assert "random-directory.example/pending-item" in response.text
    old = client.get("/review", follow_redirects=False)
    assert old.status_code == 303 and old.headers["location"] == "/results?modal=/ui/review"


def test_review_dialog_focuses_the_first_verdict_not_the_title_link(client, seeded_db):
    """Initial focus (app.js focusFirst/[autofocus]) lands on "Positive"; the
    result's title link stays a plain, focusable link."""
    import re
    _seed_pending(seeded_db)
    html = client.get("/ui/review", headers={"X-SM-Fragment": "1"}).text
    autofocused = re.findall(r"<[^>]*\bautofocus\b[^>]*>", html)
    assert len(autofocused) == 1
    assert "value='positive'" in autofocused[0]
    link = re.search(r"<a class='rtitle'[^>]*>", html).group(0)
    assert "autofocus" not in link and "tabindex" not in link


def test_review_button_shows_pending_count(client, seeded_db):
    _seed_pending(seeded_db)
    response = client.get("/results")
    assert response.status_code == 200
    assert "<span class='count' data-review-count>1</span>" in response.text


def test_correcting_verdict_removes_it_from_queue_and_is_audited(client, seeded_db):
    _seed_pending(seeded_db)
    row = seeded_db.pending_review()[0]

    response = client.post(f"/review/{row['id']}/verdict", data={"classification": "positive"})
    assert response.status_code == 200
    assert seeded_db.count_pending_review() == 0

    audited = seeded_db.list_audit(action="review_verdict")
    assert len(audited) == 1
    assert audited[0]["resource_id"] == "https://random-directory.example/pending-item"
    assert '"verdict": "positive"' in audited[0]["params_json"]


def test_correcting_verdict_then_bulk_reclassify_does_not_undo_it(client, seeded_db):
    _seed_pending(seeded_db)
    row = seeded_db.pending_review()[0]
    client.post(f"/review/{row['id']}/verdict", data={"classification": "negative"})

    response = client.post("/domains/reclassify")
    assert response.status_code == 200

    after = _row(seeded_db, "https://random-directory.example/pending-item")
    assert after["classification"] == "negative"
    assert after["classified_by"] == "human"


# --------------------------------------------------------------------- #
# The one-at-a-time Review dialog (/ui/review)
# --------------------------------------------------------------------- #
_FRAG = {"X-SM-Fragment": "1"}


def _seed_queue(db, n=3):
    from processing.classifier import classify_batch
    db.save_run(apify_run_id="r-queue", queries=["q"], country_code="us", status="completed",
                summary={"total": n, "positive": 0, "negative": 0, "neutral": n})
    db.save_results(classify_batch([
        {"domain": f"item{i}.example", "url": f"https://item{i}.example/page",
         "title": f"Queue item {i}", "description": "", "country_code": "us"}
        for i in range(n)
    ]), run_id="r-queue")
    return db.count_pending_review()


def test_review_dialog_shows_one_result_at_a_time_with_progress(client, seeded_db):
    total = _seed_queue(seeded_db)
    assert total == 3
    text = client.get("/ui/review", headers=_FRAG).text
    assert "<html" not in text
    assert "1 of 3" in text
    assert text.count("class='rev-card'") == 1
    assert "Why it's here:" in text
    for key, label in (("1", "Positive"), ("2", "Negative"), ("3", "Neutral"), ("4", "Not this person")):
        assert f"data-key='{key}'" in text and label in text
    assert "data-review-keys" in text and "data-sm-submit" in text
    assert "Skip" in text


def test_review_verdict_auto_advances_and_updates_the_badge(client, seeded_db):
    _seed_queue(seeded_db)
    first = seeded_db.pending_review(limit=1)[0]
    resp = client.post(f"/review/{first['id']}/verdict", headers=_FRAG,
                       data={"classification": "negative", "done": "0", "total": "3"})
    assert resp.status_code == 200
    assert unquote(resp.headers["X-SM-Toast"]) == "Marked negative."
    assert "2 of 3" in resp.text and first["title"] not in resp.text
    assert "data-sm-review-count='2'" in resp.text
    row = _row(seeded_db, first["url"])
    assert row["classified_by"] == "human" and row["classification"] == "negative"
    assert seeded_db.list_audit(action="review_verdict")


def test_review_not_this_person_advances_too(client, seeded_db):
    _seed_queue(seeded_db, 2)
    first = seeded_db.pending_review(limit=1)[0]
    resp = client.post(f"/review/{first['id']}/not-this-person", headers=_FRAG,
                       data={"done": "0", "total": "2"})
    assert resp.status_code == 200 and "2 of 2" in resp.text
    assert unquote(resp.headers["X-SM-Toast"]) == "Marked as a different person."


def test_review_skip_moves_the_item_to_the_end(client, seeded_db):
    _seed_queue(seeded_db)
    first = seeded_db.pending_review(limit=1)[0]
    text = client.get("/ui/review", params={"skip": str(first["id"]), "total": "3"}, headers=_FRAG).text
    assert first["title"] not in text and "1 of 3" in text


def test_review_all_caught_up(client, seeded_db):
    text = client.get("/ui/review", headers=_FRAG).text
    assert "All caught up." in text and "data-sm-review-count='0'" in text


def test_change_label_for_one_result_goes_back_to_results(client, seeded_db):
    row = seeded_db.get_results_filtered(limit=1)[0]
    text = client.get("/ui/review", params={"id": row["id"]}, headers=_FRAG).text
    assert "Change label" in text and "name='mode' value='item'" in text
    resp = client.post(f"/review/{row['id']}/verdict", headers=_FRAG,
                       data={"classification": "neutral", "mode": "item", "back": "/results?country=us"})
    assert resp.status_code == 200 and resp.headers["X-SM-Redirect"] == "/results?country=us"
    assert _row(seeded_db, row["url"])["classified_by"] == "human"


def test_review_verdict_without_js_is_post_redirect_get(client, seeded_db):
    _seed_queue(seeded_db, 1)
    first = seeded_db.pending_review(limit=1)[0]
    resp = client.post(f"/review/{first['id']}/verdict", data={"classification": "positive"},
                       follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/results?modal=/ui/review"


def test_app_js_wires_review_keyboard_shortcuts():
    js = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
    assert "[data-review-keys]" in js and "data-key=" in js
    assert "syncReviewBadge" in js and "data-sm-review-count" in js
