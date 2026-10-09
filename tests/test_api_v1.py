"""Tests for item 9.3: documented API (OpenAPI) + API keys.

Acceptance criteria (§9.3):
1. /docs shows the API organized by tags.
2. A call with a valid API key to /api/v1/results returns typed JSON.
3. Without a key -> 401 (and a revoked/nonexistent key is indistinguishable).

TDD, no network: TestClient against the seeded temporary DB (fixture `client`,
see tests/conftest.py). AUTH_USER/AUTH_PASS are enabled only in the tests
that exercise the /docs protection behind the web session.
"""
import hashlib
import re

_NEW_KEY_RE = re.compile(r"id='new-api-key'[^>]*>([^<]+)</code>")


def _create_key(client, name="ci", scope="read"):
    """Creates an API key via /settings/api-keys and returns the CLEARTEXT
    value extracted from the response -- it's shown only once (brief 9.3),
    so this is the only point where a test can retrieve it."""
    resp = client.post("/settings/api-keys", data={"name": name, "scope": scope})
    assert resp.status_code == 200
    m = _NEW_KEY_RE.search(resp.text)
    assert m, resp.text
    return m.group(1)


# --------------------------------------------------------------------- #
# Acceptance 3: without a key -> 401, indistinguishable from a revoked key
# --------------------------------------------------------------------- #
def test_results_without_api_key_returns_401(client):
    r = client.get("/api/v1/results")
    assert r.status_code == 401


def test_results_with_unknown_api_key_returns_401(client):
    r = client.get("/api/v1/results", headers={"X-API-Key": "not-a-real-key"})
    assert r.status_code == 401


def test_runs_alerts_stats_without_api_key_all_return_401(client):
    for path in ("/api/v1/runs", "/api/v1/alerts", "/api/v1/stats"):
        r = client.get(path)
        assert r.status_code == 401, path


def test_revoked_key_returns_401_identical_to_nonexistent_key(client, seeded_db):
    key = _create_key(client)
    key_id = seeded_db.list_api_keys()[0]["id"]
    revoke = client.post("/settings/api-keys/revoke", data={"key_id": key_id})
    assert revoke.status_code == 200

    r_revoked = client.get("/api/v1/results", headers={"X-API-Key": key})
    r_missing = client.get("/api/v1/results", headers={"X-API-Key": "totally-invented-key"})

    assert r_revoked.status_code == 401
    assert r_missing.status_code == 401
    # Same body -- must not leak whether the key EXISTS (and is revoked)
    # or never existed (brief 9.3, "without distinguishing which of the two").
    assert r_revoked.json() == r_missing.json()


# --------------------------------------------------------------------- #
# Acceptance 2: valid key -> 200, typed JSON
# --------------------------------------------------------------------- #
def test_results_with_valid_key_returns_typed_json(client):
    key = _create_key(client)
    r = client.get("/api/v1/results", headers={"X-API-Key": key})

    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) == 5  # SAMPLE_RESULTS from conftest.py
    row = body[0]
    assert set(row.keys()) == {
        "id", "subject_id", "run_id", "url", "title", "domain", "country_code", "engine",
        "device", "classification", "severity", "position", "scraped_at",
    }


def test_results_classification_filter_reuses_get_results_filtered(client):
    key = _create_key(client)
    r = client.get("/api/v1/results", params={"classification": "positive"},
                    headers={"X-API-Key": key})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert all(row["classification"] == "positive" for row in body)


def test_results_limit_and_offset_are_clamped_not_500(client):
    key = _create_key(client)
    headers = {"X-API-Key": key}

    huge_limit = client.get("/api/v1/results", params={"limit": 999999}, headers=headers)
    assert huge_limit.status_code == 200
    assert len(huge_limit.json()) <= 200  # clamp, not an error nor the entire table

    negative = client.get("/api/v1/results", params={"limit": -5, "offset": -5}, headers=headers)
    assert negative.status_code == 200  # defensive clamp, never 422/500

    bad_engine = client.get("/api/v1/results", params={"engine": "not-a-real-engine"},
                             headers=headers)
    assert bad_engine.status_code == 200  # ignored, same approach as /results


def test_runs_alerts_stats_endpoints_with_valid_key(client):
    key = _create_key(client)
    headers = {"X-API-Key": key}

    runs = client.get("/api/v1/runs", headers=headers)
    assert runs.status_code == 200
    assert isinstance(runs.json(), list)
    assert runs.json()[0]["apify_run_id"]

    alerts = client.get("/api/v1/alerts", headers=headers)
    assert alerts.status_code == 200
    assert isinstance(alerts.json(), list)

    stats = client.get("/api/v1/stats", headers=headers)
    assert stats.status_code == 200
    stats_body = stats.json()
    assert set(stats_body.keys()) == {
        "subject_id", "total_runs", "total_results", "by_class", "last_run", "countries",
    }
    assert stats_body["total_results"] == 5


