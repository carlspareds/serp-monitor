"""SECURITY.md items 1, 11, 14, 17, 18, 19: security headers / CSP, HTTPS,
body size limits, rate limiting, minimal and generic error output, and
secrets never echoed (HTML, JSON, logs, audit, files)."""
import json
import logging
import os
import re
import stat

import pytest
from conftest import _reload_app, sign_in

from dashboard import security

ADMIN_PW = "correct horse battery"


# --------------------------------------------------------------------- #
# Item 18: security headers
# --------------------------------------------------------------------- #
EXPECTED = {
    "content-security-policy": security.CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "x-frame-options": "DENY",
    "x-robots-tag": "noindex, nofollow",
}


@pytest.mark.parametrize("path", ["/", "/results", "/health", "/static/app.js",
                                  "/does-not-exist", "/api/v1/results", "/robots.txt"])
def test_security_headers_on_every_response(client, path):
    resp = client.get(path, follow_redirects=False)
    for name, value in EXPECTED.items():
        assert resp.headers.get(name) == value, (path, name)
    assert "camera=()" in resp.headers["permissions-policy"]
    assert "interest-cohort=()" in resp.headers["permissions-policy"]
    assert "strict-transport-security" not in resp.headers       # plain http


def test_csp_directives():
    csp = dict(d.strip().split(" ", 1) for d in security.CSP.split(";"))
    assert csp["default-src"] == "'self'" and csp["script-src"] == "'self'"
    assert csp["img-src"] == "'self' data:" and csp["connect-src"] == "'self'"
    assert csp["frame-ancestors"] == "'none'" and csp["base-uri"] == "'self'"
    assert csp["form-action"] == "'self'" and csp["object-src"] == "'none'"
    assert "unsafe-eval" not in security.CSP
    assert "'unsafe-inline'" not in csp["script-src"]


def test_dynamic_pages_are_not_cached_but_static_files_may_be(client):
    assert client.get("/").headers["cache-control"] == "no-store"
    assert "no-store" not in client.get("/static/style.css").headers.get("cache-control", "")


_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>", re.IGNORECASE)
_INLINE_HANDLER = re.compile(r"<[^>]+\son[a-z]+\s*=", re.IGNORECASE)


@pytest.mark.parametrize("path", ["/", "/results", "/settings", "/onboarding", "/setup",
                                  "/search-text?q=x", "/does-not-exist", "/ui/search",
                                  "/ui/reports", "/ui/review", "/ui/compare", "/ui/runs",
                                  "/ui/evidence", "/ui/notes", "/ui/timeline", "/assistant/panel"])
def test_pages_have_no_inline_script_or_handlers(client, path):
    """script-src 'self' without 'unsafe-inline': an inline <script>, an
    on*= attribute or a javascript: URL would be blocked by the browser."""
    for headers in ({}, {"X-SM-Fragment": "1"}):
        text = client.get(path, headers=headers).text
        assert not _INLINE_SCRIPT.search(text), (path, _INLINE_SCRIPT.search(text))
        assert not _INLINE_HANDLER.search(text), (path, _INLINE_HANDLER.search(text))
        assert "javascript:" not in text.lower(), path


def test_login_and_setup_admin_pages_have_no_inline_script(client, strict_auth, seeded_db):
    text = client.get("/setup/admin").text
    assert not _INLINE_SCRIPT.search(text) and not _INLINE_HANDLER.search(text)
    seeded_db.add_user("boss", ADMIN_PW, role="admin")
    text = client.get("/login").text
    assert not _INLINE_SCRIPT.search(text) and not _INLINE_HANDLER.search(text)


def test_static_js_has_no_eval():
    from conftest import _ROOT
    for name in ("app.js", "theme-boot.js"):
        src = (_ROOT / "static" / name).read_text()
        assert "eval(" not in src and "new Function" not in src, name


# --------------------------------------------------------------------- #
# Item 19: HTTPS
# --------------------------------------------------------------------- #
def test_hsts_only_over_https(client):
    https = type(client)(client.app, base_url="https://testserver")
    hsts = https.get("/").headers["strict-transport-security"]
    assert "max-age=31536000" in hsts and "includeSubDomains" in hsts


def test_force_https_redirects_http(client, monkeypatch):
    monkeypatch.setenv("FORCE_HTTPS", "1")
    resp = client.get("/results?x=1", follow_redirects=False)
    assert resp.status_code == 308 and resp.headers["location"] == "https://testserver/results?x=1"
    assert client.get("/health").status_code in (200, 503)       # probes keep working
    https = type(client)(client.app, base_url="https://testserver")
    assert https.get("/", follow_redirects=False).status_code == 200


