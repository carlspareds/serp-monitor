"""Integration health: the last real result of each integration, so Settings
can say "Working", "Failing: <reason>" or "Not tested" instead of only
"configured".

Results are recorded by every Test button (also a test of values typed in a
dialog, before they are saved) and by real deliveries (notify.send_via, the
alert email). Each result is stored with a fingerprint of the configuration
it was obtained with: a status only describes the CURRENT configuration when
the fingerprints match, so after a key or address changes the status goes
back to "not tested" until it is used or tested again -- and a test that ran
on the values of an unsaved dialog becomes the status the moment exactly
those values are saved.

The fingerprint is a truncated SHA-256 over the values: it never stores or
shows a secret. Stored as one JSON setting (`integration_health`), the last
few results per integration.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime

SETTING_KEY = "integration_health"
_KEEP = 4
_MAX_REASON = 300
_log = logging.getLogger("integrations")

# Environment variables behind each integration (Telegram also uses the chat
# id setting). Order matters for the fingerprint.
ENV_KEYS: dict[str, tuple[str, ...]] = {
    "apify": ("APIFY_API_KEY",),
    "email": ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM", "ALERT_EMAIL"),
    "telegram": ("TELEGRAM_BOT_TOKEN",),
    "slack": ("SLACK_WEBHOOK_URL",),
    "ntfy": ("NTFY_URL", "NTFY_TOPIC"),
}


def fingerprint(*values: str) -> str:
    joined = "\x1f".join("" if v is None else str(v).strip() for v in values)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def config_values(name: str, values: dict | None = None, chat_id: str = "") -> list[str]:
    """The values the fingerprint of `name` is computed from: `values` (env
    name -> value, e.g. typed in a dialog) or the environment."""
    src = values if values is not None else os.environ
    out = [str(src.get(k) or "").strip() for k in ENV_KEYS.get(name, ())]
    if name == "email" and not out[1]:
        out[1] = "465"
    if name == "email" and not out[4]:
        out[4] = out[2]          # From defaults to the user
    if name == "ntfy":
        url, topic = out
        out = [url or (f"https://ntfy.sh/{topic}" if topic else "")]
    if name == "telegram":
        out.append(chat_id)
    return out


def current_ident(name: str, db=None) -> str:
    chat_id = db.get_setting("telegram_chat_id", "") if (db is not None and name == "telegram") else ""
    return fingerprint(name, *config_values(name, chat_id=chat_id))


def ident_for(name: str, values: dict, chat_id: str = "") -> str:
    return fingerprint(name, *config_values(name, values, chat_id=chat_id))


def _load(db) -> dict:
    try:
        raw = db.get_setting(SETTING_KEY, "")
        data = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _plain(reason: str, secrets: tuple[str, ...] = ()) -> str:
    text = " ".join(str(reason or "").split())
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "<secret>")
    return text[:_MAX_REASON]


def record(db, name: str, ident: str, ok: bool, reason: str = "",
           secrets: tuple[str, ...] = ()) -> None:
    """Store one result for configuration `ident`. Never raises."""
    if db is None:
        return
    try:
        data = _load(db)
        entries = [e for e in data.get(name, []) if isinstance(e, dict) and e.get("ident") != ident]
        entries.insert(0, {"ident": ident, "ok": bool(ok),
                           "reason": "" if ok else (_plain(reason, secrets) or "It failed."),
                           "at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")})
        data[name] = entries[:_KEEP]
        db.set_setting(SETTING_KEY, json.dumps(data))
    except Exception:  # noqa: BLE001 -- a status write must never break a send
        _log.warning("integration_health_write_failed", exc_info=True)


def lookup(db, name: str, ident: str) -> dict | None:
    """{"ok", "reason", "at"} recorded for exactly this configuration, or None."""
    for entry in _load(db).get(name, []):
        if isinstance(entry, dict) and entry.get("ident") == ident:
            return {"ok": bool(entry.get("ok")), "reason": str(entry.get("reason") or ""),
                    "at": str(entry.get("at") or "")}
    return None


def status(db, name: str) -> dict | None:
    """Last result for the CURRENT configuration of `name` (None = not tested)."""
    return lookup(db, name, current_ident(name, db))


def record_current(db, name: str, ok: bool, reason: str = "") -> None:
    """Result of a real delivery / test with the saved configuration."""
    try:
        secrets = tuple(os.getenv(k) or "" for k in ENV_KEYS.get(name, ()))
        record(db, name, current_ident(name, db), ok, reason, secrets)
    except Exception:  # noqa: BLE001
        _log.warning("integration_health_write_failed", exc_info=True)
