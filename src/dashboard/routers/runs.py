"""Runs: the Runs side panel (/ui/runs; "Running now" on top lists the active
subject's running searches, search_runner.running_now_html), one run inside the panel
(/ui/run/{id}) and deleting a run (admin, confirmation dialog, PRG + toast).
The old /runs and /run/{id} pages redirect to /results with the panel open.
"""
import json
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dashboard import ui
from dashboard.context import redirect_with_flash, viewer
from dashboard.deps import db
from dashboard.session import audit
from dashboard.ui_helpers import (
    _esc,
    _pill,
    _short,
    fmt_day,
    fmt_utc,
    fragment_or_page,
    overlay_url,
    plural,
)

router = APIRouter()

_RUNS_PANEL = overlay_url("/ui/runs", "panel")


def _names(queries) -> str:
    """runs.queries is stored as a JSON list; show it as plain text."""
    text = str(queries or "")
    try:
        value = json.loads(text)
    except ValueError:
        return text
    return ", ".join(str(q) for q in value) if isinstance(value, list) else text


def _counts(r: dict) -> str:
    total = r.get("total_results") or 0
    return (f"{plural(total, 'result')} · {r.get('positive') or 0} positive · "
            f"{r.get('negative') or 0} negative · {r.get('neutral') or 0} neutral")


def _tags(r: dict) -> str:
    tags = [f"<span class='tag'>{_esc((r.get('country_code') or '').upper() or '?')}</span>"]
    if (r.get("engine") or "google") != "google":
        tags.append(f"<span class='tag'>{_esc((r.get('engine') or '').title())}</span>")
    if (r.get("device") or "desktop") != "desktop":
        tags.append(f"<span class='tag'>{_esc(r.get('device'))}</span>")
    status = (r.get("status") or "").upper()
    if status and status not in ("SUCCEEDED", "COMPLETED"):
        tags.append(f"<span class='tag tag-warn'>{_esc(status.lower())}</span>")
    return "".join(tags)


def _delete_form(r: dict) -> str:
    if not viewer().is_admin:
        return ""
    rid = r.get("apify_run_id") or ""
    what = (f"Delete the {(r.get('country_code') or '').upper()} run of {fmt_utc(r.get('created_at'))} "
            f"and its {plural(r.get('total_results') or 0, 'result')}? This can't be undone.")
    return (f"<form class='inline-form' action='/run/{_esc(rid)}/delete' method='post' data-sm-submit>"
            f"<button type='submit' class='btn ghost small danger-text' data-sm-confirm='{_esc(what)}'"
            " data-confirm-title='Delete this run?' data-confirm-label='Delete run'>Delete</button></form>")


def runs_panel_html() -> str:
    from dashboard.routers.search_runner import running_now_html

    running = running_now_html()
    runs = db.list_runs(limit=100)
    if not runs:
        body = (f"{running}<div class='empty'><b>No runs yet.</b><br>Each search you run shows up here.</div>")
        return ui.overlay("Runs", body)
    items = []
    for r in runs:
        rid = r.get("apify_run_id") or ""
        items.append(
            "<div class='li'><div class='li-top'>"
            f"<b class='num'>{_esc(fmt_utc(r.get('created_at')))}</b>{_tags(r)}</div>"
            f"<div class='li-sub num'>{_esc(_counts(r))}</div>"
            f"<div class='run-actions'>"
            f"<a class='btn small' href='/ui/run/{_esc(rid)}' data-sm-nav>Open</a>"
            f"<a class='btn small' href='/ui/compare?{urlencode({'b': rid})}' data-modal data-modal-wide"
            " data-title='Compare runs'>Compare with previous</a>"
            f"{_delete_form(r)}</div></div>"
        )
    body = (f"{running}"
            + ("<h3 class='section-label'>Finished runs</h3>" if running else "")
            + f"<p class='note'>{plural(len(runs), 'run')}, newest first. Open one to see its results.</p>"
            f"<div class='list'>{''.join(items)}</div>")
    return ui.overlay("Runs", body)


