"""Tests for item 1.3: mobile/desktop split (RENAMED from
test_serp_snapshots.py when the SERP HTML snapshot feature was removed --
only the device split and the generic evidence/cost coverage remain).

DOCUMENTED DEVIATION (inherited from the brief, not up for re-discussion):
"both devices" is covered with TWO runs (one per device, same approach as
"one run per country" in run_multi_country) -- runner.search()/save_run/
save_results treat `device` as a SINGLE value per call (like
country_code), not as a list (unlike `engines`, which does accept
several engines in a single call).

The screenshot actor (apify.actors.SCREENSHOT_ACTOR), the Apify
key-value store methods, and the HTML snapshot that replaced them
(processing.screenshot) are all RETIRED -- the regressions below make sure
they don't come back by mistake.

TDD, no network: ApifyClient is always replaced with a controllable stub.
"""
import hashlib

import pytest
from conftest import make_db as _db

from apify.actors import VALID_DEVICES
from apify.client import ApifyError
from config.apify_config import BING_SEARCH_SCRAPER, GOOGLE_SEARCH_SCRAPER
from report.multi_country import persist_engine_run, run_multi_country

SUMMARY = {"total": 1, "positive": 0, "negative": 0, "neutral": 1}
HTML_BYTES = b"<!doctype html><html><body>John Doe SERP</body></html>"


# --------------------------------------------------------------------- #
# 1) Constants: VALID_DEVICES / retirement of the screenshot actor, the
#    key-value store that served it, and the SERP HTML snapshot
# --------------------------------------------------------------------- #
def test_valid_devices_constant():
    assert VALID_DEVICES == ("desktop", "mobile")


def test_screenshot_actor_removed_from_apify_actors():
    """F4 wrap-up: SCREENSHOT_ACTOR ("apify~screenshot-url") is retired --
    two real paid runs never produced a PNG (see the module docstring).
    Regression: no one should reintroduce it by mistake."""
    import apify.actors as actors_module

    assert not hasattr(actors_module, "SCREENSHOT_ACTOR")


def test_apify_client_no_longer_exposes_key_value_store_methods():
    """F4 wrap-up: get_key_value_record/list_key_value_store_keys only
    existed to read the retired screenshot actor's OUTPUT."""
    from apify.client import ApifyClient

    assert not hasattr(ApifyClient, "get_key_value_record")
    assert not hasattr(ApifyClient, "list_key_value_store_keys")


def test_serp_snapshot_feature_is_gone():
    """The SERP HTML snapshot (processing.screenshot, the serp_snapshots
    setting and the /settings/serp-snapshots toggle) was removed."""
    import importlib.util

    from report import multi_country

    assert importlib.util.find_spec("processing.screenshot") is None
    assert not hasattr(multi_country, "SERP_SNAPSHOTS_SETTING")


# --------------------------------------------------------------------- #
# 2) ApifyRunner._engine_input: mobileResults ONLY for google/mobile
# --------------------------------------------------------------------- #
def test_engine_input_adds_mobile_results_only_for_google_mobile():
    from apify.runner import ApifyRunner

    base = {"queries": "q", "countryCode": "us"}

    google_desktop = ApifyRunner._engine_input("google", base, "desktop")
    google_mobile = ApifyRunner._engine_input("google", base, "mobile")
    bing_mobile = ApifyRunner._engine_input("bing", base, "mobile")

    assert "mobileResults" not in google_desktop
    assert google_mobile["mobileResults"] is True
    assert "mobileResults" not in bing_mobile
    # Doesn't mutate the base input shared between engines.
    assert base == {"queries": "q", "countryCode": "us"}


