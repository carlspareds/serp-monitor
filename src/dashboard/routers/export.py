"""Reports and data export.

The Reports dialog (/ui/reports, approved two-tab design):
  * "New report": what it is for (PDF to share / Excel to analyse / CSV for
    tools) -> check the details (subject, countries, period) -> a plain
    "What you'll get" preview computed from real data -> Create (POST
    /reports/create) -> the button becomes Download.
  * "Previous reports": the active subject's files (export.reports store,
    data/exports/<subject_id>/), including the weekly job's.
Downloads: GET /reports/{subject_id}/{file} for any signed-in viewer of that
subject; creating a report is a write (admin/operator, role_middleware).

Also: the legacy direct exports (/export/run, /export/pdf), database backup
and CSV dump. The old /export page redirects to the dialog.
"""
import csv
import io
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.background import BackgroundTask

from dashboard import deps, ui
from dashboard.context import (
    allowed_subjects,
    is_fragment_request,
    redirect_with_flash,
    viewer,
)
from dashboard.deps import db
from dashboard.session import audit, session_user
from dashboard.ui_helpers import (
    _esc,
    fmt_day,
    fragment_or_page,
    ordered_countries,
    overlay_url,
    plural,
    subject_label,
    toast_header,
)
from export import reports as store
from export.excel_exporter import build_workbook, neutralize_formula, subject_title
from export.pdf_report import build_pdf
from processing import ai_classifier

router = APIRouter()

_MODAL = overlay_url("/ui/reports", "modal")
_FORMAT_TAG = {"pdf": "PDF", "xlsx": "Excel", "csv": "CSV"}


def _subject_for(subject_id: str = "") -> dict:
    current = db.current_subject()
    try:
        sid = int(subject_id) if str(subject_id or "").strip() else current.get("id")
    except ValueError:
        sid = current.get("id")
    allowed = allowed_subjects()
    match = next((s for s in allowed if s["id"] == sid), None)
    if match:
        return match
    return current


def _inside(kind: str, info: dict, subject: str) -> list[str]:
    """Plain list of what the file contains, from the real figures."""
    country_text = ", ".join(c.upper() for c in info["countries"]) or "no country yet"
    latest = (f"the latest run of each country ({country_text}, {fmt_day(info['latest_day'])})"
              if info["latest_day"] else "the latest run of each country (no runs yet)")
    negs, wins = len(info["new_negatives"]), len(info["wins"])
    if kind == "pdf":
        items = [f"Positive / negative share and a table per country from {latest}",
                 f"{plural(negs, 'new negative result')} in the period",
                 f"{plural(wins, 'Win')} (negative results that dropped out)"
                 + (f": {', '.join(w['domain'] for w in info['wins'][:2])}" if wins else ""),
                 (f"{plural(info['open_alerts'], 'open alert')}, "
                  f"{info.get('acknowledged_alerts', 0)} acknowledged")]
        items.append("A short AI-written summary" if info["ai_summary"] else
                     "No AI-written summary: no AI provider is set up (Settings → AI provider)")
        items.append("Logged events with their measured effect, if any")
        return items
    if kind == "xlsx":
        return [f"Summary sheet built from {latest}",
                f"History of every run ({plural(info['history_runs'], 'run')})",
                f"One sheet per country with its {plural(info['latest_results'], 'result')}",
                "Labels, severity and notes",
                ("The period doesn't change the workbook: it always holds the latest run and the "
                 "full history.")]
    return [f"{plural(info['csv_rows'], 'stored result')} scraped in the period, all countries"
            if not info.get("one_country") else f"{plural(info['csv_rows'], 'stored result')} scraped in the period",
            "One row per result per run", "Plain columns, no formatting"]


def _tabs(active: str, params: dict) -> str:
    def tab(key, label):
        q = urlencode({**params, "tab": key})
        return (f"<a class='chip' href='/ui/reports?{_esc(q)}' data-sm-nav"
                f" aria-pressed='{'true' if key == active else 'false'}'>{label}</a>")
    return f"<div class='tabs-row' role='group' aria-label='Reports'>{tab('new', 'New report')}{tab('prev', 'Previous reports')}</div>"


