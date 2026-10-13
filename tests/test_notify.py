"""Tests for item 7.4: notification channels (telegram/slack/ntfy) +
notify_log + send_via().

TDD, no network: requests.post is monkeypatched in each channel module --
a real URL is never called, not even with fake webhook/token.
"""
from conftest import make_db as _db

from notify import base as notify_base
from notify import ntfy, send_via, slack, telegram


class _FakeResponse:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


# --------------------------------------------------------------------- #
# telegram
# --------------------------------------------------------------------- #
def test_telegram_not_configured_without_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert telegram.is_configured(chat_id="123") is False


def test_telegram_not_configured_without_chat_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "faketoken")
    assert telegram.is_configured(chat_id="") is False


def test_telegram_configured_with_token_and_chat_id(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "faketoken")
    assert telegram.is_configured(chat_id="999") is True


def test_telegram_send_posts_correct_url_and_escaped_payload(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "faketoken")
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse(200, "ok")

    monkeypatch.setattr(notify_base.requests, "post", fake_post)
    ok, _detail = telegram.send("hola <b>mundo</b>", chat_id="999")

    assert ok is True
    assert captured["url"] == "https://api.telegram.org/botfaketoken/sendMessage"
    assert captured["json"]["chat_id"] == "999"
    assert captured["json"]["parse_mode"] == "HTML"
    assert "&lt;b&gt;" in captured["json"]["text"]  # escaped, not interpreted as real HTML
    assert captured["timeout"] == 15


