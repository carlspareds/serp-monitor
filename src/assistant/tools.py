"""Assistant tool registry.

Every capability the assistant has is a `Tool` here: a name, a description
the model reads, a JSON-schema for its arguments, a kind and an executor.

  read      -- returns data about the ACTIVE subject; runs immediately.
  navigate  -- returns a UI intent ({target, url, params}) the panel turns
               into opening a page/modal; runs immediately, changes nothing.
  action    -- costs money or changes data. The harness NEVER calls the
               executor: it calls `prepare` (validation + the confirmation
               card's title/details/cost note) and stores a pending action;
               the executor only runs from POST /assistant/confirm.

Rules for executors: they receive a ToolContext whose `db` is already scoped
to the active subject (db.for_subject), they go through the existing
Database/processing functions (no SQL here), they return JSON-safe dicts and
they never put a secret (env value) in what they return. Invalid input or an
impossible request raises ToolError with a plain, user-facing message.
"""
from __future__ import annotations

import csv
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from common.money import run_cost_phrase
from database.base import NOTE_STATUSES, url_hash

log = logging.getLogger("assistant")

KINDS = ("read", "navigate", "action")
ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}
LABELS = ("positive", "negative", "neutral")

# Bounds that keep tool output small enough for the model's context.
_MAX_ITEMS = 15
_RESULT_SCAN_LIMIT = 20_000
_REPORT_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,120}\.(pdf|xlsx|csv)$")
_REPORT_KINDS = {
    "pdf": "Executive PDF (1 page): KPIs, per-country table and an AI-written summary "
           "when an AI provider is configured. Uses the latest run of each country.",
    "xlsx": "Excel workbook built from the latest stored run of each selected country.",
    "csv": "CSV of the stored results (optionally limited to a period and countries).",
}
_PERIODS = {"latest": None, "7d": 7, "30d": 30, "90d": 90, "all": None}
_PERIOD_LABELS = {"latest": "Latest run", "7d": "Last 7 days", "30d": "Last 30 days",
                  "90d": "Last 90 days", "all": "Everything stored"}
_REPORT_LABELS = {"pdf": "PDF summary", "xlsx": "Excel workbook", "csv": "CSV of results"}
_DEFAULT_RUN_COST_USD = 0.04
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")
_SETTINGS_SECTIONS = {
    "general": "/settings", "report": "/settings#projects", "projects": "/settings#projects",
    "domains": "/settings#domains", "apify": "/settings#search", "search": "/settings#search",
    "ai": "/settings#ai", "schedule": "/settings#scheduling", "email": "/settings#notifications",
    "notifications": "/settings#notifications", "telegram": "/settings#notifications",
    "slack": "/settings#notifications", "ntfy": "/settings#notifications",
    "api": "/settings#api", "users": "/settings#users", "audit": "/settings#audit",
    "backup": "/settings#backup", "setup": "/setup",
}


def deep_link(path: str, kind: str = "panel", base: str = "/results") -> str:
    """Deep link that opens an overlay over Results (same format as
    dashboard.ui_helpers.overlay_url, not imported to keep this module free of
    the dashboard): /results?modal=/ui/review. Used as the fallback `url` of
    navigation intents -- the panel client opens the overlay itself, the URL
    is for a new tab / no JS."""
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{'modal' if kind == 'modal' else 'panel'}={quote(path, safe='/')}"


# Fallback URLs of the navigate tools: each feature's ONE home (the old pages
# only redirect here). Results hosts the dialogs/panels; Domains live with the
# subject in Settings → Projects and subjects (open for the active subject).
URL_REVIEW = deep_link("/ui/review", "modal")
URL_RUNS = deep_link("/ui/runs", "panel")
URL_TIMELINE = deep_link("/ui/timeline", "panel")
URL_EVIDENCE = deep_link("/ui/evidence", "panel")
URL_DOMAINS = "/settings#domains"
URL_REPORTS = deep_link("/ui/reports", "modal")
URL_AI_CHECKS = deep_link("/ui/ai-checks", "modal")
URL_SEARCH = deep_link("/ui/search", "modal")
URL_NOTES = deep_link("/ui/notes", "panel")
URL_ALERTS = "/?alerts=1"


def compare_url(a: str = "", b: str = "", country: str = "") -> str:
    params = {k: v for k, v in (("a", a), ("b", b), ("country", country)) if v}
    return deep_link("/ui/compare" + (f"?{urlencode(params)}" if params else ""), "modal")


KNOWLEDGE_PATH = Path(__file__).resolve().parent.parent.parent / "docs" / "assistant-knowledge.md"


class ToolError(Exception):
    """Invalid arguments or an impossible request: the message is shown to
    the model (and from there to the user) as-is, so keep it plain."""


@dataclass
class ToolContext:
    db: Any                         # Database scoped to the active subject
    subject_id: int
    role: str = "viewer"            # viewer | operator | admin
    user: str = "local"
    jobs: Any = None                # dashboard.jobs.JobManager (start_search)
    runner_factory: Callable | None = None   # -> ApifyRunner (start_search)
    exports_dir: Path | None = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    kind: str
    executor: Callable[[ToolContext, dict], dict]
    requires_confirmation: bool = False
    min_role: str = "viewer"
    prepare: Callable[[ToolContext, dict], dict] | None = None
    summary: Callable[[dict, dict], str] | None = None

    def spec(self) -> dict:
        """Provider-neutral function spec (name/description/parameters)."""
        return {"name": self.name, "description": self.description,
                "parameters": self.parameters}


def role_allows(role: str, min_role: str) -> bool:
    return ROLE_RANK.get(role, -1) >= ROLE_RANK.get(min_role, 99)


# --------------------------------------------------------------------- #
# Argument validation (a small JSON-schema subset: no external dependency)
# --------------------------------------------------------------------- #
def _coerce(name: str, schema: dict, value):
    t = schema.get("type")
    if t == "integer":
        if isinstance(value, bool):
            raise ToolError(f"'{name}' must be a whole number.")
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value.strip())
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int):
            raise ToolError(f"'{name}' must be a whole number.")
        if "minimum" in schema and value < schema["minimum"]:
            value = schema["minimum"]
        if "maximum" in schema and value > schema["maximum"]:
            value = schema["maximum"]
        return value
    if t == "string":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            raise ToolError(f"'{name}' must be text.")
        value = value.strip()
        if "enum" in schema:
            low = value.lower()
            if low not in schema["enum"]:
                raise ToolError(f"'{name}' must be one of: {', '.join(schema['enum'])}.")
            value = low
        if len(value) > schema.get("maxLength", 10_000):
            raise ToolError(f"'{name}' is too long (max {schema['maxLength']} characters).")
        return value
    if t == "array":
        if isinstance(value, str):
            value = [v for v in re.split(r"[,\s]+", value) if v]
        if not isinstance(value, list):
            raise ToolError(f"'{name}' must be a list.")
        items = [_coerce(name, schema.get("items", {}), v) for v in value]
        if len(items) > schema.get("maxItems", 100):
            raise ToolError(f"'{name}' has too many items (max {schema['maxItems']}).")
        return list(dict.fromkeys(items))
    if t == "boolean":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes")
        return bool(value)
    return value


