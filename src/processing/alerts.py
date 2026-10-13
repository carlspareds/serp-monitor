"""
Alert system (Phase 2 · COM-02 / IT-01)
================================
Detects and persists alerts:
  - New NEGATIVE results (a domain that turns negative relative to the
    previous date, per country).
  - Negative threshold per country (configurable).

`generate_alerts(db)` returns the list of NEWLY created messages (duplicates
are ignored via `dedupe_key`).
"""

from datetime import UTC, datetime

from notify.dispatcher import emit_alert

# ponytail (chore F1-close, point 6): reuses the "no limit" ceiling from
# get_results_filtered already defined in processing.diff instead of
# repeating the 100_000 literal -- cross-import between siblings in
# processing/, no cycle (diff.py does not import alerts.py) so it's left
# here instead of moving it up to models.py.
from processing.diff import _NO_LIMIT


def thresholds(db) -> int:
    """Legacy per-country negative-count threshold (ALERT_NEG_THRESHOLD).
    Still edited by the /alerts form and used as a fallback by
    severity_threshold() when no explicit severity threshold is set."""
    try:
        return int(db.get_setting("ALERT_NEG_THRESHOLD", "1") or "1")
    except ValueError:
        return 1


# Neutral-conservative severity (2.2, brief): a negative WITHOUT a computed
# `severity` (AI not configured, failure, or simply not yet its turn in the
# pipeline -- see processing.severity.annotate_severity) counts as 3
# ("medium") in the weighted SUM -- it's neither ignored (0, which would
# underestimate real risk) nor assumed severe (5, which would over-alert
# for results not analyzed yet).
NULL_SEVERITY = 3
# Default for ALERT_SEVERITY_THRESHOLD when NO operator configured it (nor
# the old ALERT_NEG_THRESHOLD -- see severity_threshold).
SEVERITY_THRESHOLD_DEFAULT = 5


def severity_threshold(db) -> int:
    """WEIGHTED per-country severity threshold (2.2, deep dive §2.2/Area 7):
    REPLACES `thresholds(db)[0]` (simple negative count) in generate_alerts'
    per-country threshold check -- it now compares against the SUM of
    severities (1-5, NULL_SEVERITY for the ones not yet computed) of a
    country's active negatives, not the count.

    Resolution (documented -- brief 2.2 asks for "fallback to the old
    setting if the new one doesn't exist"):
      1) explicit ALERT_SEVERITY_THRESHOLD in settings -> used as-is.
      2) if not, but ALERT_NEG_THRESHOLD IS explicit (a DB that already had
         a custom threshold BEFORE this feature) -> that same number is
         reused as the severity threshold -- an operator who already had
         "alert at N negatives" configured keeps seeing an alert at
         accumulated severity N, instead of silently jumping to a new
         default without having touched anything.
      3) if neither is explicitly configured -> the weighted-era default:
         SEVERITY_THRESHOLD_DEFAULT (5).

    Read with default="" (instead of thresholds()'s "1") so we can
    distinguish "never configured" from "configured equal to the default".
    """
    raw_sev = db.get_setting("ALERT_SEVERITY_THRESHOLD", "")
    if raw_sev:
        try:
            return int(raw_sev)
        except ValueError:
            return SEVERITY_THRESHOLD_DEFAULT
    raw_neg = db.get_setting("ALERT_NEG_THRESHOLD", "")
    if raw_neg:
        try:
            return int(raw_neg)
        except ValueError:
            pass
    return SEVERITY_THRESHOLD_DEFAULT