def run_panel_html(run_id: str) -> str | None:
    meta = db.get_run(run_id)
    results = db.get_results_by_run(run_id)
    if not meta and not results:
        return None
    meta = meta or {}
    country = (meta.get("country_code") or (results[0].get("country_code") if results else "") or "").lower()
    day = (meta.get("created_at") or "")[:10]
    show = "/results?" + urlencode({k: v for k, v in (("country", country), ("date", day)) if v})
    rows = "".join(
        f"<tr class='{_esc(r.get('classification') or 'neutral')}'><td class='num muted'>{_esc(r.get('position'))}</td>"
        f"<td><span class='rtitle'>{_esc(_short(r.get('title'), 90))}</span>"
        f"<div class='rdom dom' title='{_esc(r.get('domain'))}'>{_esc(r.get('domain'))}</div></td>"
        f"<td>{_pill(r.get('classification') or 'neutral')}</td></tr>"
        for r in sorted(results, key=lambda x: (x.get("position") or 999)))
    table = (f"<div class='table-wrap card'><table><thead><tr><th class='num'>#</th><th>Result</th>"
             f"<th>Label</th></tr></thead><tbody>{rows}</tbody></table></div>"
             if rows else "<div class='empty'>This run has no results.</div>")
    body = (
        "<a class='btn ghost small back-link' href='/ui/runs' data-sm-nav>← All runs</a>"
        f"<div class='li-top'>{_tags(meta)}<span class='muted'>Tracked name: "
        f"{_esc(_names(meta.get('queries')))}</span></div>"
        f"<div class='li-sub num'>{_esc(_counts(meta))}</div>"
        f"{table}"
    )
    foot = (f"{_delete_form(meta) if meta else ''}"
            f"<a class='btn' href='/ui/compare?{urlencode({'b': run_id})}' data-modal data-modal-wide"
            " data-title='Compare runs'>Compare with previous</a>"
            f"<a class='btn primary' href='{_esc(show)}'>Show in Results</a>")
    title = f"Run of {fmt_day(meta.get('created_at'))}" if meta.get("created_at") else "Run"
    return ui.overlay(title, body, foot)


@router.get("/ui/runs", response_class=HTMLResponse, tags=["ui"])
async def ui_runs():
    return fragment_or_page("Runs", runs_panel_html(), active="/results")


@router.get("/ui/run/{run_id}", response_class=HTMLResponse, tags=["ui"])
async def ui_run(run_id: str):
    inner = run_panel_html(run_id)
    if inner is None:
        inner = ui.overlay("Run", "<div class='empty'>That run doesn't exist (it may have been "
                           "deleted).</div><a class='btn ghost small' href='/ui/runs' data-sm-nav>"
                           "← All runs</a>")
        return fragment_or_page("Run", inner, active="/results", status_code=404)
    return fragment_or_page("Run", inner, active="/results")


@router.get("/runs", tags=["runs"])
async def show_runs():
    """Old page: the Runs panel opens over Results."""
    return RedirectResponse(_RUNS_PANEL, status_code=303)


@router.get("/run/{run_id}", tags=["runs"])
async def show_run(run_id: str):
    """Old page: that run opens in the Runs panel over Results."""
    return RedirectResponse(overlay_url(f"/ui/run/{run_id}", "panel"), status_code=303)


@router.post("/run/{run_id}/delete", response_class=HTMLResponse, tags=["runs"])
async def delete_run_route(request: Request, run_id: str):
    if not viewer().is_admin:
        return redirect_with_flash(_RUNS_PANEL, "Only an admin can delete runs.", "error")
    meta = db.get_run(run_id)
    if meta is None:
        return redirect_with_flash(_RUNS_PANEL, "That run was already deleted.", "info")
    db.delete_run(run_id)
    audit(request, "run_delete", resource_type="run", resource_id=run_id)
    return redirect_with_flash(_RUNS_PANEL, f"Deleted the {(meta.get('country_code') or '').upper()} run "
                                            f"of {fmt_utc(meta.get('created_at'))}.")
