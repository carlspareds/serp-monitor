"""Serper.dev on a genuinely fresh install (wizard not completed) and the
price setting's guard. Kept apart from test_serp_provider.py because the
fixture here builds its own empty database, like test_wizard.py does."""
import os

import pytest
from conftest import _reload_app

from database.models import Database
from serp.serper_provider import DEFAULT_USD_PER_1K, usd_per_1k_setting

_ENV = ("APIFY_API_KEY", "SERPER_API_KEY", "SERP_PROVIDER", "SERPER_USD_PER_1K")


@pytest.fixture
def fresh_client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "fresh.db")
    Database(db_path=db_path)  # just runs _init_db, no seeded data
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")
    monkeypatch.setenv("ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.delenv("AUTH_USER", raising=False)
    monkeypatch.delenv("AUTH_PASS", raising=False)
    for name in _ENV:
        monkeypatch.setenv(name, "")
    (tmp_path / ".env").write_text("")
    from fastapi.testclient import TestClient
    return TestClient(_reload_app().app, follow_redirects=False)


def test_fresh_install_wizard_saves_a_serper_key(fresh_client, monkeypatch):
    """The wizard's Serper dialog posts to /settings/serper-key: the
    first-run middleware must let it through (as it does /settings/apikey)
    instead of bouncing it back to /setup unsaved."""
    import dashboard.routers.settings as settings_module

    monkeypatch.setattr(settings_module, "_verify_serper", lambda key: (True, "key verified"))
    r = fresh_client.post("/settings/serper-key", data={"serper_key": "s-1", "next": "/setup"})
    assert r.status_code == 303 and os.getenv("SERPER_API_KEY") == "s-1"
    r = fresh_client.post("/settings/serp-test", data={"provider": "serper", "next": "/setup"})
    assert r.status_code == 303 and r.headers["location"].startswith("/setup")


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan"])
def test_non_finite_serper_rate_falls_back_to_the_default(raw):
    assert usd_per_1k_setting({"SERPER_USD_PER_1K": raw}) == DEFAULT_USD_PER_1K
