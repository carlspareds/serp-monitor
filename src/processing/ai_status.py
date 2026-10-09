"""
AI provider health: last test/call result + per-run failure collection
=====================================================================
Settings → AI provider shows whether the provider actually WORKS, not just
whether it is configured. Every real LLM call (ai_classifier._call_llm, the
assistant harness, the Settings test button) reports here:

  record(ok, reason)  -> persists {"ok", "at", "reason", "ident"} as the
                         global setting 'ai_provider_status'. Writes are
                         throttled: a result equal to the last one written
                         (same ok, same provider config) is rewritten at most
                         every WRITE_EVERY_S seconds; a change (ok <-> failed,
                         or another provider/model) is written at once.
                         force=True (the Test button) always writes.
  status(db)          -> the stored status for the CURRENT provider config,
                         or None ("Configured — not tested").

collect_failures() is a context manager used around the AI steps of a
search run: every failed call inside it is appended to the yielded list,
so the run can say "AI checks skipped: <reason>" instead of silently
leaving same-name/severity empty.

Reasons are the plain, secret-free messages produced by processing/llm.py
(never a raw provider body).
"""
import contextlib
import contextvars
import json
import logging
import threading
import time
from datetime import UTC, datetime

from processing import llm

SETTING_KEY = "ai_provider_status"
WRITE_EVERY_S = 300
_MAX_REASON = 300

_log = logging.getLogger("ai")
_db = None
_lock = threading.Lock()
_last: dict = {}            # {"ident", "ok", "reason", "t"} of the last write
_COLLECTOR: contextvars.ContextVar[list | None] = contextvars.ContextVar(
    "ai_failures", default=None)


def set_db(db) -> None:
    global _db
    _db = db
    with _lock:
        _last.clear()


def ident() -> str:
    """Identity of the current provider configuration (no secrets)."""
    info = llm.provider_info()
    return f"{info['provider']}|{info['model']}|{info['base_url']}"


def _plain(reason: str) -> str:
    text = " ".join(llm._scrub(reason or "").split())
    return text[:_MAX_REASON]


def record(ok: bool, reason: str = "", *, force: bool = False, db=None) -> None:
    """Report one real call result. No-op when no provider is configured
    (a "not configured" reason isn't a provider failure). Never raises."""
    if not llm.is_configured() or llm.overridden():
        # Not configured: not a provider failure. Overridden: a test of
        # unsaved values in Settings, which says nothing about the saved
        # configuration this status describes.
        return
    reason = "" if ok else (_plain(reason) or "The AI provider call failed.")
    if not ok:
        bucket = _COLLECTOR.get()
        if bucket is not None:
            bucket.append(reason)
    target = db if db is not None else _db
    if target is None:
        return
    current = ident()
    now = time.monotonic()
    with _lock:
        same = (_last.get("ident") == current and _last.get("ok") == ok
                and _last.get("reason") == reason)
        if not force and same and now - _last.get("t", 0) < WRITE_EVERY_S:
            return
        _last.update(ident=current, ok=ok, reason=reason, t=now)
    payload = {"ok": bool(ok), "reason": reason, "ident": current,
               "at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")}
    try:
        target.set_setting(SETTING_KEY, json.dumps(payload))
    except Exception:  # noqa: BLE001 -- a status write must never break a call
        _log.warning("ai_status_write_failed", exc_info=True)


def status(db) -> dict | None:
    """{"ok", "at" (naive UTC), "reason"} for the current provider config,
    or None when it was never tested/used (or the config changed since)."""
    try:
        raw = db.get_setting(SETTING_KEY, "")
        data = json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or data.get("ident") != ident():
        return None
    return {"ok": bool(data.get("ok")), "at": str(data.get("at") or ""),
            "reason": str(data.get("reason") or "")}


@contextlib.contextmanager
def collect_failures():
    """Collects the plain reasons of the AI calls that fail inside the block."""
    bucket: list[str] = []
    token = _COLLECTOR.set(bucket)
    try:
        yield bucket
    finally:
        _COLLECTOR.reset(token)
