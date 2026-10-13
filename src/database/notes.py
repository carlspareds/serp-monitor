"""Notes query mixin: URL notes, workflow states, and overdue tracking."""
from datetime import UTC, datetime

from database.base import (
    LEGACY_NOTE_STATUSES,
    NOTE_STATUSES,
    normalize_note_status,
    note_is_overdue,
)


class NotesMixin:
    """Queries and mutations for URL notes and management workflows."""

    def set_note(self, url: str, note: str, assignee: str = "", due_date: str = "") -> None:
        """Creates/edits the TEXT of a note (URL, note, assignee, due_date) --
        does NOT touch `status` (4.6, deep dive §4.6): status transitions
        go through set_note_status, which also writes note_history. A new note
        is born 'open'; an
        EXISTING note keeps whatever status it already had, whatever it
        is, without a text save resetting it.

        BEGIN IMMEDIATE (same pattern as set_note_status/add_audit,
        lesson from the audit-race fix): deciding "create vs. update vs. delete"
        requires reading the current status, and that read must be
        serialized against a concurrent set_note_status on the SAME note
        -- otherwise, a transition could lose the race against a text save
        that reopens/closes it unintentionally.

        Deletes the row ONLY if the note is 'open' (never managed
        yet, no history to lose) AND note/assignee/due_date all end up
        empty -- same spirit as the original delete-if-empty, but it no
        longer deletes a note with an advanced status just because its
        text was cleared (that would lose the management log without anyone
        having asked for it).

        C21b: url_notes.url is the PK -- there's no room for subject_id in that
        key (known debt, see the C21b report). The SELECT/UPDATE/
        DELETE here DO filter by subject_id (another subject's note on the
        SAME url isn't read or overwritten), but two subjects that both
        want to annotate the SAME url would collide on the INSERT
        (the PK's UNIQUE) -- harmless while SCOPING_COMPLETE stays
        False; add_subject's lock prevents that case from existing today."""
        if not url:
            return
        subject_id = self._active_subject_id(None)
        conn = self._connect()
        conn.isolation_level = None
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                f"SELECT status FROM url_notes WHERE url = ? AND {subject_clause}",  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                (url, *subject_params),
            )
            row = cur.fetchone()
            now = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
            if row is None:
                if note or assignee or due_date:
                    cur.execute(
                        "INSERT INTO url_notes "
                        "(url, status, note, updated_at, assignee, due_date, subject_id) "
                        "VALUES (?, 'open', ?, ?, ?, ?, ?)",
                        (url, note, now, assignee, due_date, subject_id),
                    )
            else:
                status = normalize_note_status(row["status"])
                if status == "open" and not (note or assignee or due_date):
                    cur.execute(
                        f"DELETE FROM url_notes WHERE url = ? AND {subject_clause}",  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                        (url, *subject_params),
                    )
                else:
                    cur.execute(
                        "UPDATE url_notes SET note = ?, updated_at = ?, assignee = ?, "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                        f"due_date = ? WHERE url = ? AND {subject_clause}",
                        (note, now, assignee, due_date, url, *subject_params),
                    )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_notes_map(self) -> dict[str, dict]:
        """{url: {status, note, updated_at}} for every URL with a note."""
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        cur.execute(
            f"SELECT url, status, note, updated_at FROM url_notes WHERE {subject_clause}",  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
            subject_params,
        )
        out = {r["url"]: {"status": r["status"], "note": r["note"],
                          "updated_at": r["updated_at"]} for r in cur.fetchall()}
        conn.close()
        return out

    def list_notes(self) -> list[dict]:
        """All notes, most recent first. `id` is url_notes.rowid
        (4.6) -- see the note_history CREATE TABLE docstring for why:
        url_notes has a TEXT PK (`url`), not its own numeric id, so
        the hidden rowid SQLite already maintains is reused instead of
        restructuring the PK of a table in production. It's the `note_id`
        that set_note_status/note_history and the /notes forms consume."""
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        cur.execute(
            f"SELECT rowid AS id, * FROM url_notes WHERE {subject_clause} ORDER BY updated_at DESC",  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
            subject_params,
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def set_note_status(self, note_id: int, to_status: str, by: str, comment: str = "") -> bool:
        """A note's state transition (4.6, deep dive §4.6): UPDATE +
        a note_history row in the SAME transaction. `to_status` is validated
        AGAINST NOTE_STATUSES before opening the transaction -- a value outside
        the whitelist does nothing and returns False, never raises (same
        defensive approach as the rest of the repo's forms, F1-2.2).

        BEGIN IMMEDIATE (the EXACT same pattern as Database.add_audit, lesson
        from the audit-race fix -- "WAL alone was assumed to be enough, and
        concurrent writers broke the hash chain"): reading the current `status`
        (from_status) and writing the new status + the history row are
        A SINGLE atomic step. Without this, two concurrent transitions on
        the SAME note could read the same from_status and the log
        would end up with a jump that never happened (e.g. two operators
        moving the same board card at the same time).

        Returns True if it transitioned, False if to_status is invalid or
        note_id doesn't exist (a deleted/never-created note, or one from ANOTHER
        subject -- C21b: same treatment as "doesn't exist", it doesn't distinguish
        -- see authenticate_user's approach)."""
        to_status = LEGACY_NOTE_STATUSES.get(to_status, to_status)
        if to_status not in NOTE_STATUSES:
            return False
        conn = self._connect()
        conn.isolation_level = None
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute(
                f"SELECT status FROM url_notes WHERE rowid = ? AND {subject_clause}",  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                (note_id, *subject_params),
            )
            row = cur.fetchone()
            if row is None:
                conn.rollback()
                return False
            from_status = normalize_note_status(row["status"])
            now = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
            cur.execute(
                f"UPDATE url_notes SET status = ?, updated_at = ? WHERE rowid = ? AND {subject_clause}",  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                (to_status, now, note_id, *subject_params),
            )
            cur.execute(
                "INSERT INTO note_history (note_id, from_status, to_status, by, at, comment, "
                "subject_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (note_id, from_status, to_status, by, now, comment or "",
                 self._active_subject_id(None)),
            )
            conn.commit()
            return True
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def note_history(self, note_id: int) -> list[dict]:
        """A note's transition log (4.6), most recent first
        -- same order as list_notes/list_events (recency-first is the
        repo's convention for history views)."""
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM note_history WHERE note_id = ? AND {subject_clause} "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                    f"ORDER BY id DESC", (note_id, *subject_params))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def list_notes_by_status(self) -> dict[str, list[dict]]:
        """Notes grouped by status (4.6, /notes board) -- keys in
        NOTE_STATUSES order. A status outside the whitelist (shouldn't
        happen after _ensure_note_status, but defensively: a note is never lost
        because of an unexpected value) falls into 'open'."""
        grouped: dict[str, list[dict]] = {status: [] for status in NOTE_STATUSES}
        for n in self.list_notes():
            grouped[normalize_note_status(n.get("status"))].append(n)
        return grouped

    def overdue_notes(self, today: str) -> list[dict]:
        """Overdue notes (4.6): due_date < today, status not closed -- see
        note_is_overdue (the single criterion shared with the /notes badge and
        processing.alerts.check_overdue_notes)."""
        return [
            n for n in self.list_notes()
            if note_is_overdue(n.get("due_date") or "", n.get("status"), today)
        ]


    # ------------------------------------------------------------------ #
    # Events (timeline)
    # ------------------------------------------------------------------ #

