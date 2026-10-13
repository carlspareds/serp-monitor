"""Dependencies shared between main.py and the routers: DB and templates.

They live in their own module (instead of being defined in main.py) so
routers can do `from dashboard.deps import db, templates` without
creating a circular import with main.py (which in turn imports the
routers to register them).
"""
import os
from pathlib import Path

from fastapi.templating import Jinja2Templates

from dashboard import ui
from database.models import Database
from processing import ai_classifier

project_root = Path(__file__).parent.parent.parent
# design: DB_PATH lets tests point at a temporary DB without touching
# production; in normal runtime it falls back to the usual path. `or`
# instead of getenv's default arg on purpose: .env.example ships DB_PATH
# blank (documented as "optional"), and load_dotenv() sets os.environ["DB_PATH"]
# to that empty string rather than leaving it unset -- with getenv's default
# arg, an empty string is still "present" and wins, so Database gets db_path=""
# and sqlite3 opens a private, anonymous on-disk temp database instead of the
# real file (schema then vanishes as soon as that one connection closes).
db = Database(db_path=os.getenv("DB_PATH") or str(project_root / "data" / "seo_dashboard.db"))
templates = Jinja2Templates(directory=str(project_root / "templates"))
templates.env.globals["dialog"] = ui.dialog
templates.env.globals["opener"] = ui.opener
templates.env.globals["empty_state"] = ui.empty_state
templates.env.globals["overlay"] = ui.overlay


def _flash_html() -> str:
    from dashboard.ui_helpers import flash_html  # lazy: ui_helpers imports deps
    return flash_html()


templates.env.globals["flash_html"] = _flash_html
# The single place where the exports directory lives: shared by the
# PDF/XLSX generators (dashboard.main) -- a single definition instead of
# copies of the same path invited to drift apart without anyone noticing
# (review C23 2nd pass).
# Sibling of the database file (like processing.evidence.evidence_dir):
# data/exports/ in a default install, the test's tmp dir under pytest -- so
# nothing a test generates lands in the repository.
exports_dir = Path(db.db_path).resolve().parent / "exports"

# Injects the process's single Database instance into the AI verdict
# cache (2.5): processing/ai_classifier doesn't import dashboard/, so it
# receives `db` from here instead of building or importing it itself.
ai_classifier.set_cache_db(db)
