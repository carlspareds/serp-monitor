"""
Automated weekly report -- reusable logic (8.1)
================================
`run()` executes the full flow (multi-country -> Excel -> alerts -> email)
and is importable both from `scripts/weekly_report.py` (CLI wrapper, see
its docstring) and from the `weekly_report` job of the embedded scheduler
(src/scheduler/jobs.py), which injects the process's shared Database into
it.

`run_alert_checks()` (generate_alerts + overdue notes) is shared by
`run()`, the dashboard's "Check now" and the `alerts_check` job -- extracted
here to avoid duplicating it.

Multi-subject: `run()` and `run_alert_checks()` work for ONE subject (the one
`db` is scoped to). The scheduled jobs call `run_scheduled()` /
`run_alert_checks_scheduled()`, which iterate Database.scheduled_subjects()
(active, non-demo, live project) with `db.for_subject(sid)` inside
`subject_scope(sid)` -- the scheduler thread doesn't inherit any request
context, and code that reads the injected singleton (ai_classifier.subject_name)
only learns the subject through the ContextVar. One subject failing doesn't
stop the others; the job ends in error afterwards so the failure shows in
Settings → Schedule.
"""

import html
from datetime import UTC, datetime
from pathlib import Path

from apify.runner import ApifyRunner
from config.positive_domains import merged_with
from database.base import subject_scope
from database.models import DEFAULT_SUBJECT_COUNTRIES, DEFAULT_SUBJECT_QUERY, Database
from export.excel_exporter import build_workbook, report_label
from notify import email_sender
from processing import alerts as alerts_engine
from processing.diff import run_diff
from report.multi_country import aggregate_summary, run_multi_country

project_root = Path(__file__).parent.parent.parent

DEFAULT_QUERY = DEFAULT_SUBJECT_QUERY
DEFAULT_COUNTRIES = DEFAULT_SUBJECT_COUNTRIES


def _latest_run_pair(db, country_code):
    """(run_old, run_new): apify_run_id of the two most recent dates of a
    country, or (None, None) if there are fewer than 2 dates with data.

    Note (C4): selects by DATE (dates_for_country), unlike
    dashboard.routers.diff._previous_run_id, which selects by run ID --
    both converge in practice (1 run per date); not unified without a
    real bug to justify it."""
    dates = db.dates_for_country(country_code)
    if len(dates) < 2:
        return None, None
    return db.get_latest_run_id(country_code, dates[1]), db.get_latest_run_id(country_code, dates[0])


def _detect_new_negatives(db, countries, diffs=None):
    """Negatives present on the most recent date that were NOT present (or
    were not negative) on the previous date, per country. Sorted by
    position (C1, None last).

    Rewritten (4.2) on top of processing.diff.run_diff -- ponytail: a
    single source of diffs for /diff, the /runs link and this block of
    the email. A "new negative" is a url_hash that ENTERED already
    negative, or that was present and got RECLASSIFIED to negative.
    H16/H17 (arbitration, 2.3): same_person='no' is excluded on both
    sides -- run_diff operates on get_results_filtered, not on the raw
    get_results_by_run.

    `diffs` (C13, optional): dict country->run_diff ALREADY computed --
    avoids recomputing it if the caller (run(), which also builds the
    "Changes vs previous run" block) already needs it. If None (e.g. the
    direct test `_detect_new_negatives(db, countries)`), it is computed
    here."""
    alerts = []
    for c in countries:
        diff = (diffs or {}).get(c)
        if diff is None:
            run_old, run_new = _latest_run_pair(db, c)
            if not run_old:
                continue
            diff = run_diff(db, run_old, run_new)
        for r in diff["entered"]:
            if r.get("classification") == "negative":
                alerts.append((c.upper(), r.get("position"), r.get("domain"), r.get("url")))
        for r, _old, new in diff["reclassified"]:
            if new == "negative":
                alerts.append((c.upper(), r.get("position"), r.get("domain"), r.get("url")))
    alerts.sort(key=lambda t: (t[1] is None, t[1]))
    return alerts


