"""SERP provider abstraction (issue #3, part 1): the `serp` package.

Covers the provider contract for both adapters (Apify, Serper.dev) with
recorded SYNTHETIC payloads (requests mocked, never the network), the
registry's provider selection, the runner on top of a provider, the
budget counting across providers, the config check, the Settings/wizard
round-trip of the new key, and the compatibility aliases the rest of the
code still imports.
"""
import json
import os
from typing import ClassVar

import pytest
import requests
from conftest import make_db as _db

from serp import registry
from serp.base import (
    DEVICES,
    ENGINE_NOT_CONFIGURED_MSG,
    ENGINES,
    ProviderRun,
    SerpProviderError,
)
from serp.runner import SerpRunner

SUMMARY = {"total": 1, "positive": 0, "negative": 0, "neutral": 1}


@pytest.fixture
def no_keys(monkeypatch):
    for k in ("APIFY_API_KEY", "SERPER_API_KEY", "SERP_PROVIDER", "SERPER_USD_PER_1K"):
        monkeypatch.setenv(k, "")


# --------------------------------------------------------------------- #
# 1) Contract / base
# --------------------------------------------------------------------- #
def test_base_vocabulary_matches_the_legacy_apify_names():
    from apify.actors import ACTORS, VALID_DEVICES

    assert ENGINES == tuple(ACTORS)          # same engine vocabulary, same order
    assert DEVICES == VALID_DEVICES == ("desktop", "mobile")
    assert ENGINE_NOT_CONFIGURED_MSG == "engine not configured yet"


def test_provider_error_carries_retryable_flag():
    assert SerpProviderError("x").retryable is False
    assert SerpProviderError("x", retryable=True).retryable is True
    from apify.client import ApifyError
    assert issubclass(ApifyError, SerpProviderError)     # one `except` catches both


def test_provider_run_defaults():
    run = ProviderRun(items=[], cost_usd=None, run_id="r", provider="p", engine="google",
                      device="desktop", country="us")
    assert run.features == {} and run.feature_rows == [] and run.raw is None
    assert run.compute_units is None and run.retries == 0 and run.status == "SUCCEEDED"


# --------------------------------------------------------------------- #
# 2) Serper adapter (requests mocked)
# --------------------------------------------------------------------- #
_SERPER_PAYLOAD = {
    "searchParameters": {"q": "Jordan Ashworth", "gl": "us", "hl": "en", "type": "search",
                         "num": 10, "page": 1, "engine": "google"},
    "knowledgeGraph": {"title": "Jordan Ashworth", "type": "Fictional person",
                       "description": "A made-up person used by the SERP Monitor demo."},
    "answerBox": {"snippet": "Jordan Ashworth is a fictional name.", "title": "Who is Jordan Ashworth?",
                  "link": "https://jordanashworth.example/about"},
    "organic": [
        {"title": "Jordan Ashworth", "link": "https://www.jordanashworth.example/?utm_source=x",
         "snippet": "Official site.", "position": 1,
         "sitelinks": [{"title": "About", "link": "https://jordanashworth.example/about"}]},
        {"title": "Jordan Ashworth - Review", "link": "https://reviews.example/jordan-ashworth/",
         "snippet": "A review.", "position": 2},
        {"title": "no link at all", "snippet": "dropped", "position": 3},
    ],
    "peopleAlsoAsk": [
        {"question": "Who is Jordan Ashworth?", "snippet": "A fictional person.",
         "title": "Jordan Ashworth", "link": "https://jordanashworth.example/about"},
        {"question": 42},
    ],
    "places": [{"title": "Ashworth Studio", "address": "1 Example St"}],
    "videos": [{"title": "Jordan Ashworth talk", "link": "https://video.example/v/1"}],
    "relatedSearches": [{"query": "jordan ashworth blog"}],
    "credits": 1,
}


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _mock_post(monkeypatch, responses):
    """requests.post stub: consumes `responses` (a _Resp or an exception)
    one per call and records the calls."""
    calls = []
    seq = list(responses)

    def _post(url, headers=None, json=None, timeout=None, **kw):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(requests, "post", _post)
    monkeypatch.setattr("serp.serper_provider.time.sleep", lambda s: None)
    return calls


