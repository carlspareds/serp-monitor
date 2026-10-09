"""Positive and negative domain lists of the ACTIVE subject, batch
reclassification of its stored results, and the "AI checks" dialog.

ONE home per feature:
  * Domains live in Settings → Projects and subjects → the active subject's
    "Domains" area (`domains_manager_html`, rendered by
    routers/subjects.projects_card_html inside <details id='domains'>). The
    old /domains page and the /ui/domains dialog redirect there
    (/settings#domains). The Results row menu's "Mark this domain as mine /
    negative" is a one-click action on the same lists, not a second manager.
  * "Relabel with AI" (POST /domains/reclassify-ai) and "Run AI checks"
    (POST /ai/annotate) act on stored results, so their home is Results →
    More → "AI checks…" (/ui/ai-checks), which explains both with honest
    counts and caps.

Every POST redirects back to the `next` form field (default /settings#domains,
or /results for the AI actions) with a toast -- no POST URL is left in the
address bar.
"""
import logging
import sqlite3

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config.positive_domains import POSITIVE_DOMAINS
from dashboard import ui
from dashboard.context import (
    is_fragment_request,
    redirect_with_flash,
    safe_next,
    viewer,
)
from dashboard.deps import db
from dashboard.session import audit, can_write
from dashboard.ui_helpers import (
    _RECLASSIFY_BATCH_LIMIT_SETTING,
    _esc,
    _reclassify_batch_limit,
    active_project_and_subject,
    fragment_or_page,
    merged_positive_domains,
    plural,
    subject_label,
)
from processing import ai_checks, ai_classifier, ai_status, llm
from processing.classifier import classify_with_confidence

router = APIRouter()
log = logging.getLogger("dashboard")

_BACK = "/settings#domains"
_AI_BACK = "/results"
_AI_SETUP_LINK = "<a href='/settings#ai'>Set up an AI provider in Settings → AI provider</a>"


def _clean_domain(raw: str) -> str:
    d = (raw or "").strip().lower()
    for prefix in ("https://", "http://"):
        d = d.removeprefix(prefix)
    d = d.split("/")[0]
    d = d.removeprefix("www.")
    return d


def _domain_rows(domains, remove_action: str, next_url: str, can_write: bool) -> str:
    rows = []
    for d in domains:
        remove = ""
        if can_write:
            remove = (f"<form class='inline-form rt' action='{remove_action}' method='post'>"
                      f"<input type='hidden' name='domain' value='{_esc(d)}'>"
                      f"<input type='hidden' name='next' value='{_esc(next_url)}'>"
                      "<button type='submit' class='btn ghost small'>Remove"
                      f"<span class='sr-only'> {_esc(d)}</span></button></form>")
        rows.append(f"<div class='li-top' style='padding:6px 0;border-bottom:1px solid var(--line)'>"
                    f"<span>{_esc(d)}</span>{remove}</div>")
    return "".join(rows) or "<p class='note'>None yet.</p>"


def domains_manager_html(next_url: str = _BACK, apply_label: str = "Apply to stored results") -> str:
    v = viewer()
    _project, subject = active_project_and_subject()
    custom_pos = db.list_positive_domains()
    neg = db.list_negative_domains()
    nxt = f"<input type='hidden' name='next' value='{_esc(next_url)}'>"
    add_form = ""
    if v.can_write:
        add_form = (
            "<form class='row' method='post' action='/domains/add-any'>"
            f"{nxt}<div class='field'><label for='dm-domain'>Add a domain</label>"
            "<input id='dm-domain' type='text' name='domain' placeholder='example.com' required></div>"
            "<div class='field' style='flex:0 0 auto'><label for='dm-list'>List</label>"
            "<select id='dm-list' name='list'><option value='positive'>Mine (positive)</option>"
            "<option value='negative'>Negative</option></select></div>"
            "<button type='submit' class='btn primary'>Add</button></form>"
        )
    builtin = ""
    if POSITIVE_DOMAINS:
        builtin = (f"<p class='note'>Built in for every subject: "
                   f"{_esc(', '.join(POSITIVE_DOMAINS))}.</p>")
    apply = ""
    if v.can_write:
        apply = (
            "<div class='actions-row'>"
            f"<form class='inline-form' action='/domains/reclassify' method='post'>{nxt}"
            f"<button type='submit' class='btn primary'>{_esc(apply_label)}</button></form></div>"
            "<p class='note'>Relabels the stored results with the rules and these lists (no AI, "
            "free). To let the AI relabel what the rules leave neutral, use Results → More → "
            "AI checks.</p>"
        )
    return (
        f"<p class='note'>For <b>{_esc(subject_label(subject))}</b>. Results from your own domains count "
        "as positive; results from the negative list count as negative. New searches use the lists "
        f"right away; use <i>{_esc(apply_label)}</i> to relabel what's already stored.</p>"
        f"{add_form}"
        "<div class='two' style='grid-template-columns:repeat(auto-fit,minmax(220px,1fr))'>"
        f"<div><h3 style='margin:0 0 6px'>Mine ({len(custom_pos)})</h3>"
        f"{_domain_rows(custom_pos, '/domains/remove', next_url, v.can_write)}</div>"
        f"<div><h3 style='margin:0 0 6px'>Negative ({len(neg)})</h3>"
        f"{_domain_rows(neg, '/domains/remove_neg', next_url, v.can_write)}</div></div>"
        f"{builtin}{apply}"
    )


