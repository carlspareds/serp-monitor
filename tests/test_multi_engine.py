"""Tests for item 1.1 (Phase 4): multi-engine extraction (Bing today; news/
images/youtube remain registry entries without an actor -- see the brief).

Acceptance (§1.1): "A multi-country run
with engines=['google','bing'] produces classified rows from both engines,
filterable in /results, and the HIST history distinguishes engine."

TDD, no network: ApifyClient is replaced with a controllable stub
(_MultiEngineFakeClient) that maps actor_id -> behavior, same spirit as
tests/test_apify_costs.py::_FakeClient (but that one only knows ONE
actor -- here we need to distinguish per engine).
"""
import pytest
from conftest import make_db as _db

from apify import runner as runner_module
from apify.actors import (
    ACTORS,
    ENGINE_NOT_CONFIGURED_MSG,
    normalize_bing,
    normalize_google,
)
from apify.client import ApifyError
from apify.runner import ApifyRunner
from config.apify_config import BING_SEARCH_SCRAPER, GOOGLE_SEARCH_SCRAPER
from report.multi_country import persist_engine_run, run_multi_country
from serp.base import SerpProviderError

SUMMARY = {"total": 1, "positive": 0, "negative": 0, "neutral": 1}


# --------------------------------------------------------------------- #
# 1) Actor registry (apify.actors.ACTORS)
# --------------------------------------------------------------------- #
def test_actors_registry_shape():
    """Merges (review TESTS #13) the 3 tests that used to separately verify
    the keys, google/bing configured, and the 3 verticals without an
    actor -- a single assertion over the full registry.

    'images' has no actor on purpose: an id here would show it as an
    enabled search-engine checkbox on /search_page."""
    assert set(ACTORS.keys()) == {"google", "bing", "news", "images", "youtube"}
    assert ACTORS["google"]["id"] == GOOGLE_SEARCH_SCRAPER
    assert callable(ACTORS["google"]["normalize"])
    assert ACTORS["bing"]["id"] == BING_SEARCH_SCRAPER
    assert callable(ACTORS["bing"]["normalize"])
    for engine in ("news", "images", "youtube"):
        assert ACTORS[engine]["id"] is None
        assert ACTORS[engine]["normalize"] is None


# --------------------------------------------------------------------- #
# 2) normalize_google: extracted from _flatten_results with NO changes
# --------------------------------------------------------------------- #
def test_normalize_google_flattens_organic_results():
    raw = [{
        "searchQuery": {"term": "q"},
        "organicResults": [
            {"position": 1, "title": "T1", "url": "https://a.example/x", "displayedUrl": "a.example",
             "description": "D1"},
            {"position": 2, "title": "T2", "url": "https://b.example/y", "description": "D2"},
        ],
    }]

    results = normalize_google(raw, "us")

    assert len(results) == 2
    assert results[0]["position"] == 1
    assert results[0]["domain"] == "a.example"
    assert results[1]["displayed_url"] == ""


# --------------------------------------------------------------------- #
# 3) normalize_bing: DEFENSIVE -- complete/empty/malformed payload
# --------------------------------------------------------------------- #
def test_normalize_bing_complete_payload_with_organic_results_key():
    raw = [{
        "searchQuery": {"term": "q"},
        "organicResults": [
            {"position": 1, "title": "Bing T1", "url": "https://bing1.example/x",
             "displayedUrl": "bing1.example", "description": "Bing D1"},
            {"position": 2, "title": "Bing T2", "url": "https://bing2.com/y",
             "description": "Bing D2"},
        ],
    }]

    results = normalize_bing(raw, "us")

    assert len(results) == 2
    assert results[0]["title"] == "Bing T1"
    assert results[0]["url"] == "https://bing1.example/x"
    assert results[0]["domain"] == "bing1.example"
    assert results[0]["position"] == 1
    assert results[0]["query"] == "q"
    assert results[0]["country_code"] == "us"


def test_normalize_bing_position_falls_back_to_index_when_missing():
    raw = [{"query": "q", "organicResults": [
        {"url": "https://a.example", "title": "A"},
        {"url": "https://b.example", "title": "B"},
    ]}]

    results = normalize_bing(raw, "us")

    assert [r["position"] for r in results] == [1, 2]


def test_normalize_bing_empty_payload_returns_no_rows():
    assert normalize_bing([], "us") == []
    assert normalize_bing([{"searchQuery": {"term": "q"}, "organicResults": []}], "us") == []


def test_normalize_bing_malformed_payload_never_raises():
    malformed = [
        None,
        "not-a-dict",
        42,
        {"organicResults": "not-a-list"},
        {"organicResults": [None, 42, "x", {"no_url_here": True}]},
        {"organicResults": [{"url": 123, "title": 456}]},  # non-string url: gets discarded
    ]

    results = normalize_bing(malformed, "us")

    assert results == []


