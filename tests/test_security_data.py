"""SECURITY.md items 3, 5, 13, 15, 16: no DB details reach the browser,
optional encryption of secret-ish settings, parameterized queries under
injection payloads, output escaping of scraped/user content, formula
neutralization, and confined downloads (no traversal, no uploads)."""
import inspect
import re
from pathlib import Path

import pytest
from conftest import RUN_ID

from common import crypto

XSS_TITLE = "<script>alert('t')</script>Title"
XSS_SNIPPET = "<img src=x onerror=alert('s')>snippet"
XSS_URL = "javascript:alert('u')"


# --------------------------------------------------------------------- #
# Item 5: encryption of secret-ish settings
# --------------------------------------------------------------------- #
def test_sensitive_settings_are_encrypted_with_a_key(seeded_db, monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "a-long-test-passphrase-123")  # gitleaks:allow (fake test value)
    seeded_db.set_setting("telegram_chat_id", "-100123456789")
    conn = seeded_db._connect()
    raw = conn.execute("SELECT value FROM settings WHERE key='telegram_chat_id'").fetchone()[0]
    conn.close()
    assert raw.startswith("enc:v1:") and "100123456789" not in raw
    assert seeded_db.get_setting("telegram_chat_id") == "-100123456789"
    # Non-sensitive settings stay plain.
    seeded_db.set_setting("review_threshold", "0.7")
    assert seeded_db.get_setting("review_threshold") == "0.7"