# --------------------------------------------------------------------- #
# 3) ApifyRunner.search(device=...): end-to-end orchestration
# --------------------------------------------------------------------- #
class _DeviceFakeClient:
    """Minimal ApifyClient fake for ApifyRunner.search tests with
    `device` -- same spirit as test_multi_engine._MultiEngineFakeClient
    but simplified to ONE actor per test (no need to distinguish several)."""

    def __init__(self, by_actor: dict[str, dict]):
        self.by_actor = {aid: dict(cfg) for aid, cfg in by_actor.items()}
        self.start_inputs: dict[str, dict] = {}

    def start_actor(self, actor_id, input_data):
        self.start_inputs[actor_id] = input_data
        return {"id": self.by_actor[actor_id]["run_id"]}

    def wait_for_run(self, run_id, max_wait=300, on_status=None):
        for cfg in self.by_actor.values():
            if cfg["run_id"] == run_id:
                return cfg["final_run"]
        raise ApifyError(f"unknown run: {run_id}")

    def get_dataset_items(self, dataset_id):
        for cfg in self.by_actor.values():
            if cfg["final_run"].get("defaultDatasetId") == dataset_id:
                return cfg["dataset"]
        return []


def _google_page(url="https://g.example/x"):
    return {"searchQuery": {"term": "q"}, "organicResults": [
        {"position": 1, "title": "G", "url": url, "description": "d"}
    ]}


def test_search_default_device_is_desktop_and_tags_rows():
    from apify.runner import ApifyRunner

    client = _DeviceFakeClient({
        GOOGLE_SEARCH_SCRAPER: {"run_id": "run-g", "dataset": [_google_page()],
                                "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-g"}},
    })
    r = ApifyRunner(client=client)

    out = r.search(["q"], country_code="us")

    assert out["results"][0]["device"] == "desktop"
    assert "mobileResults" not in client.start_inputs[GOOGLE_SEARCH_SCRAPER]
    assert out["engine_runs"][0]["device"] == "desktop"


def test_search_mobile_device_tags_rows_and_adds_mobile_results_key():
    from apify.runner import ApifyRunner

    client = _DeviceFakeClient({
        GOOGLE_SEARCH_SCRAPER: {"run_id": "run-g", "dataset": [_google_page()],
                                "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-g"}},
    })
    r = ApifyRunner(client=client)

    out = r.search(["q"], country_code="us", device="mobile")

    assert out["results"][0]["device"] == "mobile"
    assert client.start_inputs[GOOGLE_SEARCH_SCRAPER]["mobileResults"] is True


def test_search_invalid_device_clamps_to_desktop():
    from apify.runner import ApifyRunner

    client = _DeviceFakeClient({
        GOOGLE_SEARCH_SCRAPER: {"run_id": "run-g", "dataset": [_google_page()],
                                "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-g"}},
    })
    r = ApifyRunner(client=client)

    out = r.search(["q"], country_code="us", device="tablet")

    assert out["results"][0]["device"] == "desktop"


def test_search_bing_tagged_desktop_even_when_mobile_requested():
    """H5 (review): bing has no known device key -- it ALWAYS renders
    desktop, regardless of `device`. Tagging its rows as "mobile" when it
    was actually captured on desktop would be FALSE evidence -- they're
    tagged with the REAL value and a warning is raised instead."""
    from apify.runner import ApifyRunner

    client = _DeviceFakeClient({
        BING_SEARCH_SCRAPER: {"run_id": "run-b", "dataset": [{"organicResults": [
            {"url": "https://b.example/x", "title": "B"}
        ]}], "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-b"}},
    })
    r = ApifyRunner(client=client)

    out = r.search(["q"], country_code="us", engines=["bing"], device="mobile")

    assert out["results"][0]["device"] == "desktop"
    assert out["engine_runs"][0]["device"] == "desktop"
    assert "mobileResults" not in client.start_inputs[BING_SEARCH_SCRAPER]
    assert any("device ignored" in w and "bing" in w for w in out["warnings"])


def test_search_bing_desktop_requested_no_warning():
    """Without requesting mobile, there's nothing to ignore -- the
    "device ignorado" warning must not appear (only applies when the
    request and the reality diverge)."""
    from apify.runner import ApifyRunner

    client = _DeviceFakeClient({
        BING_SEARCH_SCRAPER: {"run_id": "run-b", "dataset": [{"organicResults": [
            {"url": "https://b.example/x", "title": "B"}
        ]}], "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-b"}},
    })
    r = ApifyRunner(client=client)

    out = r.search(["q"], country_code="us", engines=["bing"], device="desktop")

    assert out["warnings"] == []


