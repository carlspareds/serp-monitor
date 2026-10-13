"""
Sending via Slack (Incoming Webhook)
================================
Environment variables: SLACK_WEBHOOK_URL
"""
import os

from notify.base import post


def is_configured() -> bool:
    return bool(os.getenv("SLACK_WEBHOOK_URL"))


def send(text: str, url: str | None = None) -> tuple[bool, str]:
    """POST {"text": text} to the webhook. Never raises: (ok, detail). The
    webhook URL IS the secret -- post() redacts it from errors. `url`
    defaults to SLACK_WEBHOOK_URL (Settings tests a pasted one)."""
    url = url or os.getenv("SLACK_WEBHOOK_URL")
    if not url:
        return False, "not configured"
    return post(url, secret=url, json={"text": text})