def test_forwarded_proto_trusted_only_from_trusted_proxies(tmp_path, monkeypatch, seeded_db,
                                                          db_path):
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")
    monkeypatch.setenv("TRUSTED_PROXIES", "10.0.0.5")
    monkeypatch.setenv("FORCE_HTTPS", "1")
    app = _reload_app().app
    from fastapi.testclient import TestClient
    hdrs = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "127.0.0.1"}
    via_proxy = TestClient(app, client=("10.0.0.5", 1234))
    resp = via_proxy.get("/health", headers=hdrs)
    assert "strict-transport-security" in resp.headers          # proxy said https
    spoofed = TestClient(app, client=("203.0.113.7", 1234))
    resp = spoofed.get("/results", headers=hdrs, follow_redirects=False)
    assert resp.status_code == 308                               # header ignored
    monkeypatch.delenv("TRUSTED_PROXIES")
    _reload_app()


def test_config_check_warns_public_bind_without_https(tmp_path):
    from common import config_check as cc
    r = cc.check_config({"HOST": "0.0.0.0"}, cwd=tmp_path)
    assert "FORCE_HTTPS" in {f.key for f in r.warnings}
    r = cc.check_config({"HOST": "0.0.0.0", "FORCE_HTTPS": "1"}, cwd=tmp_path)
    assert "FORCE_HTTPS" not in {f.key for f in r.warnings}
    r = cc.check_config({"HOST": "127.0.0.1"}, cwd=tmp_path)
    assert "FORCE_HTTPS" not in {f.key for f in r.warnings}


# --------------------------------------------------------------------- #
# Item 14: request size limit
# --------------------------------------------------------------------- #
def test_oversized_bodies_are_rejected(client):
    big = "x" * (1024 * 1024 + 10)
    resp = client.post("/notes/save", data={"url": "https://a.example", "note": big})
    assert resp.status_code == 413
    resp = client.post("/assistant/chat", content=json.dumps({"messages": [big]}),
                       headers={"content-type": "application/json"})
    assert resp.status_code == 413


def test_streamed_body_without_length_is_cut_off(client, monkeypatch):
    monkeypatch.setenv("MAX_BODY_BYTES", "2048")

    def chunks():
        for _ in range(10):
            yield b"note=" + b"y" * 1000

    resp = client.post("/notes/save", content=chunks(),
                       headers={"content-type": "application/x-www-form-urlencoded"})
    assert resp.status_code == 413


def test_body_limit_is_configurable(client, monkeypatch):
    monkeypatch.setenv("MAX_BODY_BYTES", "4096")
    assert client.post("/notes/save", data={"url": "u", "note": "x" * 5000}).status_code == 413


# --------------------------------------------------------------------- #
# Item 11: rate limiting
# --------------------------------------------------------------------- #
def test_token_bucket_refills():
    b = security.TokenBuckets()
    assert all(b.take(("k",), 3, 60, now=0) == 0 for _ in range(3))
    wait = b.take(("k",), 3, 60, now=0)
    assert wait == pytest.approx(20)                 # one token every 20 s
    assert b.take(("k",), 3, 60, now=21) == 0


def test_login_is_rate_limited_per_ip(client, strict_auth, seeded_db, monkeypatch):
    seeded_db.add_user("boss", ADMIN_PW, role="admin")
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("RATE_LIMIT_LOGIN", "3/60")
    codes = [client.post("/login", data={"username": f"u{i}", "password": "x" * 12},
                         follow_redirects=False).status_code for i in range(4)]
    assert codes[:3] == [303, 303, 303] and codes[3] == 429
    last = client.post("/login", data={"username": "boss", "password": ADMIN_PW},
                       follow_redirects=False)
    assert last.status_code == 429 and int(last.headers["retry-after"]) >= 1
    other_ip = type(client)(client.app, client=("127.0.0.2", 1))
    assert other_ip.post("/login", data={"username": "boss", "password": ADMIN_PW},
                         follow_redirects=False).status_code == 303


def test_search_start_is_rate_limited_per_account(client, strict_auth, seeded_db, monkeypatch):
    token = sign_in(client, seeded_db, "boss", ADMIN_PW)
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("RATE_LIMIT_SEARCH", "2/60")
    codes = [client.post("/search", data={"country": "zz"}).status_code for _ in range(3)]
    assert codes[-1] == 429 and 429 not in codes[:2]
    # Another IP, same account: still limited (per-account bucket).
    elsewhere = type(client)(client.app, client=("127.0.0.3", 1))
    elsewhere.cookies.set("sid", token)
    assert elsewhere.post("/search", data={"country": "zz"}).status_code == 429