def test_failed_engine_run_helper_carries_device_explicitly():
    from apify.runner import ApifyRunner

    r = ApifyRunner(client=_DeviceFakeClient({}))
    entry = r._failed_engine_run("news", "us", ["q"], "news: engine not configured yet", "mobile")
    assert entry["device"] == "mobile"
    # The retired SERP snapshot URL is no longer part of the shape.
    assert "html_snapshot_url" not in entry


# --------------------------------------------------------------------- #
# 4) database.models: device column, save_run/save_results, filters
# --------------------------------------------------------------------- #
def test_search_results_and_runs_tables_have_device_column(tmp_path):
    db = _db(tmp_path)
    conn = db._connect()
    result_cols = {row[1] for row in conn.execute("PRAGMA table_info(search_results)").fetchall()}
    run_cols = {row[1] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
    conn.close()
    assert "device" in result_cols
    assert "device" in run_cols


def test_save_run_persists_device_and_defaults_to_desktop(tmp_path):
    db = _db(tmp_path)
    db.save_run("run-1", ["q"], "us", "completed", SUMMARY, device="mobile")
    db.save_run("run-2", ["q"], "us", "completed", SUMMARY)

    assert db.get_run("run-1")["device"] == "mobile"
    assert db.get_run("run-2")["device"] == "desktop"


def test_save_results_defaults_device_to_desktop_for_legacy_callers(tmp_path):
    db = _db(tmp_path)
    db.save_run("run-1", ["q"], "us", "completed", SUMMARY)
    db.save_results(
        [{"query": "q", "url": "https://x.example", "title": "T", "description": "d",
          "country_code": "us", "domain": "x.example", "classification": "neutral"}],
        run_id="run-1",
    )
    rows = db.get_results_filtered(run_id="run-1")
    assert rows[0]["device"] == "desktop"


def test_save_results_persists_explicit_device(tmp_path):
    db = _db(tmp_path)
    db.save_run("run-1", ["q"], "us", "completed", SUMMARY, device="mobile")
    db.save_results(
        [{"query": "q", "url": "https://x.example", "title": "T", "description": "d",
          "country_code": "us", "domain": "x.example", "classification": "neutral",
          "device": "mobile"}],
        run_id="run-1",
    )
    rows = db.get_results_filtered(run_id="run-1")
    assert rows[0]["device"] == "mobile"


def _seed_two_devices(db):
    db.save_run("run-desktop", ["q"], "us", "completed", SUMMARY, device="desktop")
    db.save_results(
        [{"query": "q", "url": "https://d.example/1", "title": "D1", "description": "d",
          "country_code": "us", "domain": "d.example", "classification": "neutral",
          "device": "desktop"}],
        run_id="run-desktop",
    )
    db.save_run("run-mobile", ["q"], "us", "completed", SUMMARY, device="mobile")
    db.save_results(
        [{"query": "q", "url": "https://m.example/1", "title": "M1", "description": "d",
          "country_code": "us", "domain": "m.example", "classification": "neutral",
          "device": "mobile"}],
        run_id="run-mobile",
    )


def test_get_results_filtered_by_device(tmp_path):
    db = _db(tmp_path)
    _seed_two_devices(db)

    desktop_rows = db.get_results_filtered(device="desktop")
    mobile_rows = db.get_results_filtered(device="mobile")
    all_rows = db.get_results_filtered()

    assert {r["url"] for r in desktop_rows} == {"https://d.example/1"}
    assert {r["url"] for r in mobile_rows} == {"https://m.example/1"}
    assert len(all_rows) == 2


def test_get_results_filtered_device_whitelist_ignores_invalid_value(tmp_path):
    db = _db(tmp_path)
    _seed_two_devices(db)

    rows = db.get_results_filtered(device="'; DROP TABLE search_results; --")

    assert len(rows) == 2


def test_distinct_devices_returns_sorted_list(tmp_path):
    db = _db(tmp_path)
    _seed_two_devices(db)
    assert db.distinct_devices() == ["desktop", "mobile"]


def test_distinct_devices_single_device_db(tmp_path):
    db = _db(tmp_path)
    db.save_run("run-1", ["q"], "us", "completed", SUMMARY)
    db.save_results(
        [{"query": "q", "url": "https://x.example", "title": "T", "description": "d",
          "country_code": "us", "domain": "x.example", "classification": "neutral"}],
        run_id="run-1",
    )
    assert db.distinct_devices() == ["desktop"]


def test_latest_run_ids_groups_by_engine_and_device(tmp_path):
    """Without GROUP BY device, the desktop run would disappear from
    /results as soon as the mobile one launched (both same country/engine)
    -- see Database.latest_run_ids's docstring."""
    db = _db(tmp_path)
    _seed_two_devices(db)

    run_ids = set(db.latest_run_ids("us"))

    assert run_ids == {"run-desktop", "run-mobile"}


def test_latest_run_ids_single_device_unchanged_behavior(tmp_path):
    db = _db(tmp_path)
    db.save_run("run-1", ["q"], "us", "completed", SUMMARY)
    db.save_results(
        [{"query": "q", "url": "https://x.example", "title": "T", "description": "d",
          "country_code": "us", "domain": "x.example", "classification": "neutral"}],
        run_id="run-1",
    )
    assert db.latest_run_ids("us") == ["run-1"]


# --------------------------------------------------------------------- #
# 5) "One DB run per engine" now also carries device -- persist_engine_run
# --------------------------------------------------------------------- #
def test_persist_engine_run_saves_device(tmp_path):
    db = _db(tmp_path)
    engine_run = {
        "engine": "google", "device": "mobile", "run_id": "run-m-1", "status": "SUCCEEDED",
        "country_code": "us", "queries": ["q"],
        "results": [{"query": "q", "url": "https://g.example/x", "title": "T",
                     "description": "d", "position": 1, "country_code": "us",
                     "domain": "g.example", "engine": "google", "device": "mobile"}],
        "features": [], "cost_usd": None,
        "compute_units": None, "retries": 0, "error": None,
    }

    persist_engine_run(db, engine_run)

    assert db.get_run("run-m-1")["device"] == "mobile"
    assert db.get_results_filtered(run_id="run-m-1")[0]["device"] == "mobile"


def test_run_multi_country_passes_device_through(tmp_path):
    from apify.runner import ApifyRunner

    db = _db(tmp_path)
    client = _DeviceFakeClient({
        GOOGLE_SEARCH_SCRAPER: {"run_id": "run-g-mobile", "dataset": [_google_page()],
                                "final_run": {"status": "SUCCEEDED", "defaultDatasetId": "ds-g"}},
    })
    runner = ApifyRunner(client=client)

    run_multi_country(runner, db, "q", ["us"], device="mobile")

    assert db.get_run("run-g-mobile")["device"] == "mobile"
    assert client.start_inputs[GOOGLE_SEARCH_SCRAPER]["mobileResults"] is True


# --------------------------------------------------------------------- #
# 6) UI: /search_page radios, /results filter+column
# --------------------------------------------------------------------- #
def test_search_page_shows_device_radios_desktop_checked_by_default(client):
    response = client.get("/ui/search", headers={"X-SM-Fragment": "1"})
    assert response.status_code == 200
    # Device lives under "More options"; desktop is the first (default) option.
    assert "<select id='sq-device' name='device'><option value='desktop'>Desktop</option>" in response.text
    assert "<option value='mobile'>Mobile</option>" in response.text


def test_results_page_has_no_device_chip_with_single_device(client):
    response = client.get("/results")
    assert response.status_code == 200
    # The chip is identified by its stable hook, NOT by the "Device:" label:
    # the "New search" dialog UX-2 embeds in /results labels its device
    # radios with that same phrase, so searching for it here would give a
    # false positive for the chip (and, in the sibling test, a false green).
    assert "data-filter='device'" not in response.text


def test_results_page_shows_device_chip_and_column_with_multi_device(tmp_path, monkeypatch):
    from conftest import _reload_app

    from database.models import Database

    db_path = str(tmp_path / "multi_device.db")
    seed_db = Database(db_path=db_path)
    _seed_two_devices(seed_db)

    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)
    main_module = _reload_app()

    from fastapi.testclient import TestClient
    tc = TestClient(main_module.app)

    response = tc.get("/results")
    assert response.status_code == 200
    assert "data-filter='device'" in response.text          # More filters → Device
    assert "<span class='tag'>mobile</span>" in response.text  # device shown on mobile rows

    filtered = tc.get("/results", params={"device": "mobile"})
    assert filtered.status_code == 200
    assert "m.example" in filtered.text
    assert "d.example" not in filtered.text


