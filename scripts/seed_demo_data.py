"""Populates a fresh database with synthetic demo data.

Run once after installing dependencies, before starting the dashboard, so
there is something to look at immediately instead of an empty app:

    .venv/bin/python scripts/seed_demo_data.py

Everything it writes is fictional (see DATA.md): a made-up person, "Jordan
Ashworth", and domains under the RFC 2606 reserved `.example` TLD. No real
search was ever run to produce this data -- titles and positions are
hand-picked to tell a plausible story across four weekly runs (a fraud
article slowly loses rank while owned pages are reinforced; a complaints
page appears in week 3). The runs are dated relative to "now" (the last
one a few hours ago, then one per week back), and the timeline event sits
between runs 2 and 3 so its 7/14/30-day impact columns have data on both
sides.

Wherever possible the seed goes through the same code the app uses after a
real search, so what the demo shows is what the code actually produces:

  - Classification: each row goes through processing.classifier.
    classify_batch with the same domain lists a real search would use. No
    label or confidence is hand-written: rule hits come out at 1.0, rows
    no rule decides come out "neutral" at 0.5, which is below the default
    review threshold (0.7), so Database.save_results puts them in /review.
  - Alerts: processing.alerts.generate_alerts runs after each seeded run,
    as the post-search/daily check would. Notification dispatch is
    stubbed out while seeding so a configured channel never receives demo
    alerts.
  - URL lifecycle: processing.lifecycle.update_url_lifecycle runs after
    each run, at the same point as after a real search (after the results
    are saved, before evidence capture). It tracks first/last seen and
    moves a negative URL that stays out of the top for several runs to
    "gone", which is what feeds Wins on /analysis.
  - Evidence: processing.evidence.capture_new_negatives runs after each
    run, exactly as after a real search, but the network fetch is stubbed:
    the `.example` URLs don't resolve, so a small synthetic HTML page
    (labelled as such) is "fetched" instead. The file write and SHA-256
    are the real code path.

Multi-subject (phase 1): everything is seeded under ONE subject, "Jordan
Ashworth (demo)" (is_demo=1, searched as "Jordan Ashworth", countries [us]),
through db.for_subject(sid) + subject_scope(sid), and the demo's positive/
negative domains become THAT subject's DB rows (config/positive_domains.py
ships an empty global list). Which subject:

  1. an existing demo subject (is_demo=1, or one answering to the demo
     name) is reused;
  2. otherwise subject 1 BECOMES the demo subject when it is unused -- no
     runs, and no name other than the demo's (a fresh install: subject 1 is
     the empty placeholder seeded at first boot, so the demo doesn't leave
     an empty "ghost" subject next to it);
  3. otherwise (subject 1 holds someone's real data) a NEW subject is
     created, and subject 1 and the legacy settings are left untouched.

Whichever it is, the demo subject is moved into a project named "Demo" (the
existing one, or a new one -- no project is ever renamed), so the switcher
reads "Demo ▸ Jordan Ashworth (demo)" and project 1 ("My project") stays the
home of the subjects the operator adds through the wizard.

Idempotent: does nothing (beyond the project placement) if the demo subject
already has a run.
"""
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config.positive_domains import merged_with
from database.models import (
    DEFAULT_SUBJECT_ID,
    DEMO_CLIENT_NAME,
    Database,
    subject_scope,
)

SUBJECT = "Jordan Ashworth"          # the search query (what results are "about")
SUBJECT_NAME = "Jordan Ashworth (demo)"  # the subject's display name
SUBJECT_CONTEXT = ("Jordan Ashworth, a fictional Vancouver entrepreneur and founder of "
                   "Ashworth Group (synthetic demo data).")
COUNTRY = "us"

# The demo subject's owned domains -- its own DB rows (positive_domains), not the
# global config list.
POSITIVE_DOMAINS = ["jordanashworth.example", "jordan-ashworth.example",
                    "j-ashworth.example", "ashworthgroup.example",
                    "ashworthcapital.example", "cityscene-vancouver.example"]
NEGATIVE_DOMAINS = ["scam.example"]

_OFFICIAL = ("jordanashworth.example", "Jordan Ashworth", "Official site.")
_BLOG = ("jordan-ashworth.example", "Jordan's Blog", "Personal blog.")
_GROUP = ("ashworthgroup.example", "Ashworth Group – About", "Company page.")
_SCAM = ("scam.example", "Fraud lawsuit against Jordan Ashworth",
         "Alleged fraud case, unresolved.")
# Good news that still trips the "court" keyword -- the rule engine's
# known false-positive mode, kept on purpose.
_COURT = ("news.example", "Court records: charges against Jordan Ashworth dismissed",
          "Local news follow-up.")
# No rule decides -> neutral at 0.5 -> review queue.
_DIRECTORY = ("directory.example", "Jordan Ashworth profile",
              "General business directory entry.")
_COMPLAINTS = ("reviews.example", "Jordan Ashworth scam? Read customer complaints",
               "User-submitted reviews.")