def test_serper_search_maps_organic_to_the_normalized_schema(monkeypatch):
    from serp.serper_provider import SerperProvider

    calls = _mock_post(monkeypatch, [_Resp(200, _SERPER_PAYLOAD)])
    p = SerperProvider(api_key="serper-test-key", usd_per_1k=0.5)

    run = p.search("Jordan Ashworth", "us", "google", "desktop", language="en")

    assert calls[0]["url"] == "https://google.serper.dev/search"
    assert calls[0]["headers"]["X-API-KEY"] == "serper-test-key"
    assert calls[0]["json"] == {"q": "Jordan Ashworth", "gl": "us", "hl": "en", "num": 10, "page": 1}
    assert run.provider == "serper" and run.engine == "google" and run.device == "desktop"
    assert run.run_id.startswith("serper-") and run.status == "SUCCEEDED"
    assert run.cost_usd == pytest.approx(0.0005)       # 1 credit × $0.50 / 1000
    assert run.compute_units is None
    assert [r["position"] for r in run.items] == [1, 2]      # the row without a link is dropped
    first = run.items[0]
    assert first == {
        "query": "Jordan Ashworth", "country_code": "us", "position": 1,
        "title": "Jordan Ashworth", "url": "https://www.jordanashworth.example/",
        "displayed_url": "www.jordanashworth.example", "description": "Official site.",
        "domain": "jordanashworth.example",
    }
    assert run.items[1]["url"] == "https://reviews.example/jordan-ashworth"


def test_serper_features_are_best_effort_booleans_and_counts(monkeypatch):
    from serp.serper_provider import SerperProvider

    _mock_post(monkeypatch, [_Resp(200, _SERPER_PAYLOAD)])
    run = SerperProvider(api_key="k").search("q", "us", "google", "desktop")

    assert run.features == {"ai_overview": False, "answer_box": True, "knowledge_panel": True,
                            "local_pack": 1, "people_also_ask": 1, "video": 1, "sitelinks": 1}
    kinds = [row["feature_type"] for row in run.feature_rows]
    assert kinds == ["paa", "related", "knowledge_panel"]       # the malformed PAA item is skipped
    assert run.feature_rows[0]["content"]["question"] == "Who is Jordan Ashworth?"
    assert run.raw is _SERPER_PAYLOAD


def test_serper_ai_overview_present_when_the_payload_carries_it(monkeypatch):
    from serp.serper_provider import SerperProvider

    payload = {"organic": [], "aiOverview": {"text": "An overview."}, "credits": 1}
    _mock_post(monkeypatch, [_Resp(200, payload)])
    run = SerperProvider(api_key="k").search("q", "us", "google", "desktop")
    assert run.features["ai_overview"] is True
    assert run.feature_rows[0] == {"feature_type": "aio", "seed_query": "q",
                                   "content": {"text": "An overview.", "sources": []}}


def test_serper_global_country_omits_gl_and_pages_add_up(monkeypatch):
    from serp.serper_provider import SerperProvider

    page1 = {"organic": [{"title": "a", "link": "https://a.example/1", "position": 1}], "credits": 1}
    page2 = {"organic": [{"title": "b", "link": "https://b.example/2", "position": 1}], "credits": 1}
    calls = _mock_post(monkeypatch, [_Resp(200, page1), _Resp(200, page2)])
    run = SerperProvider(api_key="k", usd_per_1k=1.0).search(
        "q", "global", "google", "desktop", pages=2, per_page=10)

    assert "gl" not in calls[0]["json"] and calls[0]["json"]["hl"] == "en"
    assert [c["json"]["page"] for c in calls] == [1, 2]
    assert [r["position"] for r in run.items] == [1, 11]       # numbered across pages
    assert run.country == "global" and run.items[0]["country_code"] == "global"
    assert run.cost_usd == pytest.approx(0.002)


def test_serper_credits_fallback_when_the_response_does_not_say(monkeypatch):
    from serp.serper_provider import SerperProvider, credits_for

    assert credits_for(10) == 1 and credits_for(11) == 2 and credits_for(100) == 2
    _mock_post(monkeypatch, [_Resp(200, {"organic": []})])
    run = SerperProvider(api_key="k", usd_per_1k=1.0).search("q", "us", "google", "desktop",
                                                             per_page=20)
    assert run.cost_usd == pytest.approx(0.002)


