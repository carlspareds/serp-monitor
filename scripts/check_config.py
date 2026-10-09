"""Check SERP Monitor's configuration without starting the server.

    .venv/bin/python scripts/check_config.py      # or: make check

Loads the repo .env the same way the app does (never overriding variables
already exported), runs the same checks as the startup validation in
dashboard.main's lifespan, and prints one OK/INFO/WARN/ERROR line per item.
Secret values are never printed. Exit code 1 if there is any error, else 0
(missing optional keys -- demo mode -- are warnings, not errors).
Set SKIP_CONFIG_CHECK=1 to skip.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from common.config_check import (
    check_config,
    format_report,
    load_dotenv_like_app,
)


def main() -> int:
    load_dotenv_like_app()
    report = check_config()
    print(format_report(report))
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
