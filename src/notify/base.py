"""Shared POST for the notification channels (7.4).

A single place for the try/except, the timeout and -- critically -- the
REDACTION of secrets: requests/urllib3 exceptions include the full URL, and
in Telegram/Slack the URL IS the secret (bot token / webhook). Without
redaction, a simple timeout would persist the token in plain text in
notify_log and would display it in /settings (HIGH finding of the 7.4
review).
"""
import requests


def post(url: str, secret: str = "", **kwargs) -> tuple[bool, str]:
    """POST with a fixed timeout. Returns (ok, detail); never raises. `secret`
    is redacted from any error message before returning it."""
    try:
        resp = requests.post(url, timeout=15, **kwargs)
        if resp.status_code == 200:
            return True, "Sent."
        detail = f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:  # noqa: BLE001
        detail = f"Error: {str(e)[:300]}"
    if secret:
        detail = detail.replace(secret, "<secret>")
    return False, detail
