"""Settings redesign: guided integration dialogs (only the chosen fields),
in-dialog tests of unsaved values, Telegram chat discovery, saved secrets
(masked, Replace / Remove), alert routing grid, scheduling dialog, API key
shown once, readable audit log, and the wizard reusing the same dialogs.

No real network: requests/smtplib are replaced by fakes.
"""
import json
import os

import pytest
import requests

FRAG = {"X-SM-Fragment": "1"}

_ENV = ("LLM_PROVIDER", "GEMINI_API_KEY", "GEMINI_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY",
        "OPENAI_MODEL", "TELEGRAM_BOT_TOKEN", "SLACK_WEBHOOK_URL", "NTFY_URL", "NTFY_TOPIC",
        "APIFY_API_KEY", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM",
        "ALERT_EMAIL")

SECRETS = {
    "APIFY_API_KEY": "apify_api_SECRET111",
    "GEMINI_API_KEY": "AIzaSECRET222",
    "OPENAI_API_KEY": "sk-SECRET333",
    "TELEGRAM_BOT_TOKEN": "123456:SECRET444",
    "SLACK_WEBHOOK_URL": "https://hooks.slack.com/services/T/B/SECRET555",
    "NTFY_TOPIC": "serp-SECRET666",
    "SMTP_PASS": "pw-SECRET777",
}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ENV_FILE", str(tmp_path / ".env"))
    for name in _ENV:
        monkeypatch.setenv(name, "")
    (tmp_path / ".env").write_text("")
    return tmp_path / ".env"