HOME = "/settings#domains"


@router.get("/ui/domains", tags=["ui"])
async def ui_domains():
    """Old dialog: domains have one home, the active subject's Domains area
    in Settings → Projects and subjects."""
    if is_fragment_request():
        body = ("<p>Domains now live with each subject: <a href='/settings#domains'>Settings → "
                "Projects and subjects → Domains</a>.</p>")
        return HTMLResponse(ui.overlay("Domains", body), headers={"X-SM-Redirect": HOME})
    return RedirectResponse(HOME, status_code=303)


@router.get("/domains", tags=["domains"])
async def show_domains():
    """Old page: same single home."""
    return RedirectResponse(HOME, status_code=303)


def _back(next_url: str, default: str = _BACK) -> str:
    return safe_next(next_url, default)


async def _add(request: Request, domain: str, which: str, next_url: str):
    d = _clean_domain(domain)
    if not d or "." not in d or " " in d:
        return redirect_with_flash(_back(next_url), "Not added: enter a domain like example.com.", "error")
    existing = db.list_positive_domains() if which == "positive" else db.list_negative_domains()
    if d in existing:
        return redirect_with_flash(_back(next_url), f"{d} is already on the list.", "info")
    if which == "positive":
        db.add_positive_domain(d)
    else:
        db.add_negative_domain(d)
    audit(request, "domain_add", resource_type="domain", resource_id=d, list=which)
    label = "your domains" if which == "positive" else "the negative list"
    return redirect_with_flash(_back(next_url), f"Added {d} to {label}.")


async def _remove(request: Request, domain: str, which: str, next_url: str):
    d = (domain or "").strip()
    existing = db.list_positive_domains() if which == "positive" else db.list_negative_domains()
    if d not in existing:
        return redirect_with_flash(_back(next_url), f"{d or 'That domain'} wasn't on the list.", "info")
    if which == "positive":
        db.remove_positive_domain(d)
    else:
        db.remove_negative_domain(d)
    audit(request, "domain_remove", resource_type="domain", resource_id=d, list=which)
    return redirect_with_flash(_back(next_url), f"Removed {d}.")


@router.post("/domains/add-any", tags=["domains"])
async def add_any_domain(request: Request, domain: str = Form(""), list: str = Form("positive"),
                         next: str = Form(_BACK)):
    return await _add(request, domain, "negative" if list == "negative" else "positive", next)


@router.post("/domains/add", tags=["domains"])
async def add_domain(request: Request, domain: str = Form(""), next: str = Form(_BACK)):
    return await _add(request, domain, "positive", next)


@router.post("/domains/remove", tags=["domains"])
async def remove_domain(request: Request, domain: str = Form(""), next: str = Form(_BACK)):
    return await _remove(request, domain, "positive", next)


@router.post("/domains/add_neg", tags=["domains"])
async def add_neg_domain(request: Request, domain: str = Form(""), next: str = Form(_BACK)):
    return await _add(request, domain, "negative", next)


@router.post("/domains/remove_neg", tags=["domains"])
async def remove_neg_domain(request: Request, domain: str = Form(""), next: str = Form(_BACK)):
    return await _remove(request, domain, "negative", next)


@router.post("/domains/reclassify", tags=["domains"])
async def reclassify_results(request: Request, next: str = Form(_BACK)):
    pos = merged_positive_domains()
    neg = db.list_negative_domains()
    n = db.reclassify_all(
        lambda rec: classify_with_confidence(rec, positive_domains=pos, negative_domains=neg)
    )
    audit(request, "domains_reclassify", reclassified=n)
    msg = (f"Domains saved. {plural(n, 'result')} relabelled with the current lists." if n else
           "Domains saved. No stored result changed label.")
    return redirect_with_flash(_back(next), msg)