# --------------------------------------------------------------------- #
# /health remains public without a key (B13, deep dive §9.2)
# --------------------------------------------------------------------- #
def test_health_still_public_without_api_key(client):
    r = client.get("/health")
    assert r.status_code in (200, 503)


# --------------------------------------------------------------------- #
# Acceptance 1: /docs organized by tags + protected behind web session
# --------------------------------------------------------------------- #
def test_openapi_schema_has_api_v1_tag_and_is_organized(client):
    schema = client.get("/openapi.json").json()
    tags_used = {
        tag
        for path_item in schema["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict)
        for tag in operation.get("tags", [])
    }
    assert "api-v1" in tags_used
    assert "results" in tags_used
    assert "settings" in tags_used
    assert len(tags_used) > 5  # organized by domain, not a single block


def test_docs_requires_web_session_when_auth_enabled(client, monkeypatch):
    monkeypatch.setenv("AUTH_USER", "alice")
    monkeypatch.setenv("AUTH_PASS", "s3cr3t")

    anon = client.get("/docs", follow_redirects=False)
    assert anon.status_code == 303
    assert anon.headers["location"] == "/login"

    redoc_anon = client.get("/redoc", follow_redirects=False)
    assert redoc_anon.status_code == 303

    client.post("/login", data={"username": "alice", "password": "s3cr3t"})
    authed = client.get("/docs")
    assert authed.status_code == 200


def test_docs_open_when_auth_disabled(client):
    """Without AUTH_USER/AUTH_PASS the entire app is fail-open (a convention
    already documented in auth_enabled()) -- /docs is not a special exception."""
    r = client.get("/docs")
    assert r.status_code == 200


def test_api_v1_bypasses_web_session_when_auth_enabled(client, monkeypatch):
    """The API key coexists with, but does not depend on, the web session
    (brief 9.3): with AUTH_USER/PASS active and WITHOUT the `sid` cookie, a
    valid API key must keep working -- otherwise no external script could
    ever call /api/v1/..."""
    key = _create_key(client)  # before enabling auth (the session isn't required yet)

    monkeypatch.setenv("AUTH_USER", "alice")
    monkeypatch.setenv("AUTH_PASS", "s3cr3t")

    r = client.get("/api/v1/results", headers={"X-API-Key": key})
    assert r.status_code == 200

    # Without a key, with auth active: still 401 (not a 303 to /login --
    # /api/v1 has its own auth contract, not the web session's).
    no_key = client.get("/api/v1/results", follow_redirects=False)
    assert no_key.status_code == 401


# --------------------------------------------------------------------- #
# Key management in /settings: hashing + single reveal + audit (C24)
# --------------------------------------------------------------------- #
def test_created_key_is_stored_hashed_never_in_clear(client, seeded_db):
    key = _create_key(client, name="prod-script")

    rows = seeded_db.list_api_keys()
    assert len(rows) == 1
    assert rows[0]["name"] == "prod-script"
    assert "key_hash" not in rows[0]  # list_api_keys never exposes the hash

    conn = seeded_db._connect()
    stored_hash = conn.execute(
        "SELECT key_hash FROM api_keys WHERE id = ?", (rows[0]["id"],)
    ).fetchone()[0]
    conn.close()

    assert stored_hash != key
    assert stored_hash == hashlib.sha256(key.encode("utf-8")).hexdigest()


def test_settings_page_shows_the_new_key_only_once(client):
    key = _create_key(client, name="reveal-once")

    reload_page = client.get("/settings/api")
    assert reload_page.status_code == 200
    assert key not in reload_page.text  # the reveal was only in the POST response
    assert "reveal-once" in reload_page.text  # the name does stay listed


def test_api_key_creation_and_revocation_are_audited(client, seeded_db):
    key = _create_key(client, name="audited-key")

    create_rows = seeded_db.list_audit(action="settings_apikey_create")
    assert len(create_rows) == 1
    assert key not in create_rows[0]["params_json"]  # never the cleartext key in the audit
    assert "audited-key" in create_rows[0]["params_json"]

    key_id = seeded_db.list_api_keys()[0]["id"]
    revoke = client.post("/settings/api-keys/revoke", data={"key_id": key_id})
    assert revoke.status_code == 200

    revoke_rows = seeded_db.list_audit(action="settings_apikey_revoke")
    assert len(revoke_rows) == 1
    assert revoke_rows[0]["resource_id"] == str(key_id)


def test_invalid_scope_falls_back_to_read_default(client, seeded_db):
    client.post("/settings/api-keys", data={"name": "weird-scope", "scope": "superadmin"})
    rows = seeded_db.list_api_keys()
    assert rows[0]["scopes"] == "read"
