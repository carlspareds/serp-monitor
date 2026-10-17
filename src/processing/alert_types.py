"""Central catalog of alert kinds.

Single source of truth for the *default* severity of every alert `kind`
the code creates. A kind that is not listed here falls back to
DEFAULT_SEVERITY.

`normalize_severity()` is the only function that decides the final severity
that `Database.add_alert` persists: canonical values ({info, warning,
critical}) are kept, legacy values ('med'/'high') are translated, anything
else falls back to the catalog default for that kind.
"""

DEFAULT_SEVERITY = "warning"

# kind -> {"sev": "info" | "warning" | "critical"}
ALERT_TYPES: dict[str, dict[str, str]] = {
    # Weighted severity sum of the active negatives of a country crossed the
    # configured threshold (see processing.alerts.generate_alerts).
    "threshold": {"sev": "warning"},
    # A URL entered the SERP already negative, or was reclassified to negative.
    "negative_new": {"sev": "warning"},
    # Monthly Apify spend reached the configured budget.
    "apify_budget": {"sev": "warning"},
    # Dead-man's switch: an enabled job did not run inside its window.
    "job_missed": {"sev": "warning"},
    # The nightly database backup (scheduler.jobs.nightly_backup) failed;
    # auto-resolved by the next successful run.
    "backup_failed": {"sev": "warning"},
    # URL lifecycle: a negative URL dropped out of the tracked top-N. This is
    # a win, not an incident, so it is informational and auto-resolves if
    # the URL comes back.
    "url_gone": {"sev": "info"},
    # A note with a due date in the past and a status that is not closed.
    "note_overdue": {"sev": "warning"},
}

# Legacy severities -> canonical.
_LEGACY_SEVERITY = {"med": "warning", "high": "critical"}

_CANONICAL = {"info", "warning", "critical"}


def severity_for_kind(kind: str) -> str:
    """Default severity of `kind` according to the catalog (DEFAULT_SEVERITY
    for an unknown kind)."""
    return ALERT_TYPES.get(kind, {}).get("sev", DEFAULT_SEVERITY)


def normalize_severity(kind: str, severity: str) -> str:
    """Final canonical severity for (kind, severity)."""
    if severity in _CANONICAL:
        return severity
    if severity in _LEGACY_SEVERITY:
        return _LEGACY_SEVERITY[severity]
    return severity_for_kind(kind)