def test_normalize_bing_raw_items_not_a_list_returns_empty():
    assert normalize_bing(None, "us") == []
    assert normalize_bing({"not": "a list"}, "us") == []


# --------------------------------------------------------------------- #
# 3b) normalize_bing: FIX 1 (live-payloads fix, confirmed on a real
# run) -- REAL url reconstructed from displayedUrl, the
# bing.com/ck/a redirector must NEVER end up as `url`/`domain` when
# displayedUrl is usable.
# --------------------------------------------------------------------- #
def test_normalize_bing_reconstructs_real_url_from_displayed_url():
    """LITERAL payload shape observed live on a real run: url carries
    the redirector, displayedUrl carries the real URL as a breadcrumb with '›'."""
    raw = [{
        "searchQuery": {"term": "Jordan Ashworth"},
        "organicResults": [{
            "position": "1",
            "title": "About Jordan Ashworth",
            "url": "https://www.bing.com/ck/a?!&&p=42f7aa81190397dacba6fad0155169e3",
            "displayedUrl": "https://jordanashworth.example › about-jordan-ashworth",
            "description": "J has based in several countries over the years including the USA...",
        }],
    }]

    results = normalize_bing(raw, "us")

    assert len(results) == 1
    assert results[0]["url"] == "https://jordanashworth.example/about-jordan-ashworth"
    assert results[0]["domain"] == "jordanashworth.example"
    assert results[0]["position"] == 1  # position string -> int


def test_normalize_bing_falls_back_to_redirector_without_displayed_url(capsys):
    raw = [{
        "searchQuery": {"term": "q"},
        "organicResults": [{
            "position": 1, "title": "T",
            "url": "https://www.bing.com/ck/a?!&&p=abc123", "description": "D",
        }],
    }]

    results = normalize_bing(raw, "us")

    assert results[0]["url"] == "https://www.bing.com/ck/a?!&&p=abc123"
    assert results[0]["domain"] == "bing.com"
    out = capsys.readouterr().out
    assert "redirector" in out


def test_normalize_bing_decodes_html_entities_in_title_and_description():
    """Seen live: "Jordan Ashworth - Presidente &amp; ..."."""
    raw = [{
        "searchQuery": {"term": "q"},
        "organicResults": [{
            "position": 1,
            "title": "Jordan Ashworth - Presidente &amp; CEO",
            "url": "https://www.bing.com/ck/a?x",
            "displayedUrl": "https://x.example › about",
            "description": "Bio &amp; more details",
        }],
    }]

    results = normalize_bing(raw, "us")

    assert results[0]["title"] == "Jordan Ashworth - Presidente & CEO"
    assert results[0]["description"] == "Bio & more details"


def test_normalize_bing_position_string_converts_to_int():
    raw = [{
        "searchQuery": {"term": "q"},
        "organicResults": [{
            "position": "3", "title": "T",
            "displayedUrl": "https://x.example › a", "url": "https://www.bing.com/ck/a?x",
        }],
    }]

    results = normalize_bing(raw, "us")

    assert results[0]["position"] == 3
    assert isinstance(results[0]["position"], int)


# --------------------------------------------------------------------- #
# 4) ApifyRunner.search(engines=[...]): multi-engine orchestration
# --------------------------------------------------------------------- #
class _MultiEngineFakeClient:
    """Maps actor_id -> behavior (start/wait/dataset), to be able to
    control each engine separately within the same test (unlike
    test_apify_costs.py::_FakeClient, which only knows one actor).

    Each `by_actor` entry accepts:
      - "run_id": str
      - "dataset": list (defaults to [] if omitted)
      - "start_error": optional Exception -- start_actor fails for that engine
      - "final_run": dict -- SINGLE result of wait_for_run (simple case)
      - "wait_sequence": list[dict|Exception] -- alternative to "final_run":
        ONE element is consumed per call to wait_for_run, to test the
        two-phase retry (1.4) PER ENGINE (review TESTS #13) -- an
        ApifyError in the sequence simulates a WAIT failure that is
        retried on the SAME run_id, without relaunching start_actor.
    """

    def __init__(self, by_actor: dict[str, dict]):
        self.by_actor = {aid: dict(cfg) for aid, cfg in by_actor.items()}
        for cfg in self.by_actor.values():
            if "wait_sequence" in cfg:
                cfg["wait_sequence"] = list(cfg["wait_sequence"])
        self.start_calls: list[str] = []
        self.wait_calls: list[str] = []

    def start_actor(self, actor_id, input_data):
        self.start_calls.append(actor_id)
        cfg = self.by_actor[actor_id]
        if isinstance(cfg.get("start_error"), Exception):
            raise cfg["start_error"]
        return {"id": cfg["run_id"]}

    def wait_for_run(self, run_id, max_wait=300, on_status=None):
        self.wait_calls.append(run_id)
        for cfg in self.by_actor.values():
            if cfg["run_id"] == run_id:
                item = cfg["wait_sequence"].pop(0) if "wait_sequence" in cfg else cfg["final_run"]
                if isinstance(item, Exception):
                    raise item
                cfg["_resolved_final_run"] = item  # ver get_dataset_items
                return item
        raise ApifyError(f"unknown run: {run_id}")

    def get_dataset_items(self, dataset_id):
        for cfg in self.by_actor.values():
            final = cfg.get("_resolved_final_run", cfg.get("final_run"))
            # Guard (review TESTS #13): final_run can be an Exception
            # (wait_for_run that exhausts retries without success) -- that
            # has no .get(), so it must never be treated as the final dict
            # (in practice get_dataset_items is never invoked in that case,
            # but the guard avoids an AttributeError if some future test
            # changes the order).
            if isinstance(final, dict) and final.get("defaultDatasetId") == dataset_id:
                return cfg["dataset"]
        return []