def validate_args(tool: Tool, args) -> dict:
    """Validated/coerced copy of `args`: unknown keys dropped, required keys
    enforced, enums/types/limits checked. Raises ToolError."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ToolError("Arguments must be an object.")
    props = tool.parameters.get("properties", {})
    out = {}
    for key, schema in props.items():
        if key in args and args[key] not in (None, ""):
            out[key] = _coerce(key, schema, args[key])
    missing = [k for k in tool.parameters.get("required", []) if k not in out]
    if missing:
        raise ToolError(f"Missing: {', '.join(missing)}.")
    return out


def sanitize_args(tool: Tool | None, args: dict) -> dict:
    """Args safe for the audit log: schema keys only, long text clipped."""
    props = (tool.parameters.get("properties", {}) if tool else {})
    out = {}
    for key, value in (args or {}).items():
        if tool and key not in props:
            continue
        if isinstance(value, str) and len(value) > 300:
            value = value[:300] + "…"
        out[key] = value
    return out


# --------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def fmt_day(ts) -> str:
    """'2026-09-29T10:00:00' -> '29 Sep' (input echoed back if unparseable)."""
    try:
        return datetime.fromisoformat(str(ts)[:19]).strftime("%-d %b")
    except (TypeError, ValueError):
        return str(ts or "")


def _short(text, n: int = 120) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def compact_result(r: dict) -> dict:
    return {
        "result_id": r.get("id"),
        "position": r.get("position"),
        "domain": r.get("domain"),
        "url": r.get("url"),
        "title": _short(r.get("title")),
        "label": r.get("classification") or "neutral",
        "confidence": r.get("confidence"),
        "labelled_by": r.get("classified_by") or "rule",
        "country": (r.get("country_code") or "").upper(),
        "engine": r.get("engine") or "google",
        "run_id": r.get("run_id"),
        "review_status": r.get("review_status"),
        "same_person": r.get("same_person"),
        "severity": r.get("severity"),
        "topic": r.get("topic"),
    }


def compact_run(r: dict) -> dict:
    return {
        "run_id": r.get("apify_run_id"),
        "date": r.get("created_at"),
        "country": (r.get("country_code") or "").upper(),
        "engine": r.get("engine") or "google",
        "device": r.get("device") or "desktop",
        "status": r.get("status"),
        "total": r.get("total_results"),
        "positive": r.get("positive"),
        "negative": r.get("negative"),
        "neutral": r.get("neutral"),
        "cost_usd": r.get("cost_usd"),
    }


def compact_alert(a: dict) -> dict:
    return {
        "alert_id": a.get("id"),
        "created_at": a.get("created_at"),
        "severity": a.get("severity"),
        "kind": a.get("kind"),
        "country": (a.get("country") or "").upper(),
        "message": _short(a.get("message"), 200),
        "status": a.get("status") or "open",
        "assignee": a.get("assignee") or "",
    }


def run_groups(db, limit: int = 500) -> dict[tuple[str, str], list[dict]]:
    """{(country, engine): [runs newest first]} from Database.list_runs.
    Compare only makes sense within one country AND one engine."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in db.list_runs(limit=limit):
        key = ((r.get("country_code") or "").lower(), r.get("engine") or "google")
        groups.setdefault(key, []).append(r)
    return groups


def find_result(db, result_id: int | None = None, url: str = "") -> dict | None:
    """A result of the active subject by id or URL (newest observation of
    that URL). Scans through Database.get_results_filtered -- there is no
    get-by-id read on ResultsMixin yet."""
    if result_id is not None:
        for r in db.pending_review(limit=200):
            if r.get("id") == result_id:
                return r
    rows = db.get_results_filtered(limit=_RESULT_SCAN_LIMIT, include_homonyms=True)
    if result_id is not None:
        return next((r for r in rows if r.get("id") == result_id), None)
    if url:
        target = url_hash(url)
        return next((r for r in rows if url_hash(r.get("url") or "") == target), None)
    return None


def _domain_lists(db) -> tuple[list[str], list[str]]:
    from config.positive_domains import merged_with
    return merged_with(db.list_positive_domains()), db.list_negative_domains()


def rule_reason(row: dict, positives: list[str], negative_domains: list[str]) -> str:
    """Which rule decides `row` under the CURRENT lists -- mirrors the
    priority order of processing.classifier.classify_with_confidence."""
    from config.positive_domains import NEGATIVE_KEYWORDS
    from processing.classifier import _domain_matches, is_force_neutral

    domain = (row.get("domain") or "").lower()
    url = (row.get("url") or "").lower()
    if _domain_matches(domain, positives) or any(p.lower() in url for p in positives):
        hit = next((p for p in positives if _domain_matches(domain, [p]) or p.lower() in url), "")
        return f"positive: the domain matches your positive domain '{hit}'"
    if is_force_neutral(row):
        return "neutral: it is a public-record source that is always kept neutral"
    if _domain_matches(domain, negative_domains) or any(n.lower() in url for n in negative_domains):
        hit = next((n for n in negative_domains if _domain_matches(domain, [n]) or n.lower() in url), "")
        return f"negative: the domain is on your negative list ('{hit}')"
    haystack = f"{row.get('title') or ''} {row.get('description') or ''} {url}".lower()
    for kw in NEGATIVE_KEYWORDS:
        if kw.lower() in haystack:
            return f"negative: the text contains the negative keyword '{kw}'"
    return "neutral: no rule matched (neutral by default, confidence 0.5)"


def explain_row(db, row: dict, positives=None, negative_domains=None) -> dict:
    if positives is None:
        positives, negative_domains = _domain_lists(db)
    by = row.get("classified_by") or "rule"
    label = row.get("classification") or "neutral"
    conf = row.get("confidence")
    if by == "human":
        why = "A person set this label in the review queue. It is final: reclassification never changes it."
    elif by == "ai":
        why = (f"Your AI provider labelled it {label}"
               + (f" with confidence {conf:.2f}" if isinstance(conf, (int, float)) else "") + ".")
        reason = " ".join(str(row.get("ai_reason") or "").split())
        if reason:
            why += f" AI's reason: {reason.rstrip('.')}."
    else:
        why = "Rule: " + rule_reason(row, positives, negative_domains or []) + "."
    extra = []
    if row.get("review_status") == "pending":
        extra.append(f"It is in the review queue (confidence below the threshold {db.review_threshold():.2f}).")
    elif row.get("review_status") in ("approved", "corrected"):
        extra.append(f"Reviewed by a person ({row['review_status']}).")
    sp = row.get("same_person")
    if sp == "no":
        extra.append("Marked as a different person with the same name: hidden from stats and alerts.")
    elif sp == "unsure":
        extra.append("The AI was unsure whether it is about the same person.")
    if row.get("severity"):
        extra.append(f"Severity {row['severity']}/5" + (f", topic {row['topic']}" if row.get("topic") else "") + ".")
    return {"label": label, "confidence": conf, "labelled_by": by, "why": why, "notes": extra}


def explanation_text(result: dict) -> str:
    """Plain answer built from an explain_result tool result (used by the
    harness when the model only replied with a navigation sentence)."""
    item = result.get("result") or {}
    title = item.get("title") or item.get("domain") or item.get("url") or "This result"
    where = f" ({item['domain']})" if item.get("domain") and item.get("title") else ""
    parts = [f"“{title}”{where} is labelled {result.get('label') or 'neutral'}.",
             str(result.get("why") or "").strip()]
    parts += [str(n) for n in result.get("notes") or []]
    return " ".join(p for p in parts if p)


def _today() -> str:
    return _now().date().isoformat()


def _exports_dir(ctx: ToolContext) -> Path:
    if ctx.exports_dir is not None:
        return Path(ctx.exports_dir)
    from dashboard import deps
    return Path(deps.exports_dir)


# --------------------------------------------------------------------- #
# READ tools
# --------------------------------------------------------------------- #
def _pair_delta(db, runs: list[dict]) -> dict:
    from processing.diff import run_diff
    latest = runs[0]
    out = {"latest_run": compact_run(latest)}
    if len(runs) > 1:
        prev = runs[1]
        diff = run_diff(db, prev["apify_run_id"], latest["apify_run_id"])
        out["previous_run"] = {"run_id": prev["apify_run_id"], "date": prev.get("created_at")}
        out["delta"] = {k: int(latest.get(k) or 0) - int(prev.get(k) or 0)
                        for k in ("positive", "negative", "neutral")}
        out["entered"] = [{"domain": r.get("domain"), "position": r.get("position"),
                           "label": r.get("classification") or "neutral"}
                          for r in diff["entered"]][:5]
        out["exited"] = [{"domain": r.get("domain"), "label": r.get("classification") or "neutral"}
                         for r in diff["exited"]][:5]
    return out


