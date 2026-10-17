"""Guard test (C21b, brief point 2 arbitration -- the EXACT same pattern
as tests/test_dispatch_guard.py, which already prevented the mute-alerts
bug from coming back): walks `Database` looking for methods that TOUCH a
table with `subject_id` (Database._SUBJECT_TABLES -- the code's REAL list,
not the one from the brief's measurement script, see the note in
`_unscoped_methods` below) without mentioning `subject_id` anywhere in its
body, and fails unless they're in the explicit allowlist below -- each
entry is a reviewable statement of WHY that method is legitimately global,
mutates an already-resolved row by id (not a view that lists another
subject's rows), or belongs to a table this delivery leaves out on purpose
("deferred C21b-2", see the brief's Scope: only search_results, runs,
alerts, goals and url_notes are filtered in C21b)."""
import ast
import re
from pathlib import Path

from database.models import Database

_MODELS_PATH = Path(__file__).resolve().parent.parent / "src" / "database" / "models.py"

# The 5 core tables that THIS delivery actually filters -- the rest of
# _SUBJECT_TABLES gets the "deferred C21b-2" reason.
_CORE_TABLES = {"search_results", "runs", "alerts", "goals", "url_notes"}


def _method_sources() -> dict[str, str]:
    """{method_name: source code} of each method defined on Database
    and its domain mixins in src/database/, via AST."""
    db_dir = Path(__file__).resolve().parent.parent / "src" / "database"
    out: dict[str, str] = {}
    for path in sorted(db_dir.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                        out[member.name] = ast.get_source_segment(src, member) or ""
    return out


# MUTATIONS count the same as reads: looking only at FROM/JOIN left
# `UPDATE alerts SET seen = 1` and friends invisible -- and that's exactly
# where it bites for real, because `ack_alert`/`snooze_alert`/
# `mark_alerts_seen` are the direct siblings of the `resolve_alert` that
# WAS filtered with an explicit safety argument ("an alert_id is opaque
# but not secret"). A guard that lets through exactly what was already
# identified as dangerous doesn't protect: it gives a false sense of
# closure, which is worse than not having it.
_TABLE_RE = {
    table: re.compile(rf"\b(FROM|JOIN|INTO|UPDATE|DELETE\s+FROM)\s+{table}\b", re.IGNORECASE)
    for table in Database._SUBJECT_TABLES
}


_SCOPED_MARKER_RE = re.compile(r"subject_id|_subject_clause|_active_subject_id")


def _code_only(source: str) -> str:
    """The method's source WITHOUT docstrings or comments.

    The marker used to be searched over the full source, so **writing
    "subject_id" in a docstring was enough to vanish from the guard**.
    This wasn't theoretical: three methods used to escape purely because
    of their prose, and two nearly identical siblings got opposite
    treatment depending on whether the comment used the literal word or a
    synonym. A guard that turns itself off by writing the right word in a
    comment is a guard that turns itself off by accident."""
    import io
    import tokenize

    out = []
    try:
        toks = tokenize.generate_tokens(io.StringIO(source).readline)
        prev_type = tokenize.INDENT
        for tok in toks:
            if tok.type == tokenize.COMMENT:
                continue
            # A STRING that opens a statement is a docstring (or a stray
            # expression): it's not SQL or a call, so it doesn't count.
            if tok.type == tokenize.STRING and prev_type in (
                tokenize.INDENT, tokenize.NEWLINE, tokenize.NL, tokenize.DEDENT,
            ):
                prev_type = tok.type
                continue
            out.append(tok.string)
            if tok.type not in (tokenize.NL, tokenize.NEWLINE):
                prev_type = tok.type
    except (tokenize.TokenError, IndentationError):
        return source  # when in doubt, do NOT hide anything from the guard
    return "\n".join(out)


def _unscoped_methods() -> dict[str, list[str]]:
    """{method_name: [tables]} of each method that touches (FROM/JOIN) a
    _SUBJECT_TABLES table with no textual trace of subject filtering in
    its body. Simple textual heuristic -- same approach as
    test_dispatch_guard.py (a grep, not a real semantic analyzer): looks
    for the literal string 'subject_id' (methods that write/filter it in
    their own SQL) OR a call to the two only C21b mechanisms that
    encapsulate it -- `self._subject_clause()` (read/filter) and
    `self._active_subject_id()` (resolving the active subject in an
    INSERT) -- because those two methods ARE where the word 'subject_id'
    really lives; a caller that uses `_subject_clause()` is filtering even
    if the string doesn't appear literally in ITS body."""
    unscoped = {}
    for name, body in _method_sources().items():
        code = _code_only(body)
        tables = sorted(t for t, rx in _TABLE_RE.items() if rx.search(code))
        if tables and not _SCOPED_MARKER_RE.search(code):
            unscoped[name] = tables
    return unscoped


# method_name -> written reason. Two valid reason families:
#
# 1. "deferred C21b-2: <table>" -- the method touches a _SUBJECT_TABLES
#    table that is NOT one of the 5 core ones for this delivery. Declared
#    and accounted-for debt (brief Scope), not an oversight.
#
# 2. Any other text -- the method DOES touch a core table but:
#    (a) it's infrastructure/migration that runs ONCE over ALL rows (not
#        a view a caller queries per-subject), or
#    (b) it's an internal helper (@staticmethod, no `self`) keyed by an
#        id/run_id the CALLER already resolved by filtering per subject, or
#    (c) it mutates a row known by id with no fan-out to other rows
#        (doesn't list or expose another subject's rows to the caller).
_ALLOWLIST: dict[str, str] = {
    # -- Entries uncovered by hardening the guard (C21b review): before,
    #    they were invisible because the regex only looked at FROM/JOIN, or
    #    because the marker was also searched for in docstrings and
    #    comments. Each one now carries a reviewable reason, which is
    #    exactly what the guard exists to enforce. --
    "_ensure_alert_severities":
        "one-shot _init_db migration: normalizes `severity` across the "
        "WHOLE table at boot. Runs before any session or active subject "
        "exists, and applies the same rule to every row -- exposes none "
        "of them to anyone.",
    "_ensure_alert_status":
        "one-shot _init_db migration, same case as "
        "_ensure_alert_severities: backfills `status` on legacy rows.",
    "_ensure_review_status":
        "one-shot _init_db migration: backfills `review_status` on "
        "search_results rows predating 2.1.",
    "_ensure_url_keys":
        "one-shot _init_db migration: computes search_results.url_key "
        "(a hash of the row's OWN url) for every row -- reads nothing "
        "across rows and exposes none of them to anyone.",
    "_fill_url_keys":
        "same derivation as _ensure_url_keys for rows written by a raw "
        "INSERT without url_key: each row gets the hash of its own url; "
        "nothing is read or returned across subjects.",
    "_ensure_note_status":
        "one-shot _init_db migration: normalizes url_notes `status` to "
        "the NOTE_STATUSES whitelist (4.6).",
    "purge_resolved_alerts":
        "SCHEDULED retention job (scheduler/jobs.py): deletes alerts "
        "already closed older than N days, with the SAME age cutoff for "
        "every subject. Doesn't return rows or expose them in any view "
        "-- just a count.",
    "_filtered_run_counts":
        "@staticmethod that receives an `apify_run_id` ALREADY resolved "
        "by its caller. Its three callers (list_runs, get_run, "
        "latest_run_per_country) filter by subject before invoking it, "
        "and since the C21b review so do set_review_verdict and "
        "reclassify_all -- which were the two that broke this guarantee.",
    # -- Writes to tables OUTSIDE this delivery's 5 core ones --
    # -- Infra / one-shot _init_db migrations (run over the WHOLE table,
    #    not a view a caller queries per subject) --
    "_ensure_fts":
        "FTS5 index maintenance over the WHOLE search_results table "
        "(trigger + idempotent initial backfill) -- not a view, subject "
        "filtering is applied on READ in search_text().",
    "serp_month_cost":
        "SERP provider cost for the month, every provider (whole-deployment "
        "billing), not a PER-subject figure -- _check_serp_budget uses "
        "it for a global budget alert, there's no 'subject 1 budget' "
        "distinct from 'subject 2 budget'.",
    "deployment_alert":
        "deployment-wide alerts (apify_budget): looks one dedupe key up across "
        "every subject so the monthly budget alert fires once per deployment, "
        "not once per subject. Returns at most that one row, which concerns "
        "every subject by definition (one Apify account).",
    # -- Internal helpers keyed by id/run_id already resolved by the caller --
    "_recompute_run_counts":
        "@staticmethod keyed by run_id -- called by reclassify_all and "
        "set_review_verdict, a run_id already identifies a single `runs` "
        "row for ONE subject; it doesn't list another subject's rows.",
    "_filtered_run_counts_batch":
        "@staticmethod keyed by a list of apify_run_id -- called ONLY by "
        "list_runs/latest_run_per_country (already filtered by subject): "
        "the run_ids it receives already passed the caller's filter.",
    # -- C21b-2: SCHEDULED retention jobs (scheduler/jobs.py) on tables
    #    outside the core set -- same
    #    approach as purge_resolved_alerts above: delete by age with the
    #    SAME cutoff for every subject, don't return rows or expose them
    #    in any view (just a count). --
    "purge_evidence":
        "SCHEDULED retention job (scheduler/jobs.py): deletes evidence "
        "older than N days for ALL subjects equally (also deletes the "
        "vault file, not a row from another table). Just a count, no "
        "row exposed.",
    "purge_gone_url_lifecycle":
        "SCHEDULED retention job (scheduler/jobs.py), same approach as "
        "purge_evidence: deletes ONLY url_lifecycle.status='gone' by "
        "gone_since for ALL subjects equally.",
    # -- Per-id mutations without fan-out (don't list another subject's rows) --
    # -- The rest of _SUBJECT_TABLES (everything that is NOT one of this
    #    delivery's 5 core ones, see the brief's Scope) -- DECLARED and
    #    ACCOUNTED-FOR debt, not an oversight. C21b-2 gives them the same
    #    treatment search_results/runs/alerts/goals/url_notes got here:
    #    lock already closed (SCOPING_COMPLETE), each method still needs
    #    filtering one by one. --
}


def test_no_unscoped_method_appears_without_a_reason():
    unscoped = _unscoped_methods()
    unexplained = {name: tables for name, tables in unscoped.items() if name not in _ALLOWLIST}
    assert unexplained == {}, (
        "Database has method(s) that touch a table with subject_id without "
        "filtering it and without a reason in _ALLOWLIST (tests/test_scoping_guard.py). "
        "If it touches one of C21b's 5 core tables (search_results, runs, "
        "alerts, goals, url_notes), it must be FILTERED for real (see "
        "Database._subject_clause). If it touches another _SUBJECT_TABLES "
        "table, add it to _ALLOWLIST with the reason 'deferred C21b-2: <table>'. "
        f"Unexplained methods: {unexplained}"
    )


def test_core_table_allowlist_entries_are_not_disguised_pending_debt():
    """A method in the allowlist that touches a CORE table (search_results/
    runs/alerts/goals/url_notes) cannot use the 'deferred C21b-2' reason
    -- that phrase is reserved for tables the brief's Scope leaves out; if
    a core method appears here, its reason has to explain WHY it's
    legitimately global or a mutation without fan-out, not hide behind
    another table's declared debt."""
    unscoped = _unscoped_methods()
    for name, tables in unscoped.items():
        if name not in _ALLOWLIST:
            continue
        touches_core = _CORE_TABLES & set(tables)
        if touches_core:
            assert "deferred C21b-2" not in _ALLOWLIST[name], (
                f"{name} touches core table(s) {touches_core} but its reason "
                "says 'deferred C21b-2' -- that table IS within C21b's "
                "scope, it needs a real reason (infra/mutation without "
                "fan-out) or a real filter."
            )


def test_allowlist_entries_still_exist_and_are_still_needed():
    """Reverse regression (same approach as test_dispatch_guard.py): if an
    allowlist method no longer exists, or already filters by subject_id,
    or no longer touches any _SUBJECT_TABLES table, the entry is stale --
    an allowlist that only grows is as bad a sign as not having one."""
    methods = _method_sources()
    unscoped = _unscoped_methods()
    for name in _ALLOWLIST:
        assert name in methods, (
            f"{name} is in _ALLOWLIST but no longer exists in Database -- remove it."
        )
        assert name in unscoped, (
            f"{name} is in _ALLOWLIST but no longer appears as unfiltered "
            "(was it already filtered, or does it no longer touch a table with subject_id?) -- remove it."
        )


# ======================================================================= #
# Second guard (C21b review): the one above only parses models.py, so ALL
# the raw SQL in the rest of src/ was invisible by construction -- and
# there was a real, unfiltered user-facing view there (/diff offered to
# compare A11 peers' runs alongside the watched subject's). This one
# walks the rest of the tree and forces every site to be either filtered
# or have its reason written down.
# ======================================================================= #
_SRC_SQL_RE = re.compile(
    r"\b(FROM|JOIN|INTO|UPDATE|DELETE\s+FROM)\s+(" + "|".join(sorted(_CORE_TABLES)) + r")\b",
    re.IGNORECASE,
)

# (path relative to src/) -> why that file can query a core table without
# filtering by subject.
_SRC_ALLOWLIST: dict[str, str] = {
    # Empty on purpose: the four files that used to be here (incidents,
    # enrich, lifecycle, crisis/triggers) were filtered when C21b-2 closed.
    # A new entry here is a decision, not a formality: it means someone
    # has SQL on a core table that doesn't filter by subject.
}


def _src_files_touching_core_tables() -> dict[str, list[int]]:
    """{relative path: [lines]} of each src/ file (except
    database/models.py, covered by the guard above) with SQL on a core
    table and no trace of subject filtering on that same line."""
    src_root = Path(__file__).resolve().parent.parent / "src"
    found: dict[str, list[int]] = {}
    for path in sorted(src_root.rglob("*.py")):
        rel = path.relative_to(src_root).as_posix()
        if rel.startswith("database/"):
            continue
        text = path.read_text(encoding="utf-8")
        if not _SCOPED_MARKER_RE.search(_code_only(text)):
            hits = [i for i, ln in enumerate(text.split("\n"), 1) if _SRC_SQL_RE.search(ln)]
            if hits:
                found[rel] = hits
    return found


def test_no_sql_outside_models_touches_a_core_table_without_a_reason():
    offenders = {f: lines for f, lines in _src_files_touching_core_tables().items()
                 if f not in _SRC_ALLOWLIST}
    assert offenders == {}, (
        "Raw SQL outside database/models.py on a C21b core table without "
        "filtering by subject and without a reason in _SRC_ALLOWLIST. If it's a VIEW a "
        "user sees, filter it (db._subject_clause()); if it's an internal pipeline, "
        f"add it with the reason 'deferred C21b-2: ...'. Files: {offenders}"
    )


def test_src_allowlist_entries_still_exist():
    """An entry that's no longer needed is noise that hides the ones that are."""
    live = _src_files_touching_core_tables()
    for rel in _SRC_ALLOWLIST:
        assert rel in live, (
            f"{rel} is in _SRC_ALLOWLIST but no longer has unfiltered SQL on "
            "a core table -- remove it."
        )


# ======================================================================= #
# Third guard: global UNIQUE constraints. Filtering the queries isn't
# enough for two subjects to coexist -- if a PK or a UNIQUE doesn't
# include `subject_id`, the second subject collides with the first, and
# almost always silently (INSERT OR IGNORE swallows the row, an ON
# CONFLICT overwrites the other one's). This already happened with
# alerts/goals/url_notes, resolved in C21b-2.
#
# This test doesn't demand fixing them now: it demands that the lock
# CANNOT be opened while any remain. It turns "remember to check this
# before allowing a second subject" into something the suite checks by
# itself.
# ======================================================================= #
# Empty since multi-subject phase 1: url_lifecycle's PK became
# (url_hash, country, engine, subject_id) -- the last global key -- and
# positive/negative_domains joined _SUBJECT_TABLES with PK (domain, subject_id).
_UNIQUES_PENDING: dict[str, str] = {}


def _tables_with_global_unique() -> dict[str, str]:
    """{table: DDL} of the tables with `subject_id` whose PK/UNIQUE does
    NOT include it. Textual heuristic over the real DDL of a freshly
    created DB, which is where the schema's truth lives."""
    import sqlite3
    import tempfile

    from database.models import Database

    with tempfile.TemporaryDirectory() as tmp:
        path = f"{tmp}/schema.db"
        Database(db_path=path)
        conn = sqlite3.connect(path)
        out = {}
        for table in Database._SUBJECT_TABLES:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            ddl = (row[0] if row else "") or ""
            if "subject_id" not in ddl:
                continue
            # The space before the parenthesis matters: SQLite preserves
            # the DDL exactly as written, and here "UNIQUE(a, b)" and
            # "PRIMARY KEY (a, b)" coexist. Looking only for the
            # no-space form let url_lifecycle slip through, which is
            # exactly the worst one on the list.
            constraint_re = re.compile(r"^(UNIQUE|PRIMARY\s+KEY)\s*\(", re.IGNORECASE)
            for line in ddl.split("\n"):
                stripped = line.strip()
                if constraint_re.match(stripped) and "subject_id" not in stripped:
                    out[table] = stripped.rstrip(",")
                    break
        conn.close()
        return out


def test_the_lock_cannot_be_opened_while_global_uniques_remain():
    """`SCOPING_COMPLETE = True` means "more subjects can now be created".
    With even one global PK/UNIQUE still alive, that's false: the second
    subject would collide with the first, almost always silently. This
    test is what prevents opening the lock by oversight."""
    from database.models import SCOPING_COMPLETE

    remaining = _tables_with_global_unique()
    if SCOPING_COMPLETE:
        assert remaining == {}, (
            "SCOPING_COMPLETE is True but these tables still have a PK/UNIQUE "
            f"that doesn't include subject_id, so two subjects would collide: {remaining}"
        )


def test_the_pending_unique_list_matches_reality():
    """The debt list above has to track the real schema: if someone
    migrates a table and doesn't remove it from here, the list starts
    lying -- and a debt list that lies is worse than not having one."""
    remaining = set(_tables_with_global_unique())
    declared = set(_UNIQUES_PENDING)
    assert remaining == declared, (
        f"declared but already migrated (remove them): {sorted(declared - remaining)}; "
        f"found but undeclared (add them with their failure mode): {sorted(remaining - declared)}"
    )
