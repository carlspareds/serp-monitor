"""
Administration from the web: API key (.env).
================================
- Apify API key: saved in the .env file (APIFY_API_KEY key) and reflected
  in os.environ so the current process uses it instantly.
- Automatic scheduling (previously: the user's crontab) has lived since
  8.1 in the embedded scheduler (src/scheduler/), not here -- this module
  only keeps build_cron_expr/FREQUENCIES because the /settings UI still
  uses them to translate frequency+hour into a cron expression.
"""

import os
import re
from collections.abc import Iterable
from pathlib import Path

from common.config_check import errors_after_update

FREQUENCIES = {
    "daily": "Daily",
    "weekly": "Weekly",
    "monthly": "Monthly (1st)",
    "quarterly": "Quarterly (1st of Jan/Apr/Jul/Oct)",
    "biannual": "Biannual (1st of Jan/Jul)",
    "annual": "Annual (Jan 1)",
}

# Day of week for the weekly frequency, in crontab numbering (0 = Sunday).
WEEKDAYS = {
    1: "Monday",
    2: "Tuesday",
    3: "Wednesday",
    4: "Thursday",
    5: "Friday",
    6: "Saturday",
    0: "Sunday",
}
DEFAULT_WEEKDAY = 1


# --------------------------- generic ENV ----------------------------- #
def check_env(key: str, value: str) -> None:
    """Raises ValueError if key/value could break out of their .env line:
    a newline in a submitted form field would otherwise let it append
    extra variables (e.g. AUTH_PASS) to .env."""
    for part in (key or "", "" if value is None else str(value)):
        if any(ch in part for ch in ("\r", "\n", "\x00")):
            raise ValueError(f"{key}: line breaks and NUL characters are not allowed")


def check_env_values(pairs: Iterable[tuple[str, str]]) -> None:
    """Validates a whole save before anything is written: every value with
    check_env, then the resulting configuration with the same rules as the
    startup check (common.config_check), so a save can never produce a
    .env the next restart rejects (e.g. SMTP host without user/password,
    a Slack URL that isn't http(s)). Raises ValueError with a readable
    message naming the problem; never includes a value."""
    pairs = list(pairs)
    for key, value in pairs:
        check_env(key, value)
    problems = errors_after_update({k: "" if v is None else str(v) for k, v in pairs})
    if problems:
        raise ValueError(" ".join(
            f"{f.key}: {f.message}." + (f" To fix: {f.fix}." if f.fix else "")
            for f in problems))


def apify_check_reason(detail: str) -> str:
    """Plain reason for a failed Apify key check (ApifyClient.check_account
    detail), without the raw response body Apify sends back."""
    m = re.search(r"HTTP (\d{3})", detail or "")
    if not m:
        return "Apify could not be reached to verify this key"
    code = int(m.group(1))
    if code == 429 or code >= 500:
        return f"Apify returned an error while verifying this key (HTTP {code})"
    return f"Apify rejected this key (HTTP {code})"


def set_env(env_path: str, key: str, value: str) -> None:
    """Writes/updates a variable in .env and in os.environ.
    Raises ValueError (see check_env) before writing anything."""
    key = (key or "").strip()
    if not key:
        return
    value = "" if value is None else str(value)
    check_env(key, value)
    path = Path(env_path)
    lines = path.read_text().splitlines() if path.exists() else []
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")
    # .env holds API keys/passwords in plaintext -- owner read/write only.
    # Applied on every write (not just creation) since some platforms
    # reset permissions on file replace, and a stricter-than-default mode
    # here is cheap insurance regardless of the platform's own umask.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # e.g. read-only filesystem or unsupported platform -- best-effort
    os.environ[key] = value


def mask(value: str, keep: int = 4) -> str:
    v = value or ""
    if not v:
        return "(not configured)"
    return f"{'•' * max(0, len(v) - keep)}{v[-keep:]}"



# ------------------------------- CRON --------------------------------- #
def build_cron_expr(hour: int, frequency: str, weekday: int = DEFAULT_WEEKDAY) -> str:
    """Returns the cron expression for the given hour (0-23) and frequency.
    `weekday` (crontab numbering, 0 = Sunday) only applies to "weekly";
    anything outside 0-6 falls back to Monday."""
    h = max(0, min(23, int(hour)))
    d = int(weekday) if int(weekday) in WEEKDAYS else DEFAULT_WEEKDAY
    table = {
        "daily": f"0 {h} * * *",
        "weekly": f"0 {h} * * {d}",
        "monthly": f"0 {h} 1 * *",
        "quarterly": f"0 {h} 1 1,4,7,10 *",
        "biannual": f"0 {h} 1 1,7 *",
        "annual": f"0 {h} 1 1 *",
    }
    return table.get(frequency, table["weekly"])