def get_overview(ctx: ToolContext, args: dict) -> dict:
    db = ctx.db
    subject = db.current_subject()
    stats = db.get_stats()
    groups = run_groups(db)
    per_country = []
    for (country, engine), runs in sorted(groups.items()):
        item = {"country": country.upper(), "engine": engine}
        item.update(_pair_delta(db, runs))
        per_country.append(item)
    active_alerts = db.open_alerts()
    # db.open_alerts() returns the ACTIVE ones (open + acknowledged); only
    # status 'open' still needs someone to look at it.
    open_alerts = [a for a in active_alerts if (a.get("status") or "open") == "open"]
    acked = len(active_alerts) - len(open_alerts)
    pending = db.count_pending_review()
    wins = db.list_victories(days=90)
    overdue = db.overdue_notes(_today())
    attention = []
    if pending:
        attention.append(f"{pending} result(s) need review")
    for item in per_country:
        entered = item.get("entered") or []
        if entered:
            neg = [e for e in entered if e["label"] == "negative"]
            names = ", ".join(e["domain"] or "?" for e in entered[:3])
            attention.append(
                f"{len(entered)} new result(s) in {item['country']} since "
                f"{fmt_day(item['previous_run']['date'])} ({names})"
                + (f", {len(neg)} negative" if neg else ""))
    if open_alerts:
        crit = sum(1 for a in open_alerts if a.get("severity") == "critical")
        attention.append(f"{len(open_alerts)} open alert(s)" + (f" ({crit} critical)" if crit else "")
                         + (f", plus {acked} acknowledged" if acked else ""))
    if overdue:
        attention.append(f"{len(overdue)} overdue note(s)")
    return {
        "subject": {"id": subject["id"], "name": subject["name"], "query": subject["query"],
                    "countries": subject["countries"], "is_demo": subject.get("is_demo", False)},
        "kpis": {"total_runs": stats["total_runs"], "total_results": stats["total_results"],
                 "countries": stats["countries"], "last_run": stats["last_run"]},
        "latest_by_country": per_country[:10],
        "pending_review": pending,
        "open_alerts": {"count": len(open_alerts), "acknowledged": acked,
                        "top": [compact_alert(a) for a in open_alerts[:5]]},
        "wins_90d": {"count": len(wins),
                     "items": [{"url": w.get("url"), "country": (w.get("country") or "").upper(),
                                "gone_since": w.get("gone_since")} for w in wins[:3]]},
        "overdue_notes": len(overdue),
        "attention": attention,
    }


def _resolve_pair(db, country: str, engine: str, run_a: str, run_b: str) -> tuple[dict, dict]:
    groups = run_groups(db)
    if run_a or run_b:
        meta = {}
        for rid in (run_a, run_b):
            if rid:
                m = db.get_run(rid)
                if not m:
                    raise ToolError(f"The run '{rid}' does not exist for this subject.")
                meta[rid] = m
        if run_a and run_b:
            a, b = meta[run_a], meta[run_b]
        else:
            known = meta[run_a or run_b]
            key = ((known.get("country_code") or "").lower(), known.get("engine") or "google")
            runs = groups.get(key, [])
            ids = [r["apify_run_id"] for r in runs]
            i = ids.index(known["apify_run_id"]) if known["apify_run_id"] in ids else -1
            if run_b:   # previous of B
                if i < 0 or i + 1 >= len(runs):
                    raise ToolError(f"No earlier run to compare with '{run_b}'.")
                a, b = runs[i + 1], known
            else:       # newest other run for A
                others = [r for r in runs if r["apify_run_id"] != run_a]
                if not others:
                    raise ToolError(f"No other run to compare with '{run_a}'.")
                a, b = known, others[0]
        if (a.get("country_code") or "").lower() != (b.get("country_code") or "").lower():
            raise ToolError("Those runs are from different countries; compare runs of one country.")
        if (a.get("id") or 0) > (b.get("id") or 0):
            a, b = b, a
        return a, b
    if not groups:
        raise ToolError("There are no runs yet. Run a search first.")
    candidates = {k: v for k, v in groups.items()
                  if (not country or k[0] == country.lower()) and (not engine or k[1] == engine)}
    if not candidates:
        raise ToolError(f"No runs for {country.upper() or 'that selection'}"
                        + (f" on {engine}" if engine else "") + ".")
    # Prefer google, then the group with the most recent run.
    key = max(candidates, key=lambda k: (k[1] == "google", candidates[k][0].get("id") or 0))
    runs = candidates[key]
    if len(runs) < 2:
        raise ToolError(f"There's only one run for {key[0].upper()} ({key[1]}); a second run is needed to compare.")
    return runs[1], runs[0]


def list_changes(ctx: ToolContext, args: dict) -> dict:
    from processing.diff import run_diff
    a, b = _resolve_pair(ctx.db, args.get("country", ""), args.get("engine", ""),
                         args.get("run_a", ""), args.get("run_b", ""))
    diff = run_diff(ctx.db, a["apify_run_id"], b["apify_run_id"])

    def _row(r):
        return {"result_id": r.get("id"), "position": r.get("position"), "domain": r.get("domain"),
                "url": r.get("url"), "title": _short(r.get("title"), 90),
                "label": r.get("classification") or "neutral"}

    moved = [dict(_row(r), places=d) for r, d in diff["moved"]]
    moved.sort(key=lambda m: -abs(m["places"]))
    return {
        "country": (b.get("country_code") or "").upper(),
        "engine": b.get("engine") or "google",
        "run_a": {"run_id": a["apify_run_id"], "date": a.get("created_at")},
        "run_b": {"run_id": b["apify_run_id"], "date": b.get("created_at")},
        "summary": {
            "entered": len(diff["entered"]), "exited": len(diff["exited"]),
            "moved_up": sum(1 for _r, d in diff["moved"] if d > 0),
            "moved_down": sum(1 for _r, d in diff["moved"] if d < 0),
            "reclassified": len(diff["reclassified"]),
            "new_negatives": sum(1 for r in diff["entered"] if r.get("classification") == "negative"),
            "negatives_left": sum(1 for r in diff["exited"] if r.get("classification") == "negative"),
        },
        "entered": [_row(r) for r in diff["entered"]][:_MAX_ITEMS],
        "exited": [_row(r) for r in diff["exited"]][:_MAX_ITEMS],
        "moved": moved[:_MAX_ITEMS],
        "reclassified": [dict(_row(r), before=old or "neutral", after=new or "neutral")
                         for r, old, new in diff["reclassified"]][:_MAX_ITEMS],
        "compare_url": compare_url(a["apify_run_id"], b["apify_run_id"]),
        "note": "Positive 'places' = moved up (better rank).",
    }


def _changes_summary(args, result) -> str:
    return (f"Used: compare runs {fmt_day(result['run_a']['date'])} → "
            f"{fmt_day(result['run_b']['date'])} ({result['country']})")


def list_review_queue(ctx: ToolContext, args: dict) -> dict:
    db = ctx.db
    limit = args.get("limit", 10)
    rows = db.pending_review(limit=limit)
    positives, negatives = _domain_lists(db)
    items = []
    for r in rows:
        item = compact_result(r)
        item["why"] = explain_row(db, r, positives, negatives)["why"]
        items.append(item)
    return {"total_pending": db.count_pending_review(), "threshold": db.review_threshold(),
            "items": items, "review_url": URL_REVIEW}


_RESULTS_MAX = 50


def _label_source(classified_by) -> str:
    """Who set a result's label: 'human', 'ai' or 'rule'."""
    by = (classified_by or "").lower()
    return by if by in ("human", "ai") else "rule"