def _google_page(url="https://g.example/x", pos=1):
    return {"searchQuery": {"term": "q"}, "organicResults": [
        {"position": pos, "title": "G", "url": url, "description": "d"}
    ]}


def _bing_page(url="https://b.example/x", pos=1):
    return {"searchQuery": {"term": "q"}, "organicResults": [
        {"position": pos, "title": "B", "url": url, "description": "d"}
    ]}


def test_search_both_engines_success_tags_engine_and_intra_motor_position():
    client = _MultiEngineFakeClient({
        GOOGLE_SEARCH_SCRAPER: {
            "run_id": "run-g", "dataset": [_google_page()],
            "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-g",
                          "usageTotalUsd": 0.10, "stats": {"computeUnits": 0.01}},
        },
        BING_SEARCH_SCRAPER: {
            "run_id": "run-b", "dataset": [_bing_page()],
            "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-b",
                          "usageTotalUsd": 0.05, "stats": {"computeUnits": 0.02}},
        },
    })
    r = ApifyRunner(client=client)

    out = r.search(["q"], country_code="us", engines=["google", "bing"])

    engines_in_results = {row["engine"] for row in out["results"]}
    assert engines_in_results == {"google", "bing"}
    assert all(row["position"] == 1 for row in out["results"])  # intra-engine
    assert out["cost_usd"] == pytest.approx(0.15)
    assert out["compute_units"] == pytest.approx(0.03)
    assert out["warnings"] == []
    assert len(out["engine_runs"]) == 2
    assert {er["engine"] for er in out["engine_runs"]} == {"google", "bing"}
    assert all(er["error"] is None for er in out["engine_runs"])
    # features ONLY from google
    assert out["run_id"] == "run-g"  # primary: google, first in the list


def test_search_bing_wait_failure_retries_same_run_without_relaunching(monkeypatch):
    """Review TESTS #13: the two-phase retry (1.4) applies PER ENGINE --
    a WAIT failure for bing (not the launch) retries on the SAME bing
    run_id, without relaunching its actor (same approach as the
    single-engine case already covered in test_apify_costs.py::
    test_search_retries_wait_on_same_run_without_relaunching, now also
    verified within a multi-engine search -- the google engine, which
    doesn't even fail, must not be affected)."""
    sleeps = []
    monkeypatch.setattr(runner_module.time, "sleep", lambda s: sleeps.append(s))
    client = _MultiEngineFakeClient({
        GOOGLE_SEARCH_SCRAPER: {
            "run_id": "run-g", "dataset": [_google_page()],
            "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-g"},
        },
        BING_SEARCH_SCRAPER: {
            "run_id": "run-b", "dataset": [_bing_page()],
            "wait_sequence": [
                ApifyError("HTTP 500 from Apify: boom"),
                ApifyError("Run run-b did not finish in 300s (last status: RUNNING)"),
                {"status": "SUCCEEDED", "defaultDatasetId": "ds-b"},
            ],
        },
    })
    r = ApifyRunner(client=client)

    out = r.search(["q"], country_code="us", engines=["google", "bing"])

    bing_run = next(er for er in out["engine_runs"] if er["engine"] == "bing")
    assert bing_run["error"] is None
    assert bing_run["retries"] == 2
    assert out["warnings"] == []
    assert sleeps == [30, 60]  # 30*2^0, 30*2^1 -- backoff without actually sleeping
    # A SINGLE start_actor per engine despite the wait retry -- zero double
    # spend (same approach as 1.4 single-engine).
    assert client.start_calls.count(BING_SEARCH_SCRAPER) == 1
    assert client.wait_calls.count("run-b") == 3
    assert client.start_calls.count(GOOGLE_SEARCH_SCRAPER) == 1
    assert client.wait_calls.count("run-g") == 1


