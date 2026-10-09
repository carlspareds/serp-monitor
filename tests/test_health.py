"""Tests for item 9.2: public /health + structured JSON logging."""
import json
import logging

from dashboard import logging_setup


# --------------------------------------------------------------------- #
# /health
# --------------------------------------------------------------------- #
def test_health_public_and_shape(client, monkeypatch):
    """/health responds 200 without a session (public) with the full shape.
    The client fixture runs with DISABLE_SCHEDULER, so scheduler being
    off is NOT a problem (flag scheduler_disabled=True)."""
    monkeypatch.setenv("AUTH_USER", "u")
    monkeypatch.setenv("AUTH_PASS", "p")  # auth ACTIVE: /health must remain open

    r = client.get("/health")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded")
    assert isinstance(body["db_ping_ms"], (int, float))
    assert body["scheduler_running"] is False
    assert body["scheduler_disabled"] is True
    assert isinstance(body["jobs"], dict)
    assert isinstance(body["disk_low"], bool)
    assert "version" in body
    assert body["problems"] == []


def test_health_503_when_db_fails(client, monkeypatch):
    from dashboard import deps

    def boom():
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(deps.db, "_connect", boom)
    r = client.get("/health")

    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "error"
    assert any("db:" in p for p in body["problems"])


def test_health_503_when_scheduler_should_run_but_does_not(client, monkeypatch):
    monkeypatch.delenv("DISABLE_SCHEDULER", raising=False)
    r = client.get("/health")

    assert r.status_code == 503
    assert any("scheduler" in p for p in r.json()["problems"])


# --------------------------------------------------------------------- #
# Logging JSON
# --------------------------------------------------------------------- #
def test_json_formatter_produces_parseable_json_with_extras():
    fmt = logging_setup.JsonFormatter()
    record = logging.LogRecord("scheduler", logging.INFO, "core.py", 1,
                               "job_run", (), None)
    record.job = "weekly_report"
    record.dur_ms = 1234

    out = json.loads(fmt.format(record))

    assert out["level"] == "info"
    assert out["logger"] == "scheduler"
    assert out["msg"] == "job_run"
    assert out["job"] == "weekly_report"
    assert out["dur_ms"] == 1234
    assert "ts" in out


def test_setup_is_idempotent():
    logging_setup.setup()
    n = len(logging.getLogger().handlers)
    logging_setup.setup()
    assert len(logging.getLogger().handlers) == n


def test_request_log_middleware_emits_json_line(client, caplog):
    with caplog.at_level(logging.INFO, logger="http"):
        client.get("/health")

    recs = [r for r in caplog.records if r.name == "http"]
    assert recs, "the middleware must log the request"
    assert recs[-1].method == "GET"
    assert recs[-1].path == "/health"
    assert recs[-1].status == 200
    assert isinstance(recs[-1].dur_ms, int)
