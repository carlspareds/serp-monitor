"""Startup configuration validation (src/common/config_check.py,
scripts/check_config.py and the check in dashboard.main's lifespan).

Demo mode (no keys at all) must pass with warnings; only genuinely broken
configuration is fatal; secret values never appear in any output.
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from common import config_check as cc

_ROOT = Path(__file__).resolve().parent.parent
_CLI = _ROOT / "scripts" / "check_config.py"

SECRETS = {
    "APIFY_API_KEY": "apify_SECRET_aaaa1111",
    "AUTH_PASS": "pw_SECRET_bbbb2222",
    "SMTP_PASS": "smtp_SECRET_cccc3333",
    "TELEGRAM_BOT_TOKEN": "123456:tg_SECRET_dddd4444",
    "SLACK_WEBHOOK_URL": "hooks.slack.com/services/sl_SECRET_eeee5555",  # broken: no scheme
    "NTFY_URL": "ntfy_SECRET_ffff6666",  # broken: no scheme
    "GEMINI_API_KEY": "gm_SECRET_gggg7777",
    "OPENAI_API_KEY": "oa_SECRET_hhhh8888",
}


def _demo_env(tmp_path) -> dict:
    """Exactly what a fresh copy of .env.example gives (every key blank
    except the two shipped defaults), with the DB in tmp_path."""
    env = {k: "" for k in cc.ENV_VARS}
    env.update(SMTP_PORT="465", GEMINI_MODEL="gemini-2.5-flash-lite",
               DB_PATH=str(tmp_path / "data" / "t.db"))
    return env


def _errors(report) -> set[str]:
    return {f.key for f in report.errors}


def test_demo_no_keys_passes_with_warnings(tmp_path):
    report = cc.check_config(_demo_env(tmp_path), cwd=tmp_path)
    assert report.ok and not report.errors
    assert [f.key for f in report.warnings] == ["SERP provider"]
    assert "Demo mode" in report.warnings[0].message


def test_empty_environment_passes(tmp_path):
    report = cc.check_config({}, cwd=tmp_path)
    assert report.ok


def test_full_valid_config_has_no_errors_or_warnings(tmp_path):
    env = _demo_env(tmp_path)
    env.update(APIFY_API_KEY="k", AUTH_USER="admin", AUTH_PASS="a-long-passphrase",
               SMTP_HOST="smtp.x",
               SMTP_USER="u", SMTP_PASS="p", SMTP_PORT="587", ALERT_EMAIL="a@b.c",
               SLACK_WEBHOOK_URL="https://hooks.slack.com/x", NTFY_TOPIC="t",
               TELEGRAM_BOT_TOKEN="t", GEMINI_API_KEY="g",
               APP_ENCRYPTION_KEY="a-test-encryption-passphrase")
    report = cc.check_config(env, cwd=tmp_path)
    assert not report.errors and not report.warnings


@pytest.mark.parametrize("present,absent", [("AUTH_USER", "AUTH_PASS"),
                                            ("AUTH_PASS", "AUTH_USER")])
def test_auth_half_configured_is_fatal(tmp_path, present, absent):
    env = _demo_env(tmp_path)
    env[present] = "x-value"
    report = cc.check_config(env, cwd=tmp_path)
    assert _errors(report) == {"AUTH_USER/AUTH_PASS"}
    assert absent in report.errors[0].fix


def test_db_path_directory_cannot_be_created(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    env = _demo_env(tmp_path)
    env["DB_PATH"] = str(blocker / "sub" / "x.db")
    assert _errors(cc.check_config(env, cwd=tmp_path)) == {"DB_PATH"}


def test_db_path_is_a_directory(tmp_path):
    env = _demo_env(tmp_path)
    env["DB_PATH"] = str(tmp_path)
    assert _errors(cc.check_config(env, cwd=tmp_path)) == {"DB_PATH"}


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
def test_db_path_directory_not_writable(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        env = _demo_env(tmp_path)
        env["DB_PATH"] = str(ro / "x.db")
        assert _errors(cc.check_config(env, cwd=tmp_path)) == {"DB_PATH"}
    finally:
        ro.chmod(0o700)


def test_db_path_relative_resolves_against_cwd(tmp_path):
    env = _demo_env(tmp_path)
    env["DB_PATH"] = "data/x.db"
    report = cc.check_config(env, cwd=tmp_path)
    assert report.ok
    assert str(tmp_path / "data" / "x.db") in next(f.message for f in report.findings
                                                     if f.key == "DB_PATH")


@pytest.mark.parametrize("set_keys", [("SMTP_HOST",), ("SMTP_HOST", "SMTP_USER"),
                                      ("SMTP_USER", "SMTP_PASS"), ("SMTP_PASS",)])
def test_smtp_partial_is_fatal(tmp_path, set_keys):
    env = _demo_env(tmp_path)
    env.update({k: "v" for k in set_keys})
    report = cc.check_config(env, cwd=tmp_path)
    assert _errors(report) == {"SMTP"}
    for k in {"SMTP_HOST", "SMTP_USER", "SMTP_PASS"} - set(set_keys):
        assert k in report.errors[0].message


@pytest.mark.parametrize("port", ["abc", "0", "70000", "", "46 5"])
def test_smtp_bad_port_is_fatal_when_smtp_configured(tmp_path, port):
    env = _demo_env(tmp_path)
    env.update(SMTP_HOST="h", SMTP_USER="u", SMTP_PASS="p", SMTP_PORT=port)
    # Blank too: send_email does int(os.getenv("SMTP_PORT", "465")) -> int("").
    assert _errors(cc.check_config(env, cwd=tmp_path)) == {"SMTP_PORT"}


def test_smtp_port_unset_defaults_to_465(tmp_path):
    env = _demo_env(tmp_path)
    env.update(SMTP_HOST="h", SMTP_USER="u", SMTP_PASS="p")
    del env["SMTP_PORT"]
    assert cc.check_config(env, cwd=tmp_path).ok


def test_smtp_bad_port_ignored_when_smtp_off(tmp_path):
    env = _demo_env(tmp_path)
    env["SMTP_PORT"] = "abc"
    assert cc.check_config(env, cwd=tmp_path).ok


def test_alert_email_without_smtp_is_only_a_warning(tmp_path):
    env = _demo_env(tmp_path)
    env["ALERT_EMAIL"] = "a@b.c"
    report = cc.check_config(env, cwd=tmp_path)
    assert report.ok and "SMTP" in {f.key for f in report.warnings}


@pytest.mark.parametrize("key,value", [
    ("SLACK_WEBHOOK_URL", "hooks.slack.com/services/x"),
    ("SLACK_WEBHOOK_URL", "ftp://hooks.slack.com/x"),
    ("NTFY_URL", "ntfy.sh/topic"),
    ("NTFY_URL", "javascript:alert(1)"),
])
def test_webhook_urls_must_be_http(tmp_path, key, value):
    env = _demo_env(tmp_path)
    env[key] = value
    assert _errors(cc.check_config(env, cwd=tmp_path)) == {key}


def test_env_file_in_missing_directory_is_fatal(tmp_path):
    env = _demo_env(tmp_path)
    blocker = tmp_path / "file"
    blocker.write_text("")
    env["ENV_FILE"] = str(blocker / "x.env")
    assert _errors(cc.check_config(env, cwd=tmp_path)) == {"ENV_FILE"}


def test_env_file_is_directory_is_fatal(tmp_path):
    env = _demo_env(tmp_path)
    env["ENV_FILE"] = str(tmp_path)
    assert _errors(cc.check_config(env, cwd=tmp_path)) == {"ENV_FILE"}


def test_env_file_missing_but_creatable_is_a_warning(tmp_path):
    """The Settings writer (admin.set_env) creates the file on first save."""
    env = _demo_env(tmp_path)
    env["ENV_FILE"] = str(tmp_path / "new.env")
    report = cc.check_config(env, cwd=tmp_path)
    assert report.ok and "ENV_FILE" in {f.key for f in report.warnings}


def test_default_env_file_world_readable_warns(tmp_path):
    """ENV_FILE unset: the .env next to cwd is the one whose mode is checked."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("APIFY_API_KEY=x\n")
    dotenv.chmod(0o664)
    report = cc.check_config(_demo_env(tmp_path), cwd=tmp_path)
    found = [f for f in report.warnings if f.key == ".env"]
    assert len(found) == 1
    assert "mode 664" in found[0].message and str(dotenv) in found[0].fix