def test_search_bing_fails_google_survives_with_warning():
    client = _MultiEngineFakeClient({
        GOOGLE_SEARCH_SCRAPER: {
            "run_id": "run-g", "dataset": [_google_page()],
            "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-g"},
        },
        BING_SEARCH_SCRAPER: {
            "run_id": "run-b",
            "start_error": ApifyError("HTTP 400 from Apify: invalid input"),
            "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-b"},
        },
    })
    r = ApifyRunner(client=client)

    out = r.search(["q"], country_code="us", engines=["google", "bing"])

    assert {row["engine"] for row in out["results"]} == {"google"}
    assert len(out["warnings"]) == 1
    assert "bing" in out["warnings"][0]
    failed = next(er for er in out["engine_runs"] if er["engine"] == "bing")
    assert failed["error"] is not None
    assert failed["run_id"] is None
    succeeded = next(er for er in out["engine_runs"] if er["engine"] == "google")
    assert succeeded["error"] is None


def test_search_unknown_engine_filtered_with_warning():
    client = _MultiEngineFakeClient({
        GOOGLE_SEARCH_SCRAPER: {
            "run_id": "run-g", "dataset": [_google_page()],
            "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-g"},
        },
    })
    r = ApifyRunner(client=client)

    out = r.search(["q"], country_code="us", engines=["google", "yahoo"])

    assert {row["engine"] for row in out["results"]} == {"google"}
    assert any("yahoo" in w and "unknown engine" in w for w in out["warnings"])


def test_search_unconfigured_engine_only_raises_readable_error():
    r = ApifyRunner(client=_MultiEngineFakeClient({}))

    with pytest.raises(SerpProviderError) as exc_info:
        r.search(["q"], country_code="us", engines=["news"])

    # PONYTAIL (#9): imports the shared constant instead of repeating the
    # literal -- single source of truth for the exact text (see apify.actors).
    assert ENGINE_NOT_CONFIGURED_MSG in str(exc_info.value)


def test_search_all_engines_fail_raises_apify_error():
    client = _MultiEngineFakeClient({
        GOOGLE_SEARCH_SCRAPER: {
            "run_id": "run-g",
            "start_error": ApifyError("HTTP 401 from Apify: unauthorized"),
            "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-g"},
        },
    })
    r = ApifyRunner(client=client)

    with pytest.raises(ApifyError):
        r.search(["q"], country_code="us", engines=["google"])


def test_search_single_engine_failure_reraises_original_exception():
    """Review LOW #5: with only ONE engine requested, the failure is
    re-raised AS-IS (same object/message the client raised) -- without
    wrapping it in the combined message "All requested engines
    failed...", which before this fix cluttered the message seen by the
    user/logs for the (majority) single-engine case. HTTP 401 is not
    retryable -- there's no real backoff/time.sleep to stub."""
    original = ApifyError("HTTP 401 from Apify: unauthorized")
    client = _MultiEngineFakeClient({
        GOOGLE_SEARCH_SCRAPER: {
            "run_id": "run-g", "start_error": original,
            "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-g"},
        },
    })
    r = ApifyRunner(client=client)

    with pytest.raises(ApifyError) as exc_info:
        r.search(["q"], country_code="us", engines=["google"])

    assert exc_info.value is original
    assert str(exc_info.value) == "HTTP 401 from Apify: unauthorized"


def test_search_multi_engine_all_fail_chains_last_exception_as_cause():
    """Review LOW #5: with SEVERAL engines requested, the combined
    ApifyError chains the last real ApifyError as __cause__ (`raise
    ... from last_exc`) -- the original traceback isn't lost."""
    google_exc = ApifyError("HTTP 401 from Apify: unauthorized")
    bing_exc = ApifyError("HTTP 500 from Apify: boom")
    client = _MultiEngineFakeClient({
        GOOGLE_SEARCH_SCRAPER: {
            "run_id": "run-g", "start_error": google_exc,
            "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-g"},
        },
        BING_SEARCH_SCRAPER: {
            "run_id": "run-b", "start_error": bing_exc,
            "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-b"},
        },
    })
    r = ApifyRunner(client=client)

    with pytest.raises(SerpProviderError) as exc_info:
        r.search(["q"], country_code="us", engines=["google", "bing"])

    # last_exc is the one from the LAST engine processed in order (bing, the 2nd).
    assert exc_info.value.__cause__ is bing_exc


def test_search_default_engines_is_google_only_backward_compat():
    """Without `engines`, the behavior is IDENTICAL to before 1.1:
    a single engine (google), and rows still have a recognizable shape
    (with "engine" added, but without breaking anything that already
    reads position/url/title/etc.)."""
    client = _MultiEngineFakeClient({
        GOOGLE_SEARCH_SCRAPER: {
            "run_id": "run-g", "dataset": [_google_page()],
            "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-g",
                          "usageTotalUsd": 0.2, "stats": {"computeUnits": 0.1}},
        },
    })
    r = ApifyRunner(client=client)

    out = r.search(["q"], country_code="us")

    assert out["status"] == "SUCCEEDED"
    assert out["cost_usd"] == 0.2
    assert out["compute_units"] == 0.1
    assert len(out["results"]) == 1
    assert out["results"][0]["engine"] == "google"
    assert client.start_calls == [GOOGLE_SEARCH_SCRAPER]


