"""Results query mixin: search results, filtering, history pivot, and reclassification."""
import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any

from database.base import (
    _SERIES_NULL_SEVERITY,
    url_hash,
)


class ResultsMixin:
    """Queries and mutations for organic search results."""

    def save_results(
        self, results: list[dict[str, Any]], run_id: str | None = None,
        subject_id: int | None = None,
    ) -> None:
        """Inserts the normalized results linked to their run.

        subject_id (A11, peer benchmarking -- deep dive §A11; C21b:
        default None, resolved by _active_subject_id -- see save_run) --
        same approach as save_run, see its docstring. An explicit value
        applies ONCE per call (every row in THIS batch belongs to the same
        run_id, hence the same subject).

        Confidence score (2.1): if `r` carries 'confidence' (classify_batch
        adds it since 2.1), a result with confidence below
        settings.review_threshold enters the review queue directly
        (review_status='pending'). Without 'confidence' (legacy caller /
        test seed) the result is left 'none' -- there's no confidence to
        evaluate, so the queue isn't inflated with data that never asked for it.

        H2 (2.1, "Sisyphus queue"): if this URL already has a HUMAN verdict
        from a prior observation, the new row inherits it directly
        (classified_by='human', confidence=1.0, review_status='approved')
        instead of re-asking -- a new run shouldn't reopen something a
        human already settled. Comparison by url_hash (the same URL
        identity as the rest of the system, see normalize_url/url_hash above).

        same_person (2.3): if `r` carries 'same_person' (processing/enrich.py::
        annotate_same_person annotates it before calling here -- this method does
        NOT call the AI, it only persists what's already calculated),
        it's saved as-is. 'unsure' enters the review queue (review_status='pending')
        just like a low confidence -- unless the row is already classified_by=
        'human' (branch above), where the human already settled it and the state stays
        'approved' regardless of same_person.

        engine (1.1): if `r` carries 'engine' (apify.runner.ApifyRunner.search
        tags it on each row), it's saved as-is; without that key
        (legacy caller / test seed) it falls back to 'google' -- same honest
        default as the column (everything was Google before this item).

        device (1.3): same approach as engine -- if `r` carries 'device' it's
        saved as-is, without that key it falls back to 'desktop'.
        """
        subject_id = self._active_subject_id(subject_id)
        conn = self._connect()
        cur = conn.cursor()
        now = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
        threshold = self.review_threshold()
        # C21b: the inherited human verdict (H2, 2.1) can ONLY come from a row of the SAME
        # subject -- without this filter, a result hand-corrected by subject B would be
        # inherited into new runs of subject A that share the same URL, a real leak of a
        # verdict (and of the existence of that row) between clients.
        # Human verdicts always win, per PAGE (normalized URL): the LATEST human
        # label of the page (human_at, then id) is inherited, and a "not this person"
        # sticks (see ReviewMixin._human_target).
        human_by_hash: dict[str, dict] = {}
        if results:
            self._fill_url_keys(cur)
            groups: dict[str, list] = {}
            for row in cur.execute(
                "SELECT id, url_key, classification, classified_by, same_person, human_at "
                "FROM search_results WHERE classified_by = 'human' AND subject_id = ?",
                (subject_id,),
            ).fetchall():
                groups.setdefault(row["url_key"], []).append(row)
            human_by_hash = {k: self._human_target(g) for k, g in groups.items()}
        for r in results:
            key = url_hash(r.get("url") or "")
            human = human_by_hash.get(key)
            same_person = r.get("same_person")
            if human is not None:
                classification = human["classification"]
                confidence = 1.0
                classified_by = "human"
                review_status = "approved"
                same_person = human["same_person"] or same_person
            else:
                classification = r.get("classification")
                confidence = r.get("confidence")
                classified_by = r.get("classified_by") or "rule"
                review_status = "pending" if confidence is not None and confidence < threshold else "none"
                if r.get("same_person") == "unsure":
                    review_status = "pending"
            cur.execute(
                """
                INSERT INTO search_results
                    (run_id, query, url, title, description, displayed_url,
                     position, country_code, domain, classification, scraped_at,
                     confidence, classified_by, review_status, same_person, engine,
                     device, subject_id, url_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    r.get("query"),
                    r.get("url"),
                    r.get("title"),
                    r.get("description"),
                    r.get("displayed_url"),
                    r.get("position"),
                    r.get("country_code"),
                    r.get("domain"),
                    classification,
                    now,
                    confidence,
                    classified_by,
                    review_status,
                    same_person,
                    r.get("engine") or "google",
                    r.get("device") or "desktop",
                    subject_id,
                    key,
                ),
            )
        conn.commit()
        conn.close()
        print(f"[DB] Saved {len(results)} results (run {run_id})")

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    # Columns allowed for sorting (anti-injection whitelist)
    _SORT_COLUMNS = {
        "position": "position",
        "classification": "classification",
        "country": "country_code",
        "recent": "id",
        # Sort by severity (2.2): the "Sev" pill in /results.
        "severity": "severity",
    }
    # Columns that can be NULL and must be pushed to the end of the sort regardless of
    # direction (ASC/DESC) -- position (legacy) and severity (2.2, negatives not yet
    # calculated or non-negative results).
    _NULLABLE_SORT_COLUMNS = {"position", "severity"}

    def get_results_filtered(
        self,
        classification: str = "",
        country_code: str = "",
        date: str = "",
        sort_by: str = "recent",
        order: str = "desc",
        limit: int = 200,
        run_id: str = "",
        include_homonyms: bool = False,
        min_severity: int = 0,
        engine: str = "",
        device: str = "",
    ) -> list[dict]:
        """Results with filters (classification, country, date, run, engine,
        device) and ordering.

        include_homonyms (2.3): defaults to False -- excludes same_person=
        'no' (obvious homonyms). NULL (not evaluated) is ALWAYS included:
        only 'no' is an explicit exclusion. The "include homonyms" toggle in
        /results passes include_homonyms=True.

        min_severity (2.2): > 0 filters to severity >= min_severity. A
        NEGATIVE with severity NULL (not scored yet, e.g. no AI provider)
        counts as _SERIES_NULL_SEVERITY (3) -- the same fallback alerts and
        the series use -- so "≥3" never hides every negative of an install
        without AI. Non-negatives with NULL severity stay excluded.

        engine (1.1): filter by engine ('' = no filter). Whitelisted
        against apify.actors.ACTORS.keys() -- a value outside that list
        (a hand-crafted query param) is ignored instead of being interpolated or
        raising, same defensive approach as the rest of /results's
        parameters (see H14 further below in dashboard.main.show_results).

        device (1.3): filter by device ('' = no filter). Whitelisted
        against apify.actors.VALID_DEVICES -- same defensive approach as
        `engine` above.
        """
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        clauses, params = [subject_clause], list(subject_params)
        if classification:
            clauses.append("classification = ?")
            params.append(classification)
        if country_code:
            clauses.append("country_code = ?")
            params.append(country_code)
        if date:
            clauses.append("substr(scraped_at, 1, 10) = ?")
            params.append(date)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        if not include_homonyms:
            clauses.append("(same_person IS NULL OR same_person != 'no')")
        if min_severity > 0:
            clauses.append("(CASE WHEN severity IS NULL AND classification = 'negative' "
                           "THEN ? ELSE severity END) >= ?")
            params.extend([_SERIES_NULL_SEVERITY, min_severity])
        if engine:
            from apify.actors import ACTORS
            if engine in ACTORS:
                clauses.append("engine = ?")
                params.append(engine)
        if device:
            from apify.actors import VALID_DEVICES
            if device in VALID_DEVICES:
                clauses.append("device = ?")
                params.append(device)
        where = "WHERE " + " AND ".join(clauses)

        col = self._SORT_COLUMNS.get(sort_by, "id")
        direction = "ASC" if str(order).lower() == "asc" else "DESC"
        nulls = f"{col} IS NULL, " if col in self._NULLABLE_SORT_COLUMNS else ""
        params.append(limit)
        cur.execute(
            f"SELECT * FROM search_results {where} "  # noqa: S608 -- ORDER BY column/direction from a whitelist; where is built from constant fragments; values bound
            f"ORDER BY {nulls}{col} {direction} LIMIT ?",
            params,
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def search_text(self, q: str, limit: int = 100) -> dict[str, Any]:
        """Global text search (3.3) over title/description via FTS5.

        Returns {"results": [row...], "error": str|None}. Never raises
        sqlite3.OperationalError: if `q` carries broken FTS5 operator
        syntax, it retries treating it as a literal phrase (escaped quotes);
        if that also fails, it returns empty results + a message.
        """
        q = (q or "").strip()
        if not q:
            return {"results": [], "error": None}

        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause(alias="sr")
        sql = (
            "SELECT sr.* FROM results_fts f "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
            "JOIN search_results sr ON sr.id = f.rowid "
            f"WHERE results_fts MATCH ? AND {subject_clause} ORDER BY rank LIMIT ?"
        )
        try:
            rows = cur.execute(sql, (q, *subject_params, limit)).fetchall()
        except sqlite3.OperationalError:
            literal = '"' + q.replace('"', '""') + '"'
            try:
                rows = cur.execute(sql, (literal, *subject_params, limit)).fetchall()
            except sqlite3.OperationalError:
                conn.close()
                return {"results": [], "error": "Invalid search syntax."}
        conn.close()
        return {"results": [dict(r) for r in rows], "error": None}

    def dates_for_country(self, country_code: str) -> list[str]:
        """Dates (YYYY-MM-DD) with data for a country, most recent to oldest."""
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        cur.execute(
            "SELECT DISTINCT substr(scraped_at, 1, 10) AS d FROM search_results "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
            f"WHERE country_code = ? AND scraped_at IS NOT NULL AND {subject_clause} ORDER BY d DESC",
            (country_code, *subject_params),
        )
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        return rows

    def get_latest_run_id(self, country_code: str, date: str = "", engine: str = "google") -> str:
        """run_id of the most recent run for a country (optionally on a
        date), for ONE engine (default 'google' -- review MEDIUM #3).

        engine (1.1): without this filter, every consumer of a single run_id
        (analysis diff, alerts, health_score, weekly, timeline,
        ai_assistant) would silently mix in the most recently
        INSERTED run regardless of its engine -- with one DB run per engine
        (see apify.runner.ApifyRunner.search::engine_runs), that could return
        the bing run when the consumer assumes Google (curves/diffs from
        an engine that isn't the production one). The default 'google' pins
        ALL those consumers to the production engine without touching them.
        /results uses latest_run_ids() instead (it wants ALL
        engines, not just one)."""
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        if date:
            cur.execute(
                "SELECT run_id FROM search_results WHERE country_code = ? "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                f"AND substr(scraped_at, 1, 10) = ? AND engine = ? AND {subject_clause} "
                "ORDER BY id DESC LIMIT 1",
                (country_code, date, engine, *subject_params),
            )
        else:
            cur.execute(
                "SELECT run_id FROM search_results WHERE country_code = ? "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                f"AND engine = ? AND {subject_clause} ORDER BY id DESC LIMIT 1",
                (country_code, engine, *subject_params),
            )
        row = cur.fetchone()
        conn.close()
        return row[0] if row else ""

    def latest_run_ids(self, country_code: str, date: str = "") -> list[str]:
        """Most recent run_id of EACH (ENGINE, DEVICE) present for a
        country (optionally on a date) -- 1.1: get_latest_run_id returns
        A SINGLE run_id (that of the most recent row), which is enough when
        everything is a single run/engine but drops the other engines of a
        multi-engine run ("one DB run per engine", see apify.runner.
        ApifyRunner.search). /results uses this version so it can show
        rows from ALL engines (acceptance 1.1: "classified rows from
        both engines, filterable in /results"). With a single engine in the
        data, it returns a list of ONE element -- the same run_id as
        get_latest_run_id, so pre-1.1 behavior doesn't change.

        device (1.3): GROUP BY engine, device -- without this, a country with
        both desktop AND mobile runs of the same engine would collapse into one
        (the most recently inserted), hiding the other device
        entirely from /results even without filtering by device. With a single
        device in the data (default 'desktop'), GROUP BY engine,
        device is identical to GROUP BY engine -- pre-1.3 behavior
        doesn't change."""
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        if date:
            cur.execute(
                "SELECT run_id, MAX(id) FROM search_results WHERE country_code = ? "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                f"AND substr(scraped_at, 1, 10) = ? AND {subject_clause} GROUP BY engine, device",
                (country_code, date, *subject_params),
            )
        else:
            cur.execute(
                "SELECT run_id, MAX(id) FROM search_results WHERE country_code = ? "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                f"AND {subject_clause} GROUP BY engine, device",
                (country_code, *subject_params),
            )
        rows = [r[0] for r in cur.fetchall() if r[0]]
        conn.close()
        return rows


    def reclassify_all(self, classify_fn, recompute_pending: bool = False,
                        limit: int | None = None) -> int:
        """
        Recalculates the classification (and confidence/classified_by, 2.1) of
        results with `classify_fn` (record -> dict {'classification',
        'confidence', 'classified_by'}) and recomputes each run's counts.
        Returns how many results changed classification.

        limit (FIX 1 point 1b, hardening audit): row cap per
        call -- without it, a SELECT with no LIMIT over the WHOLE search_results
        replays the entire table through `classify_fn` in a single pass (the
        cache keyed by url_hash saves it in steady state, but bumping PROMPT_VERSIONS
        invalidates it entirely at once, and one click reclassifies everything at once).
        None (default) preserves the previous unlimited behavior -- still
        used by /domains/reclassify (rules only, cheap, no AI).
        With `limit`, the SELECT takes a RANDOM sample (ORDER BY RANDOM())
        of that size instead of always the first N rows by id: this way
        successive clicks gradually cover different chunks of the backlog instead
        of forever repeating the same N rows (there's no
        "last reclassified" column that would allow a real deterministic
        "oldest first" order) -- used by
        /domains/reclassify-ai (dashboard.main._reclassify_batch_limit).

        IMMUTABILITY of the human verdict (2.1): rows with classified_by=
        'human' are EXCLUDED -- a mass reclassification (rules or AI)
        never overwrites a human correction that's already been fixed.

        M6 (2.1): if the row already had classified_by='ai' and the rule only
        produces the "default neutral" (confidence 0.5 -- no rule
        applied), the existing AI verdict is kept: a pass of
        RULES ONLY must not degrade something the AI already decided.

        recompute_pending (2.1): if True, it also evaluates the review queue
        after the change in BOTH directions -- confidence < review_threshold
        enters 'pending', confidence >= threshold exits to 'none' (a 'pending'
        row that the AI reclassifies with more confidence must be able to leave
        the queue, not just enter it). Used by /domains/reclassify-ai; the
        rules-only pass (/domains/reclassify) doesn't touch the queue on purpose
        (by design -- avoids recomputing it on every
        manual reclassification).

        M7: only writes (UPDATE) rows whose classification/confidence/
        classified_by/review_status actually changed -- avoids unnecessary
        churn in the FTS5 triggers (see _ensure_fts) on every mass
        pass over the whole table.

        Filtered by subject (review C21b): reclassifying is a mass
        MUTATION; without a filter, a pass launched from one subject's view
        would rewrite the classification of every other subject's rows. Returning
        just a count doesn't make it harmless -- the damage is in what it
        writes, not in what it shows.
        """
        # Human verdicts always win, per page: rows of a page a person labelled
        # (in ANY run) take that verdict first and never reach classify_fn.
        self.reapply_human_verdicts()
        subject_clause, subject_params = self._subject_clause()
        query = (
            "SELECT id, run_id, url, title, description, domain, classification, confidence, "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
            "classified_by, review_status, ai_reason FROM search_results "
            f"WHERE (classified_by != 'human' OR classified_by IS NULL) AND {subject_clause}"
        )
        params: tuple = tuple(subject_params)
        if limit is not None:
            query += " ORDER BY RANDOM() LIMIT ?"
            params = (*subject_params, limit)
        conn = self._connect()
        try:
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()
        threshold = self.review_threshold() if recompute_pending else None

        # Phase 1 -- compute every verdict with NO connection/transaction
        # open. classify_fn may call the AI (seconds per call) and the AI
        # layer writes on its own connections (verdict cache, provider
        # status); holding a write transaction here made those writes wait
        # for busy_timeout and fail with "database is locked" (live test).
        updates = []
        changed = 0
        for r in rows:
            rec = {"domain": r["domain"], "url": r["url"], "run_id": r["run_id"],
                   "title": r["title"], "description": r["description"]}
            res = classify_fn(rec)
            new_cls = res["classification"]
            new_conf = res.get("confidence", 1.0)
            new_by = res.get("classified_by", "rule")
            new_reason = (res.get("reason") or None) if new_by == "ai" else None

            if (r["classified_by"] == "ai" and new_cls == "neutral"
                    and new_conf == 0.5 and new_by == "rule"):
                new_cls, new_conf, new_by = r["classification"], r["confidence"], r["classified_by"]
                new_reason = r["ai_reason"]

            new_status = r["review_status"]
            if recompute_pending and new_status in ("none", "pending"):
                new_status = "pending" if new_conf < threshold else "none"

            if (new_cls != r["classification"] or new_conf != r["confidence"]
                    or new_by != r["classified_by"] or new_status != r["review_status"]
                    or new_reason != r["ai_reason"]):
                updates.append((new_cls, new_conf, new_by, new_status, new_reason, r["id"]))
            if new_cls != r["classification"]:
                changed += 1

        # Phase 2 -- one short write transaction. A row a person labelled
        # while the AI was working is left alone (classified_by='human').
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.executemany(
                "UPDATE search_results SET classification=?, confidence=?, "
                "classified_by=?, review_status=?, ai_reason=? WHERE id=? "
                "AND (classified_by != 'human' OR classified_by IS NULL) "
                "AND NOT EXISTS (SELECT 1 FROM search_results h WHERE h.subject_id = "
                "search_results.subject_id AND h.url_key = search_results.url_key "
                "AND h.classified_by = 'human')",
                updates,
            )
            for run in cur.execute("SELECT apify_run_id FROM runs").fetchall():
                self._recompute_run_counts(cur, run["apify_run_id"])
            conn.commit()
        finally:
            conn.close()
        return changed

    # ------------------------------------------------------------------ #
    # Human review queue (2.1)
    # ------------------------------------------------------------------ #

    def get_history_pivot(self) -> dict:
        """
        Builds the historical pivot for the HIST sheet:
          rows = URL (+ last known title),
          columns = scrape date (YYYY-MM-DD),
          value = best (lowest) organic position that day.

        Excludes same_person='no' (2.3) from both queries (positions and
        title/classification per URL) -- no toggle, see get_stats.

        Engine dimension (1.1): rows are grouped by (url, engine) --
        but the "engine" key is ONLY added to each row of the result if
        there's MORE THAN ONE engine in the data. With a single engine (today's
        case, and that of any DB not using 1.1) the shape of the dict is
        IDENTICAL to before this item -- no existing consumer
        (excel_exporter._build_hist_sheet, tests) sees a new key it
        didn't ask for.
        """
        homonym_clause = "AND (same_person IS NULL OR same_person != 'no')"
        conn = self._connect()
        cur = conn.cursor()
        subject_clause, subject_params = self._subject_clause()
        cur.execute(
            f"""
            SELECT url,
                   engine,
                   substr(scraped_at, 1, 10) AS d,
                   MIN(position) AS pos
            FROM search_results
            WHERE url IS NOT NULL AND url <> '' {homonym_clause} AND {subject_clause}
            GROUP BY url, engine, d
            ORDER BY url, engine, d
            """,  # noqa: S608 -- constant fragment; subject_clause is the constant 'subject_id = ?' (values bound)
            subject_params,
        )
        agg = cur.fetchall()

        # Last known title and classification per (url, engine)
        cur.execute(
            f"""
            SELECT url, engine, title, classification FROM search_results
            WHERE url IS NOT NULL AND url <> '' {homonym_clause} AND {subject_clause}
            ORDER BY id
            """,  # noqa: S608 -- constant fragment; subject_clause is the constant 'subject_id = ?' (values bound)
            subject_params,
        )
        titles, classes = {}, {}
        engines_seen: set[str] = set()
        for url, engine, title, classification in cur.fetchall():
            engine = engine or "google"
            key = (url, engine)
            if title:
                titles[key] = title
            if classification:
                classes[key] = classification
            engines_seen.add(engine)
        conn.close()

        multi_engine = len(engines_seen) > 1
        dates = sorted({row[2] for row in agg})
        rows_by_key: dict[tuple[str, str], dict] = {}
        for url, engine, d, pos in agg:
            engine = engine or "google"
            key = (url, engine)
            entry = rows_by_key.setdefault(
                key,
                {
                    "url": url,
                    "title": titles.get(key, ""),
                    "classification": classes.get(key, "neutral"),
                    "positions": {},
                },
            )
            if multi_engine:
                entry["engine"] = engine
            entry["positions"][d] = pos

        return {"dates": dates, "rows": list(rows_by_key.values())}

    def series_stats(self, country: str = "", date_from: str = "", date_to: str = "",
                     engine: str = "google") -> list[dict]:
        """Daily aggregate for /analysis's evolution curves (4.1).

        day = substr(scraped_at, 1, 10) -- the SAME field that dates_for_
        country/get_latest_run_id/get_results_filtered/get_history_pivot
        use as a run's "date" throughout the rest of the code (not
        runs.created_at: introducing a second source of truth for the
        date would break consistency with those methods and with the tests that
        already control the date via scraped_at, see test_diff.py). If a day
        has several runs (same country or global view), their rows are aggregated
        together -- they're observations from the same day.

        country="" aggregates ALL countries (global view). Excludes
        same_person='no' (2.3) -- same invariant as get_stats/
        get_history_pivot/_filtered_run_counts, no toggle (same as
        get_stats: there's no page that needs to see homonyms here).

        engine (1.1, review MEDIUM #4): default 'google' -- without this
        filter, with 2 engines the /analysis curves (and the timeline event
        impact, which consumes this series) would inflate ~2× and neg_avg_position would mix rankings from
        different engines (a position 3 from bing isn't comparable to a
        position 3 from google). Callers don't change -- the default is
        already the production engine.

        neg_urls counts RAW URL (COUNT DISTINCT url), not url_hash --
        search_results has no url_hash column (it's computed on the fly with
        database.models.url_hash() where needed, e.g. processing/
        diff.py). Two variants of the same URL (with/without utm_, with/without
        trailing slash...) that normalize_url() would collapse into one can
        be double-counted here as 2 distinct negative URLs.

        Returns one row per day with {day, total, positive, negative,
        neutral, pct_positive, pct_negative, pct_neutral,
        neg_avg_position, neg_urls, severity_sum} sorted by ascending
        date. neg_avg_position is None if that day had no negatives.

        severity_sum (4.3): SUM of `severity` of that
        day's negatives, using _SERIES_NULL_SEVERITY (3, "medium") for
        the ones that don't have a calculated severity yet -- same
        neutral-conservative approach as processing.alerts.NULL_SEVERITY (see
        the _SERIES_NULL_SEVERITY comment: it isn't imported from here to
        avoid an import cycle, the value is duplicated). Never NULL: 0 if
        the day had no negatives.
        """
        subject_clause, subject_params = self._subject_clause()
        clauses = ["(same_person IS NULL OR same_person != 'no')", "engine = ?", subject_clause]
        params: list = [engine, *subject_params]
        if country:
            clauses.append("country_code = ?")
            params.append(country)
        # Sargable (review 4.1): comparing scraped_at directly against the bounds (instead
        # of applying substr(scraped_at, 1, 10) to EVERY row in the WHERE) allows using an
        # index on scraped_at -- e.g. idx_search_results_country_scraped when there's a
        # country filter. The substr() in the SELECT/GROUP BY below stays: it's actually
        # needed there to group by day. EXCLUSIVE upper bound (< date_to + 1 day) because
        # scraped_at carries a time -- ">= date_to AND <= date_to" would leave out almost
        # all of that day.
        if date_from:
            clauses.append("scraped_at >= ?")
            params.append(date_from)
        if date_to:
            next_day = (date.fromisoformat(date_to) + timedelta(days=1)).isoformat()
            clauses.append("scraped_at < ?")
            params.append(next_day)
        where = "WHERE " + " AND ".join(clauses)

        conn = self._connect()
        cur = conn.cursor()
        # The first '?' (severity fallback, severity_sum) appears in the SELECT, BEFORE
        # the '?'s from {where} in the final text -- it goes first in the params list to
        # respect sqlite3's positional order.
        cur.execute(
            f"""
            SELECT substr(scraped_at, 1, 10) AS day,
                   COUNT(*) AS total,
                   SUM(CASE WHEN classification = 'positive' THEN 1 ELSE 0 END) AS positive,
                   SUM(CASE WHEN classification = 'negative' THEN 1 ELSE 0 END) AS negative,
                   SUM(CASE WHEN classification = 'neutral' OR classification IS NULL
                            THEN 1 ELSE 0 END) AS neutral,
                   AVG(CASE WHEN classification = 'negative' THEN position END) AS neg_avg_position,
                   COUNT(DISTINCT CASE WHEN classification = 'negative' THEN url END) AS neg_urls,
                   SUM(CASE WHEN classification = 'negative'
                            THEN COALESCE(severity, ?) ELSE 0 END) AS severity_sum
            FROM search_results
            {where}
            GROUP BY day
            ORDER BY day
            """,  # noqa: S608 -- where is built from constant fragments; values bound
            [_SERIES_NULL_SEVERITY, *params],
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()

        out = []
        for row in rows:
            # total/positive/negative/neutral/neg_urls/severity_sum are never NULL here:
            # GROUP BY day only produces groups with >=1 real row (the one that matched
            # the WHERE), and SUM(CASE ... ELSE 0)/COUNT(*)/COUNT(DISTINCT ...) don't
            # return NULL over a non-empty group -- only AVG (neg_avg_position) can be, if
            # no result that day was negative.
            total = row["total"]
            positive = row["positive"]
            negative = row["negative"]
            neutral = row["neutral"]
            out.append({
                "day": row["day"],
                "total": total,
                "positive": positive,
                "negative": negative,
                "neutral": neutral,
                "pct_positive": round(100 * positive / total, 1),
                "pct_negative": round(100 * negative / total, 1),
                "pct_neutral": round(100 * neutral / total, 1),
                "neg_avg_position": (round(row["neg_avg_position"], 1)
                                      if row["neg_avg_position"] is not None else None),
                "neg_urls": row["neg_urls"],
                "severity_sum": row["severity_sum"],
            })
        return out