def test_default_env_file_is_cwd_not_repo(tmp_path, tmp_path_factory, monkeypatch):
    """No .env in cwd -> no .env finding, even when the repo's own .env (the
    module default) is world-readable (issue #31)."""
    elsewhere = tmp_path_factory.mktemp("repo") / ".env"
    elsewhere.write_text("APIFY_API_KEY=x\n")
    elsewhere.chmod(0o664)
    monkeypatch.setattr(cc, "DEFAULT_ENV_FILE", elsewhere)
    report = cc.check_config(_demo_env(tmp_path), cwd=tmp_path)
    assert ".env" not in {f.key for f in report.findings}


def test_env_file_relative_resolves_against_cwd(tmp_path):
    (tmp_path / "conf").mkdir()
    target = tmp_path / "conf" / "app.env"
    target.write_text("APIFY_API_KEY=x\n")
    target.chmod(0o664)
    env = _demo_env(tmp_path)
    env["ENV_FILE"] = "conf/app.env"
    report = cc.check_config(env, cwd=tmp_path)
    found = [f for f in report.warnings if f.key == "ENV_FILE"]
    assert len(found) == 1
    assert "mode 664" in found[0].message and str(target) in found[0].fix


def test_gemini_model_without_key_warns_only_if_non_default(tmp_path):
    env = _demo_env(tmp_path)
    assert "GEMINI_MODEL" not in {f.key for f in cc.check_config(env, cwd=tmp_path).warnings}
    env["GEMINI_MODEL"] = "gemini-2.5-pro"
    report = cc.check_config(env, cwd=tmp_path)
    assert report.ok and "GEMINI_MODEL" in {f.key for f in report.warnings}


