"""Tests for the first-run setup wizard (src/dashboard/routers/wizard.py).

Deliberately does NOT use the `client`/`seeded_db` fixtures (those start
from a database with a run and results already in it) -- the whole point
of this wizard is the genuinely fresh, unconfigured state, so every test
here builds its own empty Database via `_reload_app()` directly.
"""
import os

import pytest
from conftest import _reload_app

from database.models import Database

_INTEGRATION_ENV = (
    "APIFY_API_KEY", "LLM_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL", "OPENAI_BASE_URL",
    "OPENAI_API_KEY", "OPENAI_MODEL", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS",
    "SMTP_FROM", "ALERT_EMAIL", "TELEGRAM_BOT_TOKEN", "SLACK_WEBHOOK_URL", "NTFY_URL",
    "NTFY_TOPIC",
)


class _OkApify:
    def __init__(self, api_key=None):
        pass

    def check_account(self):
        return {"ok": True, "detail": "ok", "username": "x"}


@pytest.fixture
def fresh_client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "fresh.db")
    Database(db_path=db_path)  # just runs _init_db, no seeded data
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")
    monkeypatch.setenv("ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)
    monkeypatch.delenv("APIFY_API_KEY", raising=False)
    # setenv (not delenv) so pytest restores them: admin.set_env writes
    # straight to os.environ and must not leak into later tests.
    for name in _INTEGRATION_ENV:
        monkeypatch.setenv(name, "")
    (tmp_path / ".env").write_text("")

    main_module = _reload_app()
    from fastapi.testclient import TestClient
    return TestClient(main_module.app, follow_redirects=False)


def test_fresh_install_redirects_every_page_to_setup(fresh_client):
    resp = fresh_client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup"


def test_setup_page_shows_the_apify_step_first(fresh_client):
    resp = fresh_client.get("/setup", follow_redirects=True)
    assert resp.status_code == 200
    assert "Welcome to SERP Monitor" in resp.text


def test_static_and_health_are_not_redirected_to_setup(fresh_client):
    assert fresh_client.get("/static/style.css").status_code == 200
    assert fresh_client.get("/health").status_code == 200


def test_skipping_apify_advances_to_integrations_step(fresh_client):
    resp = fresh_client.post("/setup/apify?skip=1")
    assert resp.status_code == 303 and resp.headers["location"] == "/setup"

    resp = fresh_client.get("/setup", follow_redirects=True)
    assert "Optional integrations" in resp.text


def test_apify_step_saves_an_unverifiable_key_with_a_plain_message(fresh_client, monkeypatch):
    """Same as Settings → General: a key that fails verification is saved
    anyway, the reason is shown in plain words (never Apify's raw JSON),
    and the wizard moves on to the next step."""
    import dashboard.routers.wizard as wizard_module

    class _FakeClient:
        def __init__(self, api_key=None):
            pass

        def check_account(self):
            return {"ok": False, "detail": 'HTTP 401 from Apify: {"error":{"type":'
                                           '"user-or-token-not-found","message":"x"}}'}

    monkeypatch.setattr(wizard_module, "ApifyClient", _FakeClient)

    resp = fresh_client.post("/setup/apify", data={"apikey": "bad-key"})
    assert resp.status_code == 200
    assert ("Saved, but Apify rejected this key (HTTP 401). "
            "Check it in Settings → Search.") in resp.text
    assert "user-or-token-not-found" not in resp.text and "{" not in resp.text.split("Saved, but")[1][:120]
    assert os.getenv("APIFY_API_KEY") == "bad-key"
    assert "Optional integrations" in resp.text


def test_apify_step_saves_a_verified_key_and_advances(fresh_client, monkeypatch, tmp_path):
    import dashboard.routers.wizard as wizard_module

    class _FakeClient:
        def __init__(self, api_key=None):
            self.api_key = api_key

        def check_account(self):
            return {"ok": True, "detail": "Connection successful", "username": "someone"}

    monkeypatch.setattr(wizard_module, "ApifyClient", _FakeClient)

    resp = fresh_client.post("/setup/apify", data={"apikey": "good-key"})
    assert resp.status_code == 303 and resp.headers["location"] == "/setup"
    assert os.getenv("APIFY_API_KEY") == "good-key"

    resp = fresh_client.get("/setup", follow_redirects=True)
    assert "Optional integrations" in resp.text


def test_apify_key_never_appears_in_the_audit_log(fresh_client, monkeypatch):
    import dashboard.routers.wizard as wizard_module

    class _FakeClient:
        def __init__(self, api_key=None):
            pass

        def check_account(self):
            return {"ok": True, "detail": "ok", "username": "x"}

    monkeypatch.setattr(wizard_module, "ApifyClient", _FakeClient)
    fresh_client.post("/setup/apify", data={"apikey": "sk-super-secret-value"})

    from dashboard.deps import db
    rows = db.list_audit(limit=20)
    assert not any("sk-super-secret-value" in str(row.values()) for row in rows)


def test_auth_step_creates_admin_and_auto_logs_in(fresh_client):
    fresh_client.post("/setup/apify?skip=1")
    fresh_client.post("/setup/integrations?skip=1")

    resp = fresh_client.post("/setup/auth", data={"username": "op", "password": "op-password-long"})
    assert resp.status_code == 303 and resp.headers["location"] == "/setup"
    assert "sid" in resp.cookies

    from dashboard.deps import db
    assert db.authenticate_user("op", "op-password-long") is not None


def test_replaying_auth_step_after_admin_exists_does_not_create_a_second_admin(fresh_client):
    """The critical one: /setup/auth exists specifically to create an
    account with no auth required, which is only safe while no admin
    exists yet. An unauthenticated request replaying it afterward must be
    a no-op, not a way to plant a second, attacker-controlled account."""
    fresh_client.post("/setup/apify?skip=1")
    fresh_client.post("/setup/integrations?skip=1")
    fresh_client.post("/setup/auth", data={"username": "real-admin", "password": "real-passphrase"})

    from fastapi.testclient import TestClient

    import dashboard.main as main_module
    attacker = TestClient(main_module.app, follow_redirects=False)  # no session cookie

    resp = attacker.post("/setup/auth", data={"username": "attacker", "password": "hunter2-hunter2"})
    assert resp.status_code == 303 and resp.headers["location"] == "/login"

    from dashboard.deps import db
    usernames = [u["username"] for u in db.list_users()]
    assert usernames == ["real-admin"]
    assert db.authenticate_user("attacker", "hunter2-hunter2") is None


def test_unauthenticated_visitor_is_redirected_to_login_once_auth_exists(fresh_client):
    fresh_client.post("/setup/apify?skip=1")
    fresh_client.post("/setup/integrations?skip=1")
    fresh_client.post("/setup/auth", data={"username": "op", "password": "op-password-long"})

    from fastapi.testclient import TestClient

    import dashboard.main as main_module
    visitor = TestClient(main_module.app, follow_redirects=False)

    resp = visitor.get("/setup")
    assert resp.status_code == 303 and resp.headers["location"] == "/login"


def test_first_search_step_sets_the_default_query_and_redirects_to_search(fresh_client):
    fresh_client.post("/setup/apify?skip=1")
    fresh_client.post("/setup/integrations?skip=1")
    fresh_client.post("/setup/auth", data={"username": "op", "password": "op-password-long"})

    resp = fresh_client.post("/setup/first-search", data={"query": "Jane Doe", "country": "us"})
    assert resp.status_code == 303 and resp.headers["location"] == "/search_page"

    from dashboard.deps import db
    assert db.get_setting("report_query", "") == "Jane Doe"

    search_page = fresh_client.get("/search_page", follow_redirects=False)
    assert search_page.headers["location"] == "/results?modal=/ui/search"
    dialog = fresh_client.get("/ui/search", headers={"X-SM-Fragment": "1"})
    assert "Jane Doe" in dialog.text


def test_wizard_is_resumable_after_completing_only_some_steps(fresh_client):
    """Reopening /setup after quitting partway resumes at the first
    unfinished step -- no separate progress state needed, since each
    step's own persisted config IS the progress marker."""
    fresh_client.post("/setup/apify?skip=1")
    fresh_client.post("/setup/integrations?skip=1")

    resp = fresh_client.get("/setup", follow_redirects=True)
    assert "Create the admin account" in resp.text  # picks up at "auth", not back at step 1


def test_wizard_completing_every_step_stops_the_forced_redirect(fresh_client):
    fresh_client.post("/setup/apify?skip=1")
    fresh_client.post("/setup/integrations?skip=1")
    fresh_client.post("/setup/auth", data={"username": "op", "password": "op-password-long"})
    fresh_client.post("/setup/first-search", data={"query": "Jane Doe", "country": "us"})

    resp = fresh_client.get("/")
    assert resp.status_code == 200


def test_no_demo_banner_for_a_real_subject_even_without_an_apify_key(fresh_client):
    fresh_client.post("/setup/apify?skip=1")
    fresh_client.post("/setup/integrations?skip=1")
    fresh_client.post("/setup/auth", data={"username": "op", "password": "op-password-long"})
    fresh_client.post("/setup/first-search", data={"query": "Jane Doe", "country": "us"})

    resp = fresh_client.get("/")
    assert resp.status_code == 200
    assert "demo-banner" not in resp.text


def test_first_search_turns_the_placeholder_subject_into_the_tracked_one(fresh_client):
    fresh_client.post("/setup/apify?skip=1")
    fresh_client.post("/setup/integrations?skip=1")
    fresh_client.post("/setup/auth", data={"username": "op", "password": "op-password-long"})
    resp = fresh_client.post("/setup/first-search", data={"query": "Jane  Doe", "country": "gb"})
    assert "subject=1" in resp.headers.get("set-cookie", "")

    from dashboard.deps import db
    subject = db.get_subject(1)
    assert subject["name"] == "Jane Doe" and subject["queries"] == ["Jane Doe"]
    assert subject["countries"] == ["gb"]
    assert len(db.list_subjects()) == 1
    assert db.get_client(subject["client_id"])["name"] == "My project"


def test_first_subject_next_to_the_demo_goes_to_my_project_not_demo(fresh_client):
    """With the demo active, the wizard's real subject is created in the
    default project ("My project"), never in the "Demo" one."""
    import importlib.util

    from conftest import _ROOT
    spec = importlib.util.spec_from_file_location(
        "seed_demo_data", _ROOT / "scripts" / "seed_demo_data.py")
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    from dashboard.deps import db
    from dashboard.routers.wizard import _save_first_subject
    demo_id = seed.demo_subject_id(db)

    sid = _save_first_subject("Jane Doe", ["us"])
    assert sid != demo_id
    assert db.get_client(db.get_subject(sid)["client_id"])["name"] == "My project"
    assert db.get_client(db.get_subject(demo_id)["client_id"])["name"] == "Demo"


def test_skip_buttons_bypass_required_fields(fresh_client):
    text = fresh_client.get("/setup", follow_redirects=True).text
    # Step 1 reuses the Settings Apify dialog; only Skip is the wizard's own.
    assert "action='/setup/apify?skip=1'" in text and "/ui/integration/apify" in text
    fresh_client.post("/setup/apify?skip=1")
    fresh_client.post("/setup/integrations?skip=1")
    text = fresh_client.get("/setup").text
    # Sign-in is required: the admin-account step has no Skip.
    assert "/setup/auth?skip=1" not in text and "action='/setup/auth'" in text
    fresh_client.post("/setup/auth", data={"username": "op", "password": "op-password-long"})
    text = fresh_client.get("/setup").text
    assert "formaction='/setup/first-search?skip=1' formnovalidate" in text


def test_skipped_steps_are_not_shown_as_done(fresh_client):
    fresh_client.post("/setup/apify?skip=1")
    text = fresh_client.get("/setup").text
    first = text.split("wizard-step-item")[1]
    assert "skipped" in first and "✓" not in first


def test_fresh_install_copy_does_not_claim_demo_data(fresh_client):
    text = fresh_client.get("/setup", follow_redirects=True).text
    assert "demo data" not in text
    assert "Skip for now" in text and "--" not in text.split("<h2>")[1].split("</form>")[0]


def test_empty_apify_key_explains_what_to_do(fresh_client):
    resp = fresh_client.post("/setup/apify", data={"apikey": ""})
    assert resp.status_code == 200
    assert "Paste your Apify API key, or choose Skip" in resp.text


# --------------------------------------------------------------------- #
# Integrations step: the same rows and dialogs as Settings
# --------------------------------------------------------------------- #
_NEXT = "/setup?step=integrations"


def test_integrations_step_lists_the_shared_integration_rows(fresh_client):
    fresh_client.post("/setup/apify?skip=1")
    text = fresh_client.get("/setup").text
    for name in ("ai", "email", "telegram", "slack", "ntfy"):
        assert f"href='/ui/integration/{name}?next=/setup%3Fstep%3Dintegrations' data-modal" in text, name
    # No inline, divergent forms: every field lives in the dialogs.
    for field in ("gemini_key", "openai_key", "smtp_host", "telegram_token", "slack_webhook",
                  "llm_provider"):
        assert f"name='{field}'" not in text, field
    assert "/settings#notifications" in text


def test_dialogs_are_reachable_during_the_first_run(fresh_client):
    fresh_client.post("/setup/apify?skip=1")
    r = fresh_client.get(f"/ui/integration/ntfy?next={_NEXT}", headers={"X-SM-Fragment": "1"})
    assert r.status_code == 200 and "name='next' value='/setup?step=integrations'" in r.text


def test_a_dialog_save_returns_to_the_wizard(fresh_client):
    fresh_client.post("/setup/apify?skip=1")
    resp = fresh_client.post("/settings/notify", data={
        "channel": "slack", "slack_webhook": "https://hooks.slack.com/services/T/B/x",
        "next": _NEXT})
    assert resp.status_code == 303 and resp.headers["location"] == _NEXT
    assert os.getenv("SLACK_WEBHOOK_URL") == "https://hooks.slack.com/services/T/B/x"


def test_continue_after_setting_something_up_completes_the_step(fresh_client):
    fresh_client.post("/setup/apify?skip=1")
    fresh_client.post("/settings/notify", data={
        "channel": "ntfy", "ntfy_mode": "topic", "ntfy_topic": "serp-abc123", "next": _NEXT})
    fresh_client.post("/setup/integrations")
    from dashboard.deps import db
    assert db.get_setting("wizard_saved_integrations", "") == "1"
    assert db.get_setting("wizard_skipped_integrations", "") == ""
    resp = fresh_client.get("/setup", follow_redirects=True)
    assert "Create the admin account" in resp.text


def test_continue_with_nothing_set_up_is_a_skip(fresh_client):
    fresh_client.post("/setup/apify?skip=1")
    fresh_client.post("/setup/integrations")
    assert not os.getenv("LLM_PROVIDER")
    from dashboard.deps import db
    assert db.get_setting("wizard_skipped_integrations", "") == "1"


def test_partial_smtp_from_the_wizard_is_rejected_and_nothing_is_written(fresh_client, tmp_path):
    """Host/port/alert email without user/password would make the next
    restart fail the startup config check: refused in plain words, nothing
    written, back on the wizard step."""
    fresh_client.post("/setup/apify?skip=1")
    resp = fresh_client.post("/settings/email", data={
        "host": "smtp.example.com", "port": "587", "alert_to": "ops@example.com", "next": _NEXT})
    assert resp.status_code == 303 and resp.headers["location"] == _NEXT
    page = fresh_client.get(_NEXT).text
    assert "Not saved: fill in the user and the password." in page
    for key in ("SMTP_HOST", "SMTP_PORT", "ALERT_EMAIL"):
        assert not os.getenv(key), key
    assert (tmp_path / ".env").read_text() == ""


def test_non_http_webhook_is_rejected(fresh_client, tmp_path):
    fresh_client.post("/setup/apify?skip=1")
    fresh_client.post("/settings/notify", data={"channel": "slack",
                                                "slack_webhook": "hooks.slack.com/x", "next": _NEXT})
    assert "http(s)" in fresh_client.get(_NEXT).text
    assert not os.getenv("SLACK_WEBHOOK_URL")
    assert (tmp_path / ".env").read_text() == ""


def test_telegram_chat_id_from_the_dialog_makes_telegram_configured(fresh_client):
    fresh_client.post("/setup/apify?skip=1")
    fresh_client.post("/settings/notify", data={"channel": "telegram", "telegram_token": "123:abc",
                                                "telegram_chat_id": "-100200", "next": _NEXT})
    from dashboard.deps import db
    from notify import channel_status
    assert db.get_setting("telegram_chat_id", "") == "-100200"
    assert channel_status(db)["telegram"] is True


def test_ai_dialog_from_the_wizard_saves_openai_compatible(fresh_client):
    fresh_client.post("/setup/apify?skip=1")
    fresh_client.post("/settings/ai", data={
        "provider": "openai", "preset": "ollama", "openai_base_url": "http://localhost:11434/v1",
        "openai_model": "llama3", "openai_key": "sk-wizard-secret", "next": _NEXT})
    assert os.getenv("LLM_PROVIDER") == "openai"
    assert os.getenv("OPENAI_BASE_URL") == "http://localhost:11434/v1"
    assert os.getenv("OPENAI_MODEL") == "llama3"
    from dashboard.deps import db
    from processing import ai_classifier
    assert ai_classifier.is_configured()
    rows = db.list_audit(limit=20)
    assert not any("sk-wizard-secret" in str(row.values()) for row in rows)
    text = fresh_client.get(_NEXT).text
    assert "OpenAI-compatible via Ollama" in text and "sk-wizard-secret" not in text


def _finish_with_apify_skipped(client):
    client.post("/setup/apify?skip=1")
    client.post("/setup/integrations?skip=1")
    client.post("/setup/auth", data={"username": "op", "password": "op-password-long"})
    client.post("/setup/first-search?skip=1")


def test_summary_flags_missing_apify_key(fresh_client):
    _finish_with_apify_skipped(fresh_client)
    text = fresh_client.get("/setup").text
    assert "Search provider key not set" in text
    assert "/ui/integration/apify?next=/setup" in text     # the Settings dialog, not a copy
    assert "all set" not in text


def test_summary_without_the_apify_card_once_the_key_is_set(fresh_client, monkeypatch):
    import dashboard.routers.wizard as wizard_module
    monkeypatch.setattr(wizard_module, "ApifyClient", _OkApify)
    _finish_with_apify_skipped(fresh_client)
    fresh_client.post("/setup/apify", data={"apikey": "real-key"})
    text = fresh_client.get("/setup").text
    assert "Search provider key not set" not in text
    assert "Setup complete" in text
