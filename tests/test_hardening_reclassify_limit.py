"""FIX 1 point 1b (hardening audit): /domains/reclassify-ai called
Database.reclassify_all over ALL of search_results with no LIMIT -- the
url_hash cache saves it in steady state, but bumping PROMPT_VERSIONS
invalidates the whole thing and a single click re-pays the full table to
Gemini. This file covers the new `limit` parameter of reclassify_all and
the main.py guard that uses it (setting `reclassify_batch_limit`, default
200).
"""
from conftest import make_db as _db

from database.models import Database


def _seed_neutrals(db: Database, n: int, run_id: str = "run-reclassify") -> None:
    db.save_run(apify_run_id=run_id, queries=["q"], country_code="us", status="completed",
                summary={"total": n, "positive": 0, "negative": 0, "neutral": n})
    rows = [
        {"url": f"https://neutral-{i}.example.com/", "title": "t", "description": "d",
         "displayed_url": f"neutral-{i}.example.com", "position": i, "country_code": "us",
         "domain": f"neutral-{i}.example.com", "classification": "neutral", "query": "q"}
        for i in range(n)
    ]
    db.save_results(rows, run_id=run_id)


def _always_negative(rec):
    return {"classification": "negative", "confidence": 1.0, "classified_by": "rule"}


# --------------------------------------------------------------------- #
# Database.reclassify_all(limit=...)
# --------------------------------------------------------------------- #
def test_reclassify_all_without_limit_processes_every_row(tmp_path):
    db = _db(tmp_path)
    _seed_neutrals(db, 10)
    n = db.reclassify_all(_always_negative)
    assert n == 10


def test_reclassify_all_with_limit_caps_rows_processed(tmp_path):
    db = _db(tmp_path)
    _seed_neutrals(db, 10)
    n = db.reclassify_all(_always_negative, limit=3)
    assert n == 3


def test_reclassify_all_limit_larger_than_table_processes_all(tmp_path):
    db = _db(tmp_path)
    _seed_neutrals(db, 5)
    n = db.reclassify_all(_always_negative, limit=200)
    assert n == 5


# --------------------------------------------------------------------- #
# /domains: button states the cap + configurable setting
# --------------------------------------------------------------------- #
def _domains(client):
    """The AI relabel cap is explained where the action lives: Results → More
    → AI checks (one home per feature)."""
    return client.get("/ui/ai-checks", headers={"X-SM-Fragment": "1"})


def test_domains_page_shows_reclassify_ai_batch_limit(client, seeded_db, monkeypatch):
    from processing import ai_classifier
    monkeypatch.setattr(ai_classifier, "is_configured", lambda: True)
    monkeypatch.setattr(ai_classifier, "provider_info", lambda: {"model": "m"})
    resp = _domains(client)
    assert resp.status_code == 200
    assert "200" in resp.text
    assert "reclassify_batch_limit" in resp.text


def test_domains_page_reflects_custom_batch_limit_setting(client, seeded_db):
    seeded_db.set_setting("reclassify_batch_limit", "7")
    resp = _domains(client)
    assert "up to <b>7</b> stored results" in resp.text


def test_post_reclassify_ai_respects_batch_limit(client, seeded_db, monkeypatch):
    from processing import ai_classifier

    seeded_db.set_setting("reclassify_batch_limit", "2")
    _seed_neutrals(seeded_db, 10, run_id="run-reclassify-ai")
    monkeypatch.setattr(ai_classifier, "is_configured", lambda: True)
    monkeypatch.setattr(ai_classifier, "classify_with_ai_confidence",
                        lambda rec: {"classification": "negative", "confidence": 0.9,
                                     "classified_by": "ai"})

    resp = client.post("/domains/reclassify-ai", follow_redirects=True)

    assert resp.status_code == 200
    audited = seeded_db.list_audit(action="domains_reclassify_ai")
    assert len(audited) == 1
    # the displayed summary never claims to have reclassified more than the cap
    import json
    params = json.loads(audited[0]["params_json"])
    assert params["limit"] == 2
    assert params["reclassified"] <= 2
