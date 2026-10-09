"""Proactive suggestion chips and the deterministic greeting (no LLM call).

Both are computed from the active subject's state: pending reviews, new
results/negatives in the latest run, open alerts, overdue notes, failed
search jobs or scheduled jobs, and unconfigured integrations. Each chip is
{id, label, prompt}: the panel shows `label` and sends `prompt` as the
user's message when clicked.
"""
from __future__ import annotations

import logging

from assistant import tools

log = logging.getLogger("assistant")

MAX_CHIPS = 5


def _chip(cid: str, label: str, prompt: str | None = None) -> dict:
    return {"id": cid, "label": label, "prompt": prompt or label}


def _failed_search_jobs(jobs, subject_id: int) -> int:
    """Recent failed search jobs of this subject (JobManager has no public
    list; best-effort read of its snapshot per id)."""
    if jobs is None:
        return 0
    n = 0
    for job_id in list(getattr(jobs, "_jobs", {}) or {}):
        snap = jobs.snapshot(job_id) or {}
        meta = snap.get("meta") or {}
        if snap.get("status") == "error" and meta.get("subject_id", subject_id) == subject_id:
            n += 1
    return n


def compute(ctx: tools.ToolContext, overview: dict | None = None) -> list[dict]:
    db = ctx.db
    overview = overview if overview is not None else tools.get_overview(ctx, {})
    chips: list[dict] = []
    pending = overview.get("pending_review") or 0
    if pending:
        chips.append(_chip("review", f"Help me review the {pending} result{'s' if pending != 1 else ''}"))
    new_neg = sum(1 for c in overview.get("latest_by_country") or []
                  for e in c.get("entered") or [] if e.get("label") == "negative")
    has_changes = any(c.get("entered") or c.get("exited") for c in overview.get("latest_by_country") or [])
    if new_neg:
        chips.append(_chip("changes", f"What are the {new_neg} new negative result(s)?",
                           "What changed since the previous run, especially the new negatives?"))
    elif has_changes:
        chips.append(_chip("changes", "What changed since the last run?"))
    alerts = (overview.get("open_alerts") or {}).get("count") or 0
    if alerts:
        chips.append(_chip("alerts", f"Go through the {alerts} open alert(s)"))
    overdue = overview.get("overdue_notes") or 0
    if overdue:
        chips.append(_chip("notes", f"Show my {overdue} overdue note(s)"))
    failed = _failed_search_jobs(ctx.jobs, ctx.subject_id)
    try:
        failed_sched = [j for j in db.list_scheduled_jobs() if j.get("last_status") == "error"]
    except Exception:  # noqa: BLE001 -- best-effort chip
        failed_sched = []
    if failed or failed_sched:
        chips.append(_chip("failed", "Why did a search or job fail?",
                           "Some searches or scheduled jobs failed recently. What's wrong and how do I fix it?"))
    runs = (overview.get("kpis") or {}).get("total_runs") or 0
    can_act = tools.role_allows(ctx.role, "operator")
    if can_act:
        chips.append(_chip("search", "Run a new search" if runs else "Run my first search"))
    if ctx.role == "admin":
        try:
            from notify import channel_status
            if not any(channel_status(db).values()):
                chips.append(_chip("channels", "How do I get alerts on Telegram?"))
        except Exception:
            log.exception("assistant_channel_status_failed")
    if runs and can_act:
        chips.append(_chip("report", "Make a report for my client"))
    if not chips:
        chips.append(_chip("help", "What can you do?"))
    return chips[:MAX_CHIPS]


def greeting(ctx: tools.ToolContext, overview: dict | None = None) -> str:
    """Plain text greeting, one bullet per item needing attention."""
    overview = overview if overview is not None else tools.get_overview(ctx, {})
    name = (overview.get("subject") or {}).get("name") or "this subject"
    attention = overview.get("attention") or []
    if not (overview.get("kpis") or {}).get("total_runs"):
        return (f"Hi. I can see {name}, but there are no search runs yet. "
                "I can explain how the app works or help you start a first search.")
    if not attention:
        return f"Hi. I can see {name}. Nothing needs attention right now. What would you like to do?"
    bullets = "\n".join(f"• {a}" for a in attention[:5])
    return f"Hi. I can see {name}. Here's what needs attention:\n{bullets}\nWhat would you like to do?"