def run_alert_checks(db) -> dict:
    """generate_alerts(db) + the overdue-notes check. Shared by run() and by
    the `alerts_check` job so the block is not duplicated."""
    new_alerts = alerts_engine.generate_alerts(db)
    try:
        checks = {"notes_overdue": {"new": alerts_engine.check_overdue_notes(db)}}
        error = None
    except Exception as e:  # noqa: BLE001
        checks = {}
        error = str(e)
    return {"new_alerts": new_alerts, "checks": checks, "error": error}


def _run_per_subject(db, func, label: str) -> dict:
    """Calls func(scoped_db) for every scheduled subject, each inside
    subject_scope(sid). Returns {subject_id: result}; raises RuntimeError
    AFTER the loop if any subject failed (the others still ran)."""
    results: dict = {}
    failed: list[str] = []
    for subject in db.scheduled_subjects():
        sid = subject["id"]
        with subject_scope(sid):
            try:
                results[sid] = func(db.for_subject(sid))
            except Exception as e:  # noqa: BLE001 -- one subject must not stop the rest
                print(f"[{label}] subject {sid} failed: {e}")
                failed.append(f"{sid}: {e}")
    if failed:
        raise RuntimeError(f"{label} failed for subject(s) " + "; ".join(failed))
    return results


def run_alert_checks_scheduled(db) -> dict:
    """`alerts_check` job: run_alert_checks for every scheduled subject."""
    return _run_per_subject(db, run_alert_checks, "alerts_check")


def run_scheduled(db, runner_factory=None) -> dict:
    """`weekly_report` job: run() for every scheduled subject (demo subjects
    never get paid scheduled searches). `runner_factory` builds the Apify
    runner (tests pass a fake)."""
    return _run_per_subject(db, lambda sdb: run(db=sdb, runner=runner_factory() if runner_factory else None),
                            "weekly_report")