def test_search_features_only_from_google_not_bing():
    client = _MultiEngineFakeClient({
        GOOGLE_SEARCH_SCRAPER: {
            "run_id": "run-g",
            "dataset": [{
                "searchQuery": {"term": "q"}, "organicResults": [],
                "peopleAlsoAsk": [{"question": "Q1?", "answer": "A1"}],
            }],
            "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-g"},
        },
        BING_SEARCH_SCRAPER: {
            "run_id": "run-b", "dataset": [_bing_page()],
            "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-b"},
        },
    })
    r = ApifyRunner(client=client)

    out = r.search(["q"], country_code="us", engines=["google", "bing"])

    assert len(out["features"]) == 1
    assert out["features"][0]["feature_type"] == "paa"
    bing_run = next(er for er in out["engine_runs"] if er["engine"] == "bing")
    assert bing_run["features"] == []


# --------------------------------------------------------------------- #
# 5) "One DB run per engine" -- persist_engine_run / run_multi_country
# --------------------------------------------------------------------- #
def test_persist_engine_run_saves_its_own_run_row_with_engine_and_cost(tmp_path):
    db = _db(tmp_path)
    engine_run = {
        "engine": "bing", "run_id": "run-bing-1", "status": "SUCCEEDED",
        "country_code": "us", "queries": ["q"],
        "results": [{"query": "q", "url": "https://bing.com/x", "title": "T",
                     "description": "d", "displayed_url": "bing.com", "position": 1,
                     "country_code": "us", "domain": "bing.com", "engine": "bing"}],
        "features": [], "cost_usd": 0.33, "compute_units": 0.02, "retries": 1,
        "error": None,
    }

    out = persist_engine_run(db, engine_run, "q")

    row = db.get_run("run-bing-1")
    assert row["engine"] == "bing"
    assert row["cost_usd"] == 0.33
    saved = db.get_results_filtered(run_id="run-bing-1")
    assert len(saved) == 1
    assert saved[0]["engine"] == "bing"
    assert out["summary"]["total"] == 1


def test_run_multi_country_saves_one_db_run_per_engine(tmp_path, monkeypatch):
    db = _db(tmp_path)
    client = _MultiEngineFakeClient({
        GOOGLE_SEARCH_SCRAPER: {
            "run_id": "run-g-us", "dataset": [_google_page(url="https://g.example/us")],
            "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-g",
                          "usageTotalUsd": 0.1},
        },
        BING_SEARCH_SCRAPER: {
            "run_id": "run-b-us", "dataset": [_bing_page(url="https://b.example/us")],
            "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-b",
                          "usageTotalUsd": 0.2},
        },
    })
    runner = ApifyRunner(client=client)

    outcomes = run_multi_country(runner, db, "q", ["us"], engines=["google", "bing"])

    assert outcomes[0]["error"] is None
    google_row = db.get_run("run-g-us")
    bing_row = db.get_run("run-b-us")
    assert google_row["engine"] == "google"
    assert bing_row["engine"] == "bing"
    assert google_row["cost_usd"] == 0.1
    assert bing_row["cost_usd"] == 0.2
    # Country summary = sum of both engines (1 result each).
    assert outcomes[0]["summary"]["total"] == 2


def test_run_multi_country_pours_outcome_warnings_into_on_event(tmp_path):
    """Review LOW #11: outcome["warnings"] gains a production consumer --
    it's poured into on_event (which dashboard.main wires to
    jobs.add_progress) instead of remaining a field with no real reader."""
    db = _db(tmp_path)
    client = _MultiEngineFakeClient({
        GOOGLE_SEARCH_SCRAPER: {
            "run_id": "run-g-us2", "dataset": [_google_page()],
            "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-g"},
        },
    })
    runner = ApifyRunner(client=client)
    events: list[str] = []

    run_multi_country(runner, db, "q", ["us"], engines=["google", "news"],
                      on_event=events.append)

    assert any("news" in e and ENGINE_NOT_CONFIGURED_MSG in e for e in events)


# --------------------------------------------------------------------- #
# 6) database.models: engine in search_results/runs, filters and HIST
# --------------------------------------------------------------------- #
def test_search_results_and_runs_tables_have_engine_column(tmp_path):
    db = _db(tmp_path)
    conn = db._connect()
    result_cols = {row[1] for row in conn.execute("PRAGMA table_info(search_results)").fetchall()}
    run_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    conn.close()
    assert "engine" in result_cols
    assert "engine" in run_cols


def test_save_results_defaults_engine_to_google_for_legacy_callers(tmp_path):
    db = _db(tmp_path)
    db.save_run("run-1", ["q"], "us", "completed", SUMMARY)
    db.save_results(
        [{"query": "q", "url": "https://x.example", "title": "T", "description": "d",
          "displayed_url": "x.example", "position": 1, "country_code": "us",
          "domain": "x.example", "classification": "neutral"}],
        run_id="run-1",
    )
    rows = db.get_results_filtered(run_id="run-1")
    assert rows[0]["engine"] == "google"