@pytest.mark.parametrize("rule,method,path", [
    ("api", "GET", "/api/v1/results"),
    ("assistant", "POST", "/assistant/chat"),
    ("reports", "POST", "/reports/create"),
    ("integration_test", "POST", "/settings/ai-test"),
    ("password", "POST", "/users/password"),
    ("setup", "POST", "/setup/first-search"),
])
def test_rule_families_are_limited(client, monkeypatch, rule, method, path):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv(f"RATE_LIMIT_{rule.upper()}", "1/60")
    client.request(method, path)
    resp = client.request(method, path)
    assert resp.status_code == 429 and resp.headers.get("retry-after")


def test_rate_limit_answers_json_on_api(client, monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("RATE_LIMIT_API", "1/60")
    client.get("/api/v1/results")
    resp = client.get("/api/v1/results")
    assert resp.status_code == 429 and resp.json()["detail"]


# --------------------------------------------------------------------- #
# Item 17: minimal output, generic errors
# --------------------------------------------------------------------- #
def test_health_is_minimal(client):
    body = client.get("/health").json()
    assert set(body) == {"status", "db_ping_ms", "scheduler_running", "scheduler_disabled",
                         "jobs", "disk_low", "version", "problems"}
    text = json.dumps(body)
    assert os.path.dirname(os.environ["DB_PATH"]) not in text and "seo_dashboard" not in text


def test_500_is_generic_without_traceback(client, monkeypatch):
    import dashboard.routers.search as search_router

    def boom(*a, **k):
        raise RuntimeError("secret /srv/data/seo_dashboard.db detail")

    monkeypatch.setattr(search_router.db, "search_text", boom)
    client_noraise = type(client)(client.app, raise_server_exceptions=False)
    resp = client_noraise.get("/search-text?q=x")
    assert resp.status_code == 500
    assert "seo_dashboard" not in resp.text and "Traceback" not in resp.text
    assert "RuntimeError" not in resp.text


def test_json_validation_errors_do_not_echo_input(client):
    resp = client.get("/api/v1/results", params={"limit": "SECRET-VALUE-123"},
                      headers={"X-API-Key": "x"})
    assert "SECRET-VALUE-123" not in resp.text


def test_openapi_docs_are_not_public(client, strict_auth, seeded_db):
    seeded_db.add_user("boss", ADMIN_PW, role="admin")
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert client.get(path, follow_redirects=False).status_code in (303, 401, 404)


def test_api_rows_carry_no_internal_fields(client, seeded_db):
    from conftest import RUN_ID  # noqa: F401 -- seeded data
    _id, key = seeded_db.add_api_key("ci", "read")
    rows = client.get("/api/v1/results", headers={"X-API-Key": key}).json()
    assert rows
    for r in rows:
        assert not {"raw_json", "html_path", "password_hash", "key_hash", "url_key"} & set(r)
    runs = client.get("/api/v1/runs", headers={"X-API-Key": key}).json()
    assert runs and all("queries" not in r and "cost_usd" not in r for r in runs)


def test_user_listing_never_has_hashes(seeded_db):
    seeded_db.add_user("boss", ADMIN_PW, role="admin")
    assert all("password_hash" not in u for u in seeded_db.list_users())


# --------------------------------------------------------------------- #
# Item 1: secrets never echoed
# --------------------------------------------------------------------- #
SENTINELS = {
    "APIFY_API_KEY": "apify_api_SENTINELapify0123456789",  # gitleaks:allow (fake test value)
    "GEMINI_API_KEY": "AIzaSENTINELgemini0123456789abcdefgh",  # gitleaks:allow (fake test value)
    "OPENAI_API_KEY": "sk-SENTINELopenai0123456789abcdef",  # gitleaks:allow (fake test value)
    "SMTP_PASS": "SENTINEL-smtp-password",  # gitleaks:allow (fake test value)
    "TELEGRAM_BOT_TOKEN": "123456789:SENTINELtelegram0123456789abcdefghij",  # gitleaks:allow (fake test value)
    "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/T000/B000/SENTINELslack",  # gitleaks:allow (fake test value)
    "NTFY_TOPIC": "sentinel-ntfy-topic-xyz",  # gitleaks:allow (fake test value)
    "AUTH_PASS": "SENTINEL-auth-password",  # gitleaks:allow (fake test value)
}


@pytest.fixture
def secret_env(monkeypatch):
    for k, v in SENTINELS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("SMTP_HOST", "smtp.example")
    monkeypatch.setenv("SMTP_USER", "mailer@example.com")
    return SENTINELS


def _get_routes(app):
    from conftest import app_routes
    paths = sorted({r.path for r in app_routes(app) if "GET" in r.methods and "{" not in r.path})
    assert len(paths) > 50
    yield from paths


def _no_sentinel(text: str, where: str):
    for k, v in SENTINELS.items():
        assert v not in text, f"{k} leaked in {where}"
        assert v[-12:] not in text, f"{k} (suffix) leaked in {where}"


def test_no_get_route_echoes_a_secret(client, seeded_db, secret_env):
    sign_in(client, seeded_db, "envadmin", SENTINELS["AUTH_PASS"])
    for path in _get_routes(client.app):
        if path in ("/logout", "/backup/db", "/export/pdf"):
            continue
        for headers in ({}, {"X-SM-Fragment": "1"}):
            resp = client.get(path, headers=headers, follow_redirects=False)
            _no_sentinel(resp.text, path)
            _no_sentinel(json.dumps(dict(resp.headers)), path + " headers")
    for name in ("apify", "ai", "email", "telegram", "slack", "ntfy"):
        resp = client.get(f"/ui/integration/{name}", headers={"X-SM-Fragment": "1"})
        _no_sentinel(resp.text, f"/ui/integration/{name}")


def test_backups_and_exports_hold_no_env_secret(client, seeded_db, secret_env):
    sign_in(client, seeded_db, "envadmin", SENTINELS["AUTH_PASS"])
    for path in ("/backup/csv", "/backup/db", "/audit/export.csv", "/export/pdf"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        body = resp.content.decode("latin-1")
        _no_sentinel(body, path)


def test_log_records_are_redacted(secret_env):
    """The handler setup() installs (JSON formatter + redacting filter),
    on its own stream so the test doesn't depend on which stdout the
    process-wide handler captured."""
    import io

    from dashboard import logging_setup
    logging_setup.setup()
    root_filters = [f for h in logging.getLogger().handlers for f in h.filters
                    if isinstance(f, logging_setup.RedactingFilter)]
    assert root_filters, "setup() must put the redacting filter on the root handler"
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging_setup.JsonFormatter())
    handler.addFilter(logging_setup.RedactingFilter())
    log = logging.getLogger("leaky")
    log.addHandler(handler)
    try:
        log.error("key=%s url=%s", SENTINELS["APIFY_API_KEY"], SENTINELS["SLACK_WEBHOOK_URL"],
                  extra={"detail": f"token {SENTINELS['TELEGRAM_BOT_TOKEN']}"})
        try:
            raise ValueError(f"bad {SENTINELS['OPENAI_API_KEY']}")
        except ValueError:
            log.exception("failed")
        log.error("api_key=sk-unknownbutkeyshaped0123456789 and Bearer abcdefghijklmnop")  # gitleaks:allow (fake test value)
    finally:
        log.removeHandler(handler)
    out = buf.getvalue()
    assert out.count('"logger": "leaky"') == 3
    _no_sentinel(out, "logs")
    assert "sk-unknownbutkeyshaped" not in out and "abcdefghijklmnop" not in out
    assert "<redacted>" in out


def test_uvicorn_access_log_args_are_redacted_keeping_structure(secret_env):
    from dashboard.logging_setup import RedactingFilter
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                            '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4", "GET", f"/x?token={SENTINELS['NTFY_TOPIC']}", "1.1", 200),
                            None)
    RedactingFilter().filter(rec)
    assert len(rec.args) == 5 and rec.args[4] == 200
    assert SENTINELS["NTFY_TOPIC"] not in rec.getMessage()


