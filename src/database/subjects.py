"""Subjects query mixin (multi-subject phase 1): CRUD for `subjects` and the
active-subject view the rest of the app reads.

A Subject is one tracked person/brand. It owns its runs, results, lifecycle,
alerts, evidence, notes, events and its own positive/negative domains (every
table in _SUBJECT_TABLES carries subject_id), plus the tracking config that used
to live in three global settings keys:

    report_query     -> default_queries_json[0] (falls back to `name`)
    report_countries -> default_countries_json
    subject_context  -> context

The subject ROW is the source of truth. Subject 1 additionally falls back to the
legacy settings keys when its row is empty, and the two are mirrored both ways
(SettingsMixin.set_setting -> row; update_subject(1, ...) -> settings) so the
routers that still read/write the keys keep working until phase 2 switches them.
"""
import json
import sys
from datetime import UTC, datetime

from database.base import (
    DEFAULT_SUBJECT_COUNTRIES,
    DEFAULT_SUBJECT_ID,
    SCOPING_COMPLETE,
)
from database.migrations import (
    LEGACY_SUBJECT_SETTINGS,
    json_list,
    parse_countries,
    write_legacy_settings_from_subject_1,
)

SUBJECT_TYPES = ("person", "company", "brand")


def normalize_subject_name(name: str | None) -> str:
    """Case- and whitespace-insensitive identity of a name: ' Jordan  ASHWORTH ' ->
    'jordan ashworth'. Used by find_subject_by_name and scripts/maintenance/split_subjects.py."""
    return " ".join((name or "").split()).casefold()


def _now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


