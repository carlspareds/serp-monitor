"""Runs query mixin: execution runs, Apify costs, and run metadata."""
import json
from datetime import UTC, datetime

from database.base import DEFAULT_SUBJECT_ID
from processing.alert_types import normalize_severity

# The Apify budget alert is deployment-wide (one Apify account for every
# subject): one row a month, stored under the default subject. Settings →
# Search and every subject's alerts popover show it.
BUDGET_ALERT_SUBJECT_ID = DEFAULT_SUBJECT_ID


def budget_alert_key(month: str) -> str:
    """Dedupe key of the `apify_budget` alert for a 'YYYY-MM' month."""
    return f"apify_budget:{month}"


class RunsMixin:
    """Queries and mutations for search execution runs and Apify costs."""

    def save_run(
        self,
        apify_run_id: str,
        queries: list[str],
        country_code: str,
        status: str,
        summary: dict[str, int],
        cost_usd: float | None = None,
        compute_units: float | None = None,
        retries: int = 0,
        engine: str = "google",
        device: str = "desktop",
        subject_id: int | None = None,
    ) -> dict | None:
        """Inserts (or updates) a run's metadata row.

        cost_usd/compute_units (1.4) come from Apify's final run object
        (usageTotalUsd/stats.computeUnits); they're left NULL if the actor doesn't
        report them. If a known cost is saved, it checks the month's accumulated
        spend against the configured budget (see _check_apify_budget).

        Returns the newly inserted `apify_budget` alert row (FIX 1
        point 4, silent-alerts audit) if the budget was just
        exceeded, or None -- this module can't dispatch it itself (import
        cycle), so the CALLER must do `notify_alert(db, row)` if
        the returned value isn't None (see report.multi_country.
        persist_engine_run).

        engine (1.1): one DB run per engine -- the caller saves a row
        FOR EACH entry in outcome["engine_runs"] (see
        report.multi_country.persist_engine_run), each with its own
        apify_run_id (the real Apify run_id for THAT engine) and its own
        cost. Default 'google' preserves the behavior of legacy
        callers that don't pass it.

        device (1.3): same approach as engine -- 'desktop'|'mobile',
        default 'desktop' for legacy callers.

        subject_id (A11, peer benchmarking -- deep dive §A11; C21b: default
        None instead of 1): the explicit CALLER always wins -- a caller
        that passes another subject's OWN subject_id reuses this same
        table/method without mixing its rows with the main subject's
        (F2 golden rule). Without an explicit caller, the active
        subject of this instance is used (Database.for_subject) and,
        failing both, 1 -- see _active_subject_id, EXACTLY the same
        behavior as before C21b for any legacy caller that passes
        nothing."""
        subject_id = self._active_subject_id(subject_id)
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO runs
                (apify_run_id, queries, country_code, status,
                 total_results, positive, negative, neutral, created_at,
                 cost_usd, compute_units, retries, engine, device, subject_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(apify_run_id) DO UPDATE SET
                status=excluded.status,
                total_results=excluded.total_results,
                positive=excluded.positive,
                negative=excluded.negative,
                neutral=excluded.neutral,
                cost_usd=COALESCE(excluded.cost_usd, cost_usd),
                compute_units=COALESCE(excluded.compute_units, compute_units),
                retries=MAX(excluded.retries, retries),
                engine=COALESCE(excluded.engine, engine),
                device=COALESCE(excluded.device, device)
            """,
            (
                apify_run_id,
                json.dumps(queries, ensure_ascii=False),
                country_code,
                status,
                summary.get("total", 0),
                summary.get("positive", 0),
                summary.get("negative", 0),
                summary.get("neutral", 0),
                datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"),
                cost_usd,
                compute_units,
                retries,
                engine,
                device,
                subject_id,
            ),
        )
        conn.commit()
        conn.close()
        if cost_usd is not None:
            return self._check_apify_budget()
        return None

    # ------------------------------------------------------------------ #
    # Apify costs (1.4)
    # ------------------------------------------------------------------ #
    def apify_month_cost(self) -> dict[str, float]:
        """Accumulated Apify spend for the current UTC month: sum of cost_usd,
        count of runs with a known cost, and sum of compute_units.

        1.1 (review LOW #7): with "one DB run per engine" (see
        apify.runner.ApifyRunner.search::engine_runs), `runs` can have
        several rows per search cycle -- one PER ENGINE, each with
        its own cost -- so "runs" here counts runs per engine, not
        search cycles; the sum of cost_usd already reflects the real
        spend (all engines) with no adjustment needed."""
        month_prefix = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m")
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(cost_usd), 0), COUNT(*), COALESCE(SUM(compute_units), 0) "
            "FROM runs WHERE created_at LIKE ? AND cost_usd IS NOT NULL",
            (month_prefix + "%",),
        )
        cost_usd, n_runs, compute_units = cur.fetchone()
        conn.close()
        return {"cost_usd": cost_usd, "runs": n_runs, "compute_units": compute_units}

    def add_run_cost(self, apify_run_id: str, cost_usd: float | None,
                      compute_units: float | None) -> dict | None:
        """Adds cost_usd/compute_units to an ALREADY saved run (1.3, H3) --
        unlike save_run (which does a COALESCE: takes the new value if it's
        not None, otherwise keeps the existing one), this ADDS. No effect (it
        doesn't touch the row or check the budget) if both values are None.

        HISTORICAL (F4 wrap-up, PNG->HTML pivot): originally used by
        report.multi_country.persist_engine_run to bill the SERP screenshot
        -- a SEPARATE Apify run, with its own cost -- against the
        search run that triggered it. That capture is gone, so
        persist_engine_run no longer calls this method. It's kept as a
        generic cost-accounting utility (same mechanism, no
        active consumer today).

        Returns the `apify_budget` alert row (FIX 1 point 4, see
        save_run) if the budget was just exceeded, or None."""
        if cost_usd is None and compute_units is None:
            return None
        # Filtering by subject: someone else's apify_run_id shouldn't be able to inflate
        # the cost attributed to another subject (the 1.4 budget alerts on that figure).
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            f"UPDATE runs SET cost_usd = COALESCE(cost_usd, 0) + COALESCE(?, 0), "
            f"compute_units = COALESCE(compute_units, 0) + COALESCE(?, 0) "
            f"WHERE apify_run_id = ? AND {subject_clause}",
            (cost_usd, compute_units, apify_run_id, *subject_params),
        )
        conn.commit()
        conn.close()
        if cost_usd is not None:
            return self._check_apify_budget()
        return None

    def current_budget_alert(self) -> dict | None:
        """This month's deployment-wide `apify_budget` alert (any status), or
        None -- what Settings → Search and every subject's alerts popover show."""
        month = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m")
        return self.deployment_alert(budget_alert_key(month))

    def _check_apify_budget(self) -> dict | None:
        """If a monthly budget is configured (>0) and the month's accumulated
        spend exceeds it, creates the `apify_budget` alert (deduped per month:
        only once a month even if several more runs go over).

        FIX 1 point 4 (silent-alerts audit): this module CANNOT
        import notify.dispatcher.emit_alert (cycle: notify -> processing
        -> database) -- so, unlike the other alert producers, it
        doesn't dispatch here itself. Returns the newly inserted alert row
        ({id, kind, severity, country, message}, the SAME shape
        notify_alert expects) so that the EXTERNAL CALLER
        (Database.save_run/add_run_cost, and ultimately
        report.multi_country/visual, which CAN import notify.
        dispatcher) dispatches it. None if nothing was inserted (under
        budget, or already alerted this month -- dedupe)."""
        try:
            budget = float(self.get_setting("apify_monthly_budget", "0") or "0")
        except ValueError:
            budget = 0.0
        if budget <= 0:
            return None
        spent = self.apify_month_cost()["cost_usd"]
        if spent <= budget:
            return None
        month = datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m")
        key = budget_alert_key(month)
        # Deployment-wide: the budget is the whole deployment's Apify bill, so
        # the alert fires ONCE a month whichever subject's run crossed it
        # (dedupe across every subject and status, not the per-subject unique
        # index) and is stored under subject 1 (BUDGET_ALERT_SUBJECT_ID).
        if self.deployment_alert(key) is not None:
            return None
        msg = f"Apify monthly spend ${spent:.2f} exceeds budget ${budget:.2f}"
        severity = normalize_severity("apify_budget", "high")
        alert_id = self.add_alert("apify_budget", "high", "", msg, key,
                                  subject_id=BUDGET_ALERT_SUBJECT_ID)
        if not alert_id:
            return None
        return {"id": alert_id, "kind": "apify_budget", "severity": severity,
                "country": "", "message": msg, "subject_id": BUDGET_ALERT_SUBJECT_ID}


    @staticmethod
    def _recompute_run_counts(cur, run_id: str) -> None:
        """Recalculates positive/negative/neutral/total_results of ONE run
        from its current search_results (M5, 2.1). Shared by
        reclassify_all (all runs, after a mass reclassification) and
        set_review_verdict (the run of the corrected row, after an
        individual human correction)."""
        counts = {"positive": 0, "negative": 0, "neutral": 0, "total": 0}
        for rr in cur.execute(
            "SELECT classification FROM search_results WHERE run_id = ?", (run_id,)
        ).fetchall():
            counts[rr["classification"] or "neutral"] += 1
            counts["total"] += 1
        cur.execute(
            "UPDATE runs SET positive=?, negative=?, neutral=?, total_results=? "
            "WHERE apify_run_id=?",
            (counts["positive"], counts["negative"], counts["neutral"],
             counts["total"], run_id),
        )


    def set_run_ai_note(self, apify_run_id: str, note: str | None) -> None:
        """Stores (or clears, with None/'') the run's AI note (runs.ai_note)."""
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        cur.execute(
            f"UPDATE runs SET ai_note = ? WHERE apify_run_id = ? AND {subject_clause}",
            (note or None, apify_run_id, *subject_params),
        )
        conn.commit()
        conn.close()

    def delete_run(self, apify_run_id: str) -> None:
        """Deletes a run and all its results.

        C21b: with an active subject, the DELETE only reaches THAT
        subject's rows -- without this filter, anyone with another
        subject's apify_run_id (an opaque, but not secret, id) could delete
        their run."""
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        cur.execute(
            f"DELETE FROM search_results WHERE run_id = ? AND {subject_clause}",
            (apify_run_id, *subject_params),
        )
        cur.execute(
            f"DELETE FROM runs WHERE apify_run_id = ? AND {subject_clause}",
            (apify_run_id, *subject_params),
        )
        conn.commit()
        conn.close()

    def distinct_countries(self) -> list[str]:
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        cur.execute(
            "SELECT DISTINCT country_code FROM search_results "
            f"WHERE country_code IS NOT NULL AND country_code <> '' AND {subject_clause} "
            "ORDER BY country_code",
            subject_params,
        )
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        return rows

    def distinct_engines(self) -> list[str]:
        """Engines present in search_results (1.1) -- used by /results
        to decide whether it's worth showing the "Engine" chip/column
        (ONLY if there's more than one, see dashboard.main.show_results)."""
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        cur.execute(
            "SELECT DISTINCT engine FROM search_results "
            f"WHERE engine IS NOT NULL AND engine <> '' AND {subject_clause} ORDER BY engine",
            subject_params,
        )
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        return rows

    def distinct_devices(self) -> list[str]:
        """Devices present in search_results (1.3) -- same pattern
        as distinct_engines: used by /results to decide whether it's worth
        showing the "Device" chip/column (ONLY if there's more than one)."""
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        cur.execute(
            "SELECT DISTINCT device FROM search_results "
            f"WHERE device IS NOT NULL AND device <> '' AND {subject_clause} ORDER BY device",
            subject_params,
        )
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        return rows

    def get_stats(self, engine: str = "google") -> dict:
        """Global stats for the home page.

        Excludes same_person='no' (2.3, no toggle -- the only method with
        an include_homonyms toggle is get_results_filtered, which feeds
        /results; ponytail: no producer of an include_homonyms here).

        engine (1.1, review MEDIUM #4): default 'google' -- WITHOUT this
        filter, with 2 engines in the data (runs AND search_results, both
        with an `engine` column) these counts would inflate ~2× (each URL
        counted once per engine that saw it) on the home page. The caller
        (home()) doesn't change -- the default is already the correct one."""
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        cur.execute(
            f"SELECT COUNT(*) FROM runs WHERE engine = ? AND {subject_clause}",
            (engine, *subject_params),
        )
        total_runs = cur.fetchone()[0]
        where = f"WHERE (same_person IS NULL OR same_person != 'no') AND engine = ? AND {subject_clause}"
        cur.execute(f"SELECT COUNT(*) FROM search_results {where}", (engine, *subject_params))
        total_results = cur.fetchone()[0]
        cur.execute(
            f"SELECT classification, COUNT(*) FROM search_results {where} GROUP BY classification",
            (engine, *subject_params),
        )
        by_class = {"positive": 0, "negative": 0, "neutral": 0}
        for cls, n in cur.fetchall():
            by_class[cls or "neutral"] = n
        cur.execute(
            f"SELECT MAX(created_at) FROM runs WHERE engine = ? AND {subject_clause}",
            (engine, *subject_params),
        )
        last_run = cur.fetchone()[0]
        cur.execute(
            f"SELECT COUNT(DISTINCT country_code) FROM runs WHERE engine = ? AND {subject_clause}",
            (engine, *subject_params),
        )
        countries = cur.fetchone()[0]
        conn.close()
        return {
            "total_runs": total_runs,
            "total_results": total_results,
            "by_class": by_class,
            "last_run": last_run,
            "countries": countries,
        }

    def get_results_by_run(self, apify_run_id: str) -> list[dict]:
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        cur.execute(
            f"SELECT * FROM search_results WHERE run_id = ? AND {subject_clause} "
            "ORDER BY country_code, position",
            (apify_run_id, *subject_params),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    @staticmethod
    def _filtered_run_counts(cur, apify_run_id: str) -> dict:
        """positive/negative/neutral/total_results counts for ONE run,
        RECALCULATED from search_results excluding same_person='no' (H1,
        2.3). The columns saved in `runs` (see save_run/
        _recompute_run_counts) are the RAW count -- they don't know about
        same_person, so they're recomputed here on read instead of
        being trusted. Shared by latest_run_per_country, list_runs and
        get_run so that the home page ("Recent runs"), the country
        cards, and /run/{id}'s header agree with each other -- /run/{id}'s
        results TABLE still shows the raw run (drill-down),
        only this header summary is aligned.

        C21b (guard, allowlist): `apify_run_id` ALREADY identifies a single
        row of `runs`, which belongs to ONE subject -- the caller
        (list_runs/get_run/latest_run_per_country, all three filtered by
        subject) only ever passes run_ids here that already passed its own
        filter, so repeating the subject_id filter in this helper (@staticmethod,
        no `self`) would be redundant, not a leak."""
        counts = {"positive": 0, "negative": 0, "neutral": 0}
        total = 0
        for cls, n in cur.execute(
            "SELECT classification, COUNT(*) FROM search_results "
            "WHERE run_id = ? AND (same_person IS NULL OR same_person != 'no') "
            "GROUP BY classification",
            (apify_run_id,),
        ).fetchall():
            counts[cls or "neutral"] = counts.get(cls or "neutral", 0) + n
            total += n
        counts["total_results"] = total
        return counts

    @staticmethod
    def _filtered_run_counts_batch(cur, apify_run_ids: list[str]) -> dict[str, dict]:
        """Like _filtered_run_counts but for SEVERAL runs in A SINGLE query
        (4.1 point 5, N+1 fix): list_runs/latest_run_per_country used to call
        _filtered_run_counts PER ROW -- one extra GROUP BY run_id, classification
        query for every listed run. get_run (reading a single run) keeps
        the per-row version (_filtered_run_counts): it's not worth
        building a one-element list for that.

        Returns {apify_run_id: {"positive":.., "negative":.., "neutral":..,
        "total_results":..}} with ALL requested run_ids present (as
        zeros if the run has no results).

        C21b (guard, allowlist): same reason as _filtered_run_counts
        -- `apify_run_ids` already comes filtered by subject from the caller."""
        result = {rid: {"positive": 0, "negative": 0, "neutral": 0, "total_results": 0}
                   for rid in apify_run_ids}
        if not apify_run_ids:
            return result
        placeholders = ",".join("?" * len(apify_run_ids))
        cur.execute(
            f"SELECT run_id, classification, COUNT(*) FROM search_results "
            f"WHERE run_id IN ({placeholders}) "
            f"AND (same_person IS NULL OR same_person != 'no') "
            f"GROUP BY run_id, classification",
            apify_run_ids,
        )
        for run_id, cls, n in cur.fetchall():
            if run_id not in result:
                continue
            counts = result[run_id]
            counts[cls or "neutral"] = counts.get(cls or "neutral", 0) + n
            counts["total_results"] += n
        return result

    def list_runs(self, limit: int = 50) -> list[dict]:
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        cur.execute(
            f"SELECT * FROM runs WHERE {subject_clause} ORDER BY id DESC LIMIT ?",
            (*subject_params, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        counts_by_run = self._filtered_run_counts_batch(cur, [row["apify_run_id"] for row in rows])
        for row in rows:
            row.update(counts_by_run[row["apify_run_id"]])
        conn.close()
        return rows

    def get_run(self, apify_run_id: str) -> dict | None:
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        cur.execute(
            f"SELECT * FROM runs WHERE apify_run_id = ? AND {subject_clause}",
            (apify_run_id, *subject_params),
        )
        row = cur.fetchone()
        if row is None:
            conn.close()
            return None
        result = dict(row)
        result.update(self._filtered_run_counts(cur, apify_run_id))
        conn.close()
        return result

    def latest_run_per_country(self) -> list[dict]:
        """
        The most recent run for each country (by id, which is monotonic).
        Useful for building the "current state" export.

        Each run's positive/negative/neutral/total_results counts are
        RECALCULATED excluding same_person='no' -- see _filtered_run_counts.
        """
        conn = self._connect()
        cur = conn.cursor()
        # C21b: subject_id filters BOTH sides of the JOIN -- if only the outer table `r`
        # were filtered, the subquery's MAX(id) would still be the global id (potentially
        # of ANOTHER subject), and this subject's "most recent run" for that country
        # wouldn't find a match and would disappear from the result instead of showing up.
        subject_clause, subject_params = self._subject_clause(alias="r")
        subject_clause_inner, subject_params_inner = self._subject_clause()
        cur.execute(
            f"""
            SELECT r.* FROM runs r
            JOIN (
                SELECT country_code, MAX(id) AS max_id
                FROM runs WHERE {subject_clause_inner} GROUP BY country_code
            ) latest
            ON r.country_code = latest.country_code AND r.id = latest.max_id
            WHERE {subject_clause}
            ORDER BY r.country_code
            """,
            (*subject_params_inner, *subject_params),
        )
        rows = [dict(r) for r in cur.fetchall()]
        counts_by_run = self._filtered_run_counts_batch(cur, [row["apify_run_id"] for row in rows])
        for row in rows:
            row.update(counts_by_run[row["apify_run_id"]])
        conn.close()
        return rows


