"""Startup configuration validation.
================================
One set of checks shared by the FastAPI lifespan (dashboard.main) and the
CLI (scripts/check_config.py / `make check`).

The app is deliberately usable with NO keys (demo mode: synthetic data;
sign-in is always required -- the first visit creates the admin account),
so a missing optional key is never an error. Only configuration that is
actually broken -- the code would silently do the wrong thing, or crash
later -- or unsafe is reported as an error:

  - ALLOW_NO_AUTH=1 with a HOST that isn't a loopback address: running
    without sign-in is only allowed for a local demo. This one is checked
    even with SKIP_CONFIG_CHECK=1.
  - AUTH_USER without AUTH_PASS (or vice versa): the env admin can't sign
    in, the opposite of what was intended.
  - APP_ENCRYPTION_KEY set but unusable.
  - DB_PATH whose directory can't be created/written.
  - SMTP_HOST/SMTP_USER/SMTP_PASS partially set: email_sender.is_configured
    needs all three, so email would be silently disabled.
  - SMTP_PORT not an integer in 1..65535 while SMTP is configured
    (send_email does int(SMTP_PORT) and would crash on every send).
  - SLACK_WEBHOOK_URL / NTFY_URL not an http(s) URL.
  - ENV_FILE pointing at a directory, or into a directory that doesn't exist
    or isn't writable (the Settings page writes keys there).

The AI provider (LLM_PROVIDER / GEMINI_* / OPENAI_*) is optional, so its
problems are warnings, mirroring processing.llm.not_configured_reason().

Secret values are NEVER included in any message: only set/unset.

Escape hatch: SKIP_CONFIG_CHECK=1 skips every check.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "seo_dashboard.db"
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-lite"  # processing.ai_classifier._DEFAULT_MODEL

OK, INFO, WARN, ERROR = "OK", "INFO", "WARN", "ERROR"

# Every variable the code reads, for the report's "set/unset" table.
# secret=True -> its value is never shown (and is also scrubbed from output).
ENV_VARS: dict[str, bool] = {
    "APIFY_API_KEY": True,
    "AUTH_USER": False,
    "AUTH_PASS": True,
    "DB_PATH": False,
    "ENV_FILE": False,
    "SMTP_HOST": False,
    "SMTP_PORT": False,
    "SMTP_USER": False,
    "SMTP_PASS": True,
    "SMTP_FROM": False,
    "ALERT_EMAIL": False,
    "TELEGRAM_BOT_TOKEN": True,
    "SLACK_WEBHOOK_URL": True,
    "NTFY_URL": True,
    "NTFY_TOPIC": True,
    "GEMINI_API_KEY": True,
    "GEMINI_MODEL": False,
    "LLM_PROVIDER": False,
    "OPENAI_BASE_URL": False,
    "OPENAI_API_KEY": True,
    "OPENAI_MODEL": False,
    "AI_REASONING_EFFORT": False,
    "SUBJECT_QUERY": False,
    "DISABLE_SCHEDULER": False,
    "HOST": False,
    "SKIP_CONFIG_CHECK": False,
    "ALLOW_NO_AUTH": False,
    "FORCE_HTTPS": False,
    "TRUSTED_PROXIES": False,
    "APP_ENCRYPTION_KEY": True,
    "SESSION_IDLE_MINUTES": False,
    "SESSION_MAX_HOURS": False,
    "MAX_BODY_BYTES": False,
    "RATE_LIMIT_ENABLED": False,
    "BOT_MIN_SUBMIT_SECONDS": False,
}

_LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
_TRUE = {"1", "true", "yes", "on"}


def _is_loopback(host: str) -> bool:
    host = (host or "").strip()
    if host in _LOOPBACK:
        return True
    try:
        import ipaddress
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def no_auth_problem(env: Mapping[str, str] | None = None) -> str | None:
    """The one check SKIP_CONFIG_CHECK can't bypass: ALLOW_NO_AUTH on a
    non-loopback HOST. Returns the error message, or None."""
    env = os.environ if env is None else env
    if (env.get("ALLOW_NO_AUTH") or "").strip().lower() not in _TRUE:
        return None
    host = (env.get("HOST") or "").strip()
    if host and not _is_loopback(host):
        return (f"ALLOW_NO_AUTH=1 with HOST={host[:60]}: running without sign-in is only "
                "allowed on a loopback address (127.0.0.1)")
    return None

_TRUTHY_LOOKING_FALSE = {"0", "false", "no", "off"}


class ConfigError(RuntimeError):
    """Raised at startup when the configuration has fatal problems."""


@dataclass
class Finding:
    level: str
    key: str
    message: str
    fix: str = ""


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    skipped: bool = False

    def add(self, level: str, key: str, message: str, fix: str = "") -> None:
        self.findings.append(Finding(level, key, message, fix))

    def of(self, level: str) -> list[Finding]:
        return [f for f in self.findings if f.level == level]

    @property
    def errors(self) -> list[Finding]:
        return self.of(ERROR)

    @property
    def warnings(self) -> list[Finding]:
        return self.of(WARN)

    @property
    def ok(self) -> bool:
        return not self.errors


def load_dotenv_like_app() -> None:
    """Load the env file exactly like the app does (apify.client loads
    ENV_FILE when set, else the repo .env, never overriding variables
    already in the environment)."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover -- python-dotenv is a hard dependency
        return
    load_dotenv(os.getenv("ENV_FILE") or DEFAULT_ENV_FILE, override=False)