# Negative in substance, but no keyword matches -> neutral at 0.5 -> review
# queue. The keyword-gap failure mode the real-data eval measures.
_REFUND = ("forum.example", "Ashworth Group refund still pending after 3 months",
           "Forum thread.")

# Owned coverage (cityscene-vancouver.example is in POSITIVE_DOMAINS) that
# enters the top 10 in the latest run, so its positions stay contiguous 1-8.
_INTERVIEW = ("cityscene-vancouver.example",
              "Jordan Ashworth on building a company in Vancouver",
              "Interview, CityScene Vancouver.")

# One entry per weekly run, oldest first: [(position, row), ...]. Dates are
# computed in _run_times() relative to now.
_RUNS = [
    [(1, _OFFICIAL), (2, _BLOG), (3, _SCAM), (4, _COURT), (5, _DIRECTORY)],
    [(1, _OFFICIAL), (2, _BLOG), (3, _GROUP), (4, _SCAM), (5, _DIRECTORY)],
    [(1, _OFFICIAL), (2, _GROUP), (3, _BLOG), (4, _DIRECTORY), (5, _COMPLAINTS),
     (6, _SCAM)],
    [(1, _OFFICIAL), (2, _GROUP), (3, _BLOG), (4, _DIRECTORY), (5, _REFUND),
     (6, _INTERVIEW), (7, _COMPLAINTS), (8, _SCAM)],
]

_PATH_FOR = {"scam.example": "jordan-ashworth-fraud",
             "cityscene-vancouver.example": "interviews/jordan-ashworth",
             "news.example": "court/ashworth-dismissed",
             "reviews.example": "jordan-ashworth",
             "forum.example": "t/ashworth-refund"}


_EVENT_DAYS_BEFORE_NOW = 10   # between run 2 (now-14d) and run 3 (now-7d)

# (domain, note text, assignee, due date in days from now or None, status
# transitions after creation). Created through the same Database calls the
# /notes board uses, so each status change also lands in note_history.
_NOTES = [
    ("scam.example", "Asked the site owner to remove the article.", "admin", 14,
     ["in_progress"]),
    ("reviews.example", "Replied publicly to the complaint thread with the refund details.",
     "admin", None, ["in_progress", "resolved"]),
]
_NOTES_BY = "demo seed"


def _run_times(now: datetime) -> list[datetime]:
    """One timestamp per entry of _RUNS, oldest first, a week apart; the
    last one is a few hours before `now` (naive UTC, like the app stores)."""
    last = (now - timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    n = len(_RUNS)
    return [last - timedelta(weeks=n - 1 - i) for i in range(n)]


def _results_for(rows: list[tuple]) -> list[dict]:
    out = []
    for position, (domain, title, description) in rows:
        out.append({
            "query": SUBJECT,
            "url": _url_for(domain),
            "title": title,
            "description": description,
            "displayed_url": domain,
            "position": position,
            "country_code": COUNTRY,
            "domain": domain,
            "engine": "google",
            "device": "desktop",
        })
    return out


def _synthetic_page(url: str) -> bytes:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>Synthetic demo page: {url}</title></head><body>"
        "<p><b>Synthetic demo content generated by scripts/seed_demo_data.py.</b> "
        "This is not a real capture: .example domains do not resolve, so the "
        "seed stubs the network fetch and hands this page to the real "
        "evidence code path (file write + SHA-256).</p>"
        f"<p>URL: {url}</p></body></html>"
    ).encode()


def _url_for(domain: str) -> str:
    return f"https://{domain}/{_PATH_FOR.get(domain, '')}"


def _seed_notes(db: Database, now: datetime) -> None:
    for domain, text, assignee, due_in_days, statuses in _NOTES:
        due = ((now + timedelta(days=due_in_days)).date().isoformat()
               if due_in_days is not None else "")
        url = _url_for(domain)
        db.set_note(url, text, assignee=assignee, due_date=due)
        note_id = next(n["id"] for n in db.list_notes() if n["url"] == url)
        for status in statuses:
            db.set_note_status(note_id, status, by=_NOTES_BY)


def demo_project_id(db: Database) -> int:
    """The "Demo" project (a `clients` row): the existing one with that name, or a
    new one. Never renames a project -- project 1 ("My project") stays where the
    wizard puts real subjects."""
    for client in db.list_clients():
        if (client["name"] or "").strip().casefold() == DEMO_CLIENT_NAME.casefold():
            if not client["active"]:
                db.set_client_active(client["id"], True)
            return client["id"]
    return db.add_client(DEMO_CLIENT_NAME)


def demo_subject_id(db: Database) -> int:
    """Finds or creates the demo subject (see the module docstring for the rule),
    makes sure it lives in the "Demo" project, and returns its id. Only ever
    writes subject/project rows; idempotent."""
    sid = _find_or_create_demo_subject(db)
    project = demo_project_id(db)
    subject = db.get_subject(sid)
    if subject is not None and subject["client_id"] != project:
        db.set_subject_project(sid, project)
    return sid