def _download_url(meta: dict) -> str:
    return f"/reports/{int(meta.get('subject_id') or 0)}/{meta['file']}"


def reports_html(tab: str = "new", kind: str = "pdf", subject_id: str = "", country: str = "",
                 period: str = store.DEFAULT_PERIOD, created: dict | None = None, error: str = "") -> str:
    kind = kind if kind in store.KINDS else "pdf"
    period = period if period in store.PERIODS else store.DEFAULT_PERIOD
    subject = _subject_for(subject_id)
    sid = subject["id"]
    sdb = db.for_subject(sid)
    params = {"kind": kind, "subject_id": sid, "country": country, "period": period}
    head = "<span hidden data-sm-wide></span>" + _tabs(tab, params)
    if tab == "prev":
        files = store.list_reports(deps.exports_dir, db.current_subject().get("id") or sid)
        if not files:
            listing = ("<div class='empty'><b>No reports yet.</b><br>Reports you create, and the weekly "
                       "report, are kept here.</div>")
        else:
            rows = []
            for m in files:
                extra = []
                if m.get("emailed"):
                    extra.append("sent by email")
                if m.get("source") == "assistant":
                    extra.append("made by the assistant")
                if m.get("created_by"):
                    extra.append(f"by {m['created_by']}")
                rows.append(
                    "<div class='li'><div class='li-top'>"
                    f"<b>{_esc(m.get('title') or 'Report')}</b><span class='tag'>{_FORMAT_TAG.get(m.get('kind'), '')}</span>"
                    f"<span class='muted num'>{_esc(fmt_day(m.get('created_at')))}</span>"
                    f"<a class='btn small rt' href='{_esc(_download_url(m))}' download>Download</a></div>"
                    f"<span class='li-sub'>{_esc(' · '.join([store.human_size(m.get('size') or 0), *extra]))}</span></div>")
            listing = f"<div class='list'>{''.join(rows)}</div>"
        return ui.overlay("Reports", head + listing,
                          "<button type='button' class='btn ghost' data-sm-close>Close</button>")

    tiles = "".join(
        f"<a class='card kpi tile{' is-picked' if k == kind else ''}' href='/ui/reports?{_esc(urlencode({**params, 'kind': k, 'tab': 'new'}))}'"
        f" data-sm-nav aria-pressed='{'true' if k == kind else 'false'}'>"
        f"<span class='tile-title'>{_esc(v['title'])}</span><span class='muted tile-sub'>{_esc(v['format'])}</span></a>"
        for k, v in store.KINDS.items())
    countries = ordered_countries(sdb.distinct_countries())
    country = country if country in countries else ""
    subj_opts = "".join(
        f"<option value='{s['id']}'{' selected' if s['id'] == sid else ''}>{_esc(subject_label(s))}</option>"
        for s in (allowed_subjects() or [subject]))
    c_opts = "<option value=''>All countries</option>" + "".join(
        f"<option value='{_esc(c)}'{' selected' if c == country else ''}>{_esc(c.upper())}</option>"
        for c in countries)
    p_opts = "".join(f"<option value='{k}'{' selected' if k == period else ''}>{_esc(label)}</option>"
                     for k, (label, _d) in store.PERIODS.items())
    details = (
        "<form class='row' method='get' action='/ui/reports' data-sm-submit data-autosubmit>"
        f"<input type='hidden' name='tab' value='new'><input type='hidden' name='kind' value='{kind}'>"
        f"<div class='field'><label for='rp-subject'>Subject</label><select id='rp-subject' name='subject_id'>{subj_opts}</select></div>"
        f"<div class='field'><label for='rp-country'>Countries</label><select id='rp-country' name='country'>{c_opts}</select></div>"
        f"<div class='field'><label for='rp-period'>Period</label><select id='rp-period' name='period'>{p_opts}</select></div>"
        "<noscript><button type='submit' class='btn'>Update</button></noscript></form>"
    )
    sel = [country] if country else []
    info = store.summarize(sdb, kind, sel, period)
    info["one_country"] = bool(country)
    where = country.upper() if country else "all countries"
    preview = (
        "<div class='card card-pad preview'><div class='li-top' style='margin-bottom:6px'>"
        f"<b>{_esc(store.KINDS[kind]['format'])}</b><span class='tag'>{_esc(subject_title(db, sid))} · "
        f"{_esc(where)} · {_esc(info['range_text'])}</span></div>"
        f"<ul>{''.join(f'<li>{_esc(x)}</li>' for x in _inside(kind, info, subject_label(subject)))}</ul></div>"
    )
    err = f"<div class='hint hint-error' role='alert'>{_esc(error)}</div>" if error else ""
    body = (head + err
            + f"<div><div class='note step'><b>1.</b> What is it for?</div><div class='kpis kpis-3 tiles'>{tiles}</div></div>"
            + f"<div><div class='note step'><b>2.</b> Check the details</div>{details}</div>"
            + f"<div><div class='note step'><b>3.</b> What you'll get</div>{preview}</div>")
    if created:
        foot = ("<span class='note rev-note'>Ready. It's also saved under Previous reports.</span>"
                "<button type='button' class='btn ghost' data-sm-close>Close</button>"
                f"<a class='btn primary' href='{_esc(_download_url(created))}' download autofocus>Download</a>")
    elif not viewer().can_write:
        foot = ("<span class='note rev-note'>Your account is read-only: you can download previous "
                "reports but not create new ones.</span>"
                "<button type='button' class='btn ghost' data-sm-close>Close</button>")
    else:
        hidden = "".join(f"<input type='hidden' name='{k}' value='{_esc(v)}'>" for k, v in params.items())
        foot = ("<button type='button' class='btn ghost' data-sm-close>Cancel</button>"
                f"<form class='inline-form' action='/reports/create' method='post' data-sm-submit>{hidden}"
                f"<button type='submit' class='btn primary' data-busy-label='Creating…'>"
                f"{_esc(store.KINDS[kind]['button'])}</button></form>")
    return ui.overlay("Reports", body, foot)


