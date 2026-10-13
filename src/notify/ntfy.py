"""
Sending via ntfy (https://ntfy.sh or self-hosted)
================================
NTFY_URL, if set, is the full topic URL (e.g. a self-hosted server:
https://ntfy.mydomain.com/mytopic) and takes priority. If only NTFY_TOPIC
is set, the public service https://ntfy.sh/<topic> is used.

Environment variables: NTFY_URL, NTFY_TOPIC
"""
import os

from notify.base import post

PUBLIC_BASE = "https://ntfy.sh"


def _url() -> str:
    url = os.getenv("NTFY_URL", "").strip()
    if url:
        return url
    topic = os.getenv("NTFY_TOPIC", "").strip()
    return f"{PUBLIC_BASE}/{topic}" if topic else ""


def is_configured() -> bool:
    return bool(_url())


def topic_url(topic: str) -> str:
    """Public ntfy.sh URL of a topic name."""
    return f"{PUBLIC_BASE}/{topic.strip()}" if topic.strip() else ""


def send(text: str, url: str | None = None) -> tuple[bool, str]:
    """POST of the message body to the topic. Never raises: (ok, detail).
    The topic is semi-secret (whoever knows it can publish/read) -- it is
    redacted. `url` defaults to the configured one (Settings tests the
    values typed in the dialog)."""
    url = url or _url()
    if not url:
        return False, "not configured"
    return post(url, secret=url, data=text.encode("utf-8"))