def _get(env: Mapping[str, str], key: str) -> str:
    return (env.get(key) or "").strip()


def _is_http_url(value: str) -> bool:
    try:
        parts = urlsplit(value)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _resolve(path_str: str, cwd: Path) -> Path:
    p = Path(path_str).expanduser()
    return p if p.is_absolute() else cwd / p


def _writable_dir_problem(directory: Path) -> str | None:
    """None if `directory` exists and is writable, or could be created
    (its nearest existing ancestor is a writable directory). Side-effect free."""
    if directory.exists():
        if not directory.is_dir():
            return f"{directory} exists but is not a directory"
        if not os.access(directory, os.W_OK | os.X_OK):
            return f"directory {directory} is not writable by this user"
        return None
    ancestor = directory.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if not ancestor.is_dir():
        return f"cannot create {directory}: {ancestor} is not a directory"
    if not os.access(ancestor, os.W_OK | os.X_OK):
        return f"cannot create {directory}: {ancestor} is not writable by this user"
    return None


# ------------------------------- checks -------------------------------- #
def _check_auth(env, r: Report) -> None:
    problem = no_auth_problem(env)
    if problem:
        r.add(ERROR, "ALLOW_NO_AUTH", problem,
              "unset ALLOW_NO_AUTH (sign-in is required), or bind to 127.0.0.1")
    elif (env.get("ALLOW_NO_AUTH") or "").strip().lower() in _TRUE:
        r.add(WARN, "ALLOW_NO_AUTH", "set: no sign-in until an account exists (local demo "
              "only; served to loopback clients only)", "unset it for any real use")
    user, pw = _get(env, "AUTH_USER"), bool(env.get("AUTH_PASS"))
    if user and pw:
        r.add(OK, "AUTH_USER/AUTH_PASS", "env admin configured")
        if len(env.get("AUTH_PASS") or "") < 12:
            r.add(WARN, "AUTH_PASS", "shorter than 12 characters",
                  "use a long passphrase (12+ characters)")
    elif user or pw:
        have, missing = ("AUTH_USER", "AUTH_PASS") if user else ("AUTH_PASS", "AUTH_USER")
        r.add(ERROR, "AUTH_USER/AUTH_PASS",
              f"{have} is set but {missing} is empty: that admin could never sign in",
              f"set {missing} too, or clear {have} to run without env credentials")
    else:
        r.add(INFO, "AUTH_USER/AUTH_PASS",
              "unset: sign-in is required; the first visit asks you to create the admin "
              "account (or use one already in the database)")