def test_serper_retries_transient_failures_then_succeeds(monkeypatch):
    from serp.serper_provider import SerperProvider

    calls = _mock_post(monkeypatch, [
        _Resp(503, None, "upstream"), requests.ConnectionError("boom"),
        _Resp(200, {"organic": [], "credits": 1}),
    ])
    run = SerperProvider(api_key="k").search("q", "us", "google", "desktop")
    assert len(calls) == 3 and run.retries == 2


def test_serper_client_errors_are_not_retried(monkeypatch):
    from serp.serper_provider import SerperProvider

    calls = _mock_post(monkeypatch, [_Resp(403, None, '{"message":"Unauthorized."}')])
    with pytest.raises(SerpProviderError) as exc_info:
        SerperProvider(api_key="bad").search("q", "us", "google", "desktop")
    assert exc_info.value.retryable is False and "HTTP 403" in str(exc_info.value)
    assert len(calls) == 1


def test_serper_gives_up_after_the_retry_budget(monkeypatch):
    from serp.serper_provider import SerperProvider

    calls = _mock_post(monkeypatch, [_Resp(500, None, "x")] * 3)
    with pytest.raises(SerpProviderError) as exc_info:
        SerperProvider(api_key="k").search("q", "us", "google", "desktop")
    assert exc_info.value.retryable is True and len(calls) == 3


def test_serper_declares_google_desktop_only_and_needs_a_key():
    from serp.serper_provider import SerperProvider

    p = SerperProvider(api_key="k")
    assert p.name == "serper" and p.engines == {"google": {"desktop"}}
    assert p.configured() is True
    with pytest.raises(SerpProviderError):
        p.search("q", "us", "bing", "desktop")
    assert SerperProvider(api_key="").configured() is False
    with pytest.raises(SerpProviderError, match="No Serper"):
        SerperProvider(api_key="").search("q", "us", "google", "desktop")


def test_serper_check_credentials(monkeypatch):
    from serp.serper_provider import SerperProvider

    _mock_post(monkeypatch, [_Resp(200, {"organic": [], "credits": 1})])
    assert SerperProvider(api_key="k").check_credentials() == (True, "key verified (1 credit used)")
    _mock_post(monkeypatch, [_Resp(401, None, "nope")])
    ok, detail = SerperProvider(api_key="k").check_credentials()
    assert ok is False and "HTTP 401" in detail
    assert SerperProvider(api_key="").check_credentials()[0] is False


# --------------------------------------------------------------------- #
# 3) Apify adapter: the SAME behavior as the legacy runner, per engine
# --------------------------------------------------------------------- #
class _FakeApifyClient:
    def __init__(self, dataset, final_run=None, fail_start=None):
        self.dataset = dataset
        self.final_run = final_run or {"status": "SUCCEEDED", "defaultDatasetId": "ds",
                                       "usageTotalUsd": 0.10, "stats": {"computeUnits": 0.02}}
        self.inputs = []
        self.fail_start = fail_start

    def start_actor(self, actor_id, input_data):
        self.inputs.append((actor_id, input_data))
        if self.fail_start:
            raise self.fail_start
        return {"id": "run-1"}

    def wait_for_run(self, run_id, max_wait=300, on_status=None):
        return self.final_run

    def get_dataset_items(self, dataset_id):
        return self.dataset

    def check_account(self):
        return {"ok": True, "detail": "Connection successful", "username": "someone"}


_GOOGLE_PAGE = {"searchQuery": {"term": "q"}, "organicResults": [
    {"position": 1, "title": "G", "url": "https://g.example/x", "description": "d"}],
    "peopleAlsoAsk": [{"question": "Why?", "answer": "Because."}]}


def test_apify_provider_search_normalizes_and_reports_cost():
    from config.apify_config import GOOGLE_SEARCH_SCRAPER
    from serp.apify_provider import ApifyProvider

    client = _FakeApifyClient([_GOOGLE_PAGE])
    p = ApifyProvider(client=client)
    assert p.name == "apify" and p.engines == {"google": {"desktop", "mobile"}, "bing": {"desktop"}}

    run = p.search("q", "us", "google", "mobile", language="en")

    assert client.inputs[0][0] == GOOGLE_SEARCH_SCRAPER
    assert client.inputs[0][1]["mobileResults"] is True and client.inputs[0][1]["queries"] == "q"
    assert run.run_id == "run-1" and run.provider == "apify" and run.device == "mobile"
    assert run.cost_usd == 0.10 and run.compute_units == 0.02
    assert run.items[0]["domain"] == "g.example" and run.items[0]["position"] == 1
    assert run.features == {"people_also_ask": 1, "ai_overview": False, "knowledge_panel": False}
    assert run.feature_rows[0]["feature_type"] == "paa"
    assert p.check_credentials() == (True, "key verified (user: someone)")