def test_disable_scheduler_zero_warns(tmp_path):
    env = _demo_env(tmp_path)
    env["DISABLE_SCHEDULER"] = "0"
    report = cc.check_config(env, cwd=tmp_path)
    assert report.ok and "DISABLE_SCHEDULER" in {f.key for f in report.warnings}


def test_skip_config_check_bypasses_everything(tmp_path):
    env = _demo_env(tmp_path)
    env.update(AUTH_USER="only-user", SKIP_CONFIG_CHECK="1")
    report = cc.check_config(env, cwd=tmp_path)
    assert report.skipped and report.ok


def test_secrets_never_appear_in_any_output(tmp_path, caplog):
    env = _demo_env(tmp_path)
    env.update(SECRETS)
    env.update(SMTP_HOST="smtp.x", SMTP_PORT="bad")  # SMTP_USER missing -> error
    report = cc.check_config(env, cwd=tmp_path)
    assert {"SMTP", "SLACK_WEBHOOK_URL", "NTFY_URL"} <= _errors(report)
    outputs = [cc.format_report(report, env), cc.format_errors(report, env)]
    with caplog.at_level(logging.INFO), pytest.raises(cc.ConfigError) as exc:
        cc.validate_on_startup(logging.getLogger("config-test"), env)
    outputs += [str(exc.value), caplog.text]
    for out in outputs:
        for secret in SECRETS.values():
            assert secret not in out
            assert "SECRET" not in out


def test_report_lists_variables_as_set_unset(tmp_path):
    env = _demo_env(tmp_path)
    env["APIFY_API_KEY"] = SECRETS["APIFY_API_KEY"]
    out = cc.format_report(cc.check_config(env, cwd=tmp_path), env)
    assert "APIFY_API_KEY        set" in out
    assert "GEMINI_API_KEY       unset" in out