def list_results(ctx: ToolContext, args: dict) -> dict:
    """The results of the latest run of each country (or of `country`),
    optionally one label, in position order: what Results shows by default."""
    db = ctx.db
    country = (args.get("country") or "").strip().lower()
    label = args.get("label") or ""
    limit = min(int(args.get("limit", 25)), _RESULTS_MAX)
    runs = db.latest_run_per_country()
    if country:
        have = sorted({(r.get("country_code") or "").lower() for r in runs})
        runs = [r for r in runs if (r.get("country_code") or "").lower() == country]
        if not runs:
            raise ToolError(f"No run for {country.upper()} yet."
                            + (f" Countries with runs: {', '.join(c.upper() for c in have)}." if have else ""))
    rows: list[dict] = []
    for run in runs:
        rows.extend(db.get_results_filtered(run_id=run.get("apify_run_id"), sort_by="position",
                                            order="asc", limit=1000, include_homonyms=True))
    counts = {k: 0 for k in LABELS}
    for r in rows:
        if r.get("same_person") != "no":       # namesakes are listed, not counted
            c = r.get("classification") or "neutral"
            counts[c] = counts.get(c, 0) + 1
    if label:
        rows = [r for r in rows if (r.get("classification") or "neutral") == label]
    items = [{"result_id": r.get("id"), "position": r.get("position"), "title": _short(r.get("title")),
              "domain": r.get("domain"), "url": r.get("url"),
              "label": r.get("classification") or "neutral",
              "source": _label_source(r.get("classified_by")),
              "same_person": r.get("same_person"),
              "country": (r.get("country_code") or "").upper(), "engine": r.get("engine") or "google"}
             for r in rows]
    return {"country": country.upper() or "all", "label": label or "all",
            "runs": [{"run_id": r.get("apify_run_id"), "country": (r.get("country_code") or "").upper(),
                      "engine": r.get("engine") or "google", "date": r.get("created_at")} for r in runs],
            "count_by_label": counts, "total": len(items), "items": items[:limit],
            "more": max(0, len(items) - limit), "results_url": "/results"}


def _results_summary(args, result) -> str:
    what = "" if result["label"] == "all" else f"{result['label']} "
    where = "" if result["country"] == "all" else f", {result['country']}"
    return f"Used: latest {what}results ({result['total']}{where})"


def explain_result(ctx: ToolContext, args: dict) -> dict:
    rid, url = args.get("result_id"), args.get("url", "")
    if rid is None and not url:
        raise ToolError("Give a result_id or a url.")
    row = find_result(ctx.db, rid, url)
    if row is None:
        raise ToolError("No result with that id/URL for this subject.")
    out = {"result": compact_result(row)}
    out.update(explain_row(ctx.db, row))
    h = url_hash(row.get("url") or "")
    out["has_evidence"] = bool(ctx.db.has_evidence(h))
    note = ctx.db.get_notes_map().get(row.get("url"))
    if note:
        out["note"] = {"status": note.get("status"), "text": _short(note.get("note"), 300)}
    return out


def list_alerts(ctx: ToolContext, args: dict) -> dict:
    status = args.get("status", "active")
    rows = ctx.db.list_alerts(limit=args.get("limit", 10), status=status)
    return {"status": status, "count": len(rows), "items": [compact_alert(a) for a in rows],
            "alerts_url": URL_ALERTS}


def list_runs(ctx: ToolContext, args: dict) -> dict:
    rows = ctx.db.list_runs(limit=args.get("limit", 10))
    return {"count": len(rows), "items": [compact_run(r) for r in rows], "runs_url": URL_RUNS}


def list_notes(ctx: ToolContext, args: dict) -> dict:
    from database.base import normalize_note_status, note_is_overdue
    today = _today()
    status = args.get("status", "")
    rows = []
    for n in ctx.db.list_notes():
        st = normalize_note_status(n.get("status"))
        overdue = note_is_overdue(n.get("due_date") or "", st, today)
        if status and st != status:
            continue
        if args.get("overdue_only") and not overdue:
            continue
        rows.append({"note_id": n.get("id"), "url": n.get("url"), "status": st,
                     "text": _short(n.get("note"), 200), "assignee": n.get("assignee") or "",
                     "due_date": n.get("due_date") or "", "overdue": overdue,
                     "updated_at": n.get("updated_at")})
    return {"count": len(rows), "items": rows[:_MAX_ITEMS], "notes_url": URL_NOTES}


def get_setup_status(ctx: ToolContext, args: dict) -> dict:
    """What is configured -- booleans and non-secret labels only."""
    from notify import channel_status, routed_severities
    from processing import llm

    db = ctx.db
    info = llm.provider_info()
    channels = channel_status(db)
    routed = routed_severities(db)
    subject = db.current_subject()
    apify = bool(os.getenv("APIFY_API_KEY"))
    try:
        budget = float(db.get_setting("apify_monthly_budget", "0") or 0)
    except ValueError:
        budget = 0.0
    spend = db.apify_month_cost()
    jobs = []
    try:
        for j in db.list_scheduled_jobs():
            jobs.append({"name": j.get("name"), "enabled": bool(j.get("enabled", True)),
                         "last_status": j.get("last_status"), "next_run_at": j.get("next_run_at")})
    except Exception:
        log.exception("assistant_setup_jobs_failed")
    missing = []
    if not apify:
        missing.append("Apify API key (real searches): Settings → Search, or the Setup wizard.")
    if not info["configured"]:
        missing.append("AI provider: Settings → AI provider.")
    if not any(channels.values()):
        missing.append("No notification channel: Settings → Notifications (Email, Telegram, Slack, ntfy).")
    elif not routed:
        missing.append("A channel is configured but no alert severity is routed to it: "
                       "Settings → Notifications → Alert routing.")
    if not subject.get("query"):
        missing.append("Tracked name: Settings → Projects and subjects → Edit.")
    if not db.list_positive_domains():
        missing.append("No positive (owned) domains for this subject: "
                       "Settings → Projects and subjects → Domains.")
    return {
        "subject": {"name": subject["name"], "query": subject["query"], "countries": subject["countries"]},
        "apify": {"configured": apify, "demo_mode": not apify,
                  "month_spend_usd": round(float(spend.get("cost_usd") or 0), 2),
                  "monthly_budget_usd": budget or None},
        "ai_provider": {"configured": info["configured"], "provider": info["provider"],
                        "model": info["model"], "host": info["base_url"]},
        "notification_channels": channels,
        "routed_severities": routed,
        "routing": db.get_routing(),
        "review_threshold": db.review_threshold(),
        "scheduled_jobs": jobs,
        "missing": missing,
    }


def list_reports(ctx: ToolContext, args: dict) -> dict:
    """Report kinds and the subject's previous files (export.reports store:
    exports/<subject_id>/, the same list as Reports → Previous reports)."""
    from export import reports as store
    files = []
    for m in store.list_reports(_exports_dir(ctx), ctx.subject_id, limit=10):
        files.append({"name": m["file"], "kind": m.get("kind"), "title": m.get("title"),
                      "size_kb": round((m.get("size") or 0) / 1024, 1),
                      "created": m.get("created_at"), "emailed": bool(m.get("emailed")),
                      "download_url": f"/reports/{ctx.subject_id}/{m['file']}"})
    return {"kinds": [{"kind": k, "description": v} for k, v in _REPORT_KINDS.items()],
            "periods": list(_PERIODS), "previous_files": files, "export_page": URL_REPORTS}


_KB_CACHE: dict[str, Any] = {}


def knowledge_sections() -> list[tuple[str, str]]:
    """[(title, body)] of docs/assistant-knowledge.md, cached by mtime."""
    try:
        mtime = KNOWLEDGE_PATH.stat().st_mtime
    except OSError:
        return []
    if _KB_CACHE.get("mtime") != mtime:
        text = KNOWLEDGE_PATH.read_text(encoding="utf-8")
        sections = []
        for chunk in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
            title, _, body = chunk.partition("\n")
            sections.append((title.strip(), " ".join(body.split())))
        _KB_CACHE.update(mtime=mtime, sections=sections)
    return _KB_CACHE["sections"]


_STOP = {"the", "a", "an", "to", "how", "do", "i", "my", "is", "what", "of", "in", "on", "and",
         "for", "set", "up", "can", "with", "it", "where", "does", "use", "about"}


def app_help(ctx: ToolContext, args: dict) -> dict:
    sections = knowledge_sections()
    topic = args.get("topic", "").lower()
    if not topic:
        return {"topics": [t for t, _ in sections]}
    words = [w for w in re.findall(r"[a-z0-9]+", topic) if w not in _STOP] or [topic]
    scored = []
    for title, body in sections:
        tl, bl = title.lower(), body.lower()
        score = sum(5 for w in words if w in tl) + sum(min(bl.count(w), 3) for w in words)
        if score:
            scored.append((score, title, body))
    scored.sort(key=lambda s: -s[0])
    if not scored:
        return {"found": False, "topics": [t for t, _ in sections]}
    return {"found": True, "sections": [{"title": t, "text": b} for _, t, b in scored[:2]]}