def test_audit_params_are_redacted(client, seeded_db, secret_env):
    from starlette.requests import Request

    from dashboard.session import audit
    req = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1), "method": "GET",
                   "path": "/", "query_string": b""})
    audit(req, "test_action", note=f"pasted {SENTINELS['APIFY_API_KEY']} by mistake")
    _no_sentinel(str(seeded_db.list_audit(limit=5)), "audit")


def test_assistant_scrub_covers_key_shapes(secret_env):
    from assistant import harness
    out = harness.scrub_secrets(f"{SENTINELS['GEMINI_API_KEY']} and sk-otherkeyshaped0123456789")
    _no_sentinel(out, "assistant")
    assert "sk-otherkeyshaped" not in out


def test_set_env_writes_0600_even_over_a_world_readable_file(tmp_path):
    from dashboard import admin
    env = tmp_path / ".env"
    env.write_text("A=1\n")
    os.chmod(env, 0o644)
    admin.set_env(str(env), "APIFY_API_KEY", "k")
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    fresh = tmp_path / "new.env"
    admin.set_env(str(fresh), "X", "y")
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".*.tmp"))
    os.environ.pop("X", None)


def test_config_check_warns_on_readable_env_file(tmp_path):
    from common import config_check as cc
    env_file = tmp_path / ".env"
    env_file.write_text("A=1\n")
    os.chmod(env_file, 0o644)
    r = cc.check_config({"ENV_FILE": str(env_file)}, cwd=tmp_path)
    assert any("readable by other users" in f.message for f in r.warnings)
    os.chmod(env_file, 0o600)
    r = cc.check_config({"ENV_FILE": str(env_file)}, cwd=tmp_path)
    assert not any("readable by other users" in f.message for f in r.warnings)