def test_validate_on_startup_errors_raise_and_list_every_problem(tmp_path, monkeypatch, caplog):
    env = _demo_env(tmp_path)
    env.update(AUTH_USER="admin", SLACK_WEBHOOK_URL="nope")
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.ERROR), pytest.raises(cc.ConfigError) as exc:
        cc.validate_on_startup(logging.getLogger("config-test"), env)
    msg = str(exc.value)
    assert "AUTH_USER/AUTH_PASS" in msg and "SLACK_WEBHOOK_URL" in msg and "fix:" in msg
    assert "SKIP_CONFIG_CHECK" in msg
    assert "AUTH_USER/AUTH_PASS" in caplog.text


def test_validate_on_startup_demo_logs_one_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.WARNING):
        report = cc.validate_on_startup(logging.getLogger("config-test"), _demo_env(tmp_path))
    assert report.ok
    records = [r for r in caplog.records if r.name == "config-test"]
    assert len(records) == 1 and "Demo mode" in records[0].getMessage()


# ------------------------------ CLI ----------------------------------- #
def _run_cli(tmp_path, **overrides) -> subprocess.CompletedProcess:
    # Every checked variable is set explicitly (blank = unset), so the
    # repo .env -- loaded with override=False -- can't leak into the test.
    env = {"PATH": os.environ.get("PATH", ""), **_demo_env(tmp_path), **overrides}
    return subprocess.run([sys.executable, str(_CLI)], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=60, check=False)


