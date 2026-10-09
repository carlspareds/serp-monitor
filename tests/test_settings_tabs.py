"""Settings: one page with a section nav; every POST redirects back (PRG)
with a toast; old tab URLs redirect to their anchor."""
import pytest

SECTIONS = ("projects", "search", "ai", "notifications", "scheduling", "users",
            "api", "audit", "backup")


def test_settings_is_one_page_with_every_section_in_mockup_order(client):
    text = client.get("/settings").text
    positions = [text.index(f"<section id='{key}'") for key in SECTIONS]
    assert positions == sorted(positions)
    for key in SECTIONS:
        assert f"href='#{key}'" in text, key
    # Domains live inside Projects and subjects (one home), not as a section.
    assert "<section id='domains'" not in text and "<details class='subj-domains' id='domains'>" in text
    for h in ("<h2>Projects and subjects", "<b>Domains</b>", "<h2>Search (Apify)",
              "<h2>Monthly budget alert", "<h2>Results review", "<h2>AI provider",
              "<h2>Notification channels", "<h2>Alert routing", "<h2>Scheduling", "<h2>Users",
              "<h2>API keys", "<h2>Audit log", "<h2>Backup"):
        assert h in text, h


@pytest.mark.parametrize("old,anchor", [
    ("/settings/notifications", "/settings#notifications"),
    ("/settings/api", "/settings#api"),
    ("/settings/backup", "/settings#backup"),
    ("/users", "/settings#users"),
    ("/audit", "/settings#audit"),
])
def test_old_settings_urls_redirect_to_their_section(client, old, anchor):
    r = client.get(old, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == anchor


def test_settings_posts_redirect_back_with_a_toast(client):
    r = client.post("/settings/review-threshold", data={"review_threshold": "0.5"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings#search"
    assert "sm_flash" in r.headers.get("set-cookie", "")
    page = client.get("/settings")
    assert "Review threshold saved." in page.text
    # The flash is shown once: the cookie is deleted by the page that showed it.
    assert "Review threshold saved." not in client.get("/settings").text


def test_unchanged_save_says_so_instead_of_saved(client):
    client.post("/settings/apify-budget", data={"budget": "12.5"})
    r = client.post("/settings/apify-budget", data={"budget": "12.5"})
    assert "No changes to the budget." in r.text and "Apify budget saved." not in r.text


def test_empty_apify_key_is_not_a_422(client, monkeypatch):
    monkeypatch.setenv("APIFY_API_KEY", "existing-key")
    r = client.post("/settings/apikey", data={"apikey": ""})
    assert r.status_code == 200
    assert "saved Apify token was kept" in r.text
    r = client.post("/settings/apikey", data={})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")


def test_removed_settings_cards_are_gone(client):
    text = client.get("/settings").text
    assert "Saved searches" not in text and "/settings/queries" not in text
    assert "SERP snapshot" not in text and "/settings/serp-snapshots" not in text


def test_validation_errors_come_back_as_a_message_not_json(client):
    r = client.post("/settings/review-threshold", data={"review_threshold": "abc"})
    assert r.status_code == 200 and "Not saved" in r.text


# --------------------------------------------------------------------- #
# AI provider card, channel secrets, test results, scheduling
# --------------------------------------------------------------------- #
_ENV = ("LLM_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY",
        "OPENAI_MODEL", "TELEGRAM_BOT_TOKEN", "SLACK_WEBHOOK_URL", "NTFY_URL", "NTFY_TOPIC",
        "APIFY_API_KEY")


@pytest.fixture
def env_sandbox(monkeypatch, tmp_path):
    """admin.set_env writes os.environ + ENV_FILE: setenv("") first so
    pytest restores every variable, and point ENV_FILE at a temp file."""
    monkeypatch.setenv("ENV_FILE", str(tmp_path / ".env"))
    for name in _ENV:
        monkeypatch.setenv(name, "")
    return tmp_path / ".env"


def test_ai_card_has_anchor_and_opens_the_guided_dialog(client, env_sandbox):
    text = client.get("/settings").text
    assert "id='ai'" in text and "id='projects'" in text
    assert "href='/ui/integration/ai?next=/settings#ai' data-modal" in text
    dialog = client.get("/ui/integration/ai?provider=openai", headers={"X-SM-Fragment": "1"}).text
    for field in ("name='provider'", "name='openai_base_url'", "name='openai_key'",
                  "name='openai_model'", "localhost:11434/v1", "openrouter.ai/api/v1"):
        assert field in dialog, field


def test_save_ai_openai_compatible(client, seeded_db, env_sandbox):
    import os
    r = client.post("/settings/ai", data={
        "provider": "openai", "openai_base_url": "http://localhost:1234/v1",
        "openai_key": "sk-settings-secret", "openai_model": "qwen2.5", "model": "gemini-2.5-flash-lite",
    })
    assert r.status_code == 200
    assert os.getenv("LLM_PROVIDER") == "openai"
    assert os.getenv("OPENAI_BASE_URL") == "http://localhost:1234/v1"
    assert os.getenv("OPENAI_API_KEY") == "sk-settings-secret"
    assert os.getenv("OPENAI_MODEL") == "qwen2.5"
    assert "OpenAI-compatible via LM Studio" in r.text and "qwen2.5" in r.text
    row = seeded_db.list_audit(action="settings_ai_save")[0]
    assert "sk-settings-secret" not in row["params_json"]
    assert '"openai_key_changed": true' in row["params_json"]


def test_save_ai_empty_key_keeps_the_existing_one(client, env_sandbox, monkeypatch):
    import os
    monkeypatch.setenv("OPENAI_API_KEY", "keep-me")
    client.post("/settings/ai", data={"provider": "openai", "openai_key": ""})
    assert os.getenv("OPENAI_API_KEY") == "keep-me"


def test_save_ai_rejects_unknown_provider(client, env_sandbox):
    import os
    r = client.post("/settings/ai", data={"provider": "skynet"})
    assert "Not saved" in r.text
    assert not os.getenv("LLM_PROVIDER")


def test_ai_test_button_is_provider_neutral(client, env_sandbox):
    r = client.post("/settings/ai-test", follow_redirects=False)
    assert r.headers["location"] == "/settings#ai"


@pytest.mark.parametrize("data,env", [
    ({"channel": "slack", "slack_webhook": "https://hooks.slack.com/services/T/B/sec"},
     {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/T/B/sec"}),
    ({"channel": "ntfy", "ntfy_topic": "secret-topic-123"}, {"NTFY_TOPIC": "secret-topic-123"}),
    ({"channel": "telegram", "telegram_token": "123:secret", "telegram_chat_id": "42"},
     {"TELEGRAM_BOT_TOKEN": "123:secret"}),
])
def test_channel_secrets_saved_from_settings(client, seeded_db, env_sandbox, data, env):
    import os
    r = client.post("/settings/notify", data=data)
    assert r.status_code == 200
    for k, v in env.items():
        assert os.getenv(k) == v
        assert v in env_sandbox.read_text()
    row = seeded_db.list_audit(action="settings_notify_save")[0]
    for v in env.values():
        assert v not in row["params_json"]
    assert "Alert routing" in r.text  # routing hint after saving a channel
    assert "id='telegram'" in r.text and "id='slack'" in r.text and "id='ntfy'" in r.text
    assert "id='email'" in r.text


def test_channel_disconnect_clears_it(client, seeded_db, env_sandbox, monkeypatch):
    import os
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/x")
    client.post("/settings/notify", data={"channel": "slack", "clear": "1"})
    assert os.getenv("SLACK_WEBHOOK_URL") == ""


def test_telegram_save_keeps_token_when_left_empty(client, seeded_db, env_sandbox, monkeypatch):
    import os
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:keep")
    client.post("/settings/notify", data={"channel": "telegram", "telegram_chat_id": "7"})
    assert os.getenv("TELEGRAM_BOT_TOKEN") == "123:keep"
    assert seeded_db.get_setting("telegram_chat_id", "") == "7"


def test_test_results_are_not_double_escaped(client, env_sandbox):
    for r in (client.post("/settings/notify-test", data={"channel": "slack"}),
              client.post("/settings/email-test")):
        assert r.status_code == 200
        assert "&lt;span" not in r.text
        assert "Failed" in r.text


def test_apify_key_save_reports_verification(client, env_sandbox):
    r = client.post("/settings/apikey", data={"apikey": "apify_api_unverifiable"})
    assert r.status_code == 200
    assert "Apify check" in r.text and "saved anyway" in r.text


def test_deadman_job_cannot_be_rescheduled(client):
    r = client.post("/settings/cron", data={"name": "deadman_check", "frequency": "daily",
                                            "hour": "3"})
    assert r.status_code == 400


def test_general_tab_copy_has_no_spec_ids_and_utc_times(client):
    text = client.get("/settings").text
    assert "(1.3," not in text and "(3.1)" not in text
    assert "UTC" in text and "Daily digest" in text


def test_api_tab_describes_write_scopes(client):
    text = client.get("/settings/api").text
    assert "Read-only API" not in text
    assert "write:alerts" in text and "PATCH /api/v1/alerts/" in text


def test_deadman_job_cannot_be_paused(client, seeded_db):
    seeded_db.ensure_scheduled_job("deadman_check", "*/15 * * * *")
    r = client.post("/settings/cron/toggle", data={"name": "deadman_check"})
    assert r.status_code == 400
    assert seeded_db.get_scheduled_job("deadman_check")["enabled"]


def _fake_jobs():
    return [
        {"name": "weekly_report", "cron_expr": "0 8 * * 1", "enabled": 1,
         "last_run_at": "2026-10-05T08:00:01", "last_status": "ok", "last_duration_ms": 12,
         "next_run_at": "2026-10-12T04:00:00-04:00"},
        {"name": "deadman_check", "cron_expr": "*/15 * * * *", "enabled": 1,
         "last_run_at": None, "last_status": None, "last_duration_ms": None,
         "next_run_at": "2026-10-07T13:15:00+00:00"},
    ]


def test_scheduling_card_formats_times_and_hides_locked_toggle(client, monkeypatch):
    import dashboard.routers.settings as settings_module
    monkeypatch.setattr(settings_module.scheduler_core, "list_jobs", _fake_jobs)
    text = client.get("/settings").text
    # One Pause button: weekly_report's. deadman_check has none.
    assert text.count("action='/settings/cron/toggle'") == 1
    assert "value='deadman_check'><button" not in text
    # Offsets are converted to UTC and shown without seconds or offset.
    assert "Next: 2026-10-12 08:00 UTC" in text
    assert "Next: 2026-10-07 13:15 UTC" in text
    assert "Last run: <span class='pill ok'>ok</span> 2026-10-05 08:00 UTC" in text
    assert "-04:00" not in text and "+00:00 UTC" not in text


def test_weekly_frequency_saves_the_chosen_day(client, monkeypatch):
    import dashboard.routers.settings as settings_module
    saved = {}
    monkeypatch.setattr(settings_module.scheduler_core, "set_job_cron",
                        lambda name, expr, enabled=True: saved.update(name=name, expr=expr))
    # The schedule form is offered while the scheduler runs (with
    # DISABLE_SCHEDULER the card is read-only, see test_live_test_fixes).
    monkeypatch.delenv("DISABLE_SCHEDULER", raising=False)
    text = client.get("/ui/settings/schedule/weekly_report", headers={"X-SM-Fragment": "1"}).text
    assert "<select id='cr-day' name='weekday'>" in text
    assert "<option value='1' selected>Monday</option>" in text

    r = client.post("/settings/cron", data={"name": "weekly_report", "frequency": "weekly",
                                            "hour": "9", "weekday": "3"})
    assert r.status_code == 200
    assert saved == {"name": "weekly_report", "expr": "0 9 * * 3"}


def test_build_cron_expr_weekday():
    from dashboard import admin
    assert admin.build_cron_expr(8, "weekly") == "0 8 * * 1"
    assert admin.build_cron_expr(8, "weekly", weekday=0) == "0 8 * * 0"
    assert admin.build_cron_expr(8, "weekly", weekday=9) == "0 8 * * 1"
    assert admin.build_cron_expr(8, "daily", weekday=3) == "0 8 * * *"


# --------------------------------------------------------------------- #
# Saves never produce a config the startup check rejects
# --------------------------------------------------------------------- #
_SMTP_ENV = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM", "ALERT_EMAIL")


@pytest.fixture
def smtp_sandbox(monkeypatch, env_sandbox):
    for name in _SMTP_ENV:
        monkeypatch.setenv(name, "")
    env_sandbox.write_text("")
    return env_sandbox


def test_partial_smtp_is_rejected_and_nothing_written(client, smtp_sandbox):
    import os
    r = client.post("/settings/email", data={"host": "smtp.example.com", "port": "587",
                                             "alert_to": "ops@example.com"})
    assert r.status_code == 200
    assert "Not saved: fill in the user and the password." in r.text
    assert smtp_sandbox.read_text() == ""
    assert not os.getenv("SMTP_HOST") and not os.getenv("ALERT_EMAIL")


def test_complete_smtp_is_saved(client, smtp_sandbox):
    import os
    r = client.post("/settings/email", data={"host": "smtp.example.com", "port": "587",
                                             "user": "u@example.com", "password": "pw"})
    assert r.status_code == 200 and "Not saved" not in r.text
    assert os.getenv("SMTP_HOST") == "smtp.example.com" and os.getenv("SMTP_PASS") == "pw"


def test_emptying_host_and_user_turns_email_off(client, smtp_sandbox, monkeypatch):
    import os
    for key, value in (("SMTP_HOST", "smtp.example.com"), ("SMTP_USER", "u"),
                       ("SMTP_PASS", "pw")):
        monkeypatch.setenv(key, value)
    r = client.post("/settings/email", data={"host": "", "user": "", "password": ""})
    assert "Not saved" not in r.text
    assert not os.getenv("SMTP_HOST") and not os.getenv("SMTP_PASS")


def test_bad_smtp_port_is_rejected(client, smtp_sandbox):
    r = client.post("/settings/email", data={"host": "smtp.example.com", "port": "abc",
                                             "user": "u", "password": "pw"})
    assert "Not saved: the port must be a number" in r.text
    assert smtp_sandbox.read_text() == ""


@pytest.mark.parametrize("data,key", [
    ({"channel": "slack", "slack_webhook": "hooks.slack.com/services/x"}, "SLACK_WEBHOOK_URL"),
    ({"channel": "ntfy", "ntfy_url": "ntfy.sh/topic"}, "NTFY_URL"),
])
def test_non_http_channel_urls_are_rejected(client, env_sandbox, data, key):
    import os
    r = client.post("/settings/notify", data=data)
    assert "Not saved" in r.text and "http(s)" in r.text
    assert not os.getenv(key)


def test_apify_key_failure_is_plain(client, env_sandbox, monkeypatch):
    import dashboard.routers.settings as settings_module

    class _Rejecting:
        def __init__(self, api_key=None):
            pass

        def check_account(self):
            return {"ok": False, "detail": 'HTTP 401 from Apify: {"error":{"type":"x"}}'}

    monkeypatch.setattr(settings_module, "ApifyClient", _Rejecting)
    r = client.post("/settings/apikey", data={"apikey": "apify_api_bad"})
    assert "Apify rejected this key (HTTP 401); it was saved anyway" in r.text
    assert "&quot;error&quot;" not in r.text and '{"error"' not in r.text


# --------------------------------------------------------------------- #
# The AI dialog marks the provider in use; saving only takes the chosen
# provider's fields and never saves a provider that has nothing to run on.
# --------------------------------------------------------------------- #
def _in_use(text: str) -> str:
    import re
    found = re.findall(r"data-provider='(\w+)'><span class='choice-title'><b>[^<]+</b>"
                       r"<span class='tag'>In use</span>", text)
    return found[0] if found else ""


@pytest.mark.parametrize("env,expected", [
    ({"LLM_PROVIDER": "openai", "OPENAI_BASE_URL": "https://opencode.ai/zen/v1",
      "OPENAI_API_KEY": "k", "OPENAI_MODEL": "m"}, "openai"),
    # Selected but failing (custom base URL without a model): still openai.
    ({"LLM_PROVIDER": "openai", "OPENAI_BASE_URL": "https://opencode.ai/zen/v1"}, "openai"),
    ({"OPENAI_BASE_URL": "https://opencode.ai/zen/v1", "OPENAI_MODEL": "m"}, "openai"),
    # Unknown LLM_PROVIDER: mark what is actually configured.
    ({"LLM_PROVIDER": "opencode", "OPENAI_API_KEY": "k"}, "openai"),
    ({"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "g"}, "gemini"),
    ({}, ""),
])
def test_ai_dialog_marks_the_provider_in_use(client, env_sandbox, monkeypatch, env, expected):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    text = client.get("/ui/integration/ai", headers={"X-SM-Fragment": "1"}).text
    assert _in_use(text) == expected
    # Step 1 never shows fields: only the chosen provider's appear in step 2.
    assert "name='openai_key'" not in text and "name='apikey'" not in text


def _configure_openai(monkeypatch, provider="openai"):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://opencode.ai/zen/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "old-key")
    monkeypatch.setenv("OPENAI_MODEL", "big-pickle")


def test_updating_only_the_openai_key_keeps_provider_openai(client, env_sandbox, monkeypatch):
    import os
    _configure_openai(monkeypatch)
    dialog = client.get("/ui/integration/ai?provider=openai", headers={"X-SM-Fragment": "1"}).text
    assert "value='https://opencode.ai/zen/v1'" in dialog and "value='big-pickle'" in dialog
    r = client.post("/settings/ai", data={
        "provider": "openai", "preset": "custom", "openai_base_url": "https://opencode.ai/zen/v1",
        "openai_key": "new-key", "openai_model": "big-pickle"})
    assert r.status_code == 200
    assert os.getenv("LLM_PROVIDER") == "openai"
    assert os.getenv("OPENAI_API_KEY") == "new-key"
    assert not os.getenv("GEMINI_API_KEY")
    assert "LLM_PROVIDER=gemini" not in (env_sandbox.read_text() if env_sandbox.exists() else "")


def test_the_gemini_dialog_never_saves_openai_fields(client, env_sandbox, monkeypatch):
    """The owner's bug: Gemini chosen while only an OpenAI-compatible key was
    typed. The Gemini dialog has no OpenAI fields; a crafted post with them
    is refused and nothing is saved."""
    import os
    _configure_openai(monkeypatch, provider="openai")
    r = client.post("/settings/ai", data={
        "provider": "gemini", "apikey": "", "openai_key": "new-key"})
    assert "Not saved: Paste your Gemini API key." in r.text
    assert os.getenv("LLM_PROVIDER") == "openai" and os.getenv("OPENAI_API_KEY") == "old-key"


def test_choosing_gemini_without_a_key_is_refused(client, env_sandbox, monkeypatch):
    import os
    _configure_openai(monkeypatch)
    r = client.post("/settings/ai", data={"provider": "gemini", "apikey": "", "model": ""})
    assert "Not saved: Paste your Gemini API key." in r.text
    assert os.getenv("LLM_PROVIDER") == "openai"
    assert not env_sandbox.exists() or env_sandbox.read_text() == ""


def test_choosing_gemini_with_a_key_switches(client, env_sandbox, monkeypatch):
    import os
    _configure_openai(monkeypatch)
    client.post("/settings/ai", data={"provider": "gemini", "apikey": "gm-key", "model": ""})
    assert os.getenv("LLM_PROVIDER") == "gemini"
    assert os.getenv("GEMINI_API_KEY") == "gm-key"
