"""Review queue query mixin: human review verdicts, severity, and AI cache."""
import json
from datetime import UTC, datetime

from database.base import REVIEW_THRESHOLD_DEFAULT, url_hash


def _now() -> str:
    # Microseconds: "the latest human verdict wins" must order two clicks
    # within the same second.
    return datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="microseconds")


class ReviewMixin:
    """Human-in-the-loop review queue and verdict caching."""

    def review_threshold(self) -> float:
        """Confidence threshold (settings.review_threshold, 2.1) below which
        a result enters /review. Defensive parsing: an absent, invalid,
        or out-of-[0, 1] value falls back to the REVIEW_THRESHOLD_DEFAULT default."""
        raw = self.get_setting("review_threshold", str(REVIEW_THRESHOLD_DEFAULT))
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return REVIEW_THRESHOLD_DEFAULT
        if not (0.0 <= value <= 1.0):
            return REVIEW_THRESHOLD_DEFAULT
        return value

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #

    # One queue entry per PAGE: the same page (url_key = normalized URL, see
    # database.base.normalize_url -- srsltid/utm_* variants are one page) found
    # again by a later run (same country and engine) is the SAME decision, so
    # only its newest pending row is shown/counted. Older pending duplicates are
    # hidden, never deleted. A page a person already gave a verdict on (ANY row
    # of it, any run/country/engine of the subject) never shows up: human
    # verdicts always win (see reapply_human_verdicts).
    _REVIEW_QUEUE_WHERE = (
        "s.review_status = 'pending' AND COALESCE(s.classified_by, '') != 'human' "
        "AND NOT EXISTS ("
        "SELECT 1 FROM search_results n WHERE n.subject_id = s.subject_id "
        "AND COALESCE(n.url_key, n.url) = COALESCE(s.url_key, s.url) AND ("
        "n.classified_by = 'human' OR (n.review_status = 'pending' AND n.id > s.id "
        "AND COALESCE(n.country_code, '') = COALESCE(s.country_code, '') "
        "AND COALESCE(n.engine, 'google') = COALESCE(s.engine, 'google'))))"
    )

    def pending_review(self, limit: int = 50) -> list[dict]:
        """Results in the review queue (review_status='pending'), most
        recent first, one row per URL/country/engine (see _REVIEW_QUEUE_WHERE)."""
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause("s")
        cur.execute(
            f"SELECT s.* FROM search_results s WHERE {self._REVIEW_QUEUE_WHERE} "  # noqa: S608 -- constant fragment; subject_clause is the constant 'subject_id = ?' (values bound)
            f"AND {subject_clause} ORDER BY s.id DESC LIMIT ?",
            (*subject_params, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def count_pending_review(self) -> int:
        """Total in the review queue -- sidebar badge (same pattern as
        count_active_alerts). Counts exactly what pending_review lists."""
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause("s")
        cur.execute(
            f"SELECT COUNT(*) FROM search_results s WHERE {self._REVIEW_QUEUE_WHERE} "  # noqa: S608 -- constant fragment; subject_clause is the constant 'subject_id = ?' (values bound)
            f"AND {subject_clause}",
            subject_params,
        )
        n = cur.fetchone()[0]
        conn.close()
        return n

    def set_review_verdict(self, result_id: int, classification: str) -> dict | None:
        """Sets the HUMAN verdict (2.1) of a result from the queue:
        classified_by='human', confidence=1.0, review_status='approved' if
        it matches the previous classification, or 'corrected' if it changes it.
        Recomputes the row's run counts (M5 -- see
        _recompute_run_counts).

        IMMUTABLE against mass reclassifications (reclassify_all excludes
        classified_by='human' -- see there). Returns {'url',
        'previous_classification', 'review_status'} (the only thing the
        caller consumes -- see dashboard.routers.review) or None if `result_id`
        doesn't exist.

        Filtered by subject in BOTH statements (review C21b): the SELECT
        returns `url` to the caller -- reading another subject's URL is
        already a leak -- and the UPDATE drags along `_recompute_run_counts`,
        which rewrites that other subject's `runs` row. It's not "mutating a
        row by a known id": it has a read that leaks upward and an effect
        on another table.
        """
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        cur = conn.cursor()
        row = cur.execute(
            f"SELECT * FROM search_results WHERE id = ? AND {subject_clause}",  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
            (result_id, *subject_params),
        ).fetchone()
        if row is None:
            conn.close()
            return None
        previous = row["classification"]
        review_status = "approved" if classification == previous else "corrected"
        cur.execute(
            f"UPDATE search_results SET classification=?, classified_by='human', "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
            f"confidence=1.0, review_status=?, human_at=? WHERE id=? AND {subject_clause}",
            (classification, review_status, _now(), result_id, *subject_params),
        )
        self._recompute_run_counts(cur, row["run_id"])
        # Human verdicts win per PAGE: every other row of the same normalized URL
        # of this subject (all runs) gets this verdict too.
        self._reapply_human_verdicts(cur, key=url_hash(row["url"] or ""))
        conn.commit()
        conn.close()
        return {"url": row["url"], "previous_classification": previous, "review_status": review_status}

    def set_same_person_verdict(self, result_id: int) -> str | None:
        """Sets the HUMAN identity verdict (2.3, the "Not this
        person" button in /review, closes deferred item B14 from 2.1): same_person=
        'no', classified_by='human', review_status='corrected'. Doesn't touch
        `classification` -- the row can keep its previous label,
        it just ends up excluded from stats/exports by default (see
        include_homonyms).

        IMMUTABLE against automatic re-annotation: annotate_same_person
        (processing/enrich.py) only acts on URLs without a prior same_person
        (NULL) -- once set here, a new run of the same URL
        inherits it as-is, without asking Gemini again (same spirit as
        the human inheritance of classification, see save_results).

        H4 (arbitration): it propagates by url_hash to ALL rows of that
        URL (not just the clicked one) -- normalize_url can vary between
        observations (utm_, trailing slash, query order...) so the
        comparison is by hash, not by literal url; without this, a URL
        seen across several runs would keep counting as a homonym only in the
        corrected row and would contaminate stats/exports in the others.

        H6 (arbitration): it also sets confidence=1.0 (same approach as
        a human classification verdict in set_review_verdict).

        Does NOT recompute `runs` counts (arbitration: it's a no-op -- same_person
        doesn't change `classification`, and display counts are now
        recalculated on read via _filtered_run_counts, see
        latest_run_per_country/list_runs/get_run).

        Returns the URL (str) or None if result_id doesn't exist.

        C21b: the url_hash fan-out (H4 above) is scoped to the SAME
        subject -- without this, two subjects with the same URL in their data
        (possible: same domain, same article) would contaminate each
        other's identity verdict with a single click, a worse case
        than a simple filtered read because it MUTATES another subject's rows."""
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        row = cur.execute(
            f"SELECT url FROM search_results WHERE id = ? AND {subject_clause}",  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
            (result_id, *subject_params),
        ).fetchone()
        if row is None:
            conn.close()
            return None
        target_hash = url_hash(row["url"] or "")
        ids = [
            r["id"] for r in cur.execute(
                f"SELECT id, url FROM search_results WHERE {subject_clause}", subject_params  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
            ).fetchall()
            if url_hash(r["url"] or "") == target_hash
        ]
        cur.executemany(
            "UPDATE search_results SET same_person='no', classified_by='human', "
            "confidence=1.0, review_status='corrected' WHERE id=?",
            [(i,) for i in ids],
        )
        cur.execute("UPDATE search_results SET human_at=? WHERE id=?", (_now(), result_id))
        self._reapply_human_verdicts(cur, key=target_hash)
        conn.commit()
        conn.close()
        return row["url"]

    def get_negatives_pending_severity(self, run_id: str) -> list[dict]:
        """Negatives from `run_id` without a severity calculated yet -- candidates
        for processing.severity.annotate_severity (2.2).

        Same homonym exclusion criterion as get_results_filtered's
        default (same_person NULL is included, only 'no' is excluded) --
        it makes no sense to pay for an AI call for a result already
        known not to be about the subject."""
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        cur.execute(
            "SELECT * FROM search_results WHERE run_id = ? AND classification = 'negative' "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
            f"AND (same_person IS NULL OR same_person != 'no') AND severity IS NULL AND {subject_clause}",
            (run_id, *subject_params),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def get_result_pending_severity(self, result_id: int) -> dict | None:
        """Single row candidate for severity (2.2, F6: used by
        processing.severity.annotate_severity_for_result after a
        HUMAN correction in /review that sets classification='negative').
        Same eligibility criterion as get_negatives_pending_severity
        but by id instead of by run. None if `result_id` doesn't exist or doesn't
        qualify (it isn't negative, it's a confirmed 'no' homonym, or it already has
        a severity)."""
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        row = cur.execute(
            "SELECT * FROM search_results WHERE id = ? AND classification = 'negative' "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
            f"AND (same_person IS NULL OR same_person != 'no') AND severity IS NULL AND {subject_clause}",
            (result_id, *subject_params),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def set_result_severity(self, result_id: int, severity: int, topic: str) -> None:
        """Persists AI-calculated severity/topic (2.2) for ONE row.
        Best-effort: only called from processing.severity.annotate_severity
        when analyze_negative returned a valid verdict. Filtered by
        subject: a result id is opaque but not secret, and annotating the
        severity of another subject's row moves it around in that other
        subject's ranking (same approach as ack_alert/resolve_alert)."""
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            f"UPDATE search_results SET severity=?, topic=? WHERE id=? AND {subject_clause}",  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
            (severity, topic, result_id, *subject_params),
        )
        conn.commit()
        conn.close()


    def latest_run_ids_all(self) -> list[str]:
        """Most recent run_id of every (country, engine, device) of the ACTIVE
        subject -- what Results shows by default. Used by "Run AI checks"
        (processing/ai_checks.py)."""
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT run_id, MAX(id) FROM search_results "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                f"WHERE {subject_clause} GROUP BY country_code, engine, device",
                subject_params,
            ).fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows if r[0]]

    def results_for_runs(self, run_ids: list[str]) -> list[dict]:
        """Every row of `run_ids` of the ACTIVE subject, by position."""
        if not run_ids:
            return []
        subject_clause, subject_params = self._subject_clause()
        marks = ",".join("?" for _ in run_ids)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM search_results WHERE run_id IN ({marks}) AND {subject_clause} "  # noqa: S608 -- only '?' placeholders are interpolated; subject_clause is the constant 'subject_id = ?' (values bound)
                "ORDER BY position IS NULL, position, id",
                (*run_ids, *subject_params),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def set_result_same_person(self, result_id: int, verdict: str) -> bool:
        """Persists an AUTOMATIC same-name verdict ('yes'|'no'|'unsure') for
        one stored row that has none yet. Never touches a row a person
        labelled (classified_by='human') or one that already has a verdict.
        'unsure' puts the row in the review queue, like save_results does.
        Returns whether the row was updated."""
        if verdict not in ("yes", "no", "unsure"):
            return False
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE search_results SET same_person = ?, review_status = CASE "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                "WHEN ? = 'unsure' AND (review_status IS NULL OR review_status = 'none') "
                "THEN 'pending' ELSE review_status END "
                "WHERE id = ? AND same_person IS NULL "
                f"AND (classified_by != 'human' OR classified_by IS NULL) AND {subject_clause} "
                "AND NOT EXISTS (SELECT 1 FROM search_results h WHERE h.subject_id = "
                "search_results.subject_id AND h.url_key = search_results.url_key "
                "AND h.classified_by = 'human')",
                (verdict, verdict, result_id, *subject_params),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # Human verdicts always win, per page (final live test)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _human_target(group: list) -> dict | None:
        """The verdict a page's rows must carry, from its human rows: the
        classification of the LATEST human row (human_at, then id), and
        same_person 'no' when a person marked any row of the page "not this
        person". None when no person labelled the page."""
        humans = [r for r in group if r["classified_by"] == "human"]
        if not humans:
            return None
        latest = max(humans, key=lambda r: (r["human_at"] or "", r["id"]))
        not_them = any(r["same_person"] == "no" for r in humans)
        return {"id": latest["id"], "classification": latest["classification"],
                "same_person": "no" if not_them else None}

    def _reapply_human_verdicts(self, cur, key: str | None = None,
                                run_ids: list[str] | None = None,
                                dry_run: bool = False) -> list[dict]:
        """Core of reapply_human_verdicts on an open cursor (no commit).
        `key` limits it to one page (url_key); `run_ids` limits the rows
        CHANGED (the human verdict may come from any run)."""
        self._fill_url_keys(cur)
        subject_clause, subject_params = self._subject_clause()
        sql = ("SELECT id, run_id, url, url_key, classification, classified_by, same_person, "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
               f"human_at, review_status, subject_id FROM search_results WHERE {subject_clause}")
        params: list = list(subject_params)
        if key is not None:
            sql += " AND url_key = ?"
            params.append(key)
        else:
            # Only pages that have a human row somewhere.
            sql += (" AND url_key IN (SELECT url_key FROM search_results "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                    f"WHERE classified_by = 'human' AND {subject_clause})")
            params.extend(subject_params)
        groups: dict[tuple, list] = {}
        for r in cur.execute(sql, params).fetchall():
            groups.setdefault((r["subject_id"], r["url_key"]), []).append(r)
        only = set(run_ids) if run_ids is not None else None
        changes: list[dict] = []
        for group in groups.values():
            target = self._human_target(group)
            if target is None:
                continue
            for r in group:
                if r["id"] == target["id"] or (only is not None and r["run_id"] not in only):
                    continue
                new_sp = target["same_person"] or r["same_person"]
                if (r["classified_by"] == "human" and r["classification"] == target["classification"]
                        and r["same_person"] == new_sp):
                    continue
                status = ("approved" if r["classification"] == target["classification"]
                          else "corrected")
                changes.append({"id": r["id"], "run_id": r["run_id"], "url": r["url"],
                                "from": r["classification"], "to": target["classification"],
                                "from_by": r["classified_by"], "same_person": new_sp,
                                "review_status": status})
        if changes and not dry_run:
            cur.executemany(
                "UPDATE search_results SET classification=?, classified_by='human', confidence=1.0, "
                "review_status=?, same_person=? WHERE id=?",
                [(c["to"], c["review_status"], c["same_person"], c["id"]) for c in changes])
            for run_id in {c["run_id"] for c in changes if c["from"] != c["to"]}:
                self._recompute_run_counts(cur, run_id)
        return changes

    @staticmethod
    def _fill_url_keys(cur) -> None:
        """url_key for rows written without one (raw INSERTs; save_results and
        the startup migration always set it). Cheap no-op otherwise."""
        rows = cur.execute("SELECT id, url FROM search_results WHERE url_key IS NULL").fetchall()
        if rows:
            cur.executemany("UPDATE search_results SET url_key = ? WHERE id = ?",
                            [(url_hash(r["url"] or ""), r["id"]) for r in rows])

    def reapply_human_verdicts(self, run_ids: list[str] | None = None,
                               dry_run: bool = False) -> list[dict]:
        """HUMAN VERDICTS ALWAYS WIN, per page: every row of a page (same
        normalized URL, url_key) of the ACTIVE subject -- any run, country or
        engine -- takes the verdict a person gave that page (see
        _human_target: latest human label; "not this person" sticks). Rows
        already matching are left alone. Returns the changes (id, run_id,
        url, from, to, ...); `dry_run` computes them without writing.
        Called before every AI write (Run AI checks, Relabel) and by
        scripts/maintenance/repair_human_verdicts.py."""
        conn = self._connect()
        try:
            changes = self._reapply_human_verdicts(conn.cursor(), run_ids=run_ids, dry_run=dry_run)
            if not dry_run:
                conn.commit()
        finally:
            conn.close()
        return changes

    def human_verdicts_by_key(self) -> dict[str, dict]:
        """{url_key: {'classification', 'same_person'}} of every page of the
        ACTIVE subject a person gave a verdict on (see _human_target)."""
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, url_key, classification, classified_by, same_person, human_at "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                f"FROM search_results WHERE {subject_clause} AND url_key IN (SELECT url_key "
                f"FROM search_results WHERE classified_by = 'human' AND {subject_clause})",
                (*subject_params, *subject_params)).fetchall()
        finally:
            conn.close()
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r["url_key"], []).append(r)
        out = {}
        for k, group in groups.items():
            target = self._human_target(group)
            if target:
                out[k] = {"classification": target["classification"],
                          "same_person": target["same_person"]}
        return out

    def get_cached_verdict(
        self, url_hash: str, verdict_type: str, prompt_version: str
    ) -> dict | None:
        """Cached verdict (already deserialized dict) or None if there's no hit.
        Counts as a hit/miss for ai_cache_stats()."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT verdict_json FROM ai_verdict_cache "
            "WHERE url_hash = ? AND verdict_type = ? AND prompt_version = ?",
            (url_hash, verdict_type, prompt_version),
        )
        row = cur.fetchone()
        conn.close()
        if row is None:
            self._ai_cache_misses += 1
            return None
        self._ai_cache_hits += 1
        return json.loads(row[0])

    def set_cached_verdict(
        self,
        url_hash: str,
        verdict_type: str,
        prompt_version: str,
        verdict: dict,
        model: str = "",
    ) -> None:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO ai_verdict_cache "
            "(url_hash, verdict_type, prompt_version, verdict_json, model, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(url_hash, verdict_type, prompt_version) DO UPDATE SET "
            "verdict_json=excluded.verdict_json, model=excluded.model, "
            "created_at=excluded.created_at",
            (
                url_hash,
                verdict_type,
                prompt_version,
                json.dumps(verdict, ensure_ascii=False),
                model,
                datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        conn.close()

    def ai_cache_stats(self) -> dict:
        """{entries, hits, misses} to show the hit-rate in /settings.
        hits/misses are in-memory counters of this process (see __init__)."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ai_verdict_cache")
        entries = cur.fetchone()[0]
        conn.close()
        return {
            "entries": entries,
            "hits": self._ai_cache_hits,
            "misses": self._ai_cache_misses,
        }


    # ------------------------------------------------------------------ #
    # Embedded scheduler (8.1): scheduled_jobs mirror table
    # ------------------------------------------------------------------ #