@router.get("/ui/reports", response_class=HTMLResponse, tags=["ui"])
async def ui_reports(tab: str = "new", kind: str = "pdf", subject_id: str = "", country: str = "",
                     period: str = store.DEFAULT_PERIOD):
    inner = reports_html("prev" if tab == "prev" else "new", kind, subject_id, country.lower(), period)
    return fragment_or_page("Reports", inner, active="/results")


@router.post("/reports/create", response_class=HTMLResponse, tags=["export"])
def reports_create(request: Request, kind: str = Form("pdf", max_length=64), subject_id: str = Form("", max_length=64),
                   country: str = Form("", max_length=64), period: str = Form(store.DEFAULT_PERIOD, max_length=64)):
    if kind not in store.KINDS:
        kind = "pdf"
    subject = _subject_for(subject_id)
    sid = subject["id"]
    sdb = db.for_subject(sid)
    country = country.lower()
    countries = [country] if country else []
    try:
        meta = store.create_report(sdb, deps.exports_dir, sid, kind, countries, period,
                                   created_by=session_user(request) or "")
    except Exception:
        import logging
        logging.getLogger("export").exception("report_create_failed")
        msg = "Couldn't create the report. The error was logged; try again in a moment."
        if is_fragment_request():
            return HTMLResponse(reports_html("new", kind, str(sid), country, period, error=msg),
                                status_code=400)
        return redirect_with_flash(_MODAL, msg, "error")
    audit(request, f"report_create_{kind}", resource_type="report", resource_id=meta["file"],
          subject_id=sid, countries=countries, period=period)
    message = "Report ready. It's also saved under Previous reports."
    if is_fragment_request():
        return HTMLResponse(reports_html("new", kind, str(sid), country, period, created=meta),
                            headers=toast_header(message))
    return redirect_with_flash(_download_url(meta), message)


@router.get("/reports/{subject_id}/{name}", tags=["export"])
async def reports_download(subject_id: int, name: str):
    """A report file, for viewers of that subject only."""
    from fastapi import HTTPException

    allowed = {s["id"] for s in allowed_subjects()}
    if subject_id not in allowed and subject_id != db.current_subject().get("id"):
        raise HTTPException(status_code=404, detail="Report not found")
    path = store.find_report(deps.exports_dir, subject_id, name)
    if path is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(str(path), media_type=store.MEDIA[path.suffix.lstrip(".")], filename=path.name)


