"""Secret redaction shared by logging, the audit log and the assistant.

Two layers, both cheap enough to run on every log record:

1. **Known values.** The current value of every environment variable whose
   NAME looks secret (``*KEY*``, ``*TOKEN*``, ``*SECRET*``, ``*PASS*``,
   ``*WEBHOOK*``...) plus the ones config_check marks as secret (``NTFY_URL``,
   ``NTFY_TOPIC``...). Read from ``os.environ`` at call time, so a key saved
   from Settings a second ago is already covered.
2. **Key-like patterns.** Shapes that are secrets whatever variable they came
   from: provider key prefixes (``apify_api_``, ``sk-``, ``AIza``, Slack
   ``xox?-``, this app's own ``sm_live_`` API keys), Telegram bot tokens,
   Slack webhook paths, ``Bearer`` headers, ``password=...``-style pairs and
   ``user:password@`` in URLs.

``redact()`` never raises and returns the input unchanged when there is
nothing to hide. It is a safety net, not a licence to log secrets: code
should still never pass a secret to a logger, the audit log or a template.
"""
from __future__ import annotations

import os
import re

PLACEHOLDER = "<redacted>"

# Variable names whose value is a secret (or identifies a private channel).
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASS|PASSWORD|WEBHOOK|DSN|CREDENTIAL)",
                          re.IGNORECASE)
# Secret by meaning although the name doesn't say so (see config_check.ENV_VARS).
_EXTRA_SECRET_VARS = ("NTFY_URL", "NTFY_TOPIC", "SLACK_WEBHOOK_URL", "APP_ENCRYPTION_KEY")
_MIN_LEN = 6

_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"apify_api_[A-Za-z0-9]{10,}"), PLACEHOLDER),
    (re.compile(r"\bsk-(?:proj-|or-v1-|ant-)?[A-Za-z0-9_\-]{16,}"), PLACEHOLDER),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), PLACEHOLDER),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), PLACEHOLDER),
    (re.compile(r"\bsm_live_[A-Za-z0-9_\-]{8,}"), PLACEHOLDER),
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}"), PLACEHOLDER),            # Telegram bot token
    (re.compile(r"hooks\.slack\.com/services/[A-Za-z0-9/_\-]+"), "hooks.slack.com/services/" + PLACEHOLDER),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/\-]{8,}=*"), "Bearer " + PLACEHOLDER),
    # password=..., api_key: ..., "token": "..." (form bodies, query strings, JSON)
    (re.compile(r"(?i)(\"?\b(?:password|passwd|pass|secret|token|api[_-]?key|apikey|x-api-key)\"?\s*[:=]\s*\"?)"
                r"([^\s\"&,;}]{3,})"), r"\1" + PLACEHOLDER),
    # https://user:password@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://[^/\s:@]+:)([^@\s/]+)@"), r"\1" + PLACEHOLDER + "@"),
)


def secret_values(env=None) -> list[str]:
    """Current secret values (>= 6 chars), longest first."""
    env = os.environ if env is None else env
    vals = set()
    for name, value in env.items():
        v = (value or "").strip()
        if len(v) >= _MIN_LEN and (_SECRET_NAME.search(name) or name in _EXTRA_SECRET_VARS):
            vals.add(v)
    return sorted(vals, key=len, reverse=True)


def redact(text, secrets: list[str] | None = None):
    """`text` with known secret values and key-like patterns replaced.
    Non-strings are returned as they are."""
    if not isinstance(text, str) or not text:
        return text
    try:
        for s in secrets if secrets is not None else secret_values():
            if s in text:
                text = text.replace(s, PLACEHOLDER)
        for rx, repl in _PATTERNS:
            text = rx.sub(repl, text)
    except Exception:  # noqa: BLE001 -- redaction must never break the caller
        return text
    return text


def redact_obj(obj, secrets: list[str] | None = None):
    """redact() applied to every string inside lists/tuples/dicts."""
    secrets = secret_values() if secrets is None else secrets
    if isinstance(obj, str):
        return redact(obj, secrets)
    if isinstance(obj, list):
        return [redact_obj(v, secrets) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(v, secrets) for v in obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v, secrets) for k, v in obj.items()}
    return obj
