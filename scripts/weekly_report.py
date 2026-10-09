"""
Automated weekly report
================================
CLI wrapper for src/report/weekly.py::run() -- the full flow (multi-country
-> Excel -> alerts -> email) lives there so it can also be invoked as an
embedded scheduler job (8.1, src/scheduler/jobs.py). This script still
works the same way by hand:

    # Manual
    .venv/bin/python scripts/weekly_report.py
    # One-off override
    .venv/bin/python scripts/weekly_report.py --query "Other name" --countries us,gb
    # Another subject (default: subject 1), or every scheduled subject like the job
    .venv/bin/python scripts/weekly_report.py --subject 2
    .venv/bin/python scripts/weekly_report.py --all-subjects

The system cron (crontab) was retired in 8.1: scheduling now lives in
the web process's own embedded scheduler (see /settings -> APScheduler
+ `scheduled_jobs` table). This script remains available to launch by
hand or to debug without waiting for the next scheduled run.
"""

import argparse
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))

from report import weekly


def main():
    parser = argparse.ArgumentParser(description="Multi-country weekly SEO report")
    parser.add_argument("--query", help="Query to search (override of settings)")
    parser.add_argument("--countries", help="Comma-separated list of countries")
    parser.add_argument("--pages", type=int, default=1)
    parser.add_argument("--subject", type=int, default=None,
                        help="Subject id (default: 1); its query/countries unless overridden")
    parser.add_argument("--all-subjects", action="store_true",
                        help="Every scheduled subject (active, non-demo), like the weekly job")
    args = parser.parse_args()

    from database.base import subject_scope
    from database.models import Database

    db = Database(db_path=str(project_root / "data" / "seo_dashboard.db"))
    if args.all_subjects:
        weekly.run_scheduled(db)
        return 0
    sid = args.subject or 1
    with subject_scope(sid):
        weekly.run(db=db.for_subject(sid), query=args.query, countries=args.countries,
                   pages=args.pages)
    return 0


if __name__ == "__main__":
    sys.exit(main())