@router.get("/export", tags=["export"])
async def export_dialog():
    """Old page: the Reports dialog opens over Results."""
    return RedirectResponse(_MODAL, status_code=303)


@router.post("/export/run", tags=["export"])
async def export_run(request: Request, countries: list = Form(default=[])):
    """Direct Excel download of the ACTIVE subject; kept in its report store."""
    sid = db.current_subject().get("id") or 1
    out = store.report_path(db, deps.exports_dir, sid, "xlsx")
    label = subject_title(db, sid)
    build_workbook(db, str(out), countries=countries or None, subject_name=label)
    store.register_file(deps.exports_dir, sid, out, "xlsx", countries=countries,
                        created_by=session_user(request) or "", subject=label)
    audit(request, "export_excel", countries=countries, subject_id=sid)
    return FileResponse(
        str(out),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=out.name,
    )


def _summary_data_text() -> str:
    """Detailed text with the current data (context for the AI)."""
    s = db.get_stats()
    lines = [(f"Global totals: {s['by_class']['positive']} positive, "
              f"{s['by_class']['negative']} negative, {s['by_class']['neutral']} neutral. "
              f"Last run: {s.get('last_run')}.")]
    latest = db.latest_run_per_country()
    latest.sort(key=lambda r: -(r.get("negative") or 0))
    lines.append("\nBy country (sorted by most negative):")
    for r in latest:
        c = (r.get("country_code") or "").upper()
        lines.append(f"- {c}: {r.get('positive') or 0} positive, "
                     f"{r.get('negative') or 0} negative, {r.get('neutral') or 0} neutral.")
    negs = db.get_results_filtered(classification="negative", limit=60)
    if negs:
        seen = set()
        lines.append("\nNEGATIVE results detected (domain · country · position):")
        for r in negs:
            key = (r.get("domain"), r.get("country_code"))
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"- {r.get('domain')} · {(r.get('country_code') or '').upper()} "
                         f"· pos {r.get('position')}")
    return "\n".join(lines)


@router.get("/export/pdf", tags=["export"])
async def export_pdf(request: Request):
    """Direct PDF download of the ACTIVE subject; kept in its report store."""
    sid = db.current_subject().get("id") or 1
    out = store.report_path(db, deps.exports_dir, sid, "pdf")
    summary, note = "", ""
    if ai_classifier.is_configured():
        ok, text = ai_classifier.executive_summary_result(_summary_data_text())
        summary, note = (text, "") if ok else ("", text)
    label = subject_title(db, sid)
    build_pdf(db, str(out), ai_summary=summary, subject_name=label, ai_summary_note=note)
    store.register_file(deps.exports_dir, sid, out, "pdf", created_by=session_user(request) or "",
                        subject=label)
    audit(request, "export_pdf", subject_id=sid)
    return FileResponse(str(out), media_type="application/pdf", filename=out.name)


@router.get("/backup/db", tags=["backup"])
def backup_db():
    """Downloads a consistent copy of the configured SQLite database
    (honors DB_PATH). Uses SQLite's online backup API into a temp file:
    reading the live file directly could miss pages still in the WAL."""
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    src = sqlite3.connect(db.db_path)
    dst = sqlite3.connect(tmp_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return FileResponse(tmp_path, media_type="application/octet-stream",
                        filename=f"seo_dashboard_backup_{ts}.db",
                        background=BackgroundTask(os.unlink, tmp_path))


@router.get("/backup/csv", tags=["backup"])
async def backup_csv():
    """Exports all results to CSV."""
    rows = db.get_results_filtered(limit=100000, include_homonyms=True)
    buf = io.StringIO()
    cols = ["run_id", "country_code", "query", "position", "url", "domain",
            "title", "description", "classification", "same_person", "scraped_at"]
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: neutralize_formula(v) for k, v in r.items()})
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=results_{ts}.csv"},
    )