def _modal(client, name, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    r = client.get(f"/ui/integration/{name}" + (f"?{q}" if q else ""), headers=FRAG)
    assert r.status_code == 200, r.text[:300]
    return r.text


def _section(text: str, key: str, nxt: str) -> str:
    return text[text.index(f"<section id='{key}'"):text.index(f"<section id='{nxt}'")]


# --------------------------------------------------------------------- #
# Page structure
# --------------------------------------------------------------------- #
def test_every_section_has_the_header_pattern(client, env):
    text = client.get("/settings").text
    assert text.count("class='sec-head'") >= 10
    assert "data-settings-find" in text and "class='ov-strip'" in text
    # Integrations are rows with a status pill and Set up / Change, not inline forms.
    for name in ("apify", "ai", "email", "telegram", "slack", "ntfy"):
        assert f"/ui/integration/{name}?next=" in text, name
    assert "name='openai_key'" not in text and "name='apikey'" not in text
    assert "name='telegram_token'" not in text and "name='password'" not in text


# --------------------------------------------------------------------- #
# Dialogs render only the relevant fields
# --------------------------------------------------------------------- #
def test_ai_dialog_starts_with_two_provider_cards_and_no_fields(client, env):
    html = _modal(client, "ai")
    assert html.count("class='choice'") == 2
    assert "provider=gemini" in html and "provider=openai" in html
    for field in ("name='apikey'", "name='openai_key'", "name='openai_base_url'", "name='model'"):
        assert field not in html


def test_ai_dialog_gemini_shows_only_gemini_fields(client, env):
    html = _modal(client, "ai", provider="gemini")
    assert "name='apikey'" in html and "name='model'" in html
    assert "aistudio.google.com/apikey" in html
    for field in ("openai_key", "openai_base_url", "openai_model", "name='preset'"):
        assert field not in html
    assert "/settings/integration/ai/test" in html


def test_ai_dialog_openai_shows_presets_and_only_openai_fields(client, env):
    html = _modal(client, "ai", provider="openai")
    for field in ("name='openai_base_url'", "name='openai_key'", "name='openai_model'"):
        assert field in html
    assert "name='apikey'" not in html and "name='model'" not in html
    for preset in ("openai", "openrouter", "opencode", "ollama", "lmstudio", "custom"):
        assert f"name='preset' value='{preset}'" in html
    assert "https://opencode.ai/zen/go/v1" in html and "http://localhost:11434/v1" in html
    # Picking a preset from the URL preselects it and fills its base URL.
    html = _modal(client, "ai", provider="openai", preset="ollama")
    assert "value='ollama' checked" in html and "value='http://localhost:11434/v1'" in html
    assert "API key (not needed)" in html


def test_apify_dialog(client, env):
    html = _modal(client, "apify")
    assert "console.apify.com/account/integrations" in html and "name='apikey'" in html
    assert "name='budget'" not in html


def test_email_dialog_has_presets_and_no_other_channel(client, env):
    html = _modal(client, "email")
    for value in ("gmail", "outlook", "custom"):
        assert f"name='preset' value='{value}'" in html
    assert "app password" in html and "smtp.office365.com" in html
    for field in ("name='host'", "name='port'", "name='user'", "name='password'",
                  "name='sender'", "name='alert_to'"):
        assert field in html
    assert "telegram" not in html.lower() and "webhook" not in html.lower()


def test_telegram_dialog_steps(client, env):
    html = _modal(client, "telegram")
    assert "https://t.me/BotFather" in html and "Find my chat id" in html
    assert "/settings/integration/telegram/chats" in html
    assert "name='telegram_token'" in html and "name='telegram_chat_id'" in html
    assert "slack" not in html.lower()


def test_slack_and_ntfy_dialogs(client, env):
    html = _modal(client, "slack")
    assert "api.slack.com/messaging/webhooks" in html and "name='slack_webhook'" in html
    assert "ntfy" not in html
    html = _modal(client, "ntfy")
    assert "name='ntfy_mode' value='topic'" in html and "name='ntfy_mode' value='url'" in html
    assert "Anyone who knows a public topic name can read" in html
    assert "ntfy app" in html and "data-sm-fill='#nt-topic'" in html


def test_unknown_integration_is_404(client, env):
    assert client.get("/ui/integration/fax", headers=FRAG).status_code == 404


# --------------------------------------------------------------------- #
# Saved secrets: masked, never echoed
# --------------------------------------------------------------------- #
def test_saved_secrets_are_never_in_the_html(client, seeded_db, env, monkeypatch):
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "u@example.com")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    seeded_db.set_setting("telegram_chat_id", "42")
    pages = [client.get("/settings").text]
    pages += [_modal(client, n) for n in ("apify", "email", "telegram", "slack", "ntfy")]
    pages += [_modal(client, "ai", provider=p) for p in ("gemini", "openai")]
    for html in pages:
        for value in SECRETS.values():
            assert value not in html
    email = _modal(client, "email")
    assert "data-state='saved'" in email and "data-secret-replace" in email
    assert "form='rm-smtp-pass'" in email and "data-sm-confirm=" in email
    assert "action='/settings/secret/remove'" in email


def test_secret_shows_when_it_was_saved(client, seeded_db, env):
    r = client.post("/settings/notify", data={
        "channel": "slack", "slack_webhook": SECRETS["SLACK_WEBHOOK_URL"]})
    assert r.status_code == 200
    html = _modal(client, "slack")
    assert "(updated " in html and "Saved" in html and SECRETS["SLACK_WEBHOOK_URL"] not in html


def test_replace_keeps_or_replaces_the_secret(client, env, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "old-gemini-key")
    r = client.post("/settings/ai", data={"provider": "gemini", "apikey": "", "model": ""})
    assert "No changes" in r.text and os.getenv("GEMINI_API_KEY") == "old-gemini-key"
    client.post("/settings/ai", data={"provider": "gemini", "apikey": "new-gemini-key"})
    assert os.getenv("GEMINI_API_KEY") == "new-gemini-key"