def test_apify_provider_rejects_unsupported_engine_without_launching():
    from serp.apify_provider import ApifyProvider

    client = _FakeApifyClient([])
    with pytest.raises(SerpProviderError, match=ENGINE_NOT_CONFIGURED_MSG):
        ApifyProvider(client=client).search("q", "us", "news", "desktop")
    assert client.inputs == []


def test_apify_provider_configured_follows_the_key(no_keys, monkeypatch):
    from serp.apify_provider import ApifyProvider

    assert ApifyProvider().configured() is False
    monkeypatch.setenv("APIFY_API_KEY", "apify_api_x")
    assert ApifyProvider().configured() is True


# --------------------------------------------------------------------- #
# 4) Registry: SERP_PROVIDER + auto-detect
# --------------------------------------------------------------------- #
def test_registry_auto_detects_from_the_keys(no_keys, monkeypatch):
    assert registry.active_provider_name() == "" and registry.get_provider() is None
    monkeypatch.setenv("SERPER_API_KEY", "s")
    assert registry.active_provider_name() == "serper"
    assert registry.get_provider().name == "serper"
    monkeypatch.setenv("APIFY_API_KEY", "a")
    assert registry.active_provider_name() == "apify"          # Apify wins the tie
    assert registry.get_provider().name == "apify"


def test_registry_explicit_choice_wins_and_never_falls_back(no_keys, monkeypatch):
    monkeypatch.setenv("APIFY_API_KEY", "a")
    monkeypatch.setenv("SERPER_API_KEY", "s")
    monkeypatch.setenv("SERP_PROVIDER", "serper")
    assert registry.active_provider_name() == "serper"
    monkeypatch.setenv("SERPER_API_KEY", "")
    assert registry.active_provider_name() == ""               # chosen provider has no key
    assert registry.get_provider() is None
    monkeypatch.setenv("SERP_PROVIDER", "nope")
    assert registry.active_provider_name() == "apify"          # unknown value: auto
    assert registry.PROVIDERS.keys() == {"apify", "serper"}


def test_registry_engines_and_devices_follow_the_active_provider(no_keys, monkeypatch):
    assert registry.configured_engines() == frozenset({"google", "bing"})   # demo: Apify's set
    assert registry.valid_devices() == ("desktop", "mobile")
    monkeypatch.setenv("SERPER_API_KEY", "s")
    assert registry.configured_engines() == frozenset({"google"})
    assert registry.valid_devices() == ("desktop",)
    assert registry.provider_label("serper") == "Serper.dev" and registry.provider_label("") == ""


# --------------------------------------------------------------------- #
# 5) Runner over a provider (the legacy outcome shape)
# --------------------------------------------------------------------- #
class _StubProvider:
    name = "stub"
    engines: ClassVar[dict] = {"google": {"desktop"}}

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def configured(self):
        return True

    def check_credentials(self):
        return True, "ok"

    def search(self, query, country, engine, device, **opts):
        self.calls.append((query, country, engine, device, opts))
        if self.fail:
            raise self.fail
        return ProviderRun(items=[{"query": query, "country_code": country, "position": 1,
                                   "title": "t", "url": "https://x.example/", "displayed_url": "",
                                   "description": "", "domain": "x.example"}],
                           cost_usd=0.002, run_id="stub-1", provider="stub", engine=engine,
                           device=device, country=country, features={"video": 1},
                           feature_rows=[{"feature_type": "paa", "seed_query": query,
                                          "content": {}}])


