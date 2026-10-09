"""Events query mixin: timeline events."""
from datetime import UTC, datetime


class EventsMixin:
    """Queries and mutations for timeline events."""

    def add_event(self, event_date: str, description: str, kind: str = "manual",
                  expected_effect: str = "") -> None:
        if not (event_date and description):
            return
        subject_id = self._active_subject_id(None)
        conn = self._connect()
        cur = conn.cursor()
        now = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
        cur.execute(
            "INSERT INTO events (event_date, kind, description, created_at, expected_effect, "
            "subject_id) VALUES (?, ?, ?, ?, ?, ?)",
            (event_date, kind, description, now, expected_effect or "", subject_id),
        )
        conn.commit()
        conn.close()

    def list_events(self) -> list[dict]:
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM events WHERE {subject_clause} "
                    f"ORDER BY event_date DESC, id DESC", subject_params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def delete_event(self, event_id: int) -> None:
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(f"DELETE FROM events WHERE id = ? AND {subject_clause}",
                    (event_id, *subject_params))
        conn.commit()
        conn.close()