def test_save_results_persists_explicit_engine(tmp_path):
    db = _db(tmp_path)
    db.save_run("run-1", ["q"], "us", "completed", SUMMARY, engine="bing")
    db.save_results(
        [{"query": "q", "url": "https://x.example", "title": "T", "description": "d",
          "displayed_url": "x.example", "position": 1, "country_code": "us",
          "domain": "x.example", "classification": "neutral", "engine": "bing"}],
        run_id="run-1",
    )
    rows = db.get_results_filtered(run_id="run-1")
    assert rows[0]["engine"] == "bing"
    assert db.get_run("run-1")["engine"] == "bing"


def _seed_two_engines(db):
    db.save_run("run-g", ["q"], "us", "completed", SUMMARY, engine="google")
    db.save_results(
        [{"query": "q", "url": "https://g.example/1", "title": "G1", "description": "d",
          "displayed_url": "g.example", "position": 1, "country_code": "us",
          "domain": "g.example", "classification": "neutral", "engine": "google"}],
        run_id="run-g",
    )
    db.save_run("run-b", ["q"], "us", "completed", SUMMARY, engine="bing")
    db.save_results(
        [{"query": "q", "url": "https://b.example/1", "title": "B1", "description": "d",
          "displayed_url": "b.example", "position": 1, "country_code": "us",
          "domain": "b.example", "classification": "neutral", "engine": "bing"}],
        run_id="run-b",
    )


def test_get_results_filtered_by_engine(tmp_path):
    db = _db(tmp_path)
    _seed_two_engines(db)

    google_rows = db.get_results_filtered(engine="google")
    bing_rows = db.get_results_filtered(engine="bing")
    all_rows = db.get_results_filtered()

    assert {r["url"] for r in google_rows} == {"https://g.example/1"}
    assert {r["url"] for r in bing_rows} == {"https://b.example/1"}
    assert len(all_rows) == 2


def test_get_results_filtered_engine_whitelist_ignores_invalid_value(tmp_path):
    db = _db(tmp_path)
    _seed_two_engines(db)

    rows = db.get_results_filtered(engine="'; DROP TABLE search_results; --")

    # Value outside ACTORS.keys() -- gets ignored (no filter), doesn't raise.
    assert len(rows) == 2


def test_get_latest_run_id_defaults_to_google_ignoring_a_later_bing_run(tmp_path):
    """Review MEDIUM #3: without the per-engine filter, get_latest_run_id
    returned the MOST RECENTLY inserted run regardless of engine -- with
    "one DB run per engine", that could return bing's run_id to
    consumers (analysis diff, alerts, health_score, weekly, timeline)
    that assume the production engine (google). bing gets saved AFTER
    (higher id) but the default engine='google' ignores it."""
    db = _db(tmp_path)
    _seed_two_engines(db)  # saves run-g (google) and THEN run-b (bing)

    assert db.get_latest_run_id("us") == "run-g"
    assert db.get_latest_run_id("us", engine="bing") == "run-b"


def test_distinct_engines_returns_sorted_list(tmp_path):
    db = _db(tmp_path)
    _seed_two_engines(db)

    assert db.distinct_engines() == ["bing", "google"]


def test_distinct_engines_single_engine_db(tmp_path):
    db = _db(tmp_path)
    db.save_run("run-1", ["q"], "us", "completed", SUMMARY)
    db.save_results(
        [{"query": "q", "url": "https://x.example", "title": "T", "description": "d",
          "displayed_url": "x.example", "position": 1, "country_code": "us",
          "domain": "x.example", "classification": "neutral"}],
        run_id="run-1",
    )
    assert db.distinct_engines() == ["google"]


def test_get_history_pivot_single_engine_has_no_engine_key(tmp_path):
    """Regression: without an extra engine, the dict's shape is IDENTICAL
    to before 1.1 -- no consumer sees an "engine" key it didn't ask for."""
    db = _db(tmp_path)
    db.save_run("run-1", ["q"], "us", "completed", SUMMARY)
    db.save_results(
        [{"query": "q", "url": "https://x.example", "title": "T", "description": "d",
          "displayed_url": "x.example", "position": 1, "country_code": "us",
          "domain": "x.example", "classification": "positive"}],
        run_id="run-1",
    )

    pivot = db.get_history_pivot()

    assert all("engine" not in row for row in pivot["rows"])


def test_get_history_pivot_multi_engine_distinguishes_engine(tmp_path):
    db = _db(tmp_path)
    _seed_two_engines(db)
    # Both classifications default to 'neutral' in _seed_two_engines;
    # we force 'positive' so HIST (excel) would show them too, but
    # get_history_pivot itself doesn't filter by classification.
    pivot = db.get_history_pivot()

    engines_seen = {row.get("engine") for row in pivot["rows"]}
    assert engines_seen == {"google", "bing"}
    urls_by_engine = {row["engine"]: row["url"] for row in pivot["rows"]}
    assert urls_by_engine["google"] == "https://g.example/1"
    assert urls_by_engine["bing"] == "https://b.example/1"