def run(db=None, query=None, countries=None, pages: int = 1, runner=None) -> dict:
    """Executes the full weekly report flow for ONE subject: the one `db`
    is scoped to (for_subject / the ContextVar / subject 1). `db` injected
    (embedded job) or None (CLI: builds its own Database over the
    production path, subject 1). `countries` accepts a list or a
    comma-separated string; None falls back to the subject's countries,
    `query` to the subject's tracked query."""
    if db is None:
        db = Database(db_path=str(project_root / "data" / "seo_dashboard.db"))
    runner = runner or ApifyRunner()
    subject = db.current_subject()
    label = db.subject_label()

    query = query or subject.get("query") or DEFAULT_QUERY
    if countries is None:
        countries_raw = ",".join(subject.get("countries") or []) or DEFAULT_COUNTRIES
    elif isinstance(countries, str):
        countries_raw = countries
    else:
        countries_raw = ",".join(countries)
    countries_list = [c.strip().lower() for c in countries_raw.split(",") if c.strip()]

    started = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
    print(f"=== Weekly report {started} UTC -- {label} ===")
    print(f"Query: {query!r} | Countries: {countries_list}")

    outcomes = run_multi_country(
        runner, db, query, countries_list,
        positive_domains=merged_with(db.list_positive_domains()),
        negative_domains=db.list_negative_domains(),
        max_pages=pages,
        on_event=lambda m: print("  ·", m),
    )

    agg = aggregate_summary(outcomes)
    print(f"Aggregate: {agg}")
    for o in outcomes:
        status = o.get("error") or o.get("summary")
        print(f"  {o['country']}: {o['status']} -> {status}")

    date_tag = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d")
    # Kept in the subject's report store, so it shows under Reports →
    # Previous reports (export.reports).
    from export import reports as report_store
    exports = project_root / "data" / "exports"
    report_store.adopt_legacy_files(exports)
    sid = db._effective_subject_id()
    out = report_store.subject_dir(exports, sid) / f"weekly_{date_tag}.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    build_workbook(db, str(out), countries=countries_list, subject_name=report_label(label))
    report_store.register_file(exports, sid, out, "xlsx", title="Weekly report", source="weekly",
                               countries=countries_list, subject=report_label(label))
    print(f"Excel generated: {out}")

    checks = run_alert_checks(db)
    new_alerts = checks["new_alerts"]
    print(f"New alerts: {len(new_alerts)}")
    if checks["error"]:
        print(f"Checks: error {checks['error']}")

    # --- Report email + new-negatives alert ---
    if email_sender.is_configured():
        # C13: a single run_diff per country -- reused below both for
        # _detect_new_negatives and for the "Changes vs previous run" block.
        country_diffs = {}
        for c in countries_list:
            run_old, run_new = _latest_run_pair(db, c)
            if run_old:
                country_diffs[c] = run_diff(db, run_old, run_new)

        new_neg = _detect_new_negatives(db, countries_list, diffs=country_diffs)
        rows = "".join(
            f"<li><b>{c}</b> · #{p} · {d} — {u}</li>" for c, p, d, u in new_neg
        ) or "<li>None new vs the previous date.</li>"
        # H2 (arbitration, 2.3): the figures in the email BODY are
        # calculated from db.latest_run_per_country() (excludes
        # same_person='no' by default -- see Database._filtered_run_counts)
        # filtered to countries_list, AFTER run_multi_country -- not from
        # `agg` (raw aggregate_summary over `outcomes`, calculated above
        # only for the log/return value, which DOES count homonyms).
        email_latest = [
            r for r in db.latest_run_per_country()
            if (r.get("country_code") or "").lower() in countries_list
        ]
        email_tot = {"positive": 0, "negative": 0, "neutral": 0, "total": 0}
        for r in email_latest:
            email_tot["positive"] += r.get("positive") or 0
            email_tot["negative"] += r.get("negative") or 0
            email_tot["neutral"] += r.get("neutral") or 0
            email_tot["total"] += r.get("total_results") or 0

        # Changes-vs-previous-run block (4.2), reusing country_diffs (C13)
        # -- same diff as _detect_new_negatives above and as the /diff
        # page, without recomputing it.
        changes_rows = "".join(
            f"<li><b>{c.upper()}</b> · +{len(d['entered'])} entered · "
            f"-{len(d['exited'])} exited · {len(d['moved'])} moved · "
            f"{len(d['reclassified'])} reclassified</li>"
            for c, d in country_diffs.items()
        )
        changes_html = (
            f"<h3>Changes vs previous run</h3><ul>{changes_rows}</ul>" if changes_rows else ""
        )

        body = (
            f"<h2>Weekly SEO report — {html.escape(label)} — {date_tag}</h2>"
            f"<p><b>Query:</b> {html.escape(query)}</p>"
            f"<p>Positive: {email_tot['positive']} · Negative: {email_tot['negative']} · "
            f"Neutral: {email_tot['neutral']} · Total: {email_tot['total']}</p>"
            f"{changes_html}"
            f"<h3>New negatives ({len(new_neg)})</h3><ul>{rows}</ul>"
            f"<p>Excel attached.</p>"
        )
        with open(out, "rb") as f:
            data = f.read()
        _ok, detail = email_sender.send_email(
            f"Weekly SEO report — {label} — {date_tag}", body,
            attachments=[(out.name, data)],
        )
        print(f"Email: {detail}")
        if _ok:
            report_store.register_file(exports, sid, out, "xlsx",
                                       title="Weekly report", source="weekly", emailed=True,
                                       countries=countries_list, subject=report_label(label))
    else:
        print("Email not configured; sending skipped.")

    # Only what the tests/callers read; the rest has already been printed/sent.
    return {"query": query, "countries": countries_list, "aggregate": agg, "subject_id": sid}