# --------------------------------------------------------------------- #
# Item 14: central validation
# --------------------------------------------------------------------- #
def test_every_string_form_field_is_bounded(client):
    """Each str Form(...) parameter declares max_length (common.validate)."""
    from conftest import app_routes
    unbounded = []
    for r in app_routes(client.app):
        for p in r.dependant.body_params:
            meta = getattr(p.field_info, "metadata", []) or []
            ann = str(p.field_info.annotation)
            if "str" in ann and "list" not in ann and "Literal" not in ann and not any(
                    getattr(m, "max_length", None) for m in meta):
                unbounded.append((r.path, p.name))
    assert unbounded == []


def test_overlong_fields_get_a_generic_422(client):
    resp = client.post("/timeline/add", data={"event_date": "2026-01-01",
                                              "description": "x" * 10_001})
    assert resp.status_code == 422 and "x" * 100 not in resp.text
    resp = client.post("/alerts/ack", data={"alert_id": "1" * 65})
    assert resp.status_code == 422


@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:text/html,x", "/relative",
                                 "ftp://x.example/a", "https://", "http://exa mple.com/\x07"])
def test_notes_and_evidence_require_http_urls(client, seeded_db, url):
    resp = client.post("/notes/save", data={"url": url, "note": "n"}, follow_redirects=False)
    assert resp.status_code in (303, 400)
    assert seeded_db.list_notes() == []
    resp = client.post("/evidence/capture", data={"url": url}, follow_redirects=False)
    assert resp.status_code in (200, 303) and seeded_db.list_evidence() == []


def test_api_patch_body_is_an_allowlist(client, seeded_db):
    _id, key = seeded_db.add_api_key("w", "write:alerts")
    aid = seeded_db.add_alert("negative_threshold", "warning", "us", "m", dedupe_key="k1")
    h = {"X-API-Key": key}
    resp = client.patch(f"/api/v1/alerts/{aid}", headers=h,
                        json={"status": "acknowledged", "subject_id": 99, "severity": "info",
                              "message": "tampered"})
    assert resp.status_code == 200
    row = seeded_db.get_alert(aid)
    assert row["message"] == "m" and row["severity"] == "warning" and row["subject_id"] == 1
    assert client.patch(f"/api/v1/alerts/{aid}", headers=h,
                        json={"status": "resolved", "assignee": "a" * 201}).status_code == 422
    assert client.patch(f"/api/v1/alerts/{aid}", headers=h,
                        json={"status": "deleted"}).status_code == 422


def test_validate_helpers():
    from common import validate as v
    assert v.http_url("https://a.example/x?y=1") == "https://a.example/x?y=1"
    assert v.http_url("javascript:alert(1)") is None and v.http_url("https://") is None
    assert v.http_url("https://a.example/" + "x" * 3000) is None
    assert v.domain("https://www.Example.COM/path") == "example.com"
    assert v.domain("not a domain") is None and v.domain("-bad-.com") is None
    assert v.iso_date("2026-02-29") is None and v.iso_date("2028-02-29") == "2028-02-29"
    assert v.int_in("5", 1, 10) == 5 and v.int_in("50", 1, 10, 3) == 3
    assert v.int_in("x", 1, 10) is None
    assert v.choice("a", ("a", "b")) == "a" and v.choice("c", ("a", "b"), "a") == "a"
    assert v.clip("  abc  ", 2) == "ab"
