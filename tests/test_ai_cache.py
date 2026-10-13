"""Tests for item 2.5: AI verdict cache per URL.

TDD: url_hash (equivalence/differences), the cached_verdict wrapper (hit,
miss, failure not cached, prompt_version change), hit-rate stats, and
that the 3 cacheable functions (classify_with_ai/is_same_person/
analyze_negative) actually use the cache without network (stub of _call_gemini).
"""
from database.models import Database, url_hash
from processing import ai_classifier


# --------------------------------------------------------------------- #
# url_hash / normalize_url
# --------------------------------------------------------------------- #
def test_url_hash_ignores_host_case():
    assert url_hash("https://Example.COM/foo") == url_hash("https://example.com/foo")


def test_url_hash_ignores_fragment():
    assert url_hash("https://example.com/foo#section") == url_hash("https://example.com/foo")


def test_url_hash_ignores_utm_params():
    a = url_hash("https://example.com/foo?utm_source=x&utm_campaign=y")
    assert a == url_hash("https://example.com/foo")


def test_url_hash_ignores_fbclid_and_gclid():
    a = url_hash("https://example.com/foo?fbclid=abc123")
    b = url_hash("https://example.com/foo?gclid=xyz789")
    assert a == url_hash("https://example.com/foo")
    assert b == url_hash("https://example.com/foo")


def test_url_hash_keeps_non_tracking_query_param():
    # A real parameter (not tracking) DOES change the URL's identity.
    assert url_hash("https://example.com/foo?id=1") != url_hash("https://example.com/foo")


def test_url_hash_differs_for_different_path():
    assert url_hash("https://example.com/foo") != url_hash("https://example.com/bar")


def test_url_hash_differs_for_different_host():
    assert url_hash("https://example.com/foo") != url_hash("https://example.org/foo")


def test_url_hash_ignores_trailing_slash():
    assert url_hash("https://example.com/foo/") == url_hash("https://example.com/foo")


def test_url_hash_defaults_scheme_and_lowers_host_without_scheme():
    assert url_hash("Example.com/foo") == url_hash("https://example.com/foo")


def test_url_hash_ignores_query_param_order():
    assert url_hash("https://example.com/p?a=1&b=2") == url_hash("https://example.com/p?b=2&a=1")


# --------------------------------------------------------------------- #
# cached_verdict wrapper (direct DB, no network)
# --------------------------------------------------------------------- #
def test_cached_verdict_miss_then_hit_does_not_recompute(db_path):
    db = Database(db_path=db_path)
    ai_classifier.set_cache_db(db)
    calls = []

    def compute():
        calls.append(1)
        return "positive"

    first = ai_classifier.cached_verdict("https://example.com/dup", "classify", compute)
    second = ai_classifier.cached_verdict("https://example.com/dup", "classify", compute)

    assert first == "positive"
    assert second == "positive"
    assert len(calls) == 1  # the second hit does not execute compute_fn


def test_cached_verdict_failure_is_not_cached(db_path):
    db = Database(db_path=db_path)
    ai_classifier.set_cache_db(db)
    calls = []

    def compute_empty():
        calls.append(1)
        return ""

    ai_classifier.cached_verdict("https://example.com/fail", "classify", compute_empty)
    ai_classifier.cached_verdict("https://example.com/fail", "classify", compute_empty)

    assert len(calls) == 2  # a Gemini failure does not poison the cache


def test_cached_verdict_dict_with_all_empty_values_is_not_cached(db_path):
    db = Database(db_path=db_path)
    ai_classifier.set_cache_db(db)
    calls = []

    def compute_empty_dict():
        calls.append(1)
        return {"category": "", "severity": "", "action": "", "same": ""}

    ai_classifier.cached_verdict("https://example.com/fail2", "analyze_negative", compute_empty_dict)
    ai_classifier.cached_verdict("https://example.com/fail2", "analyze_negative", compute_empty_dict)

    assert len(calls) == 2


def test_prompt_version_change_forces_recompute(db_path, monkeypatch):
    db = Database(db_path=db_path)
    ai_classifier.set_cache_db(db)
    calls = []

    def compute():
        calls.append(1)
        return "negative"

    ai_classifier.cached_verdict("https://example.com/v", "classify", compute)
    # 2.1: PROMPT_VERSIONS["classify"] is already "v2" by default (confidence
    # JSON prompt) -- monkeypatch to a value DIFFERENT from the current one,
    # any value, to prove that a new PK forces recompute (the exact value
    # of the default version doesn't matter).
    monkeypatch.setitem(ai_classifier.PROMPT_VERSIONS, "classify", "v-test-bump")
    ai_classifier.cached_verdict("https://example.com/v", "classify", compute)

    assert len(calls) == 2  # new version => new PK => recompute