# --------------------------------------------------------------------- #
# 6b) get_stats/series_stats: default engine='google' (review MEDIUM #4)
# --------------------------------------------------------------------- #
def test_get_stats_defaults_to_google_not_inflated_by_bing(tmp_path):
    db = _db(tmp_path)
    _seed_two_engines(db)  # 1 google row + 1 bing row, 1 run each

    stats = db.get_stats()

    assert stats["total_runs"] == 1
    assert stats["total_results"] == 1
    assert stats["countries"] == 1

    stats_bing = db.get_stats(engine="bing")
    assert stats_bing["total_runs"] == 1
    assert stats_bing["total_results"] == 1


def test_series_stats_defaults_to_google_not_inflated_by_bing(tmp_path):
    db = _db(tmp_path)
    _seed_two_engines(db)

    series = db.series_stats()

    assert sum(row["total"] for row in series) == 1  # only the google row

    series_bing = db.series_stats(engine="bing")
    assert sum(row["total"] for row in series_bing) == 1  # only the bing one


# --------------------------------------------------------------------- #
# 7) UI: /search_page checkboxes, /results filter+column
# --------------------------------------------------------------------- #
def test_search_page_shows_engine_checkboxes(client):
    response = client.get("/ui/search", headers={"X-SM-Fragment": "1"})
    assert response.status_code == 200
    assert "name='engines' value='google'" in response.text
    assert "name='engines' value='bing'" in response.text
    # Verticals without an actor (news, images) are simply not offered:
    # no disabled "coming soon" clutter in the dialog.
    assert "value='news'" not in response.text
    assert "value='images'" not in response.text
    assert "coming soon" not in response.text.lower()


def test_post_search_pours_outcome_warnings_into_job_progress(client, monkeypatch):
    """Review LOW #11: outcome["warnings"] is poured into jobs.add_progress
    -- the user sees the skipped/failed engine in /job (before, an outcome
    field was left with no real consumer)."""
    import re

    from conftest import _wait_for_job

    import dashboard.main as main_module

    class _FakeRunner:
        def search(self, queries, country_code, language_code, max_pages, engines=None, device=None):
            google_run = {
                "engine": "google", "run_id": "run-warn-g", "status": "SUCCEEDED",
                "country_code": country_code, "queries": queries, "results": [],
                "features": [], "cost_usd": None, "compute_units": None, "retries": 0,
                "error": None,
            }
            bing_run = {
                "engine": "bing", "run_id": None, "status": "ERROR",
                "country_code": country_code, "queries": queries, "results": [],
                "features": [], "cost_usd": None, "compute_units": None, "retries": 0,
                "error": f"bing: {ENGINE_NOT_CONFIGURED_MSG}",
            }
            return {
                "run_id": "run-warn-g", "status": "SUCCEEDED", "country_code": country_code,
                "queries": queries, "results": [], "features": [],
                "cost_usd": None, "compute_units": None, "retries": 0,
                "engine_runs": [google_run, bing_run],
                "warnings": [f"bing: {ENGINE_NOT_CONFIGURED_MSG}"],
            }

    monkeypatch.setattr(main_module, "get_runner", lambda: _FakeRunner())

    resp = client.post("/search", data={"query": "Q", "country": "us", "pages": 1})
    assert resp.status_code == 200
    m = re.search(r"/job/([0-9a-f]+)", resp.text)
    assert m, resp.text
    job = _wait_for_job(main_module, m.group(1))

    assert job["status"] == "done"
    assert any(f"bing: {ENGINE_NOT_CONFIGURED_MSG}" in p for p in job["progress"])


def test_results_page_has_no_engine_chip_with_single_engine(client):
    """seeded_db (conftest) only has 'google' results (default) --
    with a single engine in the data, the "Engine" chip must not appear."""
    response = client.get("/results")
    assert response.status_code == 200
    assert "<b>Engine:</b>" not in response.text


def test_results_page_shows_engine_chip_and_column_with_multi_engine(tmp_path, monkeypatch):
    """Same pattern as test_health_score.py::
    test_health_score_page_returns_200_on_completely_empty_database: seed
    a temporary DB BEFORE reloading the app pointing at it."""
    from conftest import _reload_app

    from database.models import Database

    db_path = str(tmp_path / "multi.db")
    seed_db = Database(db_path=db_path)
    _seed_two_engines(seed_db)

    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)
    main_module = _reload_app()

    from fastapi.testclient import TestClient
    tc = TestClient(main_module.app)

    response = tc.get("/results")
    assert response.status_code == 200
    assert "data-filter='engine'" in response.text            # More filters → Engine
    assert "<span class='tag'>Bing</span>" in response.text   # engine tag on rows

    filtered = tc.get("/results", params={"engine": "bing"})
    assert filtered.status_code == 200
    assert "b.example" in filtered.text
    assert "g.example" not in filtered.text


