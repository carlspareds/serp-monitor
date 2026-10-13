"""Optional at-rest encryption for secret-ish database values.

Real secrets (API keys, SMTP password, bot tokens) never live in the
database: they are in .env (mode 0600). A few values stored in the
``settings`` table still identify a private channel or person -- today the
Telegram chat id (SENSITIVE_SETTINGS). With APP_ENCRYPTION_KEY set, those are
stored as ``enc:v1:<Fernet token>`` (AES-128-CBC + HMAC-SHA256, from the
``cryptography`` package) and decrypted transparently on read.

APP_ENCRYPTION_KEY is either a Fernet key (``Fernet.generate_key()``: 32
url-safe base64 bytes) or any passphrase of 16+ characters, which is
stretched with PBKDF2-SHA256 into one. Keep it OUT of backups of the
database, otherwise the encryption protects nothing.

Without a key, values stay in plain text (startup check warns). A value
encrypted with a key that is later removed or changed reads as "" (and the
app logs it), so a wrong key never surfaces ciphertext in the UI.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os

PREFIX = "enc:v1:"
SENSITIVE_SETTINGS = frozenset({"telegram_chat_id"})
_KDF_SALT = b"serp-monitor/settings/v1"
_KDF_ITERATIONS = 200_000
_log = logging.getLogger("crypto")
_cache: dict[str, object] = {}


class EncryptionUnavailable(RuntimeError):
    pass


def key_configured(env=None) -> bool:
    env = os.environ if env is None else env
    return bool((env.get("APP_ENCRYPTION_KEY") or "").strip())


def _fernet_key(raw: str) -> bytes:
    raw = raw.strip()
    try:
        decoded = base64.urlsafe_b64decode(raw.encode())
        if len(decoded) == 32:
            return raw.encode()
    except (binascii.Error, ValueError):
        pass
    if len(raw) < 16:
        raise EncryptionUnavailable("APP_ENCRYPTION_KEY is too short (use a Fernet key or 16+ "
                                    "characters)")
    derived = hashlib.pbkdf2_hmac("sha256", raw.encode(), _KDF_SALT, _KDF_ITERATIONS)
    return base64.urlsafe_b64encode(derived)


def _fernet(env=None):
    env = os.environ if env is None else env
    raw = (env.get("APP_ENCRYPTION_KEY") or "").strip()
    if not raw:
        return None
    cached = _cache.get(raw)
    if cached is not None:
        return cached
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover -- pinned in requirements.txt
        raise EncryptionUnavailable("the 'cryptography' package is not installed") from exc
    f = Fernet(_fernet_key(raw))
    _cache.clear()
    _cache[raw] = f
    return f


def check_key(env=None) -> str | None:
    """None if the configured key (if any) is usable, else the reason."""
    try:
        _fernet(env)
    except EncryptionUnavailable as exc:
        return str(exc)
    except Exception:  # noqa: BLE001
        return "APP_ENCRYPTION_KEY is not a valid key"
    return None


def is_encrypted(value: str | None) -> bool:
    return bool(value) and str(value).startswith(PREFIX)


def encrypt(value: str) -> str:
    """`value` encrypted when a key is configured, unchanged otherwise."""
    if not value or is_encrypted(value):
        return value
    f = _fernet()
    if f is None:
        return value
    return PREFIX + f.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str) -> str:
    """Plain text for an ``enc:v1:`` value ("" when it can't be decrypted);
    any other value is returned unchanged."""
    if not is_encrypted(value):
        return value
    try:
        f = _fernet()
        if f is None:
            _log.warning("encrypted_setting_without_key")
            return ""
        return f.decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:  # noqa: BLE001 -- wrong key / corrupt value
        _log.warning("encrypted_setting_unreadable")
        return ""