def test_runner_outcome_shape_and_provider_tag():
    p = _StubProvider()
    out = SerpRunner(provider=p).search(["q"], country_code="gb", language_code="en",
                                        engines=["google"], device="mobile")

    assert p.calls[0][:4] == ("q", "gb", "google", "desktop")       # device clamped to support
    assert p.calls[0][4] == {"language": "en", "pages": 1, "per_page": 10, "max_wait": 300,
                             "on_status": None}
    assert out["run_id"] == "stub-1" and out["status"] == "SUCCEEDED"
    assert out["cost_usd"] == 0.002 and out["compute_units"] is None and out["retries"] == 0
    assert out["results"][0]["engine"] == "google" and out["results"][0]["device"] == "desktop"
    assert out["features"] == [{"feature_type": "paa", "seed_query": "q", "content": {}}]
    assert out["warnings"] == ["google: device ignored (no device support, desktop was used)"]
    er = out["engine_runs"][0]
    assert er["provider"] == "stub" and er["serp_features"] == {"video": 1} and er["error"] is None


def test_runner_names_the_provider_in_the_not_configured_message():
    p = _StubProvider()
    out = SerpRunner(provider=p).search(["q"], engines=["google", "bing", "zzz"])
    assert out["warnings"] == [f"bing: {ENGINE_NOT_CONFIGURED_MSG} (stub)", "zzz: unknown engine"]
    assert [er["error"] for er in out["engine_runs"]] == [None, f"bing: {ENGINE_NOT_CONFIGURED_MSG} (stub)",
                                                          "zzz: unknown engine"]
    with pytest.raises(SerpProviderError, match="bing: engine not configured yet \\(stub\\)"):
        SerpRunner(provider=p).search(["q"], engines=["bing"])


def test_runner_without_any_provider_raises_a_readable_error(no_keys):
    runner = SerpRunner()
    assert runner.provider is None and runner.provider_name == ""
    with pytest.raises(SerpProviderError, match="No SERP provider"):
        runner.search(["q"])
    assert runner.check_credentials() == (
        False, "No SERP provider configured. Add an Apify or Serper.dev key in Settings → Search.")


def test_runner_single_engine_failure_reraises_the_provider_error():
    err = SerpProviderError("HTTP 500 from stub", retryable=True)
    with pytest.raises(SerpProviderError) as exc_info:
        SerpRunner(provider=_StubProvider(fail=err)).search(["q"])
    assert exc_info.value is err


def test_runner_budget_preflight_counts_every_provider(tmp_path):
    db = _db(tmp_path)
    db.set_setting("apify_monthly_budget", "1.00")
    db.save_run("r-apify", ["q"], "us", "completed", SUMMARY, cost_usd=0.60)
    db.save_run("r-serper", ["q"], "us", "completed", SUMMARY, cost_usd=0.50, provider="serper")
    p = _StubProvider()
    with pytest.raises(SerpProviderError, match="SERP monthly budget exceeded"):
        SerpRunner(provider=p, db=db).search(["q"])
    assert p.calls == []
    SerpRunner(provider=p, db=db).search(["q"], override_budget=True)
    assert len(p.calls) == 1


# --------------------------------------------------------------------- #
# 6) Budget: provider column, month cost across providers, alert text
# --------------------------------------------------------------------- #
def test_runs_provider_column_defaults_to_apify_and_counts_across_providers(tmp_path):
    db = _db(tmp_path)
    conn = db._connect()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    conn.close()
    assert "provider" in cols

    db.save_run("r1", ["q"], "us", "completed", SUMMARY, cost_usd=0.30)
    db.save_run("r2", ["q"], "us", "completed", SUMMARY, cost_usd=0.002, provider="serper")
    assert db.get_run("r1")["provider"] == "apify" and db.get_run("r2")["provider"] == "serper"
    spend = db.serp_month_cost()
    assert spend["cost_usd"] == pytest.approx(0.302) and spend["runs"] == 2
    assert spend["by_provider"] == {"apify": pytest.approx(0.30), "serper": pytest.approx(0.002)}
    assert db.apify_month_cost()["cost_usd"] == spend["cost_usd"]       # alias kept


def test_budget_alert_keeps_its_kind_but_says_serp(tmp_path):
    db = _db(tmp_path)
    db.set_setting("apify_monthly_budget", "0.10")
    alert = db.save_run("r1", ["q"], "us", "completed", SUMMARY, cost_usd=0.20, provider="serper")
    assert alert["kind"] == "apify_budget"
    assert alert["message"] == "SERP monthly spend $0.20 exceeds budget $0.10"
    assert db._check_apify_budget() is None and db._check_serp_budget() is None   # deduped