def test_post_search_passes_device_to_runner(client, monkeypatch):
    import dashboard.main as main_module

    captured = {}

    class _FakeRunner:
        def search(self, queries, country_code, language_code, max_pages,
                   engines=None, device=None):
            captured["device"] = device
            return {
                "run_id": "run-x", "status": "SUCCEEDED", "country_code": country_code,
                "queries": queries, "results": [], "features": [],
                "cost_usd": None, "compute_units": None, "retries": 0,
                "engine_runs": [{"engine": "google", "device": device or "desktop",
                                 "run_id": "run-x", "status": "SUCCEEDED",
                                 "country_code": country_code, "queries": queries,
                                 "results": [], "features": [],
                                 "cost_usd": None, "compute_units": None, "retries": 0,
                                 "error": None}],
                "warnings": [],
            }

    monkeypatch.setattr(main_module, "get_runner", lambda: _FakeRunner())

    from conftest import _wait_for_job
    resp = client.post("/search", data={"query": "Q", "country": "us", "pages": 1,
                                        "device": "mobile"})
    assert resp.status_code == 200
    import re
    m = re.search(r"/job/([0-9a-f]+)", resp.text)
    _wait_for_job(main_module, m.group(1))

    assert captured["device"] == "mobile"


# --------------------------------------------------------------------- #
# 7) database.models: add_evidence with png_path/sha256_png/run_id
#    (additive columns, no current producer -- see database.models._init_db)
# --------------------------------------------------------------------- #
def test_add_evidence_persists_png_params_and_run_id(tmp_path):
    """Column coverage (schema regression): png_path/sha256_png remain as
    additive columns even though no current producer fills them (see
    database.models._init_db)."""
    db = _db(tmp_path)
    eid = db.add_evidence(
        url="https://www.google.com/search?q=x", url_hash="h1", captured_at="t1",
        html_path="", sha256_html="", trigger="serp_snapshot",
        png_path="/tmp/serp/r1_us_desktop.png", sha256_png="b" * 64, run_id="run-1",
    )

    row = db.get_evidence_by_id(eid)
    assert row["png_path"] == "/tmp/serp/r1_us_desktop.png"
    assert row["sha256_png"] == "b" * 64
    assert row["run_id"] == "run-1"
    assert row["trigger"] == "serp_snapshot"


