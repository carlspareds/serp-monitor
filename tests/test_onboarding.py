"""Tests for the public onboarding guide (/onboarding).

Verifies:
1. Public accessibility: accessible without login even when auth is active.
2. Fresh install accessibility: accessible without forced redirect to /setup.
3. Content and features: mode choice cards, readiness checklist, and
   workspace feature guide.
4. Link integrity: no dead links on /onboarding; the removed product
   overview (/landing, /welcome) and the /boarding alias are gone.
"""
import re

from conftest import _reload_app

from dashboard import nav
from database.models import Database


def test_onboarding_page_renders_successfully(client):
    resp = client.get("/onboarding")
    assert resp.status_code == 200
    assert "Getting started guide" in resp.text
    assert "Welcome to SERP Monitor!" in resp.text

    # Progress steps
    assert "1. Install &amp; Boot" in resp.text or "1. Install & Boot" in resp.text
    assert "2. Choose Mode" in resp.text
    assert "3. Target Configuration" in resp.text
    assert "4. Explore Features" in resp.text

    # Mode selection choices
    assert "Instant Demo Mode" in resp.text
    assert "Production Live Monitoring" in resp.text

    # System checklist
    assert "Application &amp; Local Database" in resp.text or "Application & Local Database" in resp.text
    assert "Tracked name" in resp.text
    assert "Search Engine Scraper" in resp.text
    assert "Optional AI provider" in resp.text
    assert "Alert channels" in resp.text
    assert "Your own domains" in resp.text

    # Tool directory links
    for tool_path in ("/results", "/", "/diff", "/review", "/evidence", "/timeline", "/export",
                      "/settings#ai", "/settings#projects", "/search_page", "/settings#notifications",
                      "/settings#domains"):
        assert f"href='{tool_path}'" in resp.text or f'href="{tool_path}"' in resp.text


def test_removed_public_pages_are_gone(client):
    """The product overview (/landing, /welcome) and the /boarding alias
    were removed; /onboarding is the only public guide."""
    for path in ("/landing", "/welcome", "/boarding"):
        assert client.get(path).status_code == 404, path
    assert "/onboarding" in nav.HELP_PATH


def test_public_access_when_auth_is_enabled(client, seeded_db, monkeypatch):
    """When AUTH_USER / AUTH_PASS are configured, the public pages (/onboarding,
    /login) must remain reachable without a session cookie, while
    private dashboard pages like /results redirect to /login."""
    monkeypatch.setenv("AUTH_USER", "admin")
    monkeypatch.setenv("AUTH_PASS", "pass123")

    # Public routes respond 200
    assert client.get("/onboarding").status_code == 200
    assert client.get("/login").status_code == 200
    # The removed pages are no longer public (nor served at all).
    for gone in ("/landing", "/welcome", "/boarding"):
        assert client.get(gone, follow_redirects=False).status_code != 200, gone

    # Private route redirects to login
    private_resp = client.get("/results", follow_redirects=False)
    assert private_resp.status_code == 303
    assert private_resp.headers["location"] == "/login"