def _check_https(env, r: Report) -> None:
    host = _get(env, "HOST")
    https = _get(env, "FORCE_HTTPS").lower() in _TRUE
    if https:
        r.add(OK, "FORCE_HTTPS", "set: http is redirected to https, HSTS and Secure cookies on")
    elif host and not _is_loopback(host):
        r.add(WARN, "FORCE_HTTPS", f"HOST={host[:60]} is not loopback but FORCE_HTTPS is not set: "
              "passwords and session cookies would cross the network in clear text",
              "put the app behind an HTTPS reverse proxy (README: Deploying behind HTTPS) "
              "and set FORCE_HTTPS=1")


def _env_file_path(env, cwd: Path, default: Path) -> Path:
    """ENV_FILE resolved against cwd, else `default`: the repo .env the app
    loads (DEFAULT_ENV_FILE), or cwd/.env when the caller passed a cwd."""
    raw = _get(env, "ENV_FILE")
    return _resolve(raw, cwd) if raw else default


def _check_env_permissions(env, r: Report, cwd: Path, default: Path) -> None:
    path = _env_file_path(env, cwd, default)
    try:
        if not path.is_file() or os.name != "posix":
            return
        mode = path.stat().st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        r.add(WARN, "ENV_FILE" if _get(env, "ENV_FILE") else ".env",
              f"{path.name} is readable by other users (mode {mode:o}); it holds API keys",
              f"chmod 600 {path}")


def _check_encryption(env, r: Report) -> None:
    if not _get(env, "APP_ENCRYPTION_KEY"):
        # The only secret-ish DB value today is the Telegram chat id: warn when
        # Telegram is configured (so one is stored), otherwise just inform.
        level = WARN if _get(env, "TELEGRAM_BOT_TOKEN") else INFO
        r.add(level, "APP_ENCRYPTION_KEY", "unset: secret-ish settings (Telegram chat id) are "
              "stored in the database in plain text",
              "generate one with `python -c \"from cryptography.fernet import Fernet; "
              "print(Fernet.generate_key().decode())\"` and keep it out of DB backups")
        return
    from common.crypto import check_key
    problem = check_key(env)
    if problem:
        r.add(ERROR, "APP_ENCRYPTION_KEY", problem,
              "use a Fernet key (Fernet.generate_key()) or a passphrase of 16+ characters")
    else:
        r.add(OK, "APP_ENCRYPTION_KEY", "set: secret-ish settings are encrypted at rest")


def _check_db(env, r: Report, cwd: Path) -> None:
    raw = _get(env, "DB_PATH")
    path = _resolve(raw, cwd) if raw else DEFAULT_DB_PATH
    if path.exists() and path.is_dir():
        r.add(ERROR, "DB_PATH", f"{path} is a directory, not a database file",
              "point DB_PATH at a file, e.g. data/seo_dashboard.db")
        return
    problem = _writable_dir_problem(path.parent)
    if problem:
        r.add(ERROR, "DB_PATH", problem,
              "fix the permissions or point DB_PATH somewhere writable (empty = data/seo_dashboard.db)")
        return
    if path.exists() and not os.access(path, os.R_OK | os.W_OK):
        r.add(ERROR, "DB_PATH", f"{path} is not readable/writable by this user",
              "fix the file's permissions")
        return
    r.add(OK, "DB_PATH", f"{path}" + ("" if raw else " (default)"))


def _check_env_file(env, r: Report, cwd: Path) -> None:
    raw = _get(env, "ENV_FILE")
    if not raw:
        return
    path = _resolve(raw, cwd)
    if path.is_dir():
        r.add(ERROR, "ENV_FILE", f"{path} is a directory",
              "point ENV_FILE at a file (or unset it to use the repo .env)")
        return
    problem = _writable_dir_problem(path.parent)
    if problem and not path.exists():
        r.add(ERROR, "ENV_FILE", f"{path} does not exist and can't be created: {problem}",
              "create the file/directory, or unset ENV_FILE to use the repo .env")
        return
    if path.exists() and not os.access(path, os.W_OK):
        r.add(WARN, "ENV_FILE", f"{path} is not writable: saving keys from Settings will fail")
    elif not path.exists():
        r.add(WARN, "ENV_FILE", f"{path} does not exist yet; it will be created on the "
              "first save from Settings/setup")