def test_fernet_keys_work_and_a_wrong_key_reads_empty(seeded_db, monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    seeded_db.set_setting("telegram_chat_id", "42424242")
    assert seeded_db.get_setting("telegram_chat_id") == "42424242"
    monkeypatch.setenv("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert seeded_db.get_setting("telegram_chat_id") == ""          # never the ciphertext
    monkeypatch.delenv("APP_ENCRYPTION_KEY")
    assert seeded_db.get_setting("telegram_chat_id") == ""


def test_startup_migration_encrypts_existing_plain_values(seeded_db, monkeypatch):
    seeded_db.set_setting("telegram_chat_id", "777000")               # no key yet: plain
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "another-long-passphrase")  # gitleaks:allow (fake test value)
    assert seeded_db.encrypt_sensitive_settings() == 1
    assert seeded_db.encrypt_sensitive_settings() == 0                 # idempotent
    conn = seeded_db._connect()
    raw = conn.execute("SELECT value FROM settings WHERE key='telegram_chat_id'").fetchone()[0]
    conn.close()
    assert crypto.is_encrypted(raw) and seeded_db.get_setting("telegram_chat_id") == "777000"


def test_bad_key_is_a_config_error(tmp_path):
    from common import config_check as cc
    r = cc.check_config({"APP_ENCRYPTION_KEY": "short"}, cwd=tmp_path)
    assert "APP_ENCRYPTION_KEY" in {f.key for f in r.errors}
    r = cc.check_config({"TELEGRAM_BOT_TOKEN": "t"}, cwd=tmp_path)  # gitleaks:allow (fake test value)
    assert "APP_ENCRYPTION_KEY" in {f.key for f in r.warnings}


def test_api_keys_and_passwords_are_stored_hashed(seeded_db):
    _id, raw = seeded_db.add_api_key("ci", "read")
    seeded_db.add_user("boss", "correct horse battery", role="admin")
    conn = seeded_db._connect()
    dump = "\n".join(conn.iterdump())
    conn.close()
    assert raw not in dump and "correct horse battery" not in dump


# --------------------------------------------------------------------- #
# Item 3: the browser never talks to the database
# --------------------------------------------------------------------- #
def test_db_errors_and_paths_never_reach_the_client(client, monkeypatch, db_path):
    import dashboard.routers.results as results_router

    def broken(*a, **k):
        import sqlite3
        raise sqlite3.OperationalError(f"no such table: search_results in {db_path}")

    monkeypatch.setattr(results_router.db, "get_results_filtered", broken)
    c = type(client)(client.app, raise_server_exceptions=False)
    resp = c.get("/results")
    assert resp.status_code == 500
    assert "search_results" not in resp.text and db_path not in resp.text
    assert "OperationalError" not in resp.text
    for path in ("/", "/settings", "/health"):
        assert db_path not in client.get(path).text and "seo_dashboard.db" not in client.get(path).text


def test_no_sql_or_db_endpoints_exist(client):
    from conftest import app_routes
    for r in app_routes(client.app):
        assert not re.search(r"sql|query|/db\b", r.path) or r.path == "/backup/db", r.path


# --------------------------------------------------------------------- #
# Item 13: injection payloads go through bound parameters
# --------------------------------------------------------------------- #
PAYLOADS = ["' OR 1=1 --", "\" OR \"\"=\"", "x'); DROP TABLE search_results; --",
            "*", "NEAR(", ")", "title:", "\"unterminated", "%' UNION SELECT * FROM users --",
            "\x00", "Jordan* OR fraud", "a" * 2000]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_search_payloads_are_harmless(client, seeded_db, payload):
    resp = client.get("/search-text", params={"q": payload})
    assert resp.status_code == 200
    assert "password_hash" not in resp.text
    out = seeded_db.search_text(payload)
    assert isinstance(out["results"], list)
    conn = seeded_db._connect()
    assert conn.execute("SELECT COUNT(*) FROM search_results").fetchone()[0] == 5
    conn.close()


@pytest.mark.parametrize("payload", PAYLOADS[:4])
def test_filter_payloads_are_harmless(client, seeded_db, payload):
    for path, params in (("/results", {"country": payload, "sort": payload, "order": payload,
                                       "label": payload, "engine": payload}),
                         ("/ui/notes", {"overdue": payload}),
                         ("/ui/evidence", {"url_hash": payload}),
                         ("/ui/compare", {"country": payload, "a": payload, "b": payload})):
        assert client.get(path, params=params).status_code in (200, 303), path
    assert seeded_db.get_results_filtered(country_code=payload, sort_by=payload, order=payload) == []


def test_no_execute_call_formats_request_values():
    """Static guard: every execute() whose SQL is an f-string carries the
    S608 justification (ruff enforces it, this keeps the rule enabled)."""
    from conftest import _ROOT
    cfg = (_ROOT / "pyproject.toml").read_text()
    assert 'extend-select = ["S608"]' in cfg


# --------------------------------------------------------------------- #
# Item 15: scraped content is escaped everywhere
# --------------------------------------------------------------------- #
@pytest.fixture
def xss_db(seeded_db):
    seeded_db.save_results([{
        "query": "Jordan Ashworth", "url": "https://xss.example/<b>p</b>?a=\"'", "title": XSS_TITLE,
        "description": XSS_SNIPPET, "displayed_url": "xss.example", "position": 6,
        "country_code": "us", "domain": "xss.example", "classification": "neutral"}],
        run_id=RUN_ID)
    seeded_db.set_note("https://xss.example/<b>p</b>?a=\"'", "<script>alert('n')</script>")
    seeded_db.add_event("2026-01-02", "<img src=x onerror=alert('e')>")
    return seeded_db


@pytest.mark.parametrize("path", ["/results", "/ui/review", "/ui/notes", "/ui/timeline",
                                  "/search-text?q=Title", "/search-text?q=snippet", "/"])
def test_scraped_and_user_text_is_escaped(client, xss_db, path):
    for headers in ({}, {"X-SM-Fragment": "1"}):
        text = client.get(path, headers=headers).text
        assert "<script>alert(" not in text, path
        assert "<img src=x onerror" not in text, path
        assert "<b>p</b>" not in text, path


def test_scraped_urls_never_become_script_links(client, seeded_db):
    seeded_db.save_results([{"query": "q", "url": XSS_URL, "title": "js link",
                             "description": "", "displayed_url": "x", "position": 7,
                             "country_code": "us", "domain": "x", "classification": "neutral"}],
                           run_id=RUN_ID)
    text = client.get("/results").text
    assert "href='javascript:" not in text.lower() and 'href="javascript:' not in text.lower()


def test_jinja_autoescape_is_on():
    from dashboard.deps import templates
    assert templates.env.autoescape is True or callable(templates.env.autoescape)
    assert templates.env.from_string("{{ x }}").render(x="<b>") == "&lt;b&gt;"


def test_safe_filter_only_on_server_built_markup():
    from conftest import _ROOT
    allowed = {"shell.chrome", "shell.topbar", "shell.rail", "shell.banner", "flash_html()",
               "demo_mode_banner()"}
    for tpl in (_ROOT / "templates").glob("*.html"):
        for expr in re.findall(r"\{\{\s*([^|}]+?)\s*\|\s*safe\s*\}\}", tpl.read_text()):
            assert expr.strip() in allowed, (tpl.name, expr)


def test_flash_cookie_text_is_escaped(client):
    import json
    from urllib.parse import quote
    client.cookies.set("sm_flash", quote(json.dumps({"m": "<script>alert(1)</script>", "k": "ok"})))
    assert "<script>alert(1)</script>" not in client.get("/").text


@pytest.mark.parametrize("value,expected", [("=1+1", "'=1+1"), ("+SUM(A1)", "'+SUM(A1)"),
                                            ("-2", "'-2"), ("@x", "'@x"), ("\tx", "'\tx"),
                                            ("plain", "plain"), (5, 5)])
def test_formula_neutralization(value, expected):
    from export.excel_exporter import neutralize_formula
    assert neutralize_formula(value) == expected


def test_csv_exports_neutralize_formulas(client, seeded_db):
    seeded_db.save_results([{"query": "q", "url": "https://f.example", "title": "=HYPERLINK(1)",
                             "description": "@SUM(1)", "displayed_url": "f", "position": 8,
                             "country_code": "us", "domain": "f.example",
                             "classification": "neutral"}], run_id=RUN_ID)
    from starlette.requests import Request

    from dashboard.session import audit
    audit(Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1), "method": "GET",
                   "path": "/", "query_string": b""}), "=cmd|'/c calc'!A1", note="=1+1")
    for path in ("/backup/csv", "/audit/export.csv"):
        body = client.get(path).text
        for line in body.splitlines()[1:]:
            for cell in line.split(","):
                assert not cell.strip('"').startswith(("=", "@", "+")), (path, cell)