def test_fresh_install_allows_onboarding(tmp_path, monkeypatch):
    """On a brand new installation before setup wizard is completed, / redirects to /setup,
    but /onboarding remains accessible."""
    db_path = str(tmp_path / "fresh_onboarding.db")
    Database(db_path=db_path)
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")
    monkeypatch.setenv("ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)
    monkeypatch.delenv("APIFY_API_KEY", raising=False)
    (tmp_path / ".env").write_text("")

    main_module = _reload_app()
    from fastapi.testclient import TestClient
    fresh = TestClient(main_module.app, follow_redirects=False)

    # Home redirects to /setup
    assert fresh.get("/").status_code == 303
    assert fresh.get("/").headers["location"] == "/setup"

    # The onboarding guide remains accessible
    assert fresh.get("/onboarding").status_code == 200


def test_home_page_contains_onboarding_link(client, seeded_db):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "<h1>Overview</h1>" in resp.text
    assert "href='/onboarding'" in resp.text      # Help, in the account menu
    assert "/landing" not in resp.text


def test_guide_reachable_without_duplicate_chrome_links(client):
    """One Help entry (the onboarding guide) lives in the account menu; the
    top bar and the rail don't repeat it, and nothing links to the removed
    /landing."""
    menu = nav.render_account_menu("someone", auth=True)
    assert menu.count("href='/onboarding'") == 1
    rail = nav.render_rail("/")
    topbar = nav.render_topbar(project="P", subject="S", alerts=0, auth=False)
    for chrome in (rail, topbar.replace(nav.render_account_menu("", False), "")):
        assert "href='/onboarding'" not in chrome
        assert "href='/landing'" not in chrome

    onboarding = client.get("/onboarding")
    assert onboarding.status_code == 200
    assert "/landing" not in onboarding.text


_HREF_RE = re.compile(r'''(?:href|action)=['"]([^'"]+)['"]''')


def test_no_dead_links_on_onboarding_page(client, seeded_db):
    """Walk every link on /onboarding and confirm none 404."""
    for path in ("/onboarding",):
        resp = client.get(path)
        assert resp.status_code == 200

        for link in _HREF_RE.findall(resp.text):
            if link.startswith(("http://", "https://", "#", "mailto:", "javascript:", "/static/")):
                continue
            clean = link.split("?")[0]
            if clean.startswith("/"):
                # Confirm reachable
                link_resp = client.get(clean, follow_redirects=False)
                assert link_resp.status_code != 404, f"Dead link '{clean}' found on '{path}'"


def test_setup_wizard_renders_progress_and_onboarding_link(client):
    resp = client.get("/setup?step=apify")
    assert resp.status_code == 200
    assert "wizard-steps" in resp.text
    assert "/onboarding" in resp.text
    assert "Welcome to SERP Monitor" in resp.text


def test_api_v1_docs_redirect(client):
    """Confirm /api/v1/docs safely redirects to FastAPI's interactive OpenAPI /docs."""
    resp = client.get("/api/v1/docs", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/docs"


def test_onboarding_step_numbering_consistency(client):
    """Confirm that the 4-step progress tracker and section headings are numbered consistently."""
    resp = client.get("/onboarding")
    assert resp.status_code == 200
    assert "1. Install &amp; Boot" in resp.text or "1. Install & Boot" in resp.text
    assert "2. Choose Mode" in resp.text
    assert "3. Target Configuration" in resp.text
    assert "4. Explore Features" in resp.text
    assert "Step 2: Choose Your Experience Path" in resp.text
    assert "Step 3: System Status &amp; Target Configuration" in resp.text or "Step 3: System Status & Target Configuration" in resp.text
    assert "Step 4: Tour Your Core Workspace Tools" in resp.text


def test_dark_mode_table_contrast_and_styles():
    """Verify that CSS rules guarantee dark mode contrast on tables and sidebar links."""
    from pathlib import Path
    css = Path("static/style.css").read_text(encoding="utf-8")
    assert ".landing-compare-table" not in css  # product overview CSS removed
    assert ".rail" in css
    # Dark tokens apply both to an explicit choice and to the OS preference.
    assert ":root[data-theme='dark']" in css
    assert ":root:not([data-theme='light'])" in css


# --------------------------------------------------------------------- #
# "Configured" uses the same checks as Settings/wizard/senders
# --------------------------------------------------------------------- #
_CHANNEL_ENV = ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "ALERT_EMAIL", "TELEGRAM_BOT_TOKEN",
                "SLACK_WEBHOOK_URL", "NTFY_URL", "NTFY_TOPIC", "LLM_PROVIDER", "GEMINI_API_KEY",
                "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL")


def _blank_env(monkeypatch):
    for name in _CHANNEL_ENV:
        monkeypatch.setenv(name, "")


def test_onboarding_alert_email_alone_is_not_configured(client, monkeypatch):
    _blank_env(monkeypatch)
    monkeypatch.setenv("ALERT_EMAIL", "me@example.com")  # no SMTP server: nothing is sent
    text = client.get("/onboarding").text
    assert "No channel configured" in text


def test_onboarding_telegram_token_without_chat_id_is_not_configured(client, monkeypatch):
    _blank_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    text = client.get("/onboarding").text
    assert "No channel configured" in text


def test_onboarding_alerts_configured_when_a_routed_channel_works(client, monkeypatch):
    _blank_env(monkeypatch)
    for k, v in (("SMTP_HOST", "smtp.example.com"), ("SMTP_USER", "u"), ("SMTP_PASS", "p")):
        monkeypatch.setenv(k, v)
    text = client.get("/onboarding").text  # default routing: warning -> email
    assert "go to a configured channel" in text
    assert "warning" in text.split("go to a configured channel")[0][-120:]


def test_onboarding_channel_configured_but_not_routed(client, seeded_db, monkeypatch):
    _blank_env(monkeypatch)
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/x")
    text = client.get("/onboarding").text  # default routing never uses slack
    assert "no severity is routed to it yet" in text


def test_onboarding_shows_openai_compatible_provider(client, monkeypatch):
    _blank_env(monkeypatch)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("OPENAI_MODEL", "llama3")
    text = client.get("/onboarding").text
    assert "OpenAI-compatible is set" in text
    assert "localhost:11434" in text
    assert "/settings#ai" in text


def test_onboarding_demo_banner_matches_other_pages(client, seeded_db):
    """Same plain-text banner as every other page, shown only while the active
    subject is the demo one (not tied to the Apify key any more)."""
    assert "demo-banner" not in client.get("/onboarding").text
    seeded_db.update_subject(1, is_demo=True)
    from dashboard.session import demo_mode_banner
    banner = demo_mode_banner()
    assert banner and "<a " not in banner
    assert banner in client.get("/results").text
    assert banner in client.get("/onboarding").text