def _check_apify(env, r: Report) -> None:
    if _get(env, "APIFY_API_KEY"):
        r.add(OK, "APIFY_API_KEY", "set (real searches enabled)")
    else:
        r.add(WARN, "APIFY_API_KEY", "Demo mode: no APIFY_API_KEY, so searches are disabled, "
              "synthetic data only", "add the key in /setup or .env for real searches")


_OFFICIAL_OPENAI_HOST = "api.openai.com"   # processing.llm._OFFICIAL_OPENAI_HOST
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
# AI_REASONING_EFFORT values offered by Settings → AI provider (processing.llm).
REASONING_EFFORTS = ("low", "medium", "high", "off")


def _llm_provider(env) -> str:
    """Same selection as processing.llm.provider(), on `env`."""
    explicit = _get(env, "LLM_PROVIDER").lower()
    if explicit:
        return explicit
    if _get(env, "GEMINI_API_KEY"):
        return "gemini"
    if _get(env, "OPENAI_BASE_URL") or _get(env, "OPENAI_API_KEY"):
        return "openai"
    return ""


def _check_ai(env, r: Report) -> None:
    """AI provider (processing.llm): optional, so warnings only. Mirrors
    llm.not_configured_reason() so `make check` flags exactly the setups
    the app would treat as "not configured"."""
    prov = _llm_provider(env)
    gem_key, gem_model = _get(env, "GEMINI_API_KEY"), _get(env, "GEMINI_MODEL")
    if not prov:
        r.add(INFO, "AI provider", "unset: rule-based classifier only")
    elif prov == "gemini":
        if gem_key:
            r.add(OK, "AI provider", f"Gemini (model {gem_model or DEFAULT_GEMINI_MODEL})")
        else:
            r.add(WARN, "LLM_PROVIDER", "=gemini but GEMINI_API_KEY is not set: AI disabled",
                  "set GEMINI_API_KEY, or switch LLM_PROVIDER")
    elif prov == "openai":
        base = (_get(env, "OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL).rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")].rstrip("/")
        if not _is_http_url(base):
            r.add(WARN, "OPENAI_BASE_URL", "is not an http(s) URL: AI disabled",
                  "e.g. https://api.openai.com/v1 or http://localhost:11434/v1")
            return
        official = (urlsplit(base).hostname or "").lower() == _OFFICIAL_OPENAI_HOST
        model = _get(env, "OPENAI_MODEL") or (DEFAULT_OPENAI_MODEL if official else "")
        if official and not _get(env, "OPENAI_API_KEY"):
            r.add(WARN, "OPENAI_API_KEY", "not set: the official OpenAI endpoint needs it "
                  "(AI disabled)", "set OPENAI_API_KEY, or point OPENAI_BASE_URL at a "
                  "local server (Ollama, LM Studio)")
        elif not model:
            r.add(WARN, "OPENAI_MODEL", "not set: required for a custom OPENAI_BASE_URL "
                  "(AI disabled)", "set OPENAI_MODEL to a model the server serves, e.g. llama3")
        else:
            host = urlsplit(base).hostname or ""
            r.add(OK, "AI provider", f"OpenAI-compatible (model {model} on {host})")
    else:
        r.add(WARN, "LLM_PROVIDER", f"unknown provider {prov[:40]!r}: AI disabled",
              "use 'gemini' or 'openai', or leave it empty for auto")
    effort = (_get(env, "AI_REASONING_EFFORT") or "").lower()
    if effort and effort not in REASONING_EFFORTS + ("none", "minimal"):
        r.add(WARN, "AI_REASONING_EFFORT", f"unknown value {effort[:20]!r}: 'low' is used",
              "use low, medium, high or off")
    if prov != "gemini" and gem_model and gem_model != DEFAULT_GEMINI_MODEL:
        r.add(WARN, "GEMINI_MODEL", "GEMINI_MODEL is set but has no effect: Gemini is not "
              "the active provider", "set GEMINI_API_KEY (or LLM_PROVIDER=gemini) or clear GEMINI_MODEL")