def test_cli_demo_exit_0(tmp_path):
    proc = _run_cli(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "[WARN ] SERP provider" in proc.stdout
    assert "0 error(s)" in proc.stdout


def test_cli_error_exit_1_without_leaking_secrets(tmp_path):
    proc = _run_cli(tmp_path, AUTH_PASS=SECRETS["AUTH_PASS"],
                    SLACK_WEBHOOK_URL=SECRETS["SLACK_WEBHOOK_URL"])
    assert proc.returncode == 1
    assert "[ERROR] AUTH_USER/AUTH_PASS" in proc.stdout
    assert "[ERROR] SLACK_WEBHOOK_URL" in proc.stdout
    assert SECRETS["AUTH_PASS"] not in proc.stdout + proc.stderr
    assert SECRETS["SLACK_WEBHOOK_URL"] not in proc.stdout + proc.stderr


def test_cli_skip_exit_0(tmp_path):
    proc = _run_cli(tmp_path, AUTH_USER="only", SKIP_CONFIG_CHECK="1")
    assert proc.returncode == 0 and "skipped" in proc.stdout


# ------------------------- app lifespan wiring ------------------------- #
def test_lifespan_demo_mode_starts(client, monkeypatch):
    import dashboard.main as main_module
    monkeypatch.setattr(main_module.logging_setup, "setup", lambda: None)
    from fastapi.testclient import TestClient
    with TestClient(main_module.app) as c:
        assert c.get("/login").status_code == 200


def test_lifespan_broken_config_refuses_to_start(client, monkeypatch):
    import dashboard.main as main_module
    monkeypatch.setattr(main_module.logging_setup, "setup", lambda: None)
    monkeypatch.setenv("AUTH_USER", "only-user")
    monkeypatch.delenv("AUTH_PASS", raising=False)
    from fastapi.testclient import TestClient
    with pytest.raises(cc.ConfigError), TestClient(main_module.app):
        pass


# --------------------------------------------------------------------- #
# AI provider: mirrors processing.llm.not_configured_reason (warnings only)
# --------------------------------------------------------------------- #
@pytest.mark.parametrize("extra,warn_key", [
    ({"LLM_PROVIDER": "gemini"}, "LLM_PROVIDER"),
    ({"LLM_PROVIDER": "skynet"}, "LLM_PROVIDER"),
    ({"LLM_PROVIDER": "openai"}, "OPENAI_API_KEY"),                     # official, no key
    ({"OPENAI_BASE_URL": "http://localhost:11434/v1"}, "OPENAI_MODEL"),  # custom, no model
    ({"OPENAI_BASE_URL": "localhost:11434"}, "OPENAI_BASE_URL"),
])
def test_ai_provider_misconfig_is_a_warning(tmp_path, extra, warn_key):
    env = _demo_env(tmp_path)
    env.update(extra)
    report = cc.check_config(env, cwd=tmp_path)
    assert report.ok
    assert warn_key in {f.key for f in report.warnings}


@pytest.mark.parametrize("extra", [
    {"GEMINI_API_KEY": "g"},
    {"OPENAI_API_KEY": "k"},                                             # official + default model
    {"OPENAI_BASE_URL": "http://localhost:11434/v1", "OPENAI_MODEL": "llama3"},
    {"LLM_PROVIDER": "openai", "OPENAI_BASE_URL": "http://localhost:1234/v1/chat/completions",
     "OPENAI_MODEL": "qwen"},
])
def test_ai_provider_valid_setups_have_no_ai_warning(tmp_path, extra):
    env = _demo_env(tmp_path)
    env.update(extra)
    report = cc.check_config(env, cwd=tmp_path)
    assert [f.key for f in report.warnings] == ["SERP provider"]
    assert "AI provider" in {f.key for f in report.of(cc.OK)}


def test_ai_check_agrees_with_llm_module(tmp_path, monkeypatch):
    from processing import llm
    cases = [{}, {"GEMINI_API_KEY": "g"}, {"LLM_PROVIDER": "openai"},
             {"OPENAI_BASE_URL": "http://h:1/v1"}, {"OPENAI_BASE_URL": "http://h:1/v1", "OPENAI_MODEL": "m"},
             {"LLM_PROVIDER": "gemini", "OPENAI_API_KEY": "k"}, {"LLM_PROVIDER": "x"}]
    for case in cases:
        for name in ("LLM_PROVIDER", "GEMINI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL"):
            monkeypatch.setenv(name, case.get(name, ""))
        env = _demo_env(tmp_path)
        env.update(case)
        report = cc.check_config(env, cwd=tmp_path)
        ok = "AI provider" in {f.key for f in report.of(cc.OK)}
        assert ok == llm.is_configured(), case


# --------------------------------------------------------------------- #
# errors_after_update: what Settings and /setup check before writing .env
# --------------------------------------------------------------------- #
def test_errors_after_update_flags_partial_smtp(tmp_path):
    env = _demo_env(tmp_path)
    problems = cc.errors_after_update({"SMTP_HOST": "smtp.x", "ALERT_EMAIL": "a@b.c"},
                                      env=env, cwd=tmp_path)
    assert [f.key for f in problems] == ["SMTP"]
    assert "SMTP_USER, SMTP_PASS" in problems[0].message


@pytest.mark.parametrize("updates", [{"AUTH_USER": "admin"},
                                     {"SLACK_WEBHOOK_URL": "hooks.slack.com/x"},
                                     {"NTFY_URL": "ntfy.sh/topic"}])
def test_errors_after_update_flags_other_fatal_values(tmp_path, updates):
    assert cc.errors_after_update(updates, env=_demo_env(tmp_path), cwd=tmp_path)


def test_errors_after_update_ignores_preexisting_errors(tmp_path):
    env = _demo_env(tmp_path)
    env["AUTH_USER"] = "admin"  # already broken, unrelated to the save
    assert cc.check_config(env, cwd=tmp_path).errors
    assert cc.errors_after_update({"SLACK_WEBHOOK_URL": "https://hooks.slack.com/x"},
                                  env=env, cwd=tmp_path) == []


def test_errors_after_update_accepts_complete_smtp(tmp_path):
    updates = {"SMTP_HOST": "smtp.x", "SMTP_USER": "u", "SMTP_PASS": "p", "SMTP_PORT": "587"}
    assert cc.errors_after_update(updates, env=_demo_env(tmp_path), cwd=tmp_path) == []