def test_add_evidence_html_row_leaves_png_params_null(tmp_path):
    db = _db(tmp_path)
    eid = db.add_evidence(url="https://x.example", url_hash="h2", captured_at="t1",
                          html_path="/tmp/x.html", sha256_html="a" * 64, trigger="manual")
    row = db.get_evidence_by_id(eid)
    assert row["png_path"] is None
    assert row["sha256_png"] is None
    assert row["run_id"] is None


def test_evidence_run_id_index_exists(tmp_path):
    db = _db(tmp_path)
    conn = db._connect()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    conn.close()
    assert "idx_evidence_run_id" in names


def test_add_run_cost_sums_onto_existing_and_checks_budget(tmp_path):
    """add_run_cost (generic, see database.models -- HISTORICAL: used by
    the retired screenshot actor; kept as a generic cost-accounting
    utility, with no active consumer today)."""
    db = _db(tmp_path)
    db.save_run("run-x", ["q"], "us", "completed", SUMMARY, cost_usd=0.10, compute_units=0.02)

    db.add_run_cost("run-x", 0.05, 0.01)

    row = db.get_run("run-x")
    assert row["cost_usd"] == pytest.approx(0.15)
    assert row["compute_units"] == pytest.approx(0.03)


def test_add_run_cost_noop_when_both_none(tmp_path):
    db = _db(tmp_path)
    db.save_run("run-y", ["q"], "us", "completed", SUMMARY, cost_usd=0.10)

    db.add_run_cost("run-y", None, None)

    assert db.get_run("run-y")["cost_usd"] == pytest.approx(0.10)