def test_persist_engine_run_stores_the_provider(tmp_path):
    from report import multi_country

    db = _db(tmp_path)
    engine_run = {"run_id": "serper-abc", "queries": ["q"], "country_code": "us",
                  "status": "SUCCEEDED", "results": [], "cost_usd": 0.001, "compute_units": None,
                  "retries": 0, "engine": "google", "device": "desktop", "provider": "serper"}
    multi_country.persist_engine_run(db, engine_run)
    assert db.get_run("serper-abc")["provider"] == "serper"


# --------------------------------------------------------------------- #
# 7) Compatibility aliases
# --------------------------------------------------------------------- #
def test_legacy_imports_still_work():
    from apify import runner as legacy
    from apify.actors import ACTORS, CONFIGURED_ENGINES, VALID_DEVICES
    from apify.runner import ApifyRunner

    assert issubclass(ApifyRunner, SerpRunner)
    assert CONFIGURED_ENGINES == frozenset({"google", "bing"}) and VALID_DEVICES == DEVICES
    assert set(ACTORS) == set(ENGINES)
    assert callable(legacy._is_retryable) and legacy.MAX_SEARCH_ATTEMPTS == 3
    # The legacy static helper tests still call on the runner class.
    assert ApifyRunner._engine_input("google", {"a": 1}, "mobile") == {"a": 1, "mobileResults": True}

    client = _FakeApifyClient([_GOOGLE_PAGE])
    runner = ApifyRunner(client=client)
    assert runner.provider.name == "apify" and runner.client is client
    out = runner.search(["q"], country_code="us")
    assert out["engine_runs"][0]["provider"] == "apify" and out["cost_usd"] == 0.10
    assert runner.check_credentials() == (True, "key verified (user: someone)")


# --------------------------------------------------------------------- #
# 8) Config check
# --------------------------------------------------------------------- #
def _env(tmp_path, **over):
    from common import config_check as cc
    env = {k: "" for k in cc.ENV_VARS}
    env.update(SMTP_PORT="465", GEMINI_MODEL="gemini-2.5-flash-lite",
               DB_PATH=str(tmp_path / "data" / "t.db"))
    env.update(over)
    return env


@pytest.mark.parametrize("over,key,level,needle", [
    ({}, "SERP provider", "WARN", "Demo mode"),
    ({"APIFY_API_KEY": "a"}, "SERP provider", "OK", "Apify"),
    ({"SERPER_API_KEY": "s"}, "SERP provider", "OK", "Serper.dev"),
    ({"SERP_PROVIDER": "serper", "APIFY_API_KEY": "a"}, "SERP_PROVIDER", "WARN", "SERPER_API_KEY is not set"),
    ({"SERP_PROVIDER": "apify", "SERPER_API_KEY": "s"}, "SERP_PROVIDER", "WARN", "APIFY_API_KEY is not set"),
    ({"SERP_PROVIDER": "bogus", "APIFY_API_KEY": "a"}, "SERP_PROVIDER", "WARN", "unknown provider"),
    ({"SERPER_API_KEY": "s", "SERPER_USD_PER_1K": "abc"}, "SERPER_USD_PER_1K", "WARN", "not a number"),
])
def test_config_check_serp_cases(tmp_path, over, key, level, needle):
    from common import config_check as cc

    report = cc.check_config(_env(tmp_path, **over), cwd=tmp_path)
    assert report.ok
    hits = [f for f in report.findings if f.key == key and f.level == level]
    assert hits and needle in hits[0].message, [(f.key, f.level, f.message) for f in report.findings]


def test_config_check_two_keys_is_fine(tmp_path):
    from common import config_check as cc

    report = cc.check_config(_env(tmp_path, APIFY_API_KEY="a", SERPER_API_KEY="s"), cwd=tmp_path)
    assert not report.warnings and any(f.key == "SERP provider" and "Apify" in f.message
                                       for f in report.findings)


