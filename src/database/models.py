"""Database models (SQLite)

Composed from domain mixins under src/database/. All public symbols and
Database class are preserved and re-exported for 100% backwards compatibility.
"""
from database.alerts import AlertsMixin
from database.audit import AuditMixin
from database.base import (
    _ALL_SUBJECTS,
    _SERIES_NULL_SEVERITY,
    _SUBJECT_TABLES,
    API_KEY_PREFIX,
    API_KEY_SCOPE_DEFAULT,
    API_KEY_SCOPES,
    DB_PATH,
    DEFAULT_CLIENT_ID,
    DEFAULT_CLIENT_NAME,
    DEFAULT_SUBJECT_COUNTRIES,
    DEFAULT_SUBJECT_ID,
    DEFAULT_SUBJECT_NAME,
    DEFAULT_SUBJECT_QUERY,
    DEMO_CLIENT_NAME,
    LEGACY_NOTE_STATUSES,
    NOTE_STATUS_LABELS,
    NOTE_STATUSES,
    REVIEW_THRESHOLD_DEFAULT,
    SCOPING_COMPLETE,
    USER_ROLES,
    BaseDatabase,
    current_subject_id,
    normalize_note_status,
    normalize_url,
    note_is_overdue,
    reset_active_subject,
    set_active_subject,
    subject_scope,
    url_hash,
)
from database.events import EventsMixin
from database.evidence import EvidenceMixin
from database.migrations import MigrationsMixin
from database.notes import NotesMixin
from database.results import ResultsMixin
from database.review import ReviewMixin
from database.runs import RunsMixin
from database.schema import (
    SchemaMixin,
)
from database.settings import SettingsMixin
from database.subjects import SubjectsMixin, normalize_subject_name
from database.users import UsersMixin


class Database(
    BaseDatabase,
    SchemaMixin,
    MigrationsMixin,
    RunsMixin,
    ResultsMixin,
    ReviewMixin,
    AlertsMixin,
    EventsMixin,
    NotesMixin,
    EvidenceMixin,
    AuditMixin,
    UsersMixin,
    SubjectsMixin,
    SettingsMixin,
):
    """SQLite database manager composed of domain query mixins."""


__all__ = [
    "API_KEY_PREFIX",
    "API_KEY_SCOPES",
    "API_KEY_SCOPE_DEFAULT",
    "DB_PATH",
    "DEFAULT_CLIENT_ID",
    "DEFAULT_CLIENT_NAME",
    "DEFAULT_SUBJECT_COUNTRIES",
    "DEFAULT_SUBJECT_ID",
    "DEFAULT_SUBJECT_NAME",
    "DEFAULT_SUBJECT_QUERY",
    "DEMO_CLIENT_NAME",
    "LEGACY_NOTE_STATUSES",
    "NOTE_STATUSES",
    "NOTE_STATUS_LABELS",
    "REVIEW_THRESHOLD_DEFAULT",
    "SCOPING_COMPLETE",
    "USER_ROLES",
    "_ALL_SUBJECTS",
    "_SERIES_NULL_SEVERITY",
    "_SUBJECT_TABLES",
    "BaseDatabase",
    "Database",
    "current_subject_id",
    "normalize_note_status",
    "normalize_subject_name",
    "normalize_url",
    "note_is_overdue",
    "reset_active_subject",
    "set_active_subject",
    "subject_scope",
    "url_hash",
]