# --------------------------------------------------------------------- #
# NAVIGATE tools
# --------------------------------------------------------------------- #
def _intent(target: str, url: str, label: str, **params) -> dict:
    return {"intent": {"target": target, "url": url, "label": label,
                       "params": {k: v for k, v in params.items() if v not in (None, "")}}}


def _nav(target: str, url: str, label: str):
    return lambda ctx, args: _intent(target, url, label)


def open_compare(ctx: ToolContext, args: dict) -> dict:
    a, b = args.get("run_a", ""), args.get("run_b", "")
    for rid in (a, b):
        if rid and not ctx.db.get_run(rid):
            raise ToolError(f"The run '{rid}' does not exist for this subject.")
    return _intent("compare", compare_url(a, b), "Compare", run_a=a, run_b=b)


def open_settings(ctx: ToolContext, args: dict) -> dict:
    section = args.get("section", "general")
    return _intent("settings", _SETTINGS_SECTIONS[section], f"Settings → {section}", section=section)


def open_result(ctx: ToolContext, args: dict) -> dict:
    row = find_result(ctx.db, args["result_id"])
    if row is None:
        raise ToolError("No result with that id for this subject.")
    return _intent("result", deep_link(f"/ui/run/{row.get('run_id')}", "panel"),
                   f"Result: {row.get('domain')}",
                   result_id=row.get("id"), run_id=row.get("run_id"), result_url=row.get("url"))


# --------------------------------------------------------------------- #
# ACTION tools: prepare (validation + confirmation card) / execute
# --------------------------------------------------------------------- #
def _countries_engines(ctx: ToolContext, args: dict) -> tuple[list[str], list[str]]:
    from apify.actors import CONFIGURED_ENGINES
    from dashboard.ui_helpers import COUNTRIES

    subject = ctx.db.current_subject()
    countries = [c.lower() for c in (args.get("countries") or subject["countries"] or ["us"])]
    bad = [c for c in countries if c not in COUNTRIES]
    if bad:
        raise ToolError(f"Unknown country code(s): {', '.join(bad)}. Valid: {', '.join(COUNTRIES)}.")
    engines = [e.lower() for e in (args.get("engines") or ["google"])]
    bad = [e for e in engines if e not in CONFIGURED_ENGINES]
    if bad:
        raise ToolError(f"Engine(s) not available: {', '.join(bad)}. "
                        f"Available: {', '.join(sorted(CONFIGURED_ENGINES))}.")
    return countries, engines


def _avg_run_cost(db) -> tuple[float, bool]:
    costs = [float(r["cost_usd"]) for r in db.list_runs(limit=50) if r.get("cost_usd") is not None]
    if not costs:
        return _DEFAULT_RUN_COST_USD, False
    return sum(costs) / len(costs), True


def prepare_start_search(ctx: ToolContext, args: dict) -> dict:
    from dashboard.jobs import _MAX_CONCURRENT_JOBS

    if not os.getenv("APIFY_API_KEY"):
        raise ToolError("Searches can't run: no Apify API key is configured (demo mode). "
                        "An admin can add it in Settings → Search.")
    query = ctx.db.current_subject().get("query") or ""
    if not query:
        raise ToolError("This subject has no tracked name yet (Settings → Projects and subjects → Edit).")
    countries, engines = _countries_engines(ctx, args)
    pages = args.get("pages", 1)
    if ctx.jobs is not None and ctx.jobs.running_count() >= _MAX_CONCURRENT_JOBS:
        raise ToolError(f"{_MAX_CONCURRENT_JOBS} searches are already running; wait for one to finish.")
    n = len(countries) * len(engines)
    per_run, measured = _avg_run_cost(ctx.db)
    lo, hi = 20 * n, 90 * n
    time_note = f"{lo}-{hi} seconds" if hi < 180 else f"{lo // 60 or 1}-{hi // 60} minutes"
    cost_note = (f"Uses your Apify plan: {run_cost_phrase(per_run, n * pages)} ({n} run(s)"
                 + ("" if measured else ", rough estimate") + f"). Takes about {time_note}.")
    try:
        budget = float(ctx.db.get_setting("apify_monthly_budget", "0") or 0)
    except ValueError:
        budget = 0.0
    if budget > 0:
        spent = float(ctx.db.apify_month_cost().get("cost_usd") or 0)
        cost_note += f" This month: ${spent:.2f} of your ${budget:.2f} budget."
    device = args.get("device", "desktop")
    from apify.actors import CONFIGURED_ENGINES

    subject_countries = [c.lower() for c in (ctx.db.current_subject().get("countries") or [])]
    country_opts = list(dict.fromkeys(subject_countries + countries))
    spent = budget_val = None
    if budget > 0:
        budget_val, spent = budget, float(ctx.db.apify_month_cost().get("cost_usd") or 0)
    edit = {
        "fields": [
            {"name": "countries", "type": "chips", "label": "Countries",
             "options": [{"value": c, "label": c.upper()} for c in country_opts], "value": countries},
            {"name": "engines", "type": "chips", "label": "Engines",
             "options": [{"value": e, "label": e.capitalize()} for e in sorted(CONFIGURED_ENGINES)],
             "value": engines},
        ],
        "estimate": {"per_run": round(per_run, 4), "measured": measured, "pages": pages,
                     "budget": budget_val, "spent": spent},
    }
    return {
        "title": "Run this search now?",
        "details": [f"Search: {query}",
                    "Countries: " + ", ".join(c.upper() for c in countries),
                    "Engines: " + ", ".join(e.capitalize() for e in engines),
                    f"Pages: {pages} · Device: {device}"],
        "cost_note": cost_note,
        "button": "Run search",
        "edit": edit,
        "normalized_args": {"countries": countries, "engines": engines, "pages": pages,
                            "device": device},
    }


def launch_search(ctx: ToolContext, query: str, countries: list[str], engines: list[str],
                  pages: int = 1, device: str = "desktop") -> str:
    """Thin helper: the same JobManager + run_multi_country path as POST
    /search_multi (dashboard.routers.search_runner.search_multi), with the
    subject carried explicitly into the worker thread (threads don't inherit
    the request's ContextVar). Returns the job id."""
    from config.positive_domains import merged_with
    from dashboard.jobs import _MAX_CONCURRENT_JOBS, step_key
    from dashboard.ui_helpers import subject_label
    from database.base import subject_scope
    from report.multi_country import aggregate_summary, run_multi_country

    jobs = ctx.jobs
    if jobs is None or ctx.runner_factory is None:
        raise ToolError("Search jobs are not available in this context.")
    if jobs.running_count() >= _MAX_CONCURRENT_JOBS:
        raise ToolError(f"{_MAX_CONCURRENT_JOBS} searches are already running; wait for one to finish.")
    db, sid = ctx.db, ctx.subject_id
    positives = merged_with(db.list_positive_domains())
    negatives = db.list_negative_domains()
    job_id = jobs.create(
        "multi",
        {"query": query, "countries": countries, "engines": engines, "device": device,
         "via": "assistant", "subject_id": sid,
         "subject_name": subject_label(db.current_subject() or {"query": query}),
         "started_by": ctx.user or "local", "session": ctx.extra.get("session", "")},
        steps=[(c, e) for c in countries for e in engines],
    )

    def on_step(country, engine, state, **info):
        if engine is None:
            if state == "error":
                jobs.fail_open_steps(job_id, info.get("message") or "Failed", country=country)
            return
        jobs.set_step(job_id, step_key(country, engine), state=state, **info)

    runner_factory = ctx.runner_factory

    def work(jid):
        with subject_scope(sid):
            outcomes = run_multi_country(
                runner_factory(), db, query, countries,
                positive_domains=positives, negative_domains=negatives,
                max_pages=pages, on_event=lambda m: jobs.add_progress(jid, m),
                engines=engines, device=device, on_step=on_step,
            )
            agg = aggregate_summary(outcomes)
            jobs.add_progress(jid, "Report finished.")
            return {"kind": "multi", "query": query, "agg": agg, "outcomes": outcomes}

    jobs.run_in_thread(job_id, work)
    return job_id


