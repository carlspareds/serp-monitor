"""Tests for src/dashboard/admin.py: the generic .env writer used by every
settings/wizard endpoint that persists a credential."""
import os
import stat

from dashboard import admin


def test_set_env_creates_file_with_owner_only_permissions(tmp_path):
    env_path = str(tmp_path / ".env")

    admin.set_env(env_path, "APIFY_API_KEY", "sk-secret")

    mode = stat.S_IMODE(os.stat(env_path).st_mode)
    assert mode == 0o600


def test_set_env_fixes_permissions_on_an_existing_file(tmp_path):
    """A .env that pre-dates this fix (or was copied from .env.example with
    default umask permissions) gets tightened on the next write, not just
    on creation."""
    env_path = tmp_path / ".env"
    env_path.write_text("APIFY_API_KEY=old\n")
    env_path.chmod(0o644)

    admin.set_env(str(env_path), "APIFY_API_KEY", "sk-new-secret")

    mode = stat.S_IMODE(os.stat(env_path).st_mode)
    assert mode == 0o600


def test_set_env_updates_existing_key_in_place(tmp_path):
    env_path = str(tmp_path / ".env")
    admin.set_env(env_path, "APIFY_API_KEY", "first")

    admin.set_env(env_path, "APIFY_API_KEY", "second")

    with open(env_path) as f:
        lines = f.read().splitlines()
    assert lines.count("APIFY_API_KEY=second") == 1
    assert "APIFY_API_KEY=first" not in lines


def test_set_env_also_updates_process_environ(tmp_path, monkeypatch):
    monkeypatch.delenv("SOME_TEST_KEY", raising=False)
    env_path = str(tmp_path / ".env")

    admin.set_env(env_path, "SOME_TEST_KEY", "value")

    assert os.environ["SOME_TEST_KEY"] == "value"


def test_mask_shows_only_the_last_four_characters():
    assert admin.mask("sk-abcdefgh1234") == "•" * 11 + "1234"


def test_mask_of_empty_value_says_not_configured():
    assert admin.mask("") == "(not configured)"