@router.post("/domains/reclassify-ai", tags=["domains"])
def reclassify_ai(request: Request, next: str = Form(_AI_BACK)):
    # Plain `def`: FastAPI runs it in a worker thread, so the AI calls (up to
    # `limit` of them) don't block the server's event loop.
    if not ai_classifier.is_configured():
        return redirect_with_flash(_back(next, _AI_BACK), "Nothing was relabelled: AI relabelling needs an AI "
                                                "provider (Settings → AI provider).", "error")
    pos = merged_positive_domains()
    neg = db.list_negative_domains()
    counts = {"checked": 0, "labelled": 0}
    ai_runs: set[str] = set()
    failed_runs: set[str] = set()

    def fn(rec):
        base = classify_with_confidence(rec, positive_domains=pos, negative_domains=neg)
        if base["classification"] != "neutral" or base["confidence"] == 1.0:
            return base
        counts["checked"] += 1
        ai_runs.add(rec.get("run_id"))
        ai_result = ai_classifier.classify_with_ai_confidence(
            {k: v for k, v in rec.items() if k != "run_id"})
        if ai_result.get("classification"):
            counts["labelled"] += 1
            return ai_result
        failed_runs.add(rec.get("run_id"))
        return base

    limit = _reclassify_batch_limit()
    try:
        with llm.session_scope(), ai_status.collect_failures() as failures:
            n = db.reclassify_all(fn, recompute_pending=True, limit=limit)
    except sqlite3.OperationalError:
        log.exception("domains_reclassify_ai_db_error")
        return redirect_with_flash(_back(next, _AI_BACK), "Nothing was saved: the database was busy. "
                                                "Try again in a moment.", "error")
    checked, labelled = counts["checked"], counts["labelled"]
    failed = checked - labelled
    # A stale "AI checks skipped" banner must not outlive a working AI.
    try:
        ai_checks.refresh_run_notes(db, sorted(r for r in ai_runs if r), failed_runs,
                                    failures[0] if failures else "")
    except sqlite3.OperationalError:
        log.exception("domains_reclassify_ai_note_refresh")
    audit(request, "domains_reclassify_ai", reclassified=n, limit=limit, ai_checked=checked,
          ai_labelled=labelled, ai_failed=failed, ai_failed_calls=len(failures))
    model = ai_classifier.provider_info().get("model") or ""
    changed = f"{plural(n, 'result')} changed label"
    if not checked:
        return redirect_with_flash(_back(next, _AI_BACK), "No neutral results needed the AI. "
                                                + (f"{changed} with the current lists." if n else
                                                   "Nothing changed."), "info")
    if failed:
        reason = failures[0] if failures else "the AI's answers couldn't be read as a label."
        log.warning("domains_reclassify_ai_failed checked=%d failed=%d calls=%d: %s",
                    checked, failed, len(failures), reason)
        msg = (f"Relabelled {labelled} of {checked} results with AI ({changed}); "
               f"{failed} {'call' if failed == 1 else 'calls'} failed: {reason}")
        return redirect_with_flash(_back(next, _AI_BACK), msg, "error" if not labelled else "info")
    return redirect_with_flash(_back(next, _AI_BACK), f"Relabelled {labelled} of {checked} results with AI "
                                            f"({changed}; up to {limit} per click, model {model}).")


@router.post("/ai/annotate", tags=["domains"])
def ai_annotate(request: Request, next: str = Form(_AI_BACK)):
    """Run AI checks: re-runs same-name + severity on the active subject's
    latest stored results that don't have them yet (processing/ai_checks.py),
    capped per click. Operator or admin."""
    if not can_write(request):
        return redirect_with_flash(_back(next, _AI_BACK), "Only an operator or admin can run AI checks.", "error")
    if not ai_classifier.is_configured():
        return redirect_with_flash(_back(next, _AI_BACK), "No AI checks were run: they need an AI provider "
                                                "(Settings → AI provider).", "error")
    try:
        res = ai_checks.run_ai_checks(db)
    except sqlite3.OperationalError:
        log.exception("ai_annotate_db_error")
        return redirect_with_flash(_back(next, _AI_BACK), "The AI checks stopped: the database was busy. "
                                                "Try again in a moment.", "error")
    audit(request, "ai_annotate", runs=res["runs"], same_checked=res["same_checked"],
          same_set=res["same_set"], same_inherited=res["same_inherited"],
          severity_checked=res["severity_checked"], severity_set=res["severity_set"],
          failed=res["same_failed"] + res["severity_failed"], failed_calls=res["failed_calls"])
    msg, kind = ai_checks.summary_message(res)
    return redirect_with_flash(_back(next, _AI_BACK), msg, kind)


