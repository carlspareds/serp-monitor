"""Alerts query mixin: alert lifecycle, routing, notifications log, and victories."""
from datetime import UTC, datetime, timedelta

from processing.alert_types import normalize_severity


class AlertsMixin:
    """Queries and mutations for alerts, routing, and notifications."""

    def add_alert(self, kind: str, severity: str, country: str, message: str,
                  dedupe_key: str, subject_id: int | None = None) -> int | None:
        """Inserts an alert if it doesn't exist (unique dedupe_key). `severity` is
        ALWAYS normalized to {info,warning,critical} before saving (7.1):
        legacy 'med'/'high' gets translated, any other value falls back to the
        catalog's default for that `kind` (see processing.alert_types.normalize_severity).
        Returns the inserted id (truthy, useful for the 7.1 dispatcher) or
        None if it already existed (dedupe) -- the 14 existing call sites only use
        the value as a boolean ("if db.add_alert(...):"), so the change
        from bool to int|None doesn't break anything for them.

        subject_id (C21b): same as save_run/save_results -- see
        _active_subject_id. No current call site passes it, so all of them
        keep writing the instance's active subject (or 1 with no
        active subject, IDENTICAL to before C21b).

        Lifecycle (7.2): it's still INSERT OR IGNORE -- there's no need
        for a prior SELECT by status. Dedupe is PER SUBJECT: the unique index
        is (dedupe_key, subject_id) (MigrationsMixin rebuilt the old global
        column UNIQUE), so the same condition on two subjects gives two
        alerts. resolve_alert() MUTATES the row's dedupe_key when
        resolving it (appends '#resolved#<id>'), so a resolved row
        NEVER keeps the original key. Consequence: if the condition
        is still open, the active row retains the original key and the INSERT
        OR IGNORE blocks it (return None, "no duplicate"); if the only row
        with that key has already been resolved, the original key is freed
        and the INSERT goes through cleanly with a new id ("new episode", see
        resolve_alert). The brief's "by status" condition is resolved for
        free by the UNIQUE index itself -- there's no need to reimplement
        it by hand here."""
        severity = normalize_severity(kind, severity)
        subject_id = self._active_subject_id(subject_id)
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO alerts "
            "(created_at, kind, severity, country, message, dedupe_key, subject_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"), kind, severity,
             country, message, dedupe_key, subject_id),
        )
        inserted_id = cur.lastrowid if cur.rowcount > 0 else None
        conn.commit()
        conn.close()
        return inserted_id

    def deployment_alert(self, dedupe_key: str) -> dict | None:
        """The newest alert with this dedupe key in ANY subject and ANY status
        (a resolved row's key carries a '#resolved#<id>' suffix, see
        resolve_alert), or None. For deployment-wide alerts (apify_budget) whose
        dedupe must not be per subject."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM alerts WHERE dedupe_key = ? OR substr(dedupe_key, 1, ?) = ? "
                "ORDER BY id DESC LIMIT 1",
                (dedupe_key, len(dedupe_key) + 10, dedupe_key + "#resolved#"),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def list_alerts(self, limit: int = 100, status: str = "active",
                     created_from: str = "", created_to: str = "") -> list[dict]:
        """`status` (7.2, /alerts UI filter): 'active' (default --
        open + acknowledged, WITHOUT filtering by snoozed: the history table
        must still be able to see a snoozed one within "active", only the
        sidebar badge hides it -- see count_active_alerts), 'open',
        'acknowledged', 'resolved' or 'all'. Any other value falls back to the
        safe default ('active') instead of unintentionally returning the entire
        table."""
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        clauses = [subject_clause]
        params: list = list(subject_params)
        if status == "all":
            pass
        elif status in ("open", "acknowledged", "resolved"):
            clauses.append("status = ?")
            params.append(status)
        else:  # 'active' or an unknown value
            clauses.append("status IN ('open', 'acknowledged')")
        # created_from/created_to (review 8.3 M3): range in SQL so the LIMIT applies AFTER
        # the month filter -- limit-before-filter was silently truncating reports for
        # older months.
        if created_from:
            clauses.append("created_at >= ?")
            params.append(created_from)
        if created_to:
            clauses.append("created_at <= ?")
            params.append(created_to + "T23:59:59" if len(created_to) == 10 else created_to)
        where = "WHERE " + " AND ".join(clauses) + " "
        params.append(limit)
        cur.execute(f"SELECT * FROM alerts {where}ORDER BY id DESC LIMIT ?", params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def count_unseen_alerts(self) -> int:
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        cur.execute(f"SELECT COUNT(*) FROM alerts WHERE seen = 0 AND {subject_clause}", subject_params)
        n = cur.fetchone()[0]
        conn.close()
        return n

    def mark_alerts_seen(self) -> None:
        """Marks the ACTIVE SUBJECT's alerts as seen. Without the filter,
        opening one subject's bell also marked other subjects' alerts
        as read -- and a read alert stops notifying, so the effect
        wasn't cosmetic: it silenced other subjects' warnings without leaving a trace."""
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(f"UPDATE alerts SET seen = 1 WHERE seen = 0 AND {subject_clause}",
                     subject_params)
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------ #
    # Alert lifecycle (7.2): ack / assign / snooze / resolve
    # ------------------------------------------------------------------ #
    def ack_alert(self, alert_id: int, assignee: str = "") -> None:
        """Acknowledges an alert: status='acknowledged' + optional assignee.
        Still counts as active (badge/count_active_alerts) until it's
        resolved or snoozed -- ack by itself doesn't hide it, it just says
        "someone is already looking at it"."""
        # The EXACT same argument as resolve_alert: an alert_id is opaque but not secret,
        # so without this filter you could acknowledge another subject's alert just by
        # trying ids.
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            f"UPDATE alerts SET status = 'acknowledged', assignee = ? "
            f"WHERE id = ? AND {subject_clause}",
            (assignee, alert_id, *subject_params),
        )
        conn.commit()
        conn.close()

    def snooze_alert(self, alert_id: int, until_iso: str) -> None:
        """Snoozes an alert until `until_iso` (naive-UTC ISO, same
        format as created_at). While snoozed_until > now, the alert
        doesn't count in the badge/active list (open_alerts/count_active_alerts) nor
        gets renotified -- see notify.dispatcher.notify_alert, which is only
        called on freshly created alerts (they never start out snoozed) so
        today this doesn't affect it; documented for when renotification exists.
        Doesn't touch `status`: snooze is orthogonal to the open/acknowledged/
        resolved cycle (an acknowledged one can be snoozed too)."""
        # Filtered by subject for the same reason as ack_alert/resolve_alert: snoozing
        # someone else's alert silences it.
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            f"UPDATE alerts SET snoozed_until = ? WHERE id = ? AND {subject_clause}",
            (until_iso, alert_id, *subject_params),
        )
        conn.commit()
        conn.close()

    def resolve_alert(self, alert_id: int, by: str = "human") -> None:
        """Resolves an alert: status='resolved', resolved_at=now (UTC),
        resolved_by=`by` ({human,auto}). MUTATES dedupe_key (appends
        '#resolved#<id>') -- see add_alert: dedupe_key is UNIQUE and SQLite
        doesn't support DROP CONSTRAINT, so mutating the key of the row that
        stops being active is the only way to free it up for a new
        episode without a destructive migration.
        # ponytail: ceiling = resolved rows accumulate mutated keys
        (finding the original episode requires LIKE, not equality); if that
        becomes a problem, move up to CREATE UNIQUE INDEX ... WHERE status != 'resolved'
        (partial index, supported since SQLite 3.8) and drop the column's
        UNIQUE, or move to a dedicated table.

        C21b: the UPDATE is also scoped to the active subject -- an
        alert_id is an opaque but not secret id, so without this filter
        anyone could resolve ANOTHER subject's alert by knowing (or
        guessing) its id."""
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        resolved_at = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
        # AND status != 'resolved': idempotent resolve (a double click doesn't mutate the
        # key again).
        cur.execute(
            "UPDATE alerts SET status = 'resolved', resolved_at = ?, resolved_by = ?, "
            "dedupe_key = dedupe_key || '#resolved#' || id "
            f"WHERE id = ? AND status != 'resolved' AND {subject_clause}",
            (resolved_at, by, alert_id, *subject_params),
        )
        conn.commit()
        conn.close()

    def open_alerts(self, include_snoozed: bool = False) -> list[dict]:
        """Active alerts: status IN ('open', 'acknowledged'). By default
        excludes currently snoozed ones (snoozed_until > now) -- deep dive
        §7.2 acceptance: "a snoozed alert doesn't notify or count in the
        badge until its date". include_snoozed=True returns them too
        (e.g. so auto_resolve can review them anyway -- a renewed ssl
        must close the alert whether it's snoozed or not)."""
        conn = self._connect()
        cur = conn.cursor()
        now = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
        subject_clause, subject_params = self._subject_clause()
        where = f"status IN ('open', 'acknowledged') AND {subject_clause}"
        params: list = list(subject_params)
        if not include_snoozed:
            where += " AND (snoozed_until IS NULL OR snoozed_until <= ?)"
            params.append(now)
        cur.execute(f"SELECT * FROM alerts WHERE {where} ORDER BY id DESC", params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def count_open_alerts(self) -> int:
        """Bell badge: alerts nobody has looked at yet -- status 'open' and
        not currently snoozed. Acknowledged ones are still active (they show
        in the popover, in their own group) but are NOT counted as open: the
        same definition as the Overview card, the assistant and the PDF."""
        return sum(1 for a in self.open_alerts() if (a.get("status") or "open") == "open")

    def count_active_alerts(self) -> int:
        """Count for the sidebar badge (7.2, replaces
        count_unseen_alerts there -- count_unseen_alerts is kept for the
        "unread" banner on Home, "seen" is orthogonal to the lifecycle).
        Active = open/acknowledged and NOT currently snoozed — the same
        predicate as open_alerts(), a single implementation (review 7.2)."""
        return len(self.open_alerts())

    # ------------------------------------------------------------------ #
    # URL lifecycle (3.1): Wins
    # ------------------------------------------------------------------ #
    def list_victories(self, days: int = 90, country: str = "",
                        since: str = "", until: str = "") -> list[dict]:
        """Wins/disappearances won: `gone` rows from url_lifecycle whose last
        seen classification was 'negative', with gone_since within the
        last `days` days -- feeds the "Wins" section of /analysis
        (maintained by processing.lifecycle.update_url_lifecycle). Excludes
        same_person='no' (H5, 3.1 arbitration): the same predicate as the rest
        of the system -- a homonym that disappears isn't a real win
        (same_person is persisted in the row WHEN MOVING TO 'gone', no
        need for a join to search_results here). `country` (H8, optional):
        respects /analysis's country filter when given. Most recent
        order first.

        since/until (review 8.3 M2): an EXPLICIT naive-UTC range that replaces
        `days` when given -- the monthly view needs an exact calendar
        month and `days` is computed against this method's REAL clock (not
        freezable in tests): mixing both clocks made the suite
        turn red on its own as time passed.

        C21b-2 (# ponytail, see report): url_lifecycle's PRIMARY KEY
        (url_hash, country, engine) does NOT include subject_id, and its ONLY
        writer (processing.lifecycle.update_url_lifecycle) writes raw SQL
        outside this class without stamping subject_id -- every row
        is born subject_id=1 via the column DEFAULT, whatever the
        real subject of the run is. Filtering here is correct for subject 1 (the
        only one that actually writes today) but is NOT enough by itself: a
        second subject wouldn't see ANY of their own wins until
        update_url_lifecycle passes subject_id -- write-side debt, outside
        the scope of this method (it isn't a `Database.*`, see the brief's
        Scope)."""
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        cur = conn.cursor()
        where = (f"WHERE status = 'gone' AND classification = 'negative' "
                 f"AND (same_person IS NULL OR same_person != 'no') AND {subject_clause}")
        params: list = list(subject_params)
        if since or until:
            if since:
                where += " AND gone_since >= ?"
                params.append(since)
            if until:
                where += " AND gone_since <= ?"
                params.append(until)
        else:
            cutoff = (datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)).isoformat(timespec="seconds")
            where += " AND gone_since >= ?"
            params.append(cutoff)
        if country:
            where += " AND country = ?"
            params.append(country)
        cur.execute(f"SELECT * FROM url_lifecycle {where} ORDER BY gone_since DESC", params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    # ------------------------------------------------------------------ #
    # Routing by severity (7.1)
    # ------------------------------------------------------------------ #
    def get_routing(self) -> dict[str, str]:
        """{severity: channel} for the 3 severities -- seeded by
        _ensure_alert_routing if the table was empty."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT severity, channel FROM alert_routing")
        routing = {r["severity"]: r["channel"] for r in cur.fetchall()}
        conn.close()
        return routing

    def set_routing(self, severity: str, channel: str) -> None:
        """Changes a severity's channel. Immediate effect: the dispatcher
        (notify.dispatcher.notify_alert) does get_routing() on every send,
        it doesn't cache anything in memory -- no restart needed."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO alert_routing (severity, channel) VALUES (?, ?) "
            "ON CONFLICT(severity) DO UPDATE SET channel = excluded.channel",
            (severity, channel),
        )
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------ #
    # Notifications per channel (7.4)
    # ------------------------------------------------------------------ #
    def add_notify_log(self, channel: str, ok: bool, error: str = "",
                       alert_id: int | None = None) -> None:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO notify_log (alert_id, channel, ok, at, error) "
            "VALUES (?, ?, ?, ?, ?)",
            (alert_id, channel, 1 if ok else 0,
             datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"), error or ""),
        )
        conn.commit()
        conn.close()

    def recent_notify_log(self, limit: int = 10) -> list[dict]:
        """Last `limit` sends, most recent to oldest."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT * FROM notify_log ORDER BY id DESC LIMIT ?", (limit,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows


    # ------------------------------------------------------------------ #
    # AI verdict cache per URL (2.5)
    # ------------------------------------------------------------------ #

    def get_alert(self, alert_id: int) -> dict | None:
        """Read by PK -- the only piece missing from the 7.2
        lifecycle so PATCH /api/v1/alerts/{id} (C22) can check
        existence and return the already-updated row without reimplementing
        list_alerts's filtering."""
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        cur.execute(f"SELECT * FROM alerts WHERE id = ? AND {subject_clause}", (alert_id, *subject_params))
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None


    def purge_notify_log(self, days: int = 180) -> int:
        """Send history per channel (7.4) -- grows one row per
        notification attempt, with no prior purge."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("DELETE FROM notify_log WHERE at < ?", (cutoff,))
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        return deleted

    def purge_resolved_alerts(self, days: int = 180) -> int:
        """Alerts that are ALREADY resolved (status='resolved') older than `days`
        (by resolved_at) -- open/acknowledged/snoozed ones are NEVER touched,
        only history that's already closed.

        C21b (guard, allowlist): a SCHEDULED retention job (scheduler/
        jobs.py), not a view -- it deletes closed alerts from ALL
        subjects alike, same age criterion for everyone. It doesn't
        expose or mix one subject's data into another's view (it doesn't
        return rows, only a count), so it doesn't need subject_id."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM alerts WHERE status = 'resolved' AND resolved_at IS NOT NULL "
            "AND resolved_at < ?",
            (cutoff,),
        )
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        return deleted



    def purge_gone_url_lifecycle(self, days: int = 365) -> int:
        """url_lifecycle (3.1) -- ONLY status='gone' rows (a URL that's
        still 'active'/'fading' is tracking in progress, never purged by
        age) older than `days` by gone_since."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM url_lifecycle WHERE status = 'gone' AND gone_since IS NOT NULL "
            "AND gone_since < ?",
            (cutoff,),
        )
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        return deleted