# --------------------------------------------------------------------- #
# Item 16: files
# --------------------------------------------------------------------- #
def test_no_route_accepts_file_uploads(client):
    from conftest import app_routes
    routes = list(app_routes(client.app))
    assert len(routes) > 100
    for r in routes:
        for p in r.dependant.body_params:
            ann = str(getattr(p, "type_", "") or getattr(getattr(p, "field_info", None), "annotation", ""))
            assert "UploadFile" not in ann and "bytes" not in ann, (r.path, p.name)
        src = inspect.getsource(r.endpoint)
        assert "UploadFile" not in src and "File(" not in src, r.path


@pytest.mark.parametrize("name", ["../../../etc/passwd", "..%2F..%2Fetc%2Fpasswd",
                                  "....//....//x.csv", "%2e%2e%2fsecret.csv", "x.csv%00.pdf",
                                  "/etc/passwd", "C:\\boot.ini", "a.CSV", "a.exe", ".hidden.csv"])
def test_report_downloads_resist_traversal(client, seeded_db, name):
    for prefix in ("/reports/1/", "/assistant/reports/"):
        resp = client.get(prefix + name, follow_redirects=False)
        assert resp.status_code in (400, 403, 404, 405), (prefix, name, resp.status_code)
        assert "root:" not in resp.text


def test_report_download_is_an_attachment(client, seeded_db):
    exports = Path(seeded_db.db_path).parent / "exports" / "1"
    exports.mkdir(parents=True, exist_ok=True)
    (exports / "weekly-report.csv").write_text("a,b\n")
    resp = client.get("/reports/1/weekly-report.csv")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"].startswith("attachment")
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_evidence_download_is_confined_and_inert(client, seeded_db, tmp_path):
    vault = Path(seeded_db.db_path).parent / "evidence"
    vault.mkdir(exist_ok=True)
    inside = vault / "page.html"
    inside.write_text("<script>alert(1)</script>")
    outside = tmp_path / "outside.txt"
    outside.write_text("TOP SECRET")
    good = seeded_db.add_evidence("https://a.example", "a" * 64, "2026-01-01T00:00:00",
                                  str(inside), "0" * 64, "manual")
    bad = seeded_db.add_evidence("https://b.example", "b" * 64, "2026-01-01T00:00:00",
                                 str(vault / ".." / "outside.txt"), "0" * 64, "manual")
    resp = client.get(f"/evidence/{good}/download")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["content-disposition"].startswith("attachment")
    assert resp.headers["x-content-type-options"] == "nosniff"
    resp = client.get(f"/evidence/{bad}/download")
    assert resp.status_code == 404 and "TOP SECRET" not in resp.text
    for raw in ("../1/download", "1%2F..%2F..", "-1"):
        assert client.get(f"/evidence/{raw}/download").status_code in (404, 422)


@pytest.mark.parametrize("path", ["/static/../src/dashboard/main.py", "/static/%2e%2e/.env",
                                  "/static/..%2F.env.example", "/static/../../etc/passwd"])
def test_static_mount_serves_only_static(client, path):
    resp = client.get(path)
    assert resp.status_code == 404 or "APIFY_API_KEY" not in resp.text
    assert "import" not in resp.text[:200] or resp.status_code == 404