@pytest.mark.parametrize("key,cleared", [
    ("SLACK_WEBHOOK_URL", ("SLACK_WEBHOOK_URL",)),
    ("SMTP_PASS", ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "ALERT_EMAIL")),
    ("TELEGRAM_BOT_TOKEN", ("TELEGRAM_BOT_TOKEN",)),
])
def test_remove_clears_the_secret_and_what_needs_it(client, seeded_db, env, monkeypatch, key,
                                                    cleared):
    for name, value in (("SLACK_WEBHOOK_URL", SECRETS["SLACK_WEBHOOK_URL"]),
                        ("SMTP_HOST", "smtp.example.com"), ("SMTP_USER", "u"),
                        ("SMTP_PASS", SECRETS["SMTP_PASS"]), ("ALERT_EMAIL", "a@example.com"),
                        ("TELEGRAM_BOT_TOKEN", SECRETS["TELEGRAM_BOT_TOKEN"])):
        monkeypatch.setenv(name, value)
    seeded_db.set_setting("telegram_chat_id", "42")
    r = client.post("/settings/secret/remove", data={"key": key, "next": "/settings#ai"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/settings#ai"
    for name in cleared:
        assert not os.getenv(name), name
    if key == "TELEGRAM_BOT_TOKEN":
        assert seeded_db.get_setting("telegram_chat_id", "") == ""
    row = seeded_db.list_audit(action="settings_secret_remove")[0]
    for value in SECRETS.values():
        assert value not in row["params_json"]


def test_remove_rejects_unknown_keys(client, env, monkeypatch):
    monkeypatch.setenv("AUTH_PASS", "")
    r = client.post("/settings/secret/remove", data={"key": "AUTH_PASS"})
    assert "Not removed: unknown setting" in r.text


# --------------------------------------------------------------------- #
# In-dialog tests of unsaved values
# --------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_ai_test_uses_the_typed_values_and_saving_them_keeps_the_result(client, seeded_db, env,
                                                                       monkeypatch):
    seen = []

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        seen.append((url, (headers or {}).get("Authorization")))
        return _Resp(200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    monkeypatch.setattr(requests, "post", fake_post)
    form = {"provider": "openai", "preset": "ollama", "openai_base_url": "", "openai_key": "",
            "openai_model": "llama3.1"}
    r = client.post("/settings/integration/ai/test", data=form, headers=FRAG)
    assert "Test passed" in r.text and "llama3.1" in r.text
    assert seen[0][0] == "http://localhost:11434/v1/chat/completions" and seen[0][1] is None
    # Nothing was saved by the test.
    assert not os.getenv("LLM_PROVIDER") and not os.getenv("OPENAI_MODEL")
    r = client.post("/settings/ai", data=form)
    assert os.getenv("LLM_PROVIDER") == "openai"
    assert os.getenv("OPENAI_BASE_URL") == "http://localhost:11434/v1"
    assert "Test passed" in r.text
    ai = _section(client.get("/settings").text, "ai", "notifications")
    assert "Working" in ai


def test_ai_test_explains_what_is_missing_without_calling(client, env, monkeypatch):
    r = client.post("/settings/integration/ai/test", headers=FRAG,
                    data={"provider": "openai", "preset": "custom",
                          "openai_base_url": "https://ai.example.com/v1", "openai_model": ""})
    assert "Test failed" in r.text and "Enter a model name" in r.text
    r = client.post("/settings/integration/ai/test", headers=FRAG,
                    data={"provider": "gemini", "apikey": ""})
    assert "Paste your Gemini API key" in r.text


def test_ai_save_refuses_an_unusable_config_inside_the_dialog(client, env):
    r = client.post("/settings/ai", headers=FRAG,
                    data={"provider": "openai", "preset": "openai", "openai_key": ""})
    assert r.status_code == 400
    assert "Not saved: OpenAI needs an API key" in r.text
    assert "name='openai_key'" in r.text and "name='apikey'" not in r.text
    assert not os.getenv("LLM_PROVIDER")


def test_email_test_sends_with_the_typed_values(client, seeded_db, env, monkeypatch):
    from notify import email_sender
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, context=None, timeout=None):
            sent["host"], sent["port"] = host, port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, password):
            sent["login"] = (user, password)

        def send_message(self, msg):
            sent["to"] = msg["To"]

    monkeypatch.setattr(email_sender.smtplib, "SMTP_SSL", FakeSMTP)
    r = client.post("/settings/integration/email/test", headers=FRAG, data={
        "preset": "gmail", "host": "smtp.gmail.com", "port": "465", "user": "me@gmail.com",
        "password": "app-pass-1", "alert_to": "ops@example.com"})
    assert "Test passed" in r.text and "app-pass-1" not in r.text
    assert sent == {"host": "smtp.gmail.com", "port": 465, "login": ("me@gmail.com", "app-pass-1"),
                    "to": "ops@example.com"}
    assert not os.getenv("SMTP_HOST")
    r = client.post("/settings/integration/email/test", headers=FRAG, data={"host": "smtp.x"})
    assert "Fill in the user and the password first" in r.text


