"""
"Run AI checks" on stored results (live test)
=============================================
Same-name (is this result about THIS person?) and severity (1-5 + topic for
negatives) normally run only right after a search. When the AI provider was
missing or failing at that moment, the stored rows keep same_person/severity
empty forever -- they are only retried when the URL shows up again. This
re-runs both checks for the active subject's LATEST run of each
country/engine/device (what Results shows by default):

  1. same-name for rows with no verdict yet (inherited for free from another
     observation of the same URL when one exists, else asked in batches);
  2. severity for negatives that aren't a confirmed namesake and have none.

Human verdicts always win, per page: before any AI write, every row of a page
(same normalized URL) a person labelled in ANY run takes that verdict
(db.reapply_human_verdicts) and is never sent to the AI. Afterwards each
run's AI note (runs.ai_note, the "AI checks skipped" banner) is refreshed:
cleared once the run's checks went through, replaced by the new reason when
they failed again (refresh_run_notes).

Rows a person labelled are never touched. Each call is capped (`limit`
results per check) so one click can't burn the whole AI quota; the counts
returned are honest: checked / set / failed, with the first plain failure
reason.
"""
from database.models import url_hash
from processing import ai_classifier, ai_status, llm
from processing.enrich import _BATCH_SIZE, _known_same_person

DEFAULT_LIMIT = 30


def run_ai_checks(db, limit: int = DEFAULT_LIMIT) -> dict:
    limit = max(1, int(limit))
    out = {"runs": 0, "same_checked": 0, "same_set": 0, "same_inherited": 0, "same_failed": 0,
           "same_remaining": 0, "severity_checked": 0, "severity_set": 0, "severity_failed": 0,
           "severity_remaining": 0, "failed_calls": 0, "reason": ""}
    run_ids = db.latest_run_ids_all()
    out["runs"] = len(run_ids)
    if not run_ids:
        return out
    out["human_applied"] = len(db.reapply_human_verdicts(run_ids=run_ids))
    rows = db.results_for_runs(run_ids)
    context = db.get_subject_context() or ai_classifier.default_subject_context()
    failed_runs: set[str] = set()

    with llm.session_scope(), ai_status.collect_failures() as failures:
        # 1. same-name
        todo = [r for r in rows if r.get("same_person") is None and r.get("classified_by") != "human"]
        known = _known_same_person(db) if todo else {}
        ask = []
        for r in todo:
            verdict = known.get(url_hash(r.get("url") or ""))
            if verdict and db.set_result_same_person(r["id"], verdict):
                r["same_person"] = verdict
                out["same_inherited"] += 1
            elif not verdict:
                ask.append(r)
        out["same_remaining"] = max(0, len(ask) - limit)
        ask = ask[:limit]
        for i in range(0, len(ask), _BATCH_SIZE):
            chunk = ask[i:i + _BATCH_SIZE]
            verdicts = ai_classifier.is_same_person_batch(chunk, context)
            for r, verdict in zip(chunk, verdicts, strict=False):
                out["same_checked"] += 1
                if verdict and db.set_result_same_person(r["id"], verdict):
                    r["same_person"] = verdict
                    out["same_set"] += 1
                elif not verdict:
                    out["same_failed"] += 1
                    failed_runs.add(r.get("run_id"))

        # 2. severity (after same-name: a fresh 'no' isn't worth a call)
        negs = [r for r in rows if r.get("classification") == "negative"
                and r.get("same_person") != "no" and r.get("severity") is None]
        out["severity_remaining"] = max(0, len(negs) - limit)
        for r in negs[:limit]:
            out["severity_checked"] += 1
            try:
                info = ai_classifier.analyze_negative(r)
            except Exception:  # noqa: BLE001 -- one bad row never stops the batch
                info = {}
            severity, topic = info.get("severity"), info.get("topic")
            if isinstance(severity, int) and topic:
                db.set_result_severity(r["id"], severity, topic)
                out["severity_set"] += 1
            else:
                out["severity_failed"] += 1
                failed_runs.add(r.get("run_id"))
    out["failed_calls"] = len(failures)
    out["reason"] = failures[0] if failures else ""
    out["notes_cleared"] = refresh_run_notes(db, run_ids, failed_runs, out["reason"])
    return out