# --------------------------------------------------------------------- #
# 8) /evidence/{id}/download: served with containment intact (the inline
#    image/png branch is retired)
# --------------------------------------------------------------------- #
def test_download_snapshot_evidence_serves_octet_stream_attachment(client, seeded_db):
    from processing.evidence import evidence_dir
    vault = evidence_dir(seeded_db) / "serp"
    vault.mkdir(parents=True, exist_ok=True)
    html_path = vault / "run-x_us_desktop.html"
    html_path.write_bytes(HTML_BYTES)
    eid = seeded_db.add_evidence(
        url="https://www.google.com/search?q=x", url_hash="hshot", captured_at="t1",
        html_path=str(html_path), sha256_html=hashlib.sha256(HTML_BYTES).hexdigest(),
        trigger="serp_snapshot", run_id="run-x",
    )

    resp = client.get(f"/evidence/{eid}/download")

    assert resp.status_code == 200
    assert resp.content == HTML_BYTES
    assert "application/octet-stream" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_download_snapshot_outside_vault_is_404(client, seeded_db, tmp_path):
    outside = tmp_path / "leaked.html"
    outside.write_bytes(HTML_BYTES)
    eid = seeded_db.add_evidence(
        url="https://x.example", url_hash="hshot2", captured_at="t1",
        html_path=str(outside), sha256_html="a" * 64, run_id="run-y",
        trigger="serp_snapshot",
    )

    resp = client.get(f"/evidence/{eid}/download")

    assert resp.status_code == 404


def test_download_html_evidence_still_serves_octet_stream_attachment(client, seeded_db):
    """Regression 3.2: third-party HTML keeps being served the same way it
    was before 1.3 (octet-stream + attachment) -- unchanged by the pivot."""
    eid = seeded_db.add_evidence(
        url="https://scam.example/x", url_hash="hhtml", captured_at="t1",
        html_path="/tmp/does-not-need-to-exist-for-this-branch.html",
        sha256_html="a" * 64, trigger="manual",
    )
    # Creates the real file so it passes the is_file() check.
    from processing.evidence import evidence_dir
    vault = evidence_dir(seeded_db)
    vault.mkdir(parents=True, exist_ok=True)
    real_path = vault / "regression.html"
    real_path.write_text("<html>hi</html>")
    seeded_db._connect().execute(
        "UPDATE evidence SET html_path = ? WHERE id = ?", (str(real_path), eid)
    ).connection.commit()

    resp = client.get(f"/evidence/{eid}/download")

    assert resp.status_code == 200
    assert "application/octet-stream" in resp.headers["content-type"]
    assert "attachment" in resp.headers["content-disposition"]


def test_download_legacy_png_path_row_serves_octet_stream_not_inline(client, seeded_db):
    """F4 wrap-up: a historical row with ONLY png_path (no html_path, no
    current producer generates them anymore) is served the same as any
    other evidence -- octet-stream/attachment, NEVER inline image/png (the
    branch that rendered the PNG as its own was fully retired)."""
    from processing.evidence import evidence_dir
    vault = evidence_dir(seeded_db) / "serp"
    vault.mkdir(parents=True, exist_ok=True)
    legacy_png = vault / "legacy_us_desktop.png"
    legacy_png.write_bytes(b"\x89PNG\r\n\x1a\nlegacy-fake-png")
    eid = seeded_db.add_evidence(
        url="https://www.google.com/search?q=legacy", url_hash="hlegacy", captured_at="t1",
        html_path="", sha256_html="", trigger="serp_snapshot",
        png_path=str(legacy_png), sha256_png="c" * 64, run_id="run-legacy-row",
    )

    resp = client.get(f"/evidence/{eid}/download")

    assert resp.status_code == 200
    assert "application/octet-stream" in resp.headers["content-type"]
    assert "image/png" not in resp.headers["content-type"]
    assert "inline" not in resp.headers["content-disposition"]