def test_cached_verdict_without_db_injected_always_computes(monkeypatch):
    calls = []

    def compute():
        calls.append(1)
        return "positive"

    # monkeypatch restores _db on exit: no global state leak between tests.
    monkeypatch.setattr(ai_classifier, "_db", None)
    ai_classifier.cached_verdict("https://example.com/nodb", "classify", compute)
    ai_classifier.cached_verdict("https://example.com/nodb", "classify", compute)

    assert len(calls) == 2  # without an injected DB there's no cache (pre-2.5 behavior)


def test_ai_cache_stats_reflects_hits_and_misses(db_path):
    db = Database(db_path=db_path)
    ai_classifier.set_cache_db(db)

    ai_classifier.cached_verdict("https://example.com/s", "classify", lambda: "positive")  # miss
    ai_classifier.cached_verdict("https://example.com/s", "classify", lambda: "positive")  # hit

    stats = db.ai_cache_stats()
    assert stats["entries"] == 1
    assert stats["hits"] == 1
    assert stats["misses"] == 1


# --------------------------------------------------------------------- #
# Integration: classify_with_ai / is_same_person / analyze_negative
# actually use the cache (no real network: _call_gemini is stubbed).
# --------------------------------------------------------------------- #
def test_classify_with_ai_repeated_url_produces_one_gemini_call(db_path, monkeypatch):
    db = Database(db_path=db_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    calls = []

    def fake_call_gemini(prompt, max_tokens=10, temperature=0.0, lower=True):
        calls.append(prompt)
        return True, "positive"

    monkeypatch.setattr(ai_classifier, "_call_gemini", fake_call_gemini)
    record = {"url": "https://example.com/classify-me", "title": "t", "description": "d"}

    assert ai_classifier.classify_with_ai(record) == "positive"
    assert ai_classifier.classify_with_ai(record) == "positive"
    assert len(calls) == 1


def test_is_same_person_repeated_url_produces_one_gemini_call(db_path, monkeypatch):
    db = Database(db_path=db_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    calls = []

    def fake_call_gemini(prompt, max_tokens=10, temperature=0.0, lower=True):
        calls.append(prompt)
        return True, "yes"

    monkeypatch.setattr(ai_classifier, "_call_gemini", fake_call_gemini)
    record = {"url": "https://example.com/same-person", "title": "t", "description": "d"}

    assert ai_classifier.is_same_person(record) == "yes"
    assert ai_classifier.is_same_person(record) == "yes"
    assert len(calls) == 1


def test_analyze_negative_repeated_url_produces_one_gemini_call(db_path, monkeypatch):
    # 2.2: PROMPT_ANALYZE_NEG_V2 asks for JSON (severity int, topic from the
    # closed taxonomy) instead of V1's line-by-line format -- see
    # ai_classifier._parse_analyze_negative_json.
    db = Database(db_path=db_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    calls = []

    def fake_call_gemini(prompt, max_tokens=10, temperature=0.0, lower=True):
        calls.append(prompt)
        return True, '{"severity": 4, "topic": "legal", "action": "mitigate", "same": "yes"}'

    monkeypatch.setattr(ai_classifier, "_call_gemini", fake_call_gemini)
    record = {"url": "https://example.com/analyze-me", "title": "t", "description": "d"}

    first = ai_classifier.analyze_negative(record)
    second = ai_classifier.analyze_negative(record)

    assert first == second == {
        "topic": "legal", "category": "legal", "severity": 4,
        "action": "mitigate", "same": "yes",
    }
    assert len(calls) == 1


def test_classify_with_ai_force_neutral_bypasses_cache_and_gemini(db_path, monkeypatch):
    """is_force_neutral short-circuits before the AI: it must not call Gemini
    nor touch the cache (nothing to cache, it's a deterministic local rule)."""
    db = Database(db_path=db_path)
    ai_classifier.set_cache_db(db)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    calls = []
    monkeypatch.setattr(
        ai_classifier, "_call_gemini",
        lambda prompt, max_tokens=10, temperature=0.0, lower=True: (calls.append(1), (True, "positive"))[1],
    )

    from config.positive_domains import FORCE_NEUTRAL_URL_FRAGMENTS
    fragment = next(iter(FORCE_NEUTRAL_URL_FRAGMENTS))
    record = {"url": f"https://example.com/{fragment}", "title": "t", "description": "d"}

    assert ai_classifier.classify_with_ai(record) == "neutral"
    assert len(calls) == 0
    assert db.ai_cache_stats()["entries"] == 0
    # B11 (2.1): force-neutral is a deterministic rule, not an AI verdict --
    # classified_by must reflect that ('rule', not 'ai').
    assert ai_classifier.classify_with_ai_confidence(record) == {
        "classification": "neutral", "confidence": 1.0, "classified_by": "rule",
    }