def _pending_by_run(rows: list[dict]) -> dict[str, int]:
    """run_id -> rows still missing a same-name verdict or a severity
    (same selection as pending_counts)."""
    out: dict[str, int] = {}
    for r in rows:
        missing = ((r.get("same_person") is None and r.get("classified_by") != "human")
                   or (r.get("classification") == "negative" and r.get("same_person") != "no"
                       and r.get("severity") is None))
        if missing:
            out[r.get("run_id")] = out.get(r.get("run_id"), 0) + 1
    return out


def refresh_run_notes(db, run_ids, failed_runs: set, reason: str = "") -> int:
    """Brings each run's AI note (the "AI checks skipped" banner) in line
    with what just happened -- a stale note must not outlive a successful
    check:
      * a run whose rows failed again: note replaced by the NEW reason;
      * a run with nothing left to check: note cleared;
      * a run with rows still waiting (the per-click cap) but no failure:
        note replaced by a plain "N still waiting" line.
    Runs without a note are only touched when they failed. Returns how many
    notes were cleared."""
    from report.multi_country import AI_NOTE_PREFIX

    run_ids = [r for r in run_ids if r]
    if not run_ids:
        return 0
    pending = _pending_by_run(db.results_for_runs(run_ids))
    cleared = 0
    for rid in run_ids:
        run = db.get_run(rid) or {}
        note = run.get("ai_note")
        if rid in failed_runs:
            db.set_run_ai_note(rid, f"{AI_NOTE_PREFIX}{reason or 'the AI answers could not be read'}")
        elif not note:
            continue
        elif not pending.get(rid):
            db.set_run_ai_note(rid, None)
            cleared += 1
        else:
            n = pending[rid]
            db.set_run_ai_note(rid, f"AI checks partly done: {n} result{'s' if n != 1 else ''} "
                                    "still waiting (Results → More → AI checks).")
    return cleared


def summary_message(res: dict) -> tuple[str, str]:
    """(plain one-paragraph message, toast kind) for the counts above."""
    if not res["runs"]:
        return "No stored results yet: run a search first.", "info"
    checked = res["same_checked"] + res["severity_checked"]
    parts = []
    if res["same_checked"] or res["same_inherited"]:
        parts.append(f"Same-name: {res['same_set'] + res['same_inherited']} of "
                     f"{res['same_checked'] + res['same_inherited']} results checked")
    if res["severity_checked"]:
        parts.append(f"severity: {res['severity_set']} of {res['severity_checked']} negatives rated")
    if not parts:
        return "Nothing to check: every result of the latest runs already has its AI checks.", "info"
    msg = "AI checks: " + "; ".join(parts) + "."
    failed = res["same_failed"] + res["severity_failed"]
    if failed:
        reason = res["reason"] or "the AI answers couldn't be read"
        msg += f" {failed} could not be checked: {reason}"
        if not msg.endswith("."):
            msg += "."
    remaining = res["same_remaining"] + res["severity_remaining"]
    if remaining:
        msg += f" {remaining} more left: click again to continue."
    kind = "error" if failed and failed >= checked else ("info" if failed else "ok")
    return msg, kind


def pending_counts(db) -> dict:
    """What "Run AI checks" would look at right now (no AI call): rows of the
    active subject's latest runs still missing a same-name verdict, and
    negatives still missing a severity. Same selection as run_ai_checks."""
    run_ids = db.latest_run_ids_all()
    if not run_ids:
        return {"runs": 0, "same_todo": 0, "severity_todo": 0}
    rows = db.results_for_runs(run_ids)
    same = sum(1 for r in rows if r.get("same_person") is None and r.get("classified_by") != "human")
    sev = sum(1 for r in rows if r.get("classification") == "negative"
              and r.get("same_person") != "no" and r.get("severity") is None)
    return {"runs": len(run_ids), "same_todo": same, "severity_todo": sev}