# --------------------------------------------------------------------- #
# Results → More → "AI checks…"
# --------------------------------------------------------------------- #
def _relabel_candidates() -> int:
    """Stored results the rules leave neutral (not a person's label): what
    "Relabel with AI" would send to the AI, at most `limit` per click."""
    rows = db.get_results_filtered(classification="neutral", limit=20_000, include_homonyms=True)
    return sum(1 for r in rows if r.get("classified_by") != "human"
               and float(r.get("confidence") or 0) < 1.0)


def ai_checks_html(next_url: str = _AI_BACK) -> str:
    v = viewer()
    ai_on = ai_classifier.is_configured()
    limit = _reclassify_batch_limit()
    counts = ai_checks.pending_counts(db)
    neutral = _relabel_candidates()
    cap = ai_checks.DEFAULT_LIMIT
    nxt = f"<input type='hidden' name='next' value='{_esc(safe_next(next_url, _AI_BACK))}'>"
    if not ai_on:
        link = (_AI_SETUP_LINK if v.is_admin else
                "Ask an administrator to set up an AI provider (Settings → AI provider)")
        state = (f"<div class='hint' role='status' data-ai-checks-off><b>No AI provider yet.</b> "
                 f"{link}. Until then results are labelled by the rules only.</div>")
    else:
        model = ai_classifier.provider_info().get("model") or ""
        state = (f"<p class='note'>Uses your AI provider (model <code>{_esc(model)}</code>) and its "
                 "quota. Nothing runs until you click.</p>")
    disabled = "" if (ai_on and v.can_write) else " disabled aria-disabled='true'"
    who = "" if v.can_write else "<p class='note'>Only an operator or admin can run these.</p>"
    if ai_on and not (db.get_subject_context() or "").strip():
        sid = db.current_subject().get("id") or 1
        state += ("<div class='hint hint-warn' role='status' data-ai-no-context><b>This subject has "
                  "no description.</b> Add a one-line description so the AI can tell this person "
                  "from others with the same name. "
                  f"<a href='/ui/subject/{int(sid)}/edit' data-modal data-title='Edit subject'>"
                  "Edit the subject</a></div>")
    same, sev = counts["same_todo"], counts["severity_todo"]
    relabel = (
        "<div class='aic-block' data-aic='relabel'><h3>Relabel with AI</h3>"
        f"<p>Asks the AI to label stored results the rules leave <b>neutral</b>. "
        f"<b>{plural(neutral, 'result is', 'results are')}</b> neutral right now. Each click checks a "
        f"random sample of up to <b>{limit}</b> stored results (setting "
        f"<code>{_RECLASSIFY_BATCH_LIMIT_SETTING}</code>); only the neutral ones in it are sent to "
        "the AI. Labels a person set are never changed.</p>"
        f"<form class='inline-form' action='/domains/reclassify-ai' method='post' data-sm-submit>{nxt}"
        f"<button type='submit' class='btn primary' data-busy-label='Relabelling…'{disabled}>"
        "Relabel with AI</button></form></div>")
    checks = (
        "<div class='aic-block' data-aic='annotate'><h3>Run AI checks</h3>"
        "<p>Re-runs the <b>same-name</b> check (is this result about this subject?) and the "
        "<b>severity</b> score (1 to 5) on the latest run of each country, engine and device. "
        f"Waiting now: <b>{plural(same, 'result')}</b> without a same-name check and "
        f"<b>{plural(sev, 'negative')}</b> without a severity. Up to <b>{cap}</b> per check per "
        "click; click again to continue.</p>"
        f"<form class='inline-form' action='/ai/annotate' method='post' data-sm-submit>{nxt}"
        f"<button type='submit' class='btn' data-busy-label='Checking…'{disabled}>Run AI checks"
        "</button></form></div>")
    if not counts["runs"]:
        checks = checks.replace("<p>Re-runs", "<p class='note'>No stored results yet: run a search "
                                "first.</p><p>Re-runs", 1)
    return f"{state}{who}{relabel}{checks}"


@router.get("/ui/ai-checks", response_class=HTMLResponse, tags=["ui"])
async def ui_ai_checks(next: str = _AI_BACK):
    body = ai_checks_html(next)
    foot = "<button type='button' class='btn ghost' data-sm-close>Close</button>"
    return fragment_or_page("AI checks", ui.overlay("AI checks", body, foot), active="/results")