def exec_start_search(ctx: ToolContext, args: dict) -> dict:
    prep = prepare_start_search(ctx, args)["normalized_args"]
    query = ctx.db.current_subject()["query"]
    job_id = launch_search(ctx, query, prep["countries"], prep["engines"], prep["pages"],
                           prep["device"])
    return {"message": f"Search started for {query} in "
                       f"{', '.join(c.upper() for c in prep['countries'])}. "
                       "You can follow it live; results land in Runs when it finishes.",
            "job_id": job_id,
            "title": f"Search for {query}",
            # quiet: the panel shows a live job card (assistant.cards.job_card)
            # with an Open button instead of opening the progress panel.
            "intent": {"target": "job", "url": f"/job/{job_id}", "label": "Search progress",
                       "params": {"job_id": job_id}, "quiet": True},
            "resource": ("job", job_id)}


def prepare_create_report(ctx: ToolContext, args: dict) -> dict:
    from processing import llm
    kind = args["kind"]
    period = args.get("period", "latest")
    countries = [c.lower() for c in args.get("countries") or []]
    details = [f"Type: {_REPORT_KINDS[kind]}",
               "Countries: " + (", ".join(c.upper() for c in countries) if countries else "all")]
    if kind == "csv":
        details.append(f"Period: {period}")
    elif period not in ("latest", "all"):
        details.append("Note: PDF and Excel always use the latest run of each country; "
                       "the period only applies to CSV.")
    cost = "Free: built from stored data, nothing is re-fetched from Apify."
    if kind == "pdf" and llm.is_configured():
        cost = "Uses your AI provider once for the written summary (a small cost). No Apify cost."
    edit = {"fields": [
        {"name": "kind", "type": "radio", "label": "Type",
         "options": [{"value": k, "label": _REPORT_LABELS[k]} for k in _REPORT_KINDS], "value": kind},
        {"name": "period", "type": "select", "label": "Period",
         "options": [{"value": k, "label": _PERIOD_LABELS[k]} for k in _PERIODS], "value": period,
         "help": "PDF and Excel use the latest run of each country; the period applies to CSV."},
    ]}
    return {"title": f"Create the {kind.upper()} report?", "details": details,
            "cost_note": cost, "button": "Create report", "edit": edit,
            "normalized_args": {"kind": kind, "period": period, "countries": countries}}


def _report_data_text(db) -> str:
    s = db.get_stats()
    lines = [(f"Totals: {s['by_class']['positive']} positive, {s['by_class']['negative']} negative, "
              f"{s['by_class']['neutral']} neutral. Last run: {s.get('last_run')}.")]
    for r in sorted(db.latest_run_per_country(), key=lambda r: -(r.get("negative") or 0)):
        lines.append(f"- {(r.get('country_code') or '').upper()}: {r.get('positive') or 0} positive, "
                     f"{r.get('negative') or 0} negative, {r.get('neutral') or 0} neutral.")
    seen = set()
    for r in db.get_results_filtered(classification="negative", limit=60):
        key = (r.get("domain"), r.get("country_code"))
        if key not in seen:
            seen.add(key)
            lines.append(f"- negative: {r.get('domain')} · {(r.get('country_code') or '').upper()} "
                         f"· pos {r.get('position')}")
    return "\n".join(lines)


# Assistant periods -> Reports dialog periods (export.reports.PERIODS).
_STORE_PERIODS = {"7d": "week", "30d": "4weeks", "90d": "3months"}


def exec_create_report(ctx: ToolContext, args: dict) -> dict:
    """Builds the file in the subject's report store (export.reports), so it
    also shows under Reports → Previous reports."""
    from export import reports as store
    from export.excel_exporter import neutralize_formula, subject_title

    kind, period = args["kind"], args.get("period", "latest")
    countries = [c.lower() for c in args.get("countries") or []]
    base = _exports_dir(ctx)
    db = ctx.db
    if kind in ("pdf", "xlsx"):
        meta = store.create_report(db, base, ctx.subject_id, kind, countries,
                                   _STORE_PERIODS.get(period, store.DEFAULT_PERIOD),
                                   created_by=ctx.user, source="assistant")
        name = meta["file"]
    else:
        out_dir = store.subject_dir(base, ctx.subject_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = _now().strftime("%Y%m%d_%H%M%S")
        out = out_dir / f"results_{ts}.csv"
        days = _PERIODS.get(period)
        cutoff = (_now() - timedelta(days=days)).isoformat() if days else ""
        latest_ids = ({r["apify_run_id"] for r in db.latest_run_per_country()}
                      if period == "latest" else None)
        rows = []
        for c in countries or [""]:
            rows.extend(db.get_results_filtered(country_code=c, limit=100_000))
        rows = [r for r in rows if (not cutoff or str(r.get("scraped_at") or "") >= cutoff)
                and (latest_ids is None or r.get("run_id") in latest_ids)]
        cols = ["run_id", "country_code", "engine", "query", "position", "url", "domain", "title",
                "description", "classification", "confidence", "classified_by", "same_person",
                "scraped_at"]
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: neutralize_formula(r.get(k)) for k in cols})
        store.register_file(base, ctx.subject_id, out, "csv", countries=countries, period=period,
                            created_by=ctx.user, source="assistant",
                            subject=subject_title(db, ctx.subject_id))
        name = out.name
    url = f"/reports/{ctx.subject_id}/{name}"
    return {"message": f"{kind.upper()} report ready: {name}. It's also under Reports → Previous reports.",
            "file": name, "download_url": url,
            "intent": {"target": "download", "url": url, "label": f"Download {name}",
                       "params": {"file": name}},
            "resource": ("report", name)}


def _clean_url(url: str) -> str:
    url = (url or "").strip()
    if not re.match(r"^https?://[^\s/]+", url):
        raise ToolError("The URL must start with http:// or https://.")
    return url


def _existing_note(db, url: str) -> dict | None:
    return next((n for n in db.list_notes() if n.get("url") == url), None)


def prepare_save_note(ctx: ToolContext, args: dict) -> dict:
    url = _clean_url(args["url"])
    text = args["text"]
    existing = _existing_note(ctx.db, url)
    details = [f"URL: {url}", "Note:\n" + text]
    if args.get("status"):
        details.append(f"Status: {args['status']}")
    if existing:
        details.append("This replaces the existing note text on that URL (its history is kept).")
    edit = {"fields": [{"name": "text", "type": "textarea", "label": "Note (you can edit it)",
                        "value": text, "maxlength": 4000}]}
    return {"title": "Save this note?", "details": [d for d in details if not d.startswith("Note:")],
            "cost_note": "Saved on the Notes board only. Nothing is sent to anyone.",
            "button": "Save note", "edit": edit,
            "normalized_args": {"url": url, "text": text, "status": args.get("status", "")}}


def exec_save_note(ctx: ToolContext, args: dict) -> dict:
    url = _clean_url(args["url"])
    existing = _existing_note(ctx.db, url) or {}
    ctx.db.set_note(url, args["text"], assignee=existing.get("assignee") or "",
                    due_date=existing.get("due_date") or "")
    status = args.get("status")
    if status:
        note = _existing_note(ctx.db, url)
        if note:
            ctx.db.set_note_status(note["id"], status, by=ctx.user, comment="via assistant")
    return {"message": "Note saved on the Notes board.",
            "intent": {"target": "notes", "url": deep_link("/ui/note?url=" + quote(url, safe=""), "panel"),
                       "label": "Notes", "params": {"url": url}},
            "resource": ("url", url)}


def prepare_set_result_label(ctx: ToolContext, args: dict) -> dict:
    row = find_result(ctx.db, args["result_id"])
    if row is None:
        raise ToolError("No result with that id for this subject.")
    label = args["label"]
    current = row.get("classification") or "neutral"
    what = ("Mark as a different person with the same name (hidden from stats and alerts)"
            if label == "not_this_person" else f"Label: {current} → {label} (human verdict, final)")
    cost = "Changes the stored label only."
    if label == "negative":
        cost += " If an AI provider is configured, it is used once to score severity."
    return {"title": "Set this result's label?",
            "details": [(f"{row.get('domain')} · position {row.get('position')} · "
                         f"{(row.get('country_code') or '').upper()}"),
                        _short(row.get("title"), 140), what],
            "cost_note": cost, "button": "Set label",
            "normalized_args": {"result_id": row.get("id"), "label": label}}