class SubjectsMixin:
    """Queries and mutations for subjects (tracked people/brands)."""

    # ------------------------------------------------------------------ #
    # Row <-> dict
    # ------------------------------------------------------------------ #
    @staticmethod
    def _subject_dict(row) -> dict:
        keys = row.keys()
        queries = json_list(row["default_queries_json"])
        name = row["name"] or ""
        return {
            "id": row["id"],
            "client_id": row["client_id"],
            "name": name,
            "aliases": json_list(row["aliases_json"]),
            "type": row["type"] or "person",
            "context": row["context"] or "",
            "queries": queries,
            # The search term used for this subject: its first default query, or its
            # name. (The demo subject is named "Jordan Ashworth (demo)" but searched as
            # "Jordan Ashworth".)
            "query": queries[0] if queries else name,
            "countries": parse_countries(json_list(row["default_countries_json"])),
            "active": bool(row["active"]),
            "is_demo": bool(row["is_demo"]) if "is_demo" in keys else False,
            "created_at": row["created_at"],
        }

    @staticmethod
    def _clean_type(type_: str | None) -> str:
        type_ = (type_ or "person").strip().lower()
        if type_ not in SUBJECT_TYPES:
            raise ValueError(f"type must be one of {list(SUBJECT_TYPES)}, got {type_!r}")
        return type_

    @staticmethod
    def _clean_list(values) -> list[str]:
        if isinstance(values, str):
            values = values.split(",")
        out: list[str] = []
        for v in values or []:
            v = " ".join(str(v).split())
            if v and v not in out:
                out.append(v)
        return out

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def list_subjects(self, include_inactive: bool = False) -> list[dict]:
        conn = self._connect()
        try:
            sql = "SELECT * FROM subjects"
            if not include_inactive:
                sql += " WHERE active = 1"
            rows = conn.execute(sql + " ORDER BY id").fetchall()
        finally:
            conn.close()
        return [self._subject_dict(r) for r in rows]

    def get_subject(self, subject_id: int) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM subjects WHERE id = ?", (subject_id,)).fetchone()
        finally:
            conn.close()
        return self._subject_dict(row) if row else None

    def find_subject_by_name(self, name: str, include_inactive: bool = True) -> dict | None:
        """The subject whose name, an alias or a default query matches `name`
        case/whitespace-insensitively. Active subjects win over inactive ones, then
        the lowest id. None if nothing matches (or `name` is blank)."""
        target = normalize_subject_name(name)
        if not target:
            return None
        matches = []
        for subject in self.list_subjects(include_inactive=include_inactive):
            names = [subject["name"], *subject["aliases"], *subject["queries"]]
            if any(normalize_subject_name(n) == target for n in names):
                matches.append(subject)
        matches.sort(key=lambda s: (not s["active"], s["id"]))
        return matches[0] if matches else None

    def current_subject(self) -> dict:
        """The active subject of THIS instance (for_subject id -> request ContextVar ->
        DEFAULT_SUBJECT_ID) as a dict: id, name, aliases, type, query, queries,
        countries, context, is_demo, active, client_id, created_at.

        Subject 1 falls back to the legacy settings keys for anything its row leaves
        empty (and to DEFAULT_SUBJECT_COUNTRIES for countries). A missing row (a
        subject id with no `subjects` row, e.g. an A11 peer 1000+id) yields an empty
        synthetic subject instead of raising."""
        sid = self._effective_subject_id()
        subject = self.get_subject(sid) or {
            "id": sid, "client_id": 1, "name": "", "aliases": [], "type": "person",
            "context": "", "queries": [], "query": "", "countries": [], "active": False,
            "is_demo": False, "created_at": None,
        }
        if sid == DEFAULT_SUBJECT_ID:
            legacy = self._legacy_subject_settings()
            if not subject["query"]:
                subject["query"] = legacy["report_query"]
            if not subject["name"]:
                subject["name"] = subject["query"]
            if not subject["countries"]:
                subject["countries"] = parse_countries(
                    legacy["report_countries"] or DEFAULT_SUBJECT_COUNTRIES)
            if not subject["context"]:
                subject["context"] = legacy["subject_context"]
        return subject

    def scheduled_subjects(self) -> list[dict]:
        """The subjects background jobs work for (weekly_report, alerts_check,
        info_digest): active, NOT demo, and in a live (non-archived) project --
        subject 1 always counts while active. Each dict is current_subject() of
        that subject (subject 1 with its legacy fallbacks). Demo subjects are
        excluded from EVERYTHING scheduled: no paid searches, no alert checks,
        no digest lines for synthetic data."""
        conn = self._connect()
        try:
            live = {r[0] for r in conn.execute("SELECT id FROM clients WHERE active = 1")}
        finally:
            conn.close()
        out = []
        for s in self.list_subjects():
            if s["is_demo"]:
                continue
            if s["id"] != DEFAULT_SUBJECT_ID and live and s["client_id"] not in live:
                continue
            out.append(self.for_subject(s["id"]).current_subject())
        return out

    def subject_label(self, subject_id: int | None = None) -> str:
        """'<Project> ▸ <Subject>' for notifications, emails and report titles
        (plain text, no HTML). `subject_id` None = this instance's subject. A
        subject without a name falls back to its query, then to 'Subject <id>'."""
        sid = self._effective_subject_id() if subject_id is None else int(subject_id)
        subject = self.for_subject(sid).current_subject()
        name = (subject.get("name") or subject.get("query") or "").strip() or f"Subject {sid}"
        project = ""
        conn = self._connect()
        try:
            row = conn.execute("SELECT name FROM clients WHERE id = ?",
                               (subject.get("client_id") or 1,)).fetchone()
            project = (row[0] or "").strip() if row else ""
        finally:
            conn.close()
        return f"{project} ▸ {name}" if project else name

    def _legacy_subject_settings(self) -> dict:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT key, value FROM settings WHERE key IN (?, ?, ?)",
                LEGACY_SUBJECT_SETTINGS,
            ).fetchall()
        finally:
            conn.close()
        out = dict.fromkeys(LEGACY_SUBJECT_SETTINGS, "")
        out.update({r[0]: (r[1] or "").strip() for r in rows})
        return out

    # Tracking config of the ACTIVE subject (what phase-2 routers will call instead of
    # get_setting(report_query/report_countries/subject_context)).
    def get_tracked_query(self) -> str:
        return self.current_subject()["query"]

    def get_tracked_countries(self) -> list[str]:
        return self.current_subject()["countries"]

    def get_subject_context(self) -> str:
        return self.current_subject()["context"]

    def set_tracked_subject(self, query: str | None = None, countries=None,
                            context: str | None = None) -> None:
        """Updates the ACTIVE subject's tracking config (None = leave as is). The
        subject's name follows the query only while it was empty or equal to the
        previous query -- see update_subject."""
        sid = self._effective_subject_id()
        subject = self.get_subject(sid)
        if subject is None:
            raise ValueError(f"subject {sid} does not exist")
        fields: dict = {}
        if query is not None:
            query = " ".join(query.split())
            fields["queries"] = [query] if query else []
            if not subject["name"] or subject["name"] == subject["query"]:
                fields["name"] = query or subject["name"]
        if countries is not None:
            fields["countries"] = countries
        if context is not None:
            fields["context"] = context
        if fields:
            self.update_subject(sid, **fields)

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def add_subject(self, name: str, aliases: list[str] | None = None, type: str = "person",
                    countries: list[str] | str | None = None, context: str = "",
                    is_demo: bool = False, client_id: int = 1,
                    queries: list[str] | None = None) -> int:
        """Creates a subject and returns its id. `queries` defaults to [name].
        Refuses a blank name, an unknown type, or a name that already identifies an
        ACTIVE subject (find_subject_by_name) -- two subjects answering to the same
        name would make the split tool and the subject switcher ambiguous."""
        # The lock is read from database.models at call time so tests can monkeypatch
        # models.SCOPING_COMPLETE (see tests/test_scoping.py).
        mod = sys.modules.get("database.models") or sys.modules.get("src.database.models")
        scoping_complete = getattr(mod, "SCOPING_COMPLETE", SCOPING_COMPLETE) if mod else SCOPING_COMPLETE
        if not scoping_complete:
            raise RuntimeError(
                "add_subject() is locked: SCOPING_COMPLETE = False. Creating a "
                "second subject would mix its data with subject 1's in any method "
                "still unfiltered (see tests/test_scoping_guard.py)."
            )
        name = " ".join((name or "").split())
        if not name:
            raise ValueError("name is required")
        type_ = self._clean_type(type)
        existing = self.find_subject_by_name(name, include_inactive=False)
        if existing is not None:
            raise ValueError(f"an active subject already answers to {name!r} "
                             f"(id {existing['id']})")
        queries = self._clean_list(queries) or [name]
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO subjects (client_id, name, aliases_json, type, context, "
                "default_queries_json, default_countries_json, active, created_at, is_demo) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (client_id, name, json.dumps(self._clean_list(aliases)), type_,
                 (context or "").strip(), json.dumps(queries),
                 json.dumps(parse_countries(countries or [])), _now(), 1 if is_demo else 0),
            )
            subject_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
        return subject_id

    def update_subject(self, subject_id: int, name: str | None = None,
                       aliases: list[str] | None = None, type: str | None = None,
                       countries: list[str] | str | None = None, context: str | None = None,
                       queries: list[str] | None = None, is_demo: bool | None = None,
                       active: bool | None = None) -> None:
        """Updates only the fields passed (None = don't touch). For subject 1 the
        legacy settings keys are rewritten in the same transaction (phase-1 bridge)."""
        sets: list[str] = []
        params: list = []
        legacy: dict = {}
        if name is not None:
            name = " ".join(name.split())
            if not name:
                raise ValueError("name cannot be blank")
            sets.append("name = ?")
            params.append(name)
        if aliases is not None:
            sets.append("aliases_json = ?")
            params.append(json.dumps(self._clean_list(aliases)))
        if type is not None:
            sets.append("type = ?")
            params.append(self._clean_type(type))
        if countries is not None:
            clean = parse_countries(countries)
            sets.append("default_countries_json = ?")
            params.append(json.dumps(clean))
            legacy["countries"] = clean
        if context is not None:
            sets.append("context = ?")
            params.append(context.strip())
            legacy["context"] = context.strip()
        if queries is not None:
            clean_q = self._clean_list(queries)
            sets.append("default_queries_json = ?")
            params.append(json.dumps(clean_q))
            legacy["query"] = clean_q[0] if clean_q else (name or "")
        elif name is not None and subject_id == DEFAULT_SUBJECT_ID:
            current = self.get_subject(subject_id)
            if current is not None and not current["queries"]:
                legacy["query"] = name
        if is_demo is not None:
            sets.append("is_demo = ?")
            params.append(1 if is_demo else 0)
        if active is not None:
            if not active and subject_id == DEFAULT_SUBJECT_ID:
                raise ValueError("the default subject (1) cannot be deactivated")
            sets.append("active = ?")
            params.append(1 if active else 0)
        if not sets:
            return
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"UPDATE subjects SET {', '.join(sets)} WHERE id = ?",  # noqa: S608 -- SET list of constant 'col = ?' fragments
                        (*params, subject_id))
            if cur.rowcount == 0:
                raise ValueError(f"subject {subject_id} does not exist")
            if subject_id == DEFAULT_SUBJECT_ID and legacy:
                write_legacy_settings_from_subject_1(cur, legacy)
            conn.commit()
        finally:
            conn.close()

    def deactivate_subject(self, subject_id: int) -> None:
        """Soft delete: the row and ALL its data stay (audit trail, reactivation);
        the subject just stops being listed/searched. Subject 1 can't be
        deactivated (it's the fallback every unscoped instance reads)."""
        self.update_subject(subject_id, active=False)