def test_email_save_rejects_partial_configs_in_plain_words(client, env):
    r = client.post("/settings/email", headers=FRAG,
                    data={"host": "smtp.example.com", "port": "587", "alert_to": "a@example.com"})
    assert r.status_code == 400
    assert "Not saved: fill in the user and the password." in r.text
    assert not os.getenv("SMTP_HOST")


def test_slack_test_posts_to_the_pasted_webhook(client, env, monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post",
                        lambda url, **k: calls.append(url) or _Resp(200, text="ok"))
    r = client.post("/settings/integration/slack/test", headers=FRAG,
                    data={"slack_webhook": "https://hooks.slack.com/services/A/B/C"})
    assert "Test passed" in r.text and calls == ["https://hooks.slack.com/services/A/B/C"]
    assert "hooks.slack.com/services/A/B/C" not in r.text


def test_test_without_js_redirects_with_a_toast(client, env):
    r = client.post("/settings/integration/slack/test", data={"next": "/setup?step=integrations"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup?step=integrations"


# --------------------------------------------------------------------- #
# Telegram: Find my chat id
# --------------------------------------------------------------------- #
def _updates(*chats):
    return {"ok": True, "result": [{"update_id": i, "message": {"chat": c}}
                                   for i, c in enumerate(chats)]}


def test_find_chat_id_lists_recent_chats(client, env, monkeypatch):
    token = "999:SECRETtoken"
    urls = []

    def fake_get(url, timeout=None, params=None, **kw):
        urls.append(url)
        return _Resp(200, _updates({"id": 111, "type": "private", "first_name": "Ana",
                                    "username": "ana"},
                                   {"id": -100222, "type": "supergroup", "title": "Alerts team"},
                                   {"id": 111, "type": "private", "first_name": "Ana"}))

    monkeypatch.setattr(requests, "get", fake_get)
    r = client.post("/settings/integration/telegram/chats", headers=FRAG,
                    data={"telegram_token": token})
    assert r.status_code == 200
    assert urls == [f"https://api.telegram.org/bot{token}/getUpdates"]
    assert "Alerts team" in r.text and "-100222" in r.text and "<b>Ana</b>" in r.text
    assert r.text.count("data-sm-fill='#tg-chat'") == 2
    assert token not in r.text


@pytest.mark.parametrize("resp,needle", [
    (_Resp(409, {"ok": False, "description": "Conflict: can't use getUpdates while webhook is active"}),
     "webhook"),
    (_Resp(401, {"ok": False, "description": "Unauthorized"}), "rejected the bot token"),
    (_Resp(200, {"ok": True, "result": []}), "press Start"),
])
def test_find_chat_id_failures_are_plain(client, env, monkeypatch, resp, needle):
    monkeypatch.setattr(requests, "get", lambda *a, **k: resp)
    r = client.post("/settings/integration/telegram/chats", headers=FRAG,
                    data={"telegram_token": "1:tok"})
    assert needle in r.text and "{" not in r.text


def test_find_chat_id_never_leaks_the_token_on_network_errors(client, env, monkeypatch):
    token = "777:LEAKME"

    def boom(url, **k):
        raise requests.exceptions.ConnectionError(f"Max retries exceeded with url: {url}")

    monkeypatch.setattr(requests, "get", boom)
    r = client.post("/settings/integration/telegram/chats", headers=FRAG,
                    data={"telegram_token": token})
    assert "Couldn" in r.text and token not in r.text


def test_find_chat_id_needs_a_token(client, env):
    r = client.post("/settings/integration/telegram/chats", headers=FRAG, data={})
    assert "Paste the bot token first" in r.text


def test_telegram_save_needs_token_and_chat(client, seeded_db, env):
    r = client.post("/settings/notify", headers=FRAG,
                    data={"channel": "telegram", "telegram_token": "1:abc", "telegram_chat_id": ""})
    assert r.status_code == 400 and "Find my chat id" in r.text
    assert not os.getenv("TELEGRAM_BOT_TOKEN")
    r = client.post("/settings/notify", data={"channel": "telegram", "telegram_token": "1:abc",
                                              "telegram_chat_id": "-100222"})
    assert "Telegram saved." in r.text and os.getenv("TELEGRAM_BOT_TOKEN") == "1:abc"


def test_ntfy_mode_switch_clears_the_other_destination(client, env, monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/old")
    client.post("/settings/notify", data={"channel": "ntfy", "ntfy_mode": "topic",
                                          "ntfy_topic": "serp-abcdef"})
    assert os.getenv("NTFY_TOPIC") == "serp-abcdef" and not os.getenv("NTFY_URL")


# --------------------------------------------------------------------- #
# Alert routing grid
# --------------------------------------------------------------------- #
def test_routing_grid_only_offers_set_up_channels(client, seeded_db, env):
    text = client.get("/settings").text
    grid = text[text.index("id='routing-form'"):text.index("</form>", text.index("id='routing-form'"))]
    assert "name='critical' value='slack' disabled" in grid
    assert "name='warning' value='none'" in grid and "Daily digest" in grid
    assert "harm score" in grid


def test_routing_grid_save(client, seeded_db, env, monkeypatch):
    r = client.post("/settings/alerts", data={"critical": "telegram", "warning": "slack",
                                              "info": "digest", "sev": "5"})
    assert "Not saved: set up Slack first" in r.text
    assert seeded_db.get_routing()["warning"] != "slack"
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/x")
    r = client.post("/settings/alerts", data={"critical": seeded_db.get_routing()["critical"],
                                              "warning": "slack", "info": "none", "sev": "7"})
    assert "Saved: Warning alerts go to Slack" in r.text and "harm score of 7" in r.text
    assert seeded_db.get_routing()["warning"] == "slack"
    assert seeded_db.get_setting("ALERT_SEVERITY_THRESHOLD", "") == "7"


# --------------------------------------------------------------------- #
# Scheduling
# --------------------------------------------------------------------- #
def test_scheduling_is_described_in_plain_words(client, monkeypatch):
    text = client.get("/settings").text
    card = _section(text, "scheduling", "users")
    assert ("<b>Weekly report</b>: every Monday at 08:00 UTC — runs a search for each tracked "
            "subject (not demo) and emails the workbook.") in card


def test_schedule_dialog_and_save(client, seeded_db, monkeypatch):
    import dashboard.routers.settings as settings_module
    saved = {}
    monkeypatch.setattr(settings_module.scheduler_core, "set_job_cron",
                        lambda name, expr, enabled=True: saved.update(name=name, expr=expr))
    html = client.get("/ui/settings/schedule/weekly_report", headers=FRAG).text
    assert "name='frequency'" in html and "data-show-when='frequency=weekly'" in html
    assert "<option value='8' selected>08:00</option>" in html
    assert client.get("/ui/settings/schedule/deadman_check", headers=FRAG).status_code == 404
    r = client.post("/settings/cron", headers=FRAG, data={
        "name": "weekly_report", "frequency": "weekly", "weekday": "3", "hour": "9",
        "next": "/settings#scheduling"})
    assert r.headers["X-SM-Redirect"] == "/settings#scheduling"
    assert saved == {"name": "weekly_report", "expr": "0 9 * * 3"}


def test_describe_cron():
    from dashboard.integrations import describe_cron
    assert describe_cron("0 8 * * 1") == "every Monday at 08:00 UTC"
    assert describe_cron("0 9 * * *") == "every day at 09:00 UTC"
    assert describe_cron("0 4 * * 0") == "every Sunday at 04:00 UTC"
    assert describe_cron("*/15 * * * *") == "every 15 minutes"
    assert describe_cron("0 6 1 * *") == "on the 1st of every month at 06:00 UTC"


# --------------------------------------------------------------------- #
# API keys, users, audit, backup, projects
# --------------------------------------------------------------------- #
def test_api_key_dialog_and_one_time_display(client, seeded_db):
    html = client.get("/ui/settings/api-key/new", headers=FRAG).text
    assert "name='scope' value='read'" in html and "name='subject_id'" in html
    r = client.post("/settings/api-keys", headers=FRAG,
                    data={"name": "n8n", "scope": "read", "subject_id": ""})
    assert r.status_code == 200 and "won't be shown again" in r.text
    assert "data-copy='#new-api-key'" in r.text and "data-sm-reload-on-close" in r.text
    import re
    raw = re.search(r"<code id='new-api-key'>([^<]+)</code>", r.text).group(1)
    assert len(raw) >= 32
    assert raw not in client.get("/settings").text
    assert raw not in json.dumps(seeded_db.list_api_keys())


def test_users_section_is_a_table_with_one_primary_action(admin_client, seeded_db):
    client = admin_client
    seeded_db.add_user("maria", "maria-password", role="operator")
    users = _section(client.get("/settings").text, "users", "api")
    assert "<th>User</th>" in users and "maria" in users and "Operator" in users
    assert "/ui/settings/user/new" in users and users.count("btn primary") == 1
    html = client.get("/ui/settings/user/new", headers=FRAG).text
    assert "name='role'" in html and "name='password'" in html


def test_audit_is_readable_and_filterable(client, seeded_db):
    seeded_db.add_audit(actor_type="user", actor_id="maria", action="settings_ai_save",
                        params={"provider": "gemini", "apikey_changed": True})
    seeded_db.add_audit(actor_type="user", actor_id="maria", action="login_ok", params={})
    audit = _section(client.get("/settings").text, "audit", "backup")
    assert "Changed the AI provider" in audit and "apikey changed: yes" in audit
    assert "<option value='settings_ai_save'>Changed the AI provider</option>" in audit
    filtered = _section(client.get("/settings?audit_action=login_ok").text, "audit", "backup")
    assert "Signed in" in filtered and "Changed the AI provider</td>" not in filtered


def test_backup_explains_each_download(client):
    backup = client.get("/settings").text.split("<section id='backup'")[1]
    assert "Full backup (.db)" in backup and "Not\n          included" in backup
    assert "Results of the active subject (.csv)" in backup


def test_projects_mark_the_active_subject_and_subject_dialog_is_plain(client, seeded_db):
    projects = _section(client.get("/settings").text, "projects", "search")
    assert "Active now" in projects and "is-current" in projects
    html = client.get("/ui/subject/new", headers=FRAG).text
    assert "United States" in html and "Canada" in html
    assert "Short description for the AI" in html and "e.g. J. Doe" in html
    assert "name='type' value='person' checked" in html


# --------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------- #
def test_operators_cannot_open_integration_dialogs(client, seeded_db, monkeypatch):
    monkeypatch.setenv("AUTH_USER", "envadmin")
    monkeypatch.setenv("AUTH_PASS", "env-password")
    seeded_db.add_user("op", "op-password", role="operator")
    resp = client.post("/login", data={"username": "op", "password": "op-password"},
                       follow_redirects=False)
    client.cookies.set("sid", resp.cookies["sid"])
    assert client.get("/ui/integration/ai", headers=FRAG).status_code == 403
    assert client.post("/settings/integration/slack/test", data={}).status_code == 403
    assert client.post("/settings/secret/remove", data={"key": "APIFY_API_KEY"}).status_code == 403