def exec_set_result_label(ctx: ToolContext, args: dict) -> dict:
    rid, label = args["result_id"], args["label"]
    if label == "not_this_person":
        url = ctx.db.set_same_person_verdict(rid)
        if url is None:
            raise ToolError("No result with that id for this subject.")
        return {"message": "Marked as a different person with the same name.",
                "resource": ("url", url)}
    updated = ctx.db.set_review_verdict(rid, label)
    if updated is None:
        raise ToolError("No result with that id for this subject.")
    if label == "negative":
        from processing.severity import annotate_severity_for_result
        try:
            annotate_severity_for_result(ctx.db, rid)
        except Exception:
            log.exception("assistant_severity_failed")
    return {"message": f"Label set to {label} ({updated['review_status']}).",
            "previous": updated["previous_classification"], "resource": ("url", updated["url"])}


def prepare_acknowledge_alert(ctx: ToolContext, args: dict) -> dict:
    alert = ctx.db.get_alert(args["alert_id"])
    if alert is None:
        raise ToolError("No alert with that id for this subject.")
    if (alert.get("status") or "open") != "open":
        raise ToolError(f"That alert is already {alert.get('status')}.")
    return {"title": "Acknowledge this alert?",
            "details": [f"#{alert['id']} · {alert.get('severity')} · {(alert.get('country') or '').upper()}",
                        _short(alert.get("message"), 200), f"Assignee: {ctx.user}"],
            "cost_note": "Marks it as acknowledged (still active until resolved).",
            "button": "Acknowledge", "normalized_args": {"alert_id": alert["id"]}}


def exec_acknowledge_alert(ctx: ToolContext, args: dict) -> dict:
    alert = ctx.db.get_alert(args["alert_id"])
    if alert is None:
        raise ToolError("No alert with that id for this subject.")
    ctx.db.ack_alert(alert["id"], assignee=ctx.user)
    return {"message": f"Alert #{alert['id']} acknowledged.", "resource": ("alert", alert["id"])}


def clean_domain(raw: str) -> str:
    d = (raw or "").strip().lower()
    d = re.sub(r"^[a-z][a-z0-9+.-]*://", "", d)
    d = d.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].split("@")[-1].split(":", 1)[0]
    d = d.rstrip(".")
    if not _DOMAIN_RE.match(d):
        raise ToolError(f"'{_short(raw, 60)}' is not a valid domain (example: example.com).")
    return d


def prepare_add_domain(ctx: ToolContext, args: dict) -> dict:
    domain = clean_domain(args["domain"])
    which = args["list"]
    current = ctx.db.list_positive_domains() if which == "positive" else ctx.db.list_negative_domains()
    if domain in current:
        raise ToolError(f"{domain} is already on the {which} list.")
    effect = ("results on it will be labelled positive" if which == "positive"
              else "results on it will be labelled negative")
    return {"title": f"Add {domain} to the {which} domains?",
            "details": [f"Domain: {domain} (and its subdomains)", f"List: {which}: {effect}.",
                        ("Stored results keep their labels until you click \"Apply to stored "
                         "results\" in Settings → Projects and subjects → Domains.")],
            "cost_note": "Changes this subject's domain list.",
            "button": "Add domain", "normalized_args": {"domain": domain, "list": which}}


def exec_add_domain(ctx: ToolContext, args: dict) -> dict:
    domain = clean_domain(args["domain"])
    if args["list"] == "positive":
        ctx.db.add_positive_domain(domain)
    else:
        ctx.db.add_negative_domain(domain)
    return {"message": f"{domain} added to the {args['list']} domains. Use \"Apply to stored "
                       "results\" in Settings → Projects and subjects → Domains to relabel what's "
                       "already stored.",
            "intent": {"target": "domains", "url": URL_DOMAINS, "label": "Domains", "params": {}},
            "resource": ("domain", domain)}


# --------------------------------------------------------------------- #
# Editable confirmation cards
# --------------------------------------------------------------------- #
# What the user may change on a confirmation card before confirming. Anything
# else in an edit is rejected; the edited arguments go through the schema and
# the tool's `prepare` again (same checks as a proposal), and chip/radio/select
# values must be among the options the card offered.
EDITABLE = {"start_search": ("countries", "engines"), "create_report": ("kind", "period"),
            "save_note": ("text",)}


def apply_edits(ctx: ToolContext, tool: Tool, args: dict, edits, edit_spec: dict | None) -> dict:
    """Validated, normalized arguments after the user's edits on the card.
    Raises ToolError with a plain message for anything not allowed."""
    if not isinstance(edits, dict):
        raise ToolError("The changes must be an object.")
    allowed = EDITABLE.get(tool.name, ())
    extra = [k for k in edits if k not in allowed]
    if extra:
        raise ToolError(f"These can't be changed here: {', '.join(sorted(map(str, extra)))}.")
    fields = {f["name"]: f for f in (edit_spec or {}).get("fields", [])}
    for key, value in edits.items():
        field = fields.get(key)
        if field is None:
            raise ToolError(f"'{key}' can't be changed on this card.")
        options = {o["value"] for o in field.get("options", [])}
        if field["type"] == "chips":
            if not isinstance(value, list) or not value:
                raise ToolError(f"Pick at least one of the {field['label'].lower()}.")
            bad = [v for v in value if not isinstance(v, str) or v.lower() not in options]
            if bad:
                raise ToolError(f"Not allowed for {field['label'].lower()}: "
                                f"{', '.join(map(str, bad))}.")
        elif field["type"] in ("radio", "select"):
            if not isinstance(value, str) or value.lower() not in options:
                raise ToolError(f"'{value}' isn't a valid {field['label'].lower()}.")
        elif field["type"] == "textarea":
            if not isinstance(value, str) or not value.strip():
                raise ToolError(f"The {field['label'].split(' (')[0].lower()} can't be empty.")
            if len(value) > int(field.get("maxlength") or 4000):
                raise ToolError("That text is too long.")
    merged = validate_args(tool, {**args, **edits})
    prep = tool.prepare(ctx, merged)
    return prep.get("normalized_args", merged)


# --------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------- #
def _obj(properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties or {}, "required": required or [],
            "additionalProperties": False}


_COUNTRY = {"type": "string", "description": "Country code, e.g. us, gb, ca", "maxLength": 10}
_RUN_ID = {"type": "string", "description": "A run id from list_runs", "maxLength": 120}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 25, "description": "How many items (1-25)"}

TOOLS: dict[str, Tool] = {}


def register(tool: Tool) -> Tool:
    assert tool.kind in KINDS, tool.kind
    assert tool.kind != "action" or (tool.requires_confirmation and tool.prepare), tool.name
    TOOLS[tool.name] = tool
    return tool


