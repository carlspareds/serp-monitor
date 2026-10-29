"""Which .env the permission check inspects when ENV_FILE is unset (#31):
without a cwd it is the repo .env the app loads, wherever the check runs."""
import common.config_check as cc


def test_default_env_file_without_cwd_is_the_repo_env(tmp_path, monkeypatch):
    repo_env = tmp_path / "repo" / ".env"
    repo_env.parent.mkdir()
    repo_env.write_text("APIFY_API_KEY=x\n")
    repo_env.chmod(0o664)
    monkeypatch.setattr(cc, "DEFAULT_ENV_FILE", repo_env)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    env = {k: "" for k in cc.ENV_VARS}
    env["DB_PATH"] = str(tmp_path / "data" / "t.db")
    report = cc.check_config(env)
    found = [f for f in report.warnings if f.key == ".env"]
    assert len(found) == 1 and str(repo_env) in found[0].fix
