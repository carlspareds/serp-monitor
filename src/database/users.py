"""Users query mixin: user accounts, authentication, and multi-tenant scoping."""
import os
import sqlite3
from datetime import UTC, datetime

from auth.passwords import hash_password, needs_rehash, verify_password
from database.base import (
    _DUMMY_PASSWORD_HASH,
    USER_ROLES,
)


class UsersMixin:
    """Queries and mutations for users, roles, and subjects."""

    _USER_PUBLIC_COLUMNS = ("id", "client_id", "username", "role", "active",
                             "created_at", "last_login_at")

    @classmethod
    def _public_user(cls, row) -> dict:
        """A `users` row WITHOUT `password_hash`. It's the only format that leaves
        this class upward: the hash has no business traveling to a
        template, a log, or an HTTP response, and what doesn't leave doesn't get
        leaked (the same lesson as B20's network dump, and as api_keys's
        keys, which are also only stored hashed)."""
        return {k: row[k] for k in cls._USER_PUBLIC_COLUMNS}

    def add_user(self, username: str, password: str, role: str = "operator",
                  client_id: int | None = None) -> int:
        """Creates a user. `password` is the PLAIN-TEXT password: it is hashed
        here (auth.passwords.hash_password) and only the hash is stored --
        never pass an already hashed value. A duplicate `username` propagates the UNIQUE constraint's IntegrityError
        as-is, with no wrapper of its own: the DB is the one that truly
        guarantees uniqueness (two simultaneous requests can't get around
        it), so a prior check in Python would only give a false
        sense of control."""
        role = (role or "operator").strip()
        if role not in USER_ROLES:
            raise ValueError(f"role must be one of {list(USER_ROLES)}, got {role!r}")
        username = (username or "").strip()
        if not username:
            raise ValueError("username is required")
        if not password:
            raise ValueError("password is required")
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO users (client_id, username, password_hash, role, active, "
                "created_at) VALUES (?, ?, ?, ?, 1, ?)",
                (client_id, username, hash_password(password), role,
                 datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")),
            )
            user_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
        return user_id

    def list_users(self) -> list[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users ORDER BY id")
            return [self._public_user(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def get_user(self, user_id: int) -> dict | None:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = cur.fetchone()
        finally:
            conn.close()
        return self._public_user(row) if row else None

    def get_user_by_username(self, username: str) -> dict | None:
        """Public user by name -- used by the session's role
        resolution (dashboard.main.session_role), which only has the
        username saved in _SESSIONS."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE username = ?", ((username or "").strip(),))
            row = cur.fetchone()
        finally:
            conn.close()
        return self._public_user(row) if row else None

    def set_user_active(self, user_id: int, active: bool) -> None:
        """Deactivating is the way to remove access WITHOUT deleting the row: the
        `audit_log` (C24) references users by their actor_id, and deleting them
        would leave the trail pointing at nobody."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id))
            conn.commit()
        finally:
            conn.close()

    def set_user_password(self, user_id: int, password: str) -> None:
        """Replaces a user's password. `password` is the PLAIN-TEXT password:
        it is hashed here (auth.passwords.hash_password); only the hash is
        stored."""
        if not password:
            raise ValueError("password is required")
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (hash_password(password), user_id))
            conn.commit()
        finally:
            conn.close()

    def authenticate_user(self, username: str, password: str) -> dict | None:
        """Returns the public user if the password matches AND the account
        is active; None in any other case -- WITHOUT distinguishing a
        nonexistent user from a wrong password or from a deactivated account, which
        is what keeps the login from being used as an oracle to find out which
        users exist (same approach as a revoked API key, which
        returns the same 401 as a nonexistent one).

        `last_login_at` is updated ONLY on success: if it were also touched
        on failure, it would stop serving its one purpose -- knowing
        when each account really logged in, and detecting the ones nobody
        uses anymore."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE username = ?", ((username or "").strip(),))
            row = cur.fetchone()
            # It's ALWAYS verified against a real hash, even if the user doesn't exist or
            # is deactivated (review C21a): pbkdf2 with 600,000 iterations takes ~370 ms,
            # so cutting short earlier turned the login into an oracle -- measured, 369 ms
            # for a user that exists versus 53 ms for one that doesn't, enough to
            # enumerate accounts AND find out which ones are deactivated. The result is
            # decided AFTERWARD, once the same cost has already been paid.
            stored = row["password_hash"] if row is not None else _DUMMY_PASSWORD_HASH
            password_ok = verify_password(password, stored)
            if row is None or not row["active"] or not password_ok:
                return None
            cur.execute("UPDATE users SET last_login_at = ? WHERE id = ?",
                         (datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"), row["id"]))
            if needs_rehash(stored):
                # Upgrade-on-login: the plain password is only available right
                # now, so this is the moment to re-hash it with today's
                # parameters (auth.passwords._ITERATIONS).
                cur.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                            (hash_password(password), row["id"]))
            conn.commit()
            return self._public_user(row)
        finally:
            conn.close()

    def ensure_admin_seed(self) -> dict | None:
        """Seeds the AUTH_USER/AUTH_PASS admin *on demand* -- the
        login calls it (brief C21a §4). Covers the case where the variables
        are added to .env AFTER the first startup: without this, the
        empty table would force a process restart just to be able to log in. Returns
        the seeded user, or None if there was nothing to seed (no
        variables, or the table already populated -- it never adds a second
        admin nor overwrites what the operator edited)."""
        username = os.getenv("AUTH_USER", "").strip()
        password = os.getenv("AUTH_PASS", "")
        if not username or not password:
            return None
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM users")
            if cur.fetchone()[0] != 0:
                return None
        finally:
            conn.close()
        try:
            user_id = self.add_user(username, password, role="admin", client_id=None)
        except sqlite3.IntegrityError:
            # Two simultaneous logins against the empty table both pass the COUNT: the
            # second one loses the race against the UNIQUE and has nothing left to seed --
            # it's not an error that should bubble up as a 500 to the operator who's
            # trying to log in.
            return None
        return self.get_user(user_id)

    def list_clients(self, include_inactive: bool = True) -> list[dict]:
        """Clients are shown as PROJECTS in the UI (a project groups subjects).
        `include_inactive=False` hides archived ones (the subject switcher)."""
        conn = self._connect()
        try:
            cur = conn.cursor()
            sql = "SELECT * FROM clients"
            if not include_inactive:
                sql += " WHERE active = 1"
            cur.execute(sql + " ORDER BY id")
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def get_client(self, client_id: int) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None

    def add_client(self, name: str) -> int:
        """Creates a project (a `clients` row) and returns its id."""
        name = " ".join((name or "").split())
        if not name:
            raise ValueError("name is required")
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO clients (name, active, created_at) VALUES (?, 1, ?)",
                (name, datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")),
            )
            client_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
        return client_id

    def rename_client(self, client_id: int, name: str) -> None:
        name = " ".join((name or "").split())
        if not name:
            raise ValueError("name cannot be blank")
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE clients SET name = ? WHERE id = ?", (name, client_id))
            if cur.rowcount == 0:
                raise ValueError(f"project {client_id} does not exist")
            conn.commit()
        finally:
            conn.close()

    def set_client_active(self, client_id: int, active: bool) -> None:
        """Archive (active=False) or restore a project. Archiving also
        deactivates its subjects (soft delete, data kept); the default project
        (id 1, which holds the default subject) can't be archived."""
        if not active and client_id == 1:
            raise ValueError("the default project cannot be archived")
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE clients SET active = ? WHERE id = ?", (1 if active else 0, client_id))
            if cur.rowcount == 0:
                raise ValueError(f"project {client_id} does not exist")
            if not active:
                cur.execute("UPDATE subjects SET active = 0 WHERE client_id = ? AND id != 1",
                            (client_id,))
            conn.commit()
        finally:
            conn.close()

    def set_subject_project(self, subject_id: int, client_id: int) -> None:
        """Moves a subject to another project (clients row)."""
        if self.get_client(client_id) is None:
            raise ValueError(f"project {client_id} does not exist")
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE subjects SET client_id = ? WHERE id = ?", (client_id, subject_id))
            if cur.rowcount == 0:
                raise ValueError(f"subject {subject_id} does not exist")
            conn.commit()
        finally:
            conn.close()



    # FIX 3 (audit hardening): 41 tables, only 4 purge routines -- extends purge_checks
    # (scheduler/jobs.py) with per-table retention. The EXACT same pattern as
    # purge_monitor_checks/purge_notify_queue above: cutoff = now - N days, DELETE,
    # returns rows deleted, best-effort by the caller's design
    # (scheduler/jobs.py::purge_checks already runs each purge independently, none brings
    # down the others).
    #
    # audit_log is LEFT OUT on purpose (documented arbitration, see the hardening brief
    # §FIX 3): it's a chained hash chain (row_hash = sha256(prev_hash + row), see
    # add_audit/verify_audit_chain) where EVERY row proves the integrity of ALL the
    # previous ones back to the first (prev_hash=""). Purging a contiguous block from the
    # start (the only safe way -- never a hole in the middle) would leave the new oldest
    # row with a row_hash that was computed against a prev_hash from a row that no longer
    # exists: verify_audit_chain can no longer recompute it from scratch, so that row
    # would have to be treated as a trust "checkpoint" that can't be verified against
    # anything -- that quietly weakens the tamper-evidence guarantee C24 built (an
    # attacker with write access to the DB could rewrite that "anchor" row without
    # anything detecting it). Implementing this properly requires storing a separate
    # protected checkpoint (outside this DB, or signed), out of scope for this fix. At 942
    # rows today (the largest table, but far from a real disk problem on a small host),
    # it's left OUT. Trigger: if audit_log grows to a point that's actually concerning
    # (several GB, or a specific row ceiling the operator sets), design the protected
    # checkpoint as its own item, not as an afterthought here.
    #
    # search_results isn't purged either -- it's the FACTS table (the source of truth): the
    # complete history of what appeared in the SERP on each date is the primary data from
    # which url_lifecycle/analytics/exports/Excel are derived; purging it would lose
    # irreplaceable evidence (unlike evidence/ai_answers/etc., which are derivable or
    # recapturable byproducts). Its FTS index (results_fts) exactly follows search_results
    # via the _ensure_fts triggers -- with no purge on the base table, there's nothing to
    # purge there either.

