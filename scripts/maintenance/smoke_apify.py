"""
Manual smoke check of the FULL flow against Apify (makes a real, paid call)
================================
Run from the project root (needs APIFY_API_KEY in .env):

    .venv/bin/python scripts/maintenance/smoke_apify.py

Launches a real search, classifies, saves to the same SQLite DB the app uses
(DB_PATH from .env/environment, else data/seo_dashboard.db) and prints a
summary. Not collected by pytest on purpose (no test_ prefix).
"""

import os
import sys
from pathlib import Path

# Allow imports from the root and from src/
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "src"))

from apify.client import ApifyError
from apify.runner import ApifyRunner
from database.models import Database
from processing.classifier import classify_batch, get_summary


def main():
    runner = ApifyRunner()
    # Same resolution as dashboard.deps (`or`, not getenv's default: a blank
    # DB_PATH= in .env must fall back too). apify.client already ran load_dotenv().
    db = Database(db_path=os.getenv("DB_PATH") or str(project_root / "data" / "seo_dashboard.db"))

    print("=== Full Apify flow smoke check ===")
    try:
        outcome = runner.search(
            queries=["Jordan Ashworth"],
            country_code="us",
            max_pages=1,
            on_status=lambda status, elapsed: print(f"  [{elapsed:>3}s] status={status}"),
        )
    except ApifyError as e:
        print(f"Apify ERROR: {e}")
        return

    print(f"\nRun ID : {outcome['run_id']}")
    print(f"Status : {outcome['status']}")
    print(f"Results extracted: {len(outcome['results'])}")

    results = classify_batch(outcome["results"])
    summary = get_summary(results)
    print(f"Summary: {summary}")

    db.save_run(
        apify_run_id=outcome["run_id"],
        queries=outcome["queries"],
        country_code=outcome["country_code"],
        status=outcome["status"],
        summary=summary,
    )
    db.save_results(results, run_id=outcome["run_id"])

    print("\nFirst 5 results:")
    for r in results[:5]:
        title = (r.get("title") or "")[:60]
        print(f"  #{r.get('position')} [{r.get('classification')}] "
              f"{r.get('domain')} - {title}")


if __name__ == "__main__":
    main()