# --------------------------------------------------------------------- #
# 8) Excel: engine dimension in SUM/HIST
# --------------------------------------------------------------------- #
def test_excel_sum_sheet_no_engine_column_with_single_engine(tmp_path):
    from openpyxl import load_workbook

    from export.excel_exporter import build_workbook

    db = _db(tmp_path)
    db.save_run("run-1", ["q"], "us", "completed",
                {"total": 1, "positive": 1, "negative": 0, "neutral": 0})
    db.save_results(
        [{"query": "q", "url": "https://x.example", "title": "T", "description": "d",
          "displayed_url": "x.example", "position": 1, "country_code": "us",
          "domain": "x.example", "classification": "positive"}],
        run_id="run-1",
    )

    out_path = build_workbook(db, str(tmp_path / "out.xlsx"))
    wb = load_workbook(out_path)
    ws = wb["SUM"]
    headers = [c.value for c in ws[7]]

    assert "Engine" not in headers


def test_excel_sum_sheet_has_engine_column_with_multi_engine(tmp_path):
    """latest_run_per_country() groups by country (a single row per country,
    see # ponytail in excel_exporter._build_sum_sheet) -- for 'latest' to
    bring in DIFFERENT engines we need a different engine PER COUNTRY (not
    two engines for the SAME country, which collapse into one in
    latest_run_per_country)."""
    from openpyxl import load_workbook

    from export.excel_exporter import build_workbook

    db = _db(tmp_path)
    db.save_run("run-g-us", ["q"], "us", "completed", SUMMARY, engine="google")
    db.save_results(
        [{"query": "q", "url": "https://g.example/1", "title": "G1", "description": "d",
          "displayed_url": "g.example", "position": 1, "country_code": "us",
          "domain": "g.example", "classification": "neutral", "engine": "google"}],
        run_id="run-g-us",
    )
    db.save_run("run-b-ca", ["q"], "ca", "completed", SUMMARY, engine="bing")
    db.save_results(
        [{"query": "q", "url": "https://b.example/1", "title": "B1", "description": "d",
          "displayed_url": "b.example", "position": 1, "country_code": "ca",
          "domain": "b.example", "classification": "neutral", "engine": "bing"}],
        run_id="run-b-ca",
    )

    out_path = build_workbook(db, str(tmp_path / "out.xlsx"))
    wb = load_workbook(out_path)
    ws = wb["SUM"]
    headers = [c.value for c in ws[7]]

    assert "Engine" in headers


def test_excel_hist_sheet_has_engine_column_with_multi_engine(tmp_path):
    from openpyxl import load_workbook

    from export.excel_exporter import build_workbook

    db = _db(tmp_path)
    db.save_run("run-g", ["q"], "us", "completed", SUMMARY, engine="google")
    db.save_results(
        [{"query": "q", "url": "https://g.example/1", "title": "G1", "description": "d",
          "displayed_url": "g.example", "position": 1, "country_code": "us",
          "domain": "g.example", "classification": "positive", "engine": "google"}],
        run_id="run-g",
    )
    db.save_run("run-b", ["q"], "us", "completed", SUMMARY, engine="bing")
    db.save_results(
        [{"query": "q", "url": "https://b.example/1", "title": "B1", "description": "d",
          "displayed_url": "b.example", "position": 1, "country_code": "us",
          "domain": "b.example", "classification": "positive", "engine": "bing"}],
        run_id="run-b",
    )

    out_path = build_workbook(db, str(tmp_path / "out.xlsx"))
    wb = load_workbook(out_path)
    ws = wb["HIST"]
    headers = [c.value for c in ws[5]]

    assert "Engine" in headers


def test_excel_hist_sheet_no_engine_column_with_single_engine(tmp_path):
    from openpyxl import load_workbook

    from export.excel_exporter import build_workbook

    db = _db(tmp_path)
    db.save_run("run-1", ["q"], "us", "completed",
                {"total": 1, "positive": 1, "negative": 0, "neutral": 0})
    db.save_results(
        [{"query": "q", "url": "https://x.example", "title": "T", "description": "d",
          "displayed_url": "x.example", "position": 1, "country_code": "us",
          "domain": "x.example", "classification": "positive"}],
        run_id="run-1",
    )

    out_path = build_workbook(db, str(tmp_path / "out.xlsx"))
    wb = load_workbook(out_path)
    ws = wb["HIST"]
    headers = [c.value for c in ws[5]]

    assert "Engine" not in headers

# Review TESTS #13: test_search_page_smoke/test_results_page_smoke were
# deleted -- redundant with test_search_page_shows_engine_checkboxes and
# test_results_page_has_no_engine_chip_with_single_engine (section 7),
# which already exercise the same GET + status_code 200.
