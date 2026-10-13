"""Audit query mixin: tamper-evident audit logging, hash chaining, and API keys."""
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime

from database.base import (
    API_KEY_SCOPE_DEFAULT,
    API_KEY_SCOPES,
)


class AuditMixin:
    """Queries and mutations for audit logging and API keys."""

    @staticmethod
    def _audit_row_canonical(row) -> str:
        """Canonical repr of an audit_log row for the chained hash.

        CONTRACT (don't change without invalidating the entire existing
        chain): json.dumps with sort_keys=True over exactly these 8 fields
        -- {at, actor_type, actor_id, action, resource_type, resource_id,
        params_json, ip} -- it NEVER includes id or row_hash/prev_hash (the
        prev_hash is prepended separately, outside this repr). `row` can be
        a dict or a sqlite3.Row: it's only accessed by key `row[...]`.
        """
        return json.dumps(
            {
                "at": row["at"],
                "actor_type": row["actor_type"],
                "actor_id": row["actor_id"],
                "action": row["action"],
                "resource_type": row["resource_type"],
                "resource_id": row["resource_id"],
                "params_json": row["params_json"],
                "ip": row["ip"],
            },
            sort_keys=True,
            ensure_ascii=False,
        )

    def add_audit(self, actor_type: str, actor_id: str, action: str,
                  resource_type: str = "", resource_id: str = "",
                  params: dict | None = None, ip: str = "") -> None:
        """Inserts a row into audit_log -- ONLY INSERT: there is no
        UPDATE/DELETE on this table anywhere in the code (C24 acceptance,
        verified by a static grep test over src/). Chains
        row_hash = sha256(prev_hash + canonical_repr(row)).hexdigest(); the
        table's first row uses prev_hash="" (see _audit_row_canonical
        for the exact format, it's a contract).

        prev_hash is read with `SELECT row_hash ... ORDER BY id DESC LIMIT 1`
        INSIDE a transaction opened with `BEGIN IMMEDIATE` -- this is
        mandatory, not cosmetic (audit-race fix): an earlier docstring for
        this method claimed that "with journal_mode=WAL... there's no race
        condition between reading the last row_hash and inserting the next
        row" -- that assumption was FALSE and it broke the chain under
        concurrent writers. WAL serializes
        INSERTs against each other, but a plain SELECT doesn't take a writer
        lock: two threads calling add_audit at the same instant (e.g. two
        APScheduler jobs firing in the same minute/second) could
        read the SAME prev_hash, and whichever committed second ended up chained
        against a parent that was no longer the immediately preceding one.
        `BEGIN IMMEDIATE` acquires the writer lock BEFORE the SELECT, so
        the second thread blocks (busy_timeout=5000ms) until the
        first commits, and then reads the row_hash the first one already
        inserted -- read+write are now genuinely serialized. Regression test:
        test_audit.py::
        test_add_audit_serializes_concurrent_writes_with_begin_immediate
        (2 threads x N=20, fails reproducibly without this BEGIN
        IMMEDIATE).

        NEVER raises through this API except from the INSERT itself (the
        caller -- dashboard/main.py's `audit()` helper -- is the one
        that wraps the call in try/except so that an audit failure
        doesn't bring down the route that triggered it)."""
        at = datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")
        params_json = json.dumps(params or {}, sort_keys=True, ensure_ascii=False, default=str)
        conn = self._connect()
        # isolation_level=None -> real autocommit: this way we control the explicit BEGIN
        # IMMEDIATE below ourselves, instead of the driver issuing its own BEGIN
        # (deferred, and only before the INSERT -- which is exactly the behavior that was
        # causing the race).
        conn.isolation_level = None
        cur = conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            cur.execute("SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1")
            prev = cur.fetchone()
            prev_hash = prev[0] if prev else ""
            fields = {
                "at": at, "actor_type": actor_type, "actor_id": actor_id, "action": action,
                "resource_type": resource_type, "resource_id": resource_id,
                "params_json": params_json, "ip": ip,
            }
            row_hash = hashlib.sha256(
                (prev_hash + self._audit_row_canonical(fields)).encode("utf-8")
            ).hexdigest()
            cur.execute(
                "INSERT INTO audit_log (at, actor_type, actor_id, action, resource_type, "
                "resource_id, params_json, ip, row_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (at, actor_type, actor_id, action, resource_type, resource_id,
                 params_json, ip, row_hash),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_audit(self, limit: int = 200, action: str = "", resource_id: str = "") -> list[dict]:
        """Last `limit` rows, most recent to oldest. `resource_id`
        filters the complete history of a resource (e.g. a URL) --
        combined with DESC order, the most recent entry comes first."""
        conn = self._connect()
        cur = conn.cursor()
        query = "SELECT * FROM audit_log WHERE 1=1"
        args: list = []
        if action:
            query += " AND action = ?"
            args.append(action)
        if resource_id:
            query += " AND resource_id = ?"
            args.append(resource_id)
        query += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        cur.execute(query, args)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def audit_actions(self) -> list[str]:
        """Distinct actions in the log (the Settings → Audit log filter)."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")
        rows = [r[0] for r in cur.fetchall() if r[0]]
        conn.close()
        return rows

    def audit_chain_report(self, limit: int = 5000) -> dict:
        """FIX 2 (audit-race fix): walks
        audit_log in insertion order (id ASC) recomputing each chained
        row_hash and comparing it against the saved one -- like
        verify_audit_chain, but instead of a blind boolean it returns WHAT
        is broken and from where:

        {"ok": bool, "checked": n, "broken_ids": [id, ...], "first_break": id|None}

        Before this fix, verify_audit_chain() returned False on the
        production DB and there was no way to know whether it was ONE tampered
        row or 30 concurrency collisions (the real bug, see FIX 1) without
        dropping down to raw SQL. `/audit` uses this method to show the detail.

        Note on chaining past a broken row: the hash of the NEXT
        row keeps being compared against the broken row's SAVED `row_hash`
        (not against the recomputed `expected`) -- this way a single
        badly chained row (e.g. a historical concurrency collision, FIX 1)
        is reported as ONE isolated break instead of cascading down
        everything that comes after, which is already chaining correctly
        again from there. It's the same approach verify_audit_chain
        already used."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, at, actor_type, actor_id, action, resource_type, resource_id, "
            "params_json, ip, row_hash FROM audit_log ORDER BY id ASC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        conn.close()
        prev_hash = ""
        checked = 0
        broken_ids: list[int] = []
        for row in rows:
            checked += 1
            expected = hashlib.sha256(
                (prev_hash + self._audit_row_canonical(row)).encode("utf-8")
            ).hexdigest()
            if expected != row["row_hash"]:
                broken_ids.append(row["id"])
            prev_hash = row["row_hash"]
        return {
            "ok": not broken_ids,
            "checked": checked,
            "broken_ids": broken_ids,
            "first_break": broken_ids[0] if broken_ids else None,
        }

    def verify_audit_chain(self, limit: int = 5000) -> bool:
        """Walks audit_log in insertion order (id ASC) recomputing
        each chained row_hash and comparing it against the saved one. True if the
        chain is consistent; False as soon as it detects an altered row
        (or the gap a deleted row leaves -- although the code never
        deletes, this would also detect it if someone did it from outside).

        Boolean wrapper around audit_chain_report (FIX 2) -- the
        signature is kept as-is because there are callers that only want the
        boolean (e.g. this branch's tests from before the fix). To know WHICH
        row broke and from where, use audit_chain_report directly.

        # ponytail: `limit` caps the work per request (/audit uses it on
        # every page load) -- it verifies from the first row up to
        # `limit` rows, enough while the log comfortably fits in one
        # pass. If the audit log grew a lot, this would need to be paginated
        # or the result cached instead of walking everything on every request.
        """
        return self.audit_chain_report(limit)["ok"]

    # ------------------------------------------------------------------ #
    # API keys (9.3): /api/v1/... auth via the X-API-Key header
    # ------------------------------------------------------------------ #
    def add_api_key(self, name: str, scopes: str = API_KEY_SCOPE_DEFAULT,
                    subject_id: int | None = None) -> tuple[int, str]:
        """Generates a new API key and persists it HASHED (sha256) --
        NEVER in plaintext. Returns (id, raw_key): raw_key is the only moment
        in this credential's life where it exists as plain text inside
        the process; the caller (dashboard.main, /settings section) must
        show it to the operator ONCE and never store it (not in a log,
        not in audit() -- see audit()'s docstring in dashboard/main.py:
        "NEVER pass secret values in params").

        token_hex(32) -- 256 bits, the same generator (secrets, cryptographic)
        dashboard.main already uses for session cookies (`sid`).

        `subject_id`: None = an "All subjects" key (the API's `subject` param
        picks one, subject 1 by default); an id = a key PINNED to that subject
        (dashboard.routers.api_v1.resolve_api_subject)."""
        raw_key = secrets.token_hex(32)
        key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        scopes = scopes if scopes in API_KEY_SCOPES else API_KEY_SCOPE_DEFAULT
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO api_keys (key_hash, name, scopes, created_at, subject_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (key_hash, name, scopes,
             datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"),
             int(subject_id) if subject_id is not None else None),
        )
        key_id = cur.lastrowid
        conn.commit()
        conn.close()
        return key_id, raw_key

    def list_api_keys(self) -> list[dict]:
        """Listing for /settings -- never includes key_hash (no need to
        expose it even hashed; see the test that confirms the
        plaintext key never matches what's persisted)."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, name, scopes, created_at, last_used, revoked_at, subject_id "
            "FROM api_keys ORDER BY id DESC"
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def revoke_api_key(self, key_id: int) -> bool:
        """Sets revoked_at (idempotent: revoking twice doesn't overwrite the
        original timestamp). Returns True if this call revoked the row
        (False if it was already revoked or doesn't exist)."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
            (datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"), key_id),
        )
        revoked = cur.rowcount > 0
        conn.commit()
        conn.close()
        return revoked

    def verify_api_key(self, raw_key: str) -> dict | None:
        """Verifies a plaintext API key against the saved hashes.

        Constant-time comparison (hmac.compare_digest) over the
        hash -- never `==` (special care from brief 9.3, avoids a
        timing oracle on the credential). Returns None both if the
        key doesn't exist and if it exists but is revoked: the caller
        (dashboard.routers.api_v1's FastAPI dependency) responds 401 in
        both cases INDISTINGUISHABLY -- it must not leak which keys
        exist. Never updates last_used here (see touch_api_key_last_
        used, a separate, best-effort write)."""
        if not raw_key:
            return None
        computed = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT id, name, scopes, revoked_at, key_hash, subject_id FROM api_keys")
        rows = cur.fetchall()
        conn.close()
        match = None
        for row in rows:
            if hmac.compare_digest(row["key_hash"], computed):
                match = dict(row)
                break
        if match is None or match["revoked_at"]:
            return None
        match.pop("key_hash", None)
        return match

    def touch_api_key_last_used(self, key_id: int) -> None:
        """Updates last_used -- best-effort, NEVER raises (same approach
        as audit(): a failure of this write must not bring down the request
        that was already successfully authenticated)."""
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute(
                "UPDATE api_keys SET last_used = ? WHERE id = ?",
                (datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"), key_id),
            )
            conn.commit()
            conn.close()
        except Exception:  # noqa: BLE001, S110 -- see docstring, intentional best-effort
            pass