MAX_FOLLOW_UPS = 3


def _short_domain(d) -> str:
    return str(d or "that site")[:60]


def follow_ups(ctx: tools.ToolContext, trace: list[dict], events: list[dict],
               last_user: str = "", overview: dict | None = None) -> list[dict]:
    """2-3 next-step chips after a reply, derived from what the turn's tools
    returned (deterministic, no LLM call). Falls back to the state chips."""
    can_act = tools.role_allows(ctx.role, "operator")
    out: list[dict] = []
    for entry in trace:
        if not entry.get("executed"):
            continue
        name, res = entry.get("name"), entry.get("result") or {}
        if name == "list_changes":
            new_neg = [e for e in res.get("entered") or [] if e.get("label") == "negative"]
            if new_neg:
                d = _short_domain(new_neg[0].get("domain"))
                if can_act:
                    out.append(_chip("draft", f"Draft a removal request for {d}",
                                     f"Draft a short, polite removal request for the new negative result "
                                     f"on {d} and save it as a note on its URL."))
                out.append(_chip("why", f"Why is {d} negative?",
                                 f"Why is the result on {d} labelled negative?"))
            if res.get("entered") or res.get("moved"):
                out.append(_chip("moved", "Which positive results moved down?"))
            if can_act:
                out.append(_chip("report", "Make a PDF report of this", "Make a PDF report for my client."))
        elif name == "list_review_queue" and res.get("items"):
            first = res["items"][0]
            out.append(_chip("explain", f"Why is {_short_domain(first.get('domain'))} in review?",
                             f"Why is result {first.get('result_id')} in the review queue? "
                             "Which label would you pick?"))
            if len(res["items"]) > 1:
                out.append(_chip("pattern", "What do these have in common?"))
        elif name == "explain_result" and res.get("result"):
            item = res["result"]
            d = _short_domain(item.get("domain"))
            if res.get("label") == "negative" and can_act:
                out.append(_chip("draft", "Draft a removal request for it",
                                 f"Draft a short, polite removal request for {item.get('url') or d} "
                                 "and save it as a note."))
            if res.get("label") == "neutral" and can_act:
                out.append(_chip("mine", f"{d} is mine: add it", f"Add {d} to my positive domains."))
            out.append(_chip("changes", "What else changed in the latest run?"))
        elif name == "get_overview":
            if res.get("pending_review"):
                out.append(_chip("review", f"Help me review the {res['pending_review']} result(s)"))
            if any(c.get("entered") for c in res.get("latest_by_country") or []):
                out.append(_chip("changes", "What changed since the last run?"))
            if (res.get("open_alerts") or {}).get("count"):
                out.append(_chip("alerts", "Go through the open alerts"))
        elif name == "list_alerts" and res.get("items") and can_act:
            out.append(_chip("ack", "Acknowledge the newest open alert"))
        elif name == "list_runs" and (res.get("count") or 0) > 1:
            out.append(_chip("compare", "Compare the two latest runs", "What changed between the two latest runs?"))
        elif name == "list_reports" and can_act:
            out.append(_chip("pdf", "Make a PDF summary", "Make a PDF report for my client."))
        elif name == "get_setup_status" and res.get("missing"):
            first = str(res["missing"][0]).split(":")[0]
            out.append(_chip("setup", f"How do I set up {first[:50]}?"))
    if any(e.get("type") == "confirm" for e in events):
        out.append(_chip("other", "What else needs my attention?"))
    if len(out) < 2:
        try:
            out += compute(ctx, overview)
        except Exception:
            log.exception("assistant_followups_failed")
    seen, chips = set(), []
    said = (last_user or "").strip().lower()
    for c in out:
        key = c["prompt"].strip().lower()
        if key in seen or key == said:
            continue
        seen.add(key)
        chips.append(c)
    return chips[:MAX_FOLLOW_UPS]