def test_telegram_send_without_config_returns_false_without_request(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    called = {"n": 0}
    monkeypatch.setattr(notify_base.requests, "post", lambda *a, **k: called.__setitem__("n", called["n"] + 1))

    ok, detail = telegram.send("hola", chat_id="999")

    assert ok is False
    assert detail == "not configured"
    assert called["n"] == 0


def test_telegram_send_http_failure_returns_false_with_detail(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "faketoken")
    monkeypatch.setattr(notify_base.requests, "post",
                        lambda *a, **k: _FakeResponse(403, "Forbidden: bot blocked"))

    ok, detail = telegram.send("hola", chat_id="999")

    assert ok is False
    assert "403" in detail


def test_telegram_send_network_exception_is_caught(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "faketoken")

    def boom(*a, **k):
        raise ConnectionError("dns fail")

    monkeypatch.setattr(notify_base.requests, "post", boom)
    ok, detail = telegram.send("hola", chat_id="999")

    assert ok is False
    assert "dns fail" in detail


# --------------------------------------------------------------------- #
# slack
# --------------------------------------------------------------------- #
def test_slack_not_configured_without_webhook(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert slack.is_configured() is False


def test_slack_send_posts_json_text_payload(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/T000/B000/xxx")
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse(200, "ok")

    monkeypatch.setattr(notify_base.requests, "post", fake_post)
    ok, _detail = slack.send("test message")

    assert ok is True
    assert captured["url"] == "https://hooks.slack.example/T000/B000/xxx"
    assert captured["json"] == {"text": "test message"}
    assert captured["timeout"] == 15


def test_slack_send_without_config_returns_false_without_request(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    called = {"n": 0}
    monkeypatch.setattr(notify_base.requests, "post", lambda *a, **k: called.__setitem__("n", 1))

    ok, detail = slack.send("hola")

    assert ok is False and detail == "not configured"
    assert called["n"] == 0


def test_slack_send_http_failure_returns_false(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")
    monkeypatch.setattr(notify_base.requests, "post", lambda *a, **k: _FakeResponse(500, "internal_error"))

    ok, detail = slack.send("hola")

    assert ok is False
    assert "500" in detail


def test_slack_send_network_exception_is_caught(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.example/x")

    def boom(*a, **k):
        raise ConnectionError("dns fail")

    monkeypatch.setattr(notify_base.requests, "post", boom)
    ok, detail = slack.send("hola")

    assert ok is False
    assert "dns fail" in detail


# --------------------------------------------------------------------- #
# ntfy
# --------------------------------------------------------------------- #
def test_ntfy_not_configured_without_url_or_topic(monkeypatch):
    monkeypatch.delenv("NTFY_URL", raising=False)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    assert ntfy.is_configured() is False


def test_ntfy_uses_ntfy_sh_when_only_topic_set(monkeypatch):
    monkeypatch.delenv("NTFY_URL", raising=False)
    monkeypatch.setenv("NTFY_TOPIC", "mytopic")
    assert ntfy.is_configured() is True

    captured = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured["url"] = url
        captured["data"] = data
        captured["headers"] = headers
        return _FakeResponse(200, "ok")

    monkeypatch.setattr(notify_base.requests, "post", fake_post)
    ok, _detail = ntfy.send("mensaje")

    assert ok is True
    assert captured["url"] == "https://ntfy.sh/mytopic"
    assert captured["data"] == b"mensaje"


def test_ntfy_prefers_full_url_when_ntfy_url_set(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "https://ntfy.example.com/mytopic")
    monkeypatch.setenv("NTFY_TOPIC", "ignored")
    captured = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured["url"] = url
        return _FakeResponse(200, "ok")

    monkeypatch.setattr(notify_base.requests, "post", fake_post)
    ok, _detail = ntfy.send("mensaje")

    assert ok is True
    assert captured["url"] == "https://ntfy.example.com/mytopic"


def test_ntfy_send_without_config_returns_false_without_request(monkeypatch):
    monkeypatch.delenv("NTFY_URL", raising=False)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    called = {"n": 0}
    monkeypatch.setattr(notify_base.requests, "post", lambda *a, **k: called.__setitem__("n", 1))

    ok, detail = ntfy.send("hola")

    assert ok is False and detail == "not configured"
    assert called["n"] == 0


def test_ntfy_send_http_failure_returns_false(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "mytopic")
    monkeypatch.setattr(notify_base.requests, "post", lambda *a, **k: _FakeResponse(400, "bad request"))

    ok, detail = ntfy.send("hola")

    assert ok is False
    assert "400" in detail


def test_ntfy_send_network_exception_is_caught(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "mytopic")

    def boom(*a, **k):
        raise ConnectionError("dns fail")

    monkeypatch.setattr(notify_base.requests, "post", boom)
    ok, detail = ntfy.send("hola")

    assert ok is False
    assert "dns fail" in detail


def test_secret_redacted_from_exception_detail(monkeypatch):
    """Regression from review 7.4 HIGH: requests exceptions include the
    URL, and in Telegram/Slack the URL contains the secret. The detail that
    reaches notify_log/-/settings must carry it redacted."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:SECRETTOKEN")

    def boom(url, **k):
        raise ConnectionError(f"Max retries exceeded with url: {url}")

    monkeypatch.setattr(notify_base.requests, "post", boom)
    ok, detail = telegram.send("hola", chat_id="555")

    assert ok is False
    assert "SECRETTOKEN" not in detail
    assert "<secret>" in detail


# --------------------------------------------------------------------- #
# Database.add_notify_log / recent_notify_log
# --------------------------------------------------------------------- #
def test_add_notify_log_and_recent_order(tmp_path):
    db = _db(tmp_path)
    db.add_notify_log(channel="telegram", ok=True, error="")
    db.add_notify_log(channel="slack", ok=False, error="HTTP 500", alert_id=7)

    rows = db.recent_notify_log(limit=10)

    assert len(rows) == 2
    assert rows[0]["channel"] == "slack"  # most recent first
    assert rows[0]["ok"] == 0
    assert rows[0]["error"] == "HTTP 500"
    assert rows[0]["alert_id"] == 7
    assert rows[1]["channel"] == "telegram"
    assert rows[1]["ok"] == 1
    assert rows[1]["alert_id"] is None


def test_recent_notify_log_respects_limit(tmp_path):
    db = _db(tmp_path)
    for _ in range(15):
        db.add_notify_log(channel="ntfy", ok=True, error="")

    rows = db.recent_notify_log(limit=10)

    assert len(rows) == 10


# --------------------------------------------------------------------- #
# send_via
# --------------------------------------------------------------------- #
def test_send_via_unknown_channel_logs_and_returns_false(tmp_path):
    db = _db(tmp_path)

    ok, _detail = send_via(db, "carrier-pigeon", "hola")

    assert ok is False
    rows = db.recent_notify_log()
    assert rows[0]["channel"] == "carrier-pigeon"
    assert rows[0]["ok"] == 0


def test_send_via_not_configured_channel_does_not_log(tmp_path, monkeypatch):
    """An unconfigured channel returns feedback but does NOT consume history
    rows (review 7.4 #3: every test on an empty channel would push real
    sends out of the 'last 10')."""
    db = _db(tmp_path)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)

    ok, detail = send_via(db, "slack", "hola")

    assert ok is False
    assert detail == "not configured"
    assert db.recent_notify_log() == []


def test_send_via_telegram_reads_chat_id_from_db_setting(tmp_path, monkeypatch):
    db = _db(tmp_path)
    db.set_setting("telegram_chat_id", "555")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "faketoken")
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["chat_id"] = json["chat_id"]
        return _FakeResponse(200, "ok")

    monkeypatch.setattr(notify_base.requests, "post", fake_post)

    ok, _detail = send_via(db, "telegram", "hola")

    assert ok is True
    assert captured["chat_id"] == "555"
    rows = db.recent_notify_log()
    assert rows[0]["channel"] == "telegram"
    assert rows[0]["ok"] == 1


def test_send_via_success_logs_ok_row_with_alert_id(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setenv("NTFY_TOPIC", "mytopic")
    monkeypatch.setattr(notify_base.requests, "post", lambda *a, **k: _FakeResponse(200, "ok"))

    ok, _detail = send_via(db, "ntfy", "hola", alert_id=3)

    assert ok is True
    rows = db.recent_notify_log()
    assert rows[0]["alert_id"] == 3
    assert rows[0]["ok"] == 1
    assert not rows[0]["error"]


# --------------------------------------------------------------------- #
# UI /settings: channels card + per-channel test
# --------------------------------------------------------------------- #
def test_settings_page_shows_notify_channels_card(client):
    response = client.get("/settings/notifications")
    assert response.status_code == 200
    assert "Telegram" in response.text
    assert "Slack" in response.text
    assert "ntfy" in response.text


def test_notify_test_invalid_channel_returns_400(client):
    response = client.post("/settings/notify-test", data={"channel": "carrier-pigeon"},
                           follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/settings#notifications"
    assert "Unknown channel." in client.get("/settings").text


def test_notify_test_valid_channel_not_configured_shows_message(client):
    response = client.post("/settings/notify-test", data={"channel": "slack"})
    assert response.status_code == 200
    assert "not configured" in response.text.lower()


def test_notify_save_chat_id_persists_and_shows_in_settings(client):
    response = client.post("/settings/notify", data={"telegram_token": "1:abc",
                                                     "telegram_chat_id": "123456"})
    assert response.status_code == 200
    assert "chat 123456" in response.text