def generate_alerts(db) -> list[str]:
    new_msgs: list[str] = []
    sev_thr = severity_threshold(db)

    # 1) Negatives per country
    # H16/H17 (arbitration, 2.3, deep dive §2.3 "excluding the 'no's from
    # stats, ALERTS and exports"): get_results_filtered (excludes
    # same_person='no' by default) instead of the raw get_results_by_run
    # read -- both the threshold (H16) and the new-negatives detection
    # (H17) start from `cur`/`prevm`, so filtering their construction
    # covers both points at once.
    for c in db.distinct_countries():
        dates = db.dates_for_country(c)
        if not dates:
            continue
        latest = dates[0]
        cur = {
            r["url"]: r for r in
            db.get_results_filtered(run_id=db.get_latest_run_id(c, latest), limit=_NO_LIMIT)
        }
        neg_now = [r for r in cur.values() if r.get("classification") == "negative"]

        # WEIGHTED severity threshold (2.2): SUM of severities of the
        # active negatives, not the count -- see severity_threshold. A
        # negative without a computed `severity` yet counts as
        # NULL_SEVERITY (3, neutral-conservative) in the sum.
        sev_sum = sum(
            r["severity"] if isinstance(r.get("severity"), int) else NULL_SEVERITY
            for r in neg_now
        )
        if sev_thr > 0 and sev_sum >= sev_thr:
            # Breakdown (brief 2.2, F7): domain=severity of each active
            # negative, SORTED by severity desc BEFORE the cap of 8 -- if
            # not, the cutoff was by SERP position and could omit exactly
            # the most severe ones (the ones that really justify the
            # alert), leaving only low-severity noise in the message.
            by_severity = sorted(
                neg_now,
                key=lambda r: r["severity"] if isinstance(r.get("severity"), int) else NULL_SEVERITY,
                reverse=True,
            )
            parts = [
                f"{r.get('domain')}={r['severity'] if isinstance(r.get('severity'), int) else '~3'}"
                for r in by_severity[:8]
            ]
            more = f" +{len(neg_now) - 8} more" if len(neg_now) > 8 else ""
            msg = (f"{c.upper()}: {len(neg_now)} negative result(s), severity sum "
                   f"{sev_sum} (≥ threshold {sev_thr}). Breakdown: "
                   f"{', '.join(parts)}{more}.")
            # F4 (review 2.2): dedupe_key WITHOUT the sum -- one threshold
            # alert per country/date, period. With the sum in the key, a
            # same-day re-run that only changes real severities vs the
            # provisional NULL_SEVERITY=3 (e.g. AI finishing annotation
            # between two runs) generated a NEW alert every time sev_sum
            # changed, even though the country was still over the same
            # threshold.
            # FIX 1 point 5 (fix/mute-alerts audit): "high" was legacy and
            # normalize_severity ALWAYS translates it to "critical" -- but
            # the catalog (ALERT_TYPES) says "warning" for threshold. ""
            # lets the catalog decide, without the false severity alarm.
            if emit_alert(db, "threshold", "", c.upper(), msg, f"thr:{c}:{latest}"):
                new_msgs.append(msg)

        # New negatives vs previous date
        if len(dates) >= 2:
            prevm = {
                r["url"]: r for r in
                db.get_results_filtered(run_id=db.get_latest_run_id(c, dates[1]), limit=_NO_LIMIT)
            }
            new_negatives = [
                (url, r) for url, r in cur.items()
                if r.get("classification") == "negative"
                and prevm.get(url, {}).get("classification") != "negative"
            ]
            for _url, r in new_negatives:
                msg = (f"{c.upper()}: new NEGATIVE '{r.get('domain')}' "
                       f"at position {r.get('position')}.")
                dedupe_key = f"newneg:{c}:{latest}:{r.get('domain')}"
                # FIX 1 point 5: same case as "threshold" above -- legacy
                # "high" contradicted the catalog ("warning" for
                # negative_new).
                if emit_alert(db, "negative_new", "", c.upper(), msg, dedupe_key):
                    new_msgs.append(msg)

    return new_msgs


def check_overdue_notes(db, today: str | None = None) -> int:
    """`note_overdue` alert (4.6, deep dive §4.6): one alert per note from
    Database.overdue_notes (due_date past, status NOT closed --
    resolved/dismissed). Hooked into report.weekly.run_alert_checks (job
    `alerts_check`, daily 9am) instead of its own job -- reuses the cron
    that ALREADY walks alerts every day (brief 4.6: "reuse the one that
    already walks alerts") instead of adding a new cron that would compete
    for the same sqlite connection at the same instant (see the comments in
    scheduler/jobs.py about why each new job is staggered to its own
    minute).

    Dedupe `noteoverdue:{note_id}:{due_date}` -- if the operator MOVES the
    target date, the key changes and the note re-alerts (a new due date is
    a new incident); postponing without touching due_date does not
    re-alert (same note, same due date, already notified). severity warning
    (an overdue management task, not a reputation incident in itself).

    Returns how many NEW alerts were emitted (0 if no note is overdue or
    all had already been notified with this date)."""
    today = today or datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d")
    emitted = 0
    for note in db.overdue_notes(today):
        due = note.get("due_date") or ""
        assignee = note.get("assignee") or ""
        who = f" (assignee: {assignee})" if assignee else ""
        msg = f"Note for {note['url']} is overdue (due {due}){who}."
        alert_id = emit_alert(db, "note_overdue", "warning", "", msg,
                              f"noteoverdue:{note['id']}:{due}")
        if alert_id:
            emitted += 1
    return emitted