for _t in (
    # ---- read ----
    Tool("get_overview",
         "Current state of the active subject: KPIs, latest run per country with deltas vs the "
         "previous run (new/left results), open alerts, pending review count, wins, overdue "
         "notes and an 'attention' list. Call this first when asked what's going on.",
         _obj(), "read", get_overview,
         summary=lambda a, r: "Used: overview of " + (r["subject"]["name"] or "this subject")),
    Tool("list_changes",
         "What changed between two runs of one country (entered, left, moved, reclassified). "
         "Without arguments: the two latest runs of the most recent country.",
         _obj({"country": _COUNTRY,
               "engine": {"type": "string", "enum": ["google", "bing"], "description": "Search engine"},
               "run_a": dict(_RUN_ID, description="Older run id (optional)"),
               "run_b": dict(_RUN_ID, description="Newer run id (optional)")}),
         "read", list_changes, summary=_changes_summary),
    Tool("list_review_queue",
         "Results waiting for a human label (no rule could decide), with why each one is there.",
         _obj({"limit": _LIMIT}), "read", list_review_queue,
         summary=lambda a, r: f"Used: review queue ({r['total_pending']} items)"),
    Tool("list_results",
         "Results of the latest run of each country (the Results page): position, title, domain, "
         "label, who set it (human/rule/ai) and the same-person flag, in position order. Use it "
         "for 'what are the current results' or a table of results by label.",
         _obj({"country": _COUNTRY,
               "label": {"type": "string", "enum": list(LABELS), "description": "Only this label"},
               "limit": {"type": "integer", "minimum": 1, "maximum": _RESULTS_MAX,
                         "description": f"How many items (1-{_RESULTS_MAX})"}}),
         "read", list_results, summary=_results_summary),
    Tool("explain_result",
         "Explain one result's label: which rule hit (domain list, keyword), AI verdict or human "
         "label, confidence, review status, namesake flag, severity, evidence and note.",
         _obj({"result_id": {"type": "integer", "minimum": 1, "description": "Result id"},
               "url": {"type": "string", "maxLength": 2000, "description": "Result URL"}}),
         "read", explain_result,
         summary=lambda a, r: f"Used: result {r['result']['domain']} (position {r['result']['position']})"),
    Tool("list_alerts", "Alerts of the active subject by status.",
         _obj({"status": {"type": "string", "enum": ["active", "open", "acknowledged", "resolved", "all"]},
               "limit": _LIMIT}), "read", list_alerts,
         summary=lambda a, r: f"Used: alerts ({r['count']} {r['status']})"),
    Tool("list_runs", "Most recent search runs (date, country, engine, counts, cost).",
         _obj({"limit": _LIMIT}), "read", list_runs,
         summary=lambda a, r: f"Used: run history ({r['count']} runs)"),
    Tool("list_notes", "Notes board items (removal requests, follow-ups), optionally by status or overdue.",
         _obj({"status": {"type": "string", "enum": list(NOTE_STATUSES)},
               "overdue_only": {"type": "boolean"}}), "read", list_notes,
         summary=lambda a, r: f"Used: notes ({r['count']})"),
    Tool("get_setup_status",
         "Which integrations are configured and what is missing (Apify, AI provider, notification "
         "channels, alert routing, scheduled jobs). Never returns secret values.",
         _obj(), "read", get_setup_status, summary=lambda a, r: "Used: setup status"),
    Tool("list_reports", "Report types that can be created and previously generated report files.",
         _obj(), "read", list_reports,
         summary=lambda a, r: f"Used: reports ({len(r['previous_files'])} previous files)"),
    Tool("app_help",
         "The app's built-in guide: what a feature does, where it is in the UI and how to use it "
         "(e.g. 'telegram', 'review queue', 'compare', 'roles', 'labels'). Empty topic lists topics.",
         _obj({"topic": {"type": "string", "maxLength": 200}}), "read", app_help,
         summary=lambda a, r: "Used: app guide" + (f" ({a['topic']})" if a.get("topic") else "")),
    # ---- navigate ----
    Tool("open_review", "Open the review queue.", _obj(), "navigate",
         _nav("review", URL_REVIEW, "Review queue")),
    Tool("open_compare", "Open Compare for two runs (default: the latest two).",
         _obj({"run_a": _RUN_ID, "run_b": _RUN_ID}), "navigate", open_compare),
    Tool("open_runs", "Open the run history.", _obj(), "navigate", _nav("runs", URL_RUNS, "Runs")),
    Tool("open_timeline", "Open the timeline of events.", _obj(), "navigate",
         _nav("timeline", URL_TIMELINE, "Timeline")),
    Tool("open_evidence", "Open the evidence vault.", _obj(), "navigate",
         _nav("evidence", URL_EVIDENCE, "Evidence")),
    Tool("open_domains",
         "Open the active subject's positive/negative domain lists (Settings → Projects and "
         "subjects → Domains).", _obj(), "navigate",
         _nav("domains", URL_DOMAINS, "Domains")),
    Tool("open_reports", "Open Reports (Results → More → Reports).", _obj(), "navigate",
         _nav("reports", URL_REPORTS, "Reports")),
    Tool("open_ai_checks",
         "Open AI checks (Results → More → AI checks): relabel neutral results with AI and "
         "re-run the same-name/severity checks.", _obj(), "navigate",
         _nav("ai_checks", URL_AI_CHECKS, "AI checks")),
    Tool("open_search",
         "Open the New search dialog (Results → New search) so the user can pick the options "
         "themselves. To run a search for them, propose start_search instead.", _obj(), "navigate",
         _nav("search", URL_SEARCH, "New search"), min_role="operator"),
    Tool("open_notes", "Open the notes board.", _obj(), "navigate", _nav("notes", URL_NOTES, "Notes")),
    Tool("open_alerts", "Open the alerts (the bell in the top bar).", _obj(), "navigate",
         _nav("alerts", URL_ALERTS, "Alerts")),
    Tool("open_settings", "Open a settings section (admin only).",
         _obj({"section": {"type": "string", "enum": list(_SETTINGS_SECTIONS)}}, ["section"]),
         "navigate", open_settings, min_role="admin"),
    Tool("open_result", "Open one result (its run, highlighted).",
         _obj({"result_id": {"type": "integer", "minimum": 1}}, ["result_id"]),
         "navigate", open_result),
    # ---- action (confirmation required) ----
    Tool("start_search",
         "PROPOSE a new search for the active subject (costs money on Apify). The user must "
         "confirm; it does not run until then. Defaults: the subject's countries, google.",
         _obj({"countries": {"type": "array", "items": _COUNTRY, "maxItems": 10},
               "engines": {"type": "array", "items": {"type": "string", "enum": ["google", "bing"]},
                           "maxItems": 2},
               "pages": {"type": "integer", "minimum": 1, "maximum": 3},
               "device": {"type": "string", "enum": ["desktop", "mobile"]}}),
         "action", exec_start_search, True, "operator", prepare_start_search),
    Tool("create_report",
         "PROPOSE creating a report file (pdf | xlsx | csv). The user must confirm.",
         _obj({"kind": {"type": "string", "enum": ["pdf", "xlsx", "csv"]},
               "countries": {"type": "array", "items": _COUNTRY, "maxItems": 10},
               "period": {"type": "string", "enum": list(_PERIODS)}}, ["kind"]),
         "action", exec_create_report, True, "operator", prepare_create_report),
    Tool("save_note",
         "PROPOSE saving a note on a URL (e.g. a drafted removal request). Nothing is sent. "
         "The user must confirm.",
         _obj({"url": {"type": "string", "maxLength": 2000},
               "text": {"type": "string", "maxLength": 4000},
               "status": {"type": "string", "enum": list(NOTE_STATUSES)}}, ["url", "text"]),
         "action", exec_save_note, True, "operator", prepare_save_note),
    Tool("set_result_label",
         "PROPOSE a human label for a result: positive | negative | neutral | not_this_person. "
         "The user must confirm.",
         _obj({"result_id": {"type": "integer", "minimum": 1},
               "label": {"type": "string", "enum": [*LABELS, "not_this_person"]}},
              ["result_id", "label"]),
         "action", exec_set_result_label, True, "operator", prepare_set_result_label),
    Tool("acknowledge_alert", "PROPOSE acknowledging an open alert. The user must confirm.",
         _obj({"alert_id": {"type": "integer", "minimum": 1}}, ["alert_id"]),
         "action", exec_acknowledge_alert, True, "operator", prepare_acknowledge_alert),
    Tool("add_domain",
         "PROPOSE adding a domain to the subject's positive (owned) or negative (blacklist) list. "
         "The user must confirm.",
         _obj({"domain": {"type": "string", "maxLength": 300},
               "list": {"type": "string", "enum": ["positive", "negative"]}}, ["domain", "list"]),
         "action", exec_add_domain, True, "operator", prepare_add_domain),
):
    register(_t)


def get_tool(name: str) -> Tool | None:
    return TOOLS.get(name)


def tool_specs(role: str | None = None) -> list[dict]:
    """Specs of every tool (role=None) or of those `role` may use."""
    return [t.spec() for t in TOOLS.values() if role is None or role_allows(role, t.min_role)]


def normalize_report_name(name: str) -> str | None:
    """A previously generated report's file name if it is safe, else None."""
    return name if _REPORT_PATTERN.match(name or "") and "/" not in name and ".." not in name else None


__all__ = ["EDITABLE", "KINDS", "TOOLS", "Tool", "ToolContext", "ToolError", "apply_edits",
           "get_tool", "role_allows", "sanitize_args", "tool_specs", "validate_args"]
