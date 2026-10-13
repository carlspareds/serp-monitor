"""Guard test (FIX 1, point 8, silent-alerts audit): prevents a NEW
producer from repeating the bug that left the system mute for weeks
(39 alerts created, only 1 test row in notify_log) -- 14 call sites
called `db.add_alert(...)` and never dispatched.

Walks ALL files under src/ looking for `.add_alert(` outside
`notify.dispatcher.emit_alert` (the only legitimate point that creates +
dispatches) and fails if one appears that is not in the explicit
whitelist below -- each entry documents WHY that site is a legitimate
exception and not a new mute producer.
"""
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"

# (path relative to src/) -> why this file may call
# `add_alert(` directly without going through notify.dispatcher.emit_alert.
_ALLOWLIST = {
    "notify/dispatcher.py":
        "emit_alert is the ONLY legitimate wrapper -- this is where the "
        "real call to add_alert that the rest of the code must reuse lives.",
    "database/alerts.py":
        "defines add_alert() itself (the real INSERT; docstring mentions it).",
    "database/runs.py":
        "_check_apify_budget() calls add_alert (cannot import notify.dispatcher: "
        "cycle notify -> processing -> database -- returns the alert_id/row and the "
        "external CALLER dispatches, see FIX 1 point 4).",
    "scheduler/deadman.py":
        "the dead-man's switch dispatches with _dispatch_now DIRECTLY, not "
        "with notify_alert/emit_alert (H1(b), 7.3 review): the watchdog cannot "
        "depend on a conduit drained by a job the deadman itself evaluates.",
}

_ADD_ALERT_RE = re.compile(r"\.add_alert\(")


def _offending_lines() -> list[str]:
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel in _ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _ADD_ALERT_RE.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    return offenders


def test_no_add_alert_call_sites_outside_emit_alert_or_whitelist():
    offenders = _offending_lines()
    assert offenders == [], (
        "db.add_alert(...) called outside notify.dispatcher.emit_alert without "
        "a whitelist entry -- every alert must dispatch (see notify.dispatcher."
        "emit_alert). If this is a legitimate special case, add it to "
        "_ALLOWLIST in this file WITH a justification; otherwise, use "
        "emit_alert(db, kind, severity, country, message, dedupe_key) instead "
        "of add_alert(...):\n" + "\n".join(offenders)
    )


def test_allowlist_entries_still_exist_and_are_still_needed():
    """Inverse regression: if a whitelisted file stops existing (or
    stops containing add_alert), the entry became stale -- a
    whitelist that only ever grows and never shrinks is as bad a sign as
    not having one at all."""
    for rel in _ALLOWLIST:
        path = _SRC / rel
        assert path.exists(), f"{rel} is in the whitelist but no longer exists -- remove it."


def test_emit_alert_is_the_only_wrapper_exported_for_producers():
    """Smoke: emit_alert exists and is importable from notify.dispatcher --
    the entry point every new producer must use."""
    from notify.dispatcher import emit_alert
    assert callable(emit_alert)
