"""Evidence query mixin: cryptographic HTML evidence vault and snapshot hashes."""
from datetime import UTC, datetime, timedelta
from pathlib import Path


class EvidenceMixin:
    """Queries and mutations for forensic evidence snapshots."""

    def add_evidence(self, url: str, url_hash: str, captured_at: str, html_path: str,
                      sha256_html: str, trigger: str, result_id: int | None = None,
                      notes: str = "", png_path: str | None = None,
                      sha256_png: str | None = None, run_id: str | None = None) -> int:
        """Inserts a snapshot row.

        3.2 HTML: ALWAYS called from processing.evidence.capture_url
        (the single write point for html_path/sha256_html via this
        trigger), with png_path/sha256_png/run_id set to None (not tied to
        a specific search run).

        png_path/sha256_png/run_id: filled only by the retired SERP
        snapshot capture (trigger='serp_snapshot'/'serp_screenshot', 1.3);
        no current producer sets them -- the columns are kept because
        they're additive (see _init_db) and historical rows may carry
        them. Returns the inserted id."""
        subject_id = self._active_subject_id(None)
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO evidence (url, url_hash, captured_at, html_path, "
            "sha256_html, trigger, result_id, notes, png_path, sha256_png, run_id, "
            "subject_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (url, url_hash, captured_at, html_path, sha256_html,
             trigger, result_id, notes or "", png_path, sha256_png, run_id, subject_id),
        )
        conn.commit()
        inserted_id = cur.lastrowid
        conn.close()
        return inserted_id

    def has_evidence(self, url_hash: str) -> bool:
        """True if `url_hash` already has AT LEAST one snapshot -- used by the
        automatic trigger (processing.evidence.capture_new_negatives, "no
        prior evidence") and by /results (link vs. capture button)."""
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(f"SELECT 1 FROM evidence WHERE url_hash = ? AND {subject_clause} LIMIT 1",  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                    (url_hash, *subject_params))
        row = cur.fetchone()
        conn.close()
        return row is not None

    def evidence_for(self, url_hash: str) -> list[dict]:
        """Snapshots of a URL, most recent to oldest -- the
        /evidence?url_hash=... page."""
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM evidence WHERE url_hash = ? AND {subject_clause} "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                    f"ORDER BY id DESC", (url_hash, *subject_params))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def list_evidence(self, limit: int = 200) -> list[dict]:
        """Every snapshot (any URL, any trigger), most recent first -- the
        /evidence page when no url_hash is given."""
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM evidence WHERE {subject_clause} ORDER BY id DESC LIMIT ?",  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                    (*subject_params, limit))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def get_evidence_by_id(self, evidence_id: int) -> dict | None:
        """A single evidence row by id -- consumed by GET
        /evidence/{id}/download."""
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM evidence WHERE id = ? AND {subject_clause}",  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                    (evidence_id, *subject_params))
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None

    # ------------------------------------------------------------------ #
    # Immutable audit log (C24)
    # ------------------------------------------------------------------ #

    def purge_evidence(self, days: int = 365) -> int:
        """Evidence vault (3.2) -- rows AND vault files
        (html_path), not just the DB row (unlike the rest of
        this block's purges, here there's real disk to free up). Best-effort
        file deletion: an already-missing file (deleted by hand,
        vault moved) must not block purging the row -- it's logged and continues."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        conn = self._connect()
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT id, html_path FROM evidence WHERE captured_at < ?", (cutoff,)
        ).fetchall()
        for row in rows:
            path = row["html_path"]
            if not path:
                continue
            try:
                Path(path).unlink(missing_ok=True)
            except OSError as exc:
                print(f"[purge] could not delete evidence file {path}: {exc}")
        cur.execute("DELETE FROM evidence WHERE captured_at < ?", (cutoff,))
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        return deleted



