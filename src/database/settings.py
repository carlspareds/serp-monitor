"""Settings query mixin: positive/negative domains, custom settings, branding, and scheduled jobs."""
import re
from datetime import UTC, datetime

from common import crypto
from database.base import (
    _BRANDING_DEFAULTS,
    _HEX_COLOR_RE,
)
from database.migrations import (
    LEGACY_SUBJECT_SETTINGS,
    apply_legacy_setting_to_subject_1,
)


class SettingsMixin:
    """Queries and mutations for positive/negative domains, settings, and jobs."""

    # Multi-subject phase 1: positive/negative domains belong to a subject (PK (domain,
    # subject_id)). Writes go to the ACTIVE subject (for_subject / request ContextVar /
    # 1); reads and deletes only see that subject's rows. The static, deployment-wide
    # list in config/positive_domains.py is still merged on top by the callers
    # (config.positive_domains.merged_with).
    _DOMAIN_TABLES = ("positive_domains", "negative_domains")

    def _add_domain(self, table: str, domain: str) -> None:
        if table not in self._DOMAIN_TABLES:  # identifier whitelist (interpolated below)
            raise ValueError(f"unknown domain table {table!r}")
        domain = (domain or "").strip().lower()
        if not domain:
            return
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(f"INSERT OR IGNORE INTO {table} (domain, subject_id) VALUES (?, ?)",  # noqa: S608 -- table is a fixed identifier from code, never user input
                    (domain, self._active_subject_id(None)))
        conn.commit()
        conn.close()

    def _remove_domain(self, table: str, domain: str) -> None:
        if table not in self._DOMAIN_TABLES:  # identifier whitelist (interpolated below)
            raise ValueError(f"unknown domain table {table!r}")
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(f"DELETE FROM {table} WHERE domain = ? AND {subject_clause}",  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound); table is a fixed identifier from code, never user input
                    ((domain or "").strip().lower(), *subject_params))
        conn.commit()
        conn.close()

    def _list_domains(self, table: str) -> list[str]:
        if table not in self._DOMAIN_TABLES:  # identifier whitelist (interpolated below)
            raise ValueError(f"unknown domain table {table!r}")
        subject_clause, subject_params = self._subject_clause()
        conn = self._connect()
        cur = conn.cursor()
        # DISTINCT: a for_all_subjects() instance would otherwise list a domain once
        # per subject that owns it.
        cur.execute(f"SELECT DISTINCT domain FROM {table} WHERE {subject_clause} "  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound); table is a fixed identifier from code, never user input
                    "ORDER BY domain", subject_params)
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        return rows

    # Positives
    def add_positive_domain(self, domain: str) -> None:
        self._add_domain("positive_domains", domain)

    def remove_positive_domain(self, domain: str) -> None:
        self._remove_domain("positive_domains", domain)

    def list_positive_domains(self) -> list[str]:
        return self._list_domains("positive_domains")

    # Negatives
    def add_negative_domain(self, domain: str) -> None:
        self._add_domain("negative_domains", domain)

    def remove_negative_domain(self, domain: str) -> None:
        self._remove_domain("negative_domains", domain)

    def list_negative_domains(self) -> list[str]:
        return self._list_domains("negative_domains")

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #
    def get_setting(self, key: str, default: str = "") -> str:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return default
        # Secret-ish values may be stored encrypted (common.crypto).
        return crypto.decrypt(row[0]) if crypto.is_encrypted(row[0]) else row[0]

    def set_setting(self, key: str, value: str) -> None:
        if key in crypto.SENSITIVE_SETTINGS and value:
            value = crypto.encrypt(value)
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        # Multi-subject phase 1 bridge: the legacy single-subject keys (still written
        # by /settings and the wizard until phase 2) are mirrored into subject 1's
        # row, which is now the source of truth -- same transaction, so the two can't
        # diverge.
        if key in LEGACY_SUBJECT_SETTINGS:
            apply_legacy_setting_to_subject_1(cur, key, value)
        conn.commit()
        conn.close()

    def encrypt_sensitive_settings(self) -> int:
        """Startup migration: with APP_ENCRYPTION_KEY set, encrypt every
        SENSITIVE_SETTINGS value still stored in plain text. Returns how many
        rows changed (0 without a key). Idempotent."""
        if not crypto.key_configured() or crypto.check_key():
            return 0
        conn = self._connect()
        try:
            keys = sorted(crypto.SENSITIVE_SETTINGS)
            rows = conn.execute(
                f"SELECT key, value FROM settings WHERE key IN ({','.join('?' for _ in keys)})",  # noqa: S608 -- only '?' placeholders are interpolated
                keys).fetchall()
            changed = 0
            for key, value in rows:
                if value and not crypto.is_encrypted(value):
                    conn.execute("UPDATE settings SET value = ? WHERE key = ?",
                                 (crypto.encrypt(value), key))
                    changed += 1
            conn.commit()
            return changed
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # Client branding (5.1)
    # ------------------------------------------------------------------ #
    def get_branding(self) -> dict:
        """Single row (id=1) with sane defaults (_BRANDING_DEFAULTS) if
        it doesn't exist yet, or if any field is empty / has a corrupt
        color (manual DB edit, legacy row...) -- it falls back to THAT
        field's default, never raises."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "SELECT logo_path, primary_color, secondary_color, footer_text "
            "FROM branding WHERE id = 1"
        )
        row = cur.fetchone()
        conn.close()
        out = dict(_BRANDING_DEFAULTS)
        if row:
            for key in out:
                value = row[key]
                if value:
                    out[key] = value
        for key in ("primary_color", "secondary_color"):
            if not _HEX_COLOR_RE.match(out[key]):
                out[key] = _BRANDING_DEFAULTS[key]
        return out

    def set_branding(self, logo_path: str | None = None, primary_color: str | None = None,
                      secondary_color: str | None = None, footer_text: str | None = None) -> None:
        """Updates ONLY the fields passed (None = don't touch -- e.g. a
        branding POST without a new file mustn't delete the current logo).
        Colors outside #rrggbb fall back to THAT field's default instead of
        saving garbage (brief 5.1: "if not -> default")."""
        current = self.get_branding()
        if logo_path is not None:
            # Review 5.1 S6: the only legitimate value is the one _save_logo writes -- an
            # arbitrary path in the DB would travel straight to the filesystem
            # (project_root / path discards the base if path is absolute).
            if logo_path and not re.fullmatch(r"data/branding/logo\.(png|jpe?g)", logo_path):
                logo_path = ""
            current["logo_path"] = logo_path
        if primary_color is not None:
            current["primary_color"] = (primary_color if _HEX_COLOR_RE.match(primary_color)
                                        else _BRANDING_DEFAULTS["primary_color"])
        if secondary_color is not None:
            current["secondary_color"] = (secondary_color if _HEX_COLOR_RE.match(secondary_color)
                                          else _BRANDING_DEFAULTS["secondary_color"])
        if footer_text is not None:
            current["footer_text"] = footer_text
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO branding (id, logo_path, primary_color, secondary_color, footer_text) "
            "VALUES (1, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET logo_path = excluded.logo_path, "
            "primary_color = excluded.primary_color, secondary_color = excluded.secondary_color, "
            "footer_text = excluded.footer_text",
            (current["logo_path"], current["primary_color"], current["secondary_color"],
             current["footer_text"]),
        )
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------ #
    # Notes / status per URL
    # ------------------------------------------------------------------ #

    def ensure_scheduled_job(self, name: str, default_cron: str) -> None:
        """Inserts the row if it didn't exist (doesn't touch cron_expr/enabled if the
        operator already customized them from /settings) — this way a service
        restart keeps the changes instead of overwriting them with the default."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO scheduled_jobs (name, cron_expr, enabled) VALUES (?, ?, 1)",
            (name, default_cron),
        )
        conn.commit()
        conn.close()

    def get_scheduled_job(self, name: str) -> dict | None:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT * FROM scheduled_jobs WHERE name = ?", (name,))
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None

    def list_scheduled_jobs(self) -> list[dict]:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT * FROM scheduled_jobs ORDER BY name")
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    def set_scheduled_job_cron(self, name: str, cron_expr: str, enabled: bool = True) -> None:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "UPDATE scheduled_jobs SET cron_expr = ?, enabled = ? WHERE name = ?",
            (cron_expr, 1 if enabled else 0, name),
        )
        conn.commit()
        conn.close()

    def record_job_run(self, name: str, status: str, duration_ms: int | None) -> None:
        """Updates the result of the last run (called by the
        APScheduler listeners in scheduler/core.py)."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "UPDATE scheduled_jobs SET last_run_at = ?, last_status = ?, "
            "last_duration_ms = ? WHERE name = ?",
            (datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"), status, duration_ms, name),
        )
        conn.commit()
        conn.close()

    def update_job_next_run(self, name: str, next_run_at: str | None) -> None:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(
            "UPDATE scheduled_jobs SET next_run_at = ? WHERE name = ?",
            (next_run_at, name),
        )
        conn.commit()
        conn.close()


    # ------------------------------------------------------------------ #
    # HTTP monitors (B13, deep dive §B13) -- CRUD for watched endpoints
    # ------------------------------------------------------------------ #
    _HTTP_MONITOR_METHODS = ("GET", "HEAD", "POST")


    # ------------------------------------------------------------------ #
    # Evidence vault (3.2)
    # ------------------------------------------------------------------ #