# --------------------------------------------------------------------- #
# 9) Settings / wizard round-trip of the Serper key and the provider choice
# --------------------------------------------------------------------- #
_ENV = ("APIFY_API_KEY", "SERPER_API_KEY", "SERP_PROVIDER", "SERPER_USD_PER_1K")
_FRAG = {"HX-Request": "true"}


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV_FILE", str(tmp_path / ".env"))
    for name in _ENV:
        monkeypatch.setenv(name, "")
    (tmp_path / ".env").write_text("")
    return tmp_path / ".env"


def test_settings_search_section_lists_both_providers_and_the_selector(client, env_file):
    text = client.get("/settings").text
    assert "<h2>Search (Apify)" in text and "<h2>Search (Serper.dev)" in text
    assert "/ui/integration/serper?next=/settings#search" in text
    assert "name='provider'" in text and "Monthly SERP budget" in text
    assert "<option value='' selected>Auto" in text


def test_serper_dialog_saves_the_key_encrypted_like_apify(client, env_file, monkeypatch):
    import dashboard.main as main_module
    import dashboard.routers.settings as settings_module

    html = client.get("/ui/integration/serper", headers=_FRAG).text
    assert "name='serper_key'" in html and "serper.dev" in html
    monkeypatch.setattr(settings_module, "_verify_serper", lambda key: (True, "key verified"))

    r = client.post("/settings/serper-key", data={"serper_key": "serper-secret-123"},
                    follow_redirects=True)
    assert r.status_code == 200 and "Serper.dev key saved" in r.text
    assert os.getenv("SERPER_API_KEY") == "serper-secret-123"
    assert "SERPER_API_KEY=serper-secret-123" in env_file.read_text()
    assert main_module._runner is None
    text = client.get("/settings").text
    assert "serper-secret-123" not in text and "Serper.dev" in text
    # An empty submit keeps the saved key; the same key is "no changes".
    r = client.post("/settings/serper-key", data={"serper_key": ""}, follow_redirects=True)
    assert "kept" in r.text and os.getenv("SERPER_API_KEY") == "serper-secret-123"


def test_settings_provider_choice_round_trip(client, env_file):
    import dashboard.main as main_module

    main_module._runner = object()
    r = client.post("/settings/serp-provider", data={"provider": "serper"}, follow_redirects=True)
    assert r.status_code == 200 and os.getenv("SERP_PROVIDER") == "serper"
    assert main_module._runner is None
    assert "<option value='serper' selected>" in client.get("/settings").text
    r = client.post("/settings/serp-provider", data={"provider": "bogus"}, follow_redirects=True)
    assert "Unknown provider" in r.text and os.getenv("SERP_PROVIDER") == "serper"
    client.post("/settings/serp-provider", data={"provider": ""}, follow_redirects=True)
    assert os.getenv("SERP_PROVIDER") == ""


def test_settings_test_button_checks_the_active_provider(client, env_file, monkeypatch):
    import dashboard.routers.settings as settings_module

    class _Runner:
        provider_name = "serper"

        def check_credentials(self):
            return True, "key verified (1 credit used)"

    monkeypatch.setattr(settings_module, "_get_runner", lambda: _Runner())
    r = client.post("/settings/apify-test", follow_redirects=True)
    assert "Serper.dev check" in r.text and "key verified" in r.text
    r = client.post("/settings/serp-test", data={"provider": "serper"}, follow_redirects=True)
    assert "Serper.dev check" in r.text


def test_demo_mode_ends_with_a_serper_key(client, env_file, monkeypatch):
    from dashboard.session import is_demo_mode

    assert is_demo_mode() is True
    monkeypatch.setenv("SERPER_API_KEY", "s")
    assert is_demo_mode() is False
    text = client.get("/ui/search", headers=_FRAG).text
    assert "Needs an Apify key" not in text
    # Serper: Google only, so the engine field collapses to the hidden input.
    assert "name='engines' value='bing'" not in text


def test_wizard_accepts_a_serper_key_as_step_one(client, env_file, monkeypatch):
    import dashboard.routers.settings as settings_module
    import dashboard.routers.wizard as wizard_module

    assert wizard_module._step_configured("apify") is False
    monkeypatch.setattr(settings_module, "_verify_serper", lambda key: (True, "key verified"))
    text = client.get("/setup?step=apify").text
    assert "/ui/integration/apify" in text and "/ui/integration/serper" in text
    client.post("/settings/serper-key", data={"serper_key": "s-1", "next": "/setup"})
    assert wizard_module._step_configured("apify") is True