def _check_smtp(env, r: Report) -> None:
    # Same truthiness as notify.email_sender.is_configured.
    core = {k: bool(env.get(k)) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS")}
    if all(core.values()):
        # send_email does int(os.getenv("SMTP_PORT", "465")): unset -> 465,
        # but a blank SMTP_PORT= is int("") and crashes every send.
        port = env.get("SMTP_PORT")
        try:
            n = 465 if port is None else int(port)
        except ValueError:
            n = -1
        if not 1 <= n <= 65535:
            r.add(ERROR, "SMTP_PORT", "not an integer between 1 and 65535 (every email "
                  "send would crash)", "use 465 (SSL) or 587 (STARTTLS)")
        else:
            r.add(OK, "SMTP", f"configured (port {n}, "
                  f"{'SSL' if n == 465 else 'STARTTLS'})")
        alert = _get(env, "ALERT_EMAIL")
        if alert and "@" not in alert:
            r.add(WARN, "ALERT_EMAIL", "does not look like an email address")
        return
    if any(core.values()):
        missing = ", ".join(k for k, v in core.items() if not v)
        r.add(ERROR, "SMTP", f"partially configured: missing {missing} -- email would be "
              "silently disabled (SMTP_HOST, SMTP_USER and SMTP_PASS are all required)",
              f"set {missing}, or clear all SMTP_* variables to disable email")
        return
    extra = [k for k in ("SMTP_FROM", "ALERT_EMAIL") if _get(env, k)]
    if extra:
        r.add(WARN, "SMTP", f"{'/'.join(extra)} set but SMTP_HOST/SMTP_USER/SMTP_PASS are "
              "not: no email will be sent", "configure SMTP or clear these")
    else:
        r.add(INFO, "SMTP", "unset: email notifications off")


def _check_channels(env, r: Report) -> None:
    configured = []
    if _get(env, "TELEGRAM_BOT_TOKEN"):
        configured.append("Telegram")
        r.add(OK, "TELEGRAM_BOT_TOKEN", "set (also needs the chat id, saved in Settings → Notifications)")
    slack = _get(env, "SLACK_WEBHOOK_URL")
    if slack:
        if _is_http_url(slack):
            configured.append("Slack")
            r.add(OK, "SLACK_WEBHOOK_URL", "set")
        else:
            r.add(ERROR, "SLACK_WEBHOOK_URL", "is not an http(s) URL (value not shown)",
                  "paste the full https://hooks.slack.com/... webhook URL")
    ntfy_url, topic = _get(env, "NTFY_URL"), _get(env, "NTFY_TOPIC")
    if ntfy_url:
        if _is_http_url(ntfy_url):
            configured.append("ntfy")
            r.add(OK, "NTFY_URL", "set" + (" (NTFY_TOPIC ignored)" if topic else ""))
        else:
            r.add(ERROR, "NTFY_URL", "is not an http(s) URL (value not shown)",
                  "use the full topic URL, e.g. https://ntfy.example.com/mytopic, "
                  "or clear it and set only NTFY_TOPIC")
    elif topic:
        if "/" in topic or any(c.isspace() for c in topic):
            r.add(WARN, "NTFY_TOPIC", "contains '/' or whitespace: the ntfy.sh URL will be wrong")
        else:
            configured.append("ntfy")
            r.add(OK, "NTFY_TOPIC", "set (public ntfy.sh)")
    if not configured and not any(f.level == OK and f.key == "SMTP" for f in r.findings):
        r.add(INFO, "notifications", "no channel configured (email/Telegram/Slack/ntfy): "
              "alerts are visible in the dashboard only")


def _check_misc(env, r: Report) -> None:
    sched = (env.get("DISABLE_SCHEDULER") or "").strip()
    if sched:
        if sched.lower() in _TRUTHY_LOOKING_FALSE:
            r.add(WARN, "DISABLE_SCHEDULER", f"={sched} still DISABLES the scheduler "
                  "(any non-empty value does)", "unset it to keep the scheduler on")
        else:
            r.add(INFO, "DISABLE_SCHEDULER", "set: weekly report/alert checks won't run")


def check_config(env: Mapping[str, str] | None = None, cwd: Path | None = None) -> Report:
    """Run every check against `env` (default: os.environ). Pure: no writes."""
    env = os.environ if env is None else env
    # No cwd: check the .env the app actually loads (load_dotenv_like_app),
    # wherever the check runs from. An explicit cwd (tests) never reads it.
    default_env = DEFAULT_ENV_FILE if cwd is None else cwd / ".env"
    cwd = Path.cwd() if cwd is None else cwd
    r = Report()
    if _get(env, "SKIP_CONFIG_CHECK") not in ("", "0"):
        r.skipped = True
        r.add(WARN, "SKIP_CONFIG_CHECK", "set: configuration checks skipped")
        return r
    _check_auth(env, r)
    _check_https(env, r)
    _check_db(env, r, cwd)
    _check_env_file(env, r, cwd)
    _check_env_permissions(env, r, cwd, default_env)
    _check_encryption(env, r)
    _check_apify(env, r)
    _check_ai(env, r)
    _check_smtp(env, r)
    _check_channels(env, r)
    _check_misc(env, r)
    return r


def errors_after_update(updates: Mapping[str, str], env: Mapping[str, str] | None = None,
                        cwd: Path | None = None) -> list[Finding]:
    """Errors the startup check would report once `updates` are applied on
    top of `env` (default: os.environ) that it does not already report
    today. Settings and /setup call this (via dashboard.admin.check_env_values)
    BEFORE writing .env, so a save can never leave a configuration that the
    next restart refuses to start with. Pre-existing errors unrelated to
    the save don't block it. Pure: no writes."""
    env = os.environ if env is None else env
    current = {(f.key, f.message) for f in check_config(env, cwd).errors}
    merged = {**env, **updates}
    return [f for f in check_config(merged, cwd).errors if (f.key, f.message) not in current]


# ------------------------------ output --------------------------------- #
def _scrub(text: str, env: Mapping[str, str]) -> str:
    """Belt and braces: remove any secret value that leaked into a message."""
    for key, secret in ENV_VARS.items():
        val = env.get(key) or ""
        if secret and len(val) >= 3:
            text = text.replace(val, "<secret>")
    return text


def _line(f: Finding) -> str:
    s = f"[{f.level:5}] {f.key}: {f.message}"
    return s + (f"\n          fix: {f.fix}" if f.fix and f.level in (WARN, ERROR) else "")


def format_report(report: Report, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    lines = ["SERP Monitor configuration check", ""]
    lines += [_line(f) for f in report.findings]
    if not report.skipped:
        lines += ["", "Variables (values never shown):"]
        lines += [f"  {k:20} {'set' if (env.get(k) or '').strip() else 'unset'}"
                  for k in ENV_VARS]
    lines += ["", f"Result: {len(report.errors)} error(s), {len(report.warnings)} warning(s)"
              + (" -- FIX THE ERRORS ABOVE" if report.errors else " -- OK")]
    return _scrub("\n".join(lines), env)


def format_errors(report: Report, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    lines = [f"Invalid configuration ({len(report.errors)} problem(s)); the server will not start:"]
    for f in report.errors:
        lines.append(f"  - {f.key}: {f.message}")
        if f.fix:
            lines.append(f"      fix: {f.fix}")
    lines.append("Run `make check` for the full report; set SKIP_CONFIG_CHECK=1 to bypass.")
    return _scrub("\n".join(lines), env)


def validate_on_startup(logger: logging.Logger | None = None,
                        env: Mapping[str, str] | None = None) -> Report:
    """Called once from the FastAPI lifespan. Logs warnings as one line;
    on errors logs them and raises ConfigError (uvicorn then exits non-zero)."""
    log = logger or logging.getLogger("config")
    env = os.environ if env is None else env
    report = check_config(env)
    if report.skipped:
        log.warning("config_check_skipped (SKIP_CONFIG_CHECK is set)")
        # The unsafe opt-out is refused even when every other check is skipped.
        problem = no_auth_problem(env)
        if problem:
            text = _scrub(f"Invalid configuration; the server will not start: {problem}", env)
            log.error(text)
            raise ConfigError(text)
        return report
    if report.warnings:
        log.warning(_scrub("config: " + "; ".join(f"{f.key}: {f.message}"
                                                    for f in report.warnings), env))
    return report