def _find_or_create_demo_subject(db: Database) -> int:
    for subject in db.list_subjects(include_inactive=True):
        if subject["is_demo"]:
            return subject["id"]
    existing = db.find_subject_by_name(SUBJECT_NAME)
    if existing is not None:
        db.update_subject(existing["id"], is_demo=True)
        return existing["id"]
    fields = {"queries": [SUBJECT], "countries": [COUNTRY], "context": SUBJECT_CONTEXT,
              "type": "person", "is_demo": True}
    first = db.get_subject(DEFAULT_SUBJECT_ID)
    first_unused = (
        first is not None
        and not db.for_subject(DEFAULT_SUBJECT_ID).list_runs()
        and first["name"] in ("", SUBJECT, SUBJECT_NAME)
    )
    if first_unused:
        # update_subject(1, ...) also rewrites the legacy report_query/
        # report_countries/subject_context settings (phase-1 bridge).
        db.update_subject(DEFAULT_SUBJECT_ID, name=SUBJECT_NAME, aliases=[SUBJECT], **fields)
        return DEFAULT_SUBJECT_ID
    return db.add_subject(SUBJECT_NAME, aliases=[SUBJECT], type="person",
                          countries=[COUNTRY], context=SUBJECT_CONTEXT, is_demo=True,
                          queries=[SUBJECT])


def main() -> None:
    db_path = os.getenv("DB_PATH") or str(_ROOT / "data" / "seo_dashboard.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    root = Database(db_path=db_path)
    sid = demo_subject_id(root)
    db = root.for_subject(sid)

    if db.list_runs():
        print(f"[seed_demo_data] demo subject {sid} in {db_path} already has runs "
              "-- nothing to do.")
        return
    # The ContextVar too, not just for_subject: code that reads the module-level
    # injected Database (processing.ai_classifier.subject_name) only learns the
    # subject from it.
    with subject_scope(sid):
        _seed(db, db_path, sid)


def _seed(db: Database, db_path: str, sid: int) -> None:
    from notify import dispatcher
    from processing import evidence
    from processing.alerts import generate_alerts
    from processing.classifier import classify_batch, get_summary
    from processing.lifecycle import update_url_lifecycle

    for domain in POSITIVE_DOMAINS:
        db.add_positive_domain(domain)
    for domain in NEGATIVE_DOMAINS:
        db.add_negative_domain(domain)

    positives = merged_with(db.list_positive_domains())
    negatives = db.list_negative_domains()

    real_fetch = evidence.safe_urlopen
    real_notify = dispatcher.notify_alert
    evidence.safe_urlopen = lambda url, **_kw: (200, _synthetic_page(url))
    dispatcher.notify_alert = lambda *_a, **_kw: None
    n_evidence = 0
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        for i, (when, rows) in enumerate(zip(_run_times(now), _RUNS, strict=True)):
            run_id = f"demo-run-{i + 1}"
            results = classify_batch(_results_for(rows), positive_domains=positives,
                                     negative_domains=negatives)
            db.save_run(
                apify_run_id=run_id, queries=[SUBJECT], country_code=COUNTRY,
                status="completed", summary=get_summary(results),
                cost_usd=0.04, compute_units=0.015,
            )
            db.save_results(results, run_id=run_id)
            # save_run/save_results stamp "now"; overwrite both so the demo
            # history spans several weeks instead of one instant.
            # dates_for_country/get_latest_run_id (and therefore alerts/diff)
            # key off search_results.scraped_at, not runs.created_at.
            started_at = when.isoformat(timespec="seconds")
            with db._connect() as conn:
                conn.execute("UPDATE runs SET created_at = ? WHERE apify_run_id = ? "
                             "AND subject_id = ?", (started_at, run_id, sid))
                conn.execute("UPDATE search_results SET scraped_at = ? WHERE run_id = ? "
                             "AND subject_id = ?", (started_at, run_id, sid))
                conn.commit()
            # Same post-save order as report.multi_country.persist_engine_run:
            # lifecycle first, then evidence capture.
            update_url_lifecycle(db, run_id)
            n_evidence += len(evidence.capture_new_negatives(db, run_id))
            generate_alerts(db)
    finally:
        evidence.safe_urlopen = real_fetch
        dispatcher.notify_alert = real_notify

    db.add_event((now - timedelta(days=_EVENT_DAYS_BEFORE_NOW)).date().isoformat(),
                 "Published a rebuttal statement", kind="manual",
                 expected_effect="Fraud article loses rank")
    _seed_notes(db, now)

    n_alerts = len(db.list_alerts(limit=1000, status="all"))
    print(f"[seed_demo_data] Seeded {len(_RUNS)} runs into {db_path} for "
          f"subject {sid} '{SUBJECT_NAME}': {db.count_pending_review()} result(s) in the "
          f"review queue, {n_alerts} alert(s), {n_evidence} evidence snapshot(s), "
          f"{len(db.list_events())} timeline event(s), "
          f"{len(db.list_victories())} win(s), {len(db.list_notes())} note(s).")


if __name__ == "__main__":
    main()
