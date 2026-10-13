"""Notes on results (4.6): the Note side panel (/ui/note?url=), the Notes list
panel (/ui/notes), POST /notes/save and POST /notes/status.

A note is a small management record per URL: text, status (open ->
in_progress -> escalated -> resolved / dismissed), assignee, due date, and a
status-change history (note_history, "by <user>"). /notes/save edits the text
and, when the status changed, records the transition (audited separately as a
management action). Both answer with PRG + toast; a fetch submit from the
panel gets the refreshed panel back so it stays usable. The old /notes board
redirects to /results with the Notes panel open.
"""
from datetime import UTC, date, datetime
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from common.validate import http_url
from dashboard import ui
from dashboard.context import is_fragment_request, redirect_with_flash, viewer
from dashboard.deps import db
from dashboard.main import _esc, audit
from dashboard.session import session_user
from dashboard.ui_helpers import (
    fmt_day,
    fragment_or_page,
    overlay_url,
    plural,
    toast_header,
)
from database.models import (
    NOTE_STATUS_LABELS,
    NOTE_STATUSES,
    normalize_note_status,
    note_is_overdue,
)

router = APIRouter()


def _today_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).strftime("%Y-%m-%d")


def _who(request: Request) -> str:
    return session_user(request) or "local admin"


def _status_options(current: str) -> str:
    return "".join(
        f"<option value='{s}'{' selected' if s == current else ''}>{_esc(NOTE_STATUS_LABELS[s])}</option>"
        for s in NOTE_STATUSES
    )


def _result_for(url: str) -> dict:
    """Title/domain of the latest stored result with this URL (for the header)."""
    clause, params = db._subject_clause()
    conn = db._connect()
    try:
        row = conn.execute(f"SELECT title, domain FROM search_results WHERE url = ? AND {clause} "  # noqa: S608 -- clause is the constant 'subject_id = ?' (values bound)
                           "ORDER BY id DESC LIMIT 1", (url, *params)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else {}


def _history_html(note_id: int) -> str:
    hist = db.note_history(note_id)
    if not hist:
        return "<p class='note'>No status changes yet.</p>"
    items = "".join(
        "<li>"
        f"{_esc(NOTE_STATUS_LABELS.get(normalize_note_status(h.get('from_status')), h.get('from_status') or 'New'))}"
        f" → {_esc(NOTE_STATUS_LABELS.get(normalize_note_status(h.get('to_status')), h.get('to_status')))}"
        f" · by {_esc(h.get('by') or 'someone')} · {_esc(fmt_day(h.get('at')))}"
        + (f"<div class='li-sub'>{_esc(h.get('comment'))}</div>" if h.get("comment") else "")
        + "</li>"
        for h in hist
    )
    return f"<ul class='history'>{items}</ul>"


def note_panel_html(url: str, error: str = "", values: dict | None = None) -> str:
    url = (url or "").strip()
    if not url:
        return ui.overlay("Note", "<div class='empty'>Pick a result first.</div>")
    existing = next((n for n in db.list_notes() if n["url"] == url), None)
    values = values or {}
    res = _result_for(url)
    title = res.get("title") or url
    status = normalize_note_status((existing or {}).get("status")) if existing else "open"
    status = values.get("status") or status
    text = values.get("note") if "note" in values else (existing or {}).get("note") or ""
    due = values.get("due_date") if "due_date" in values else (existing or {}).get("due_date") or ""
    assignee = values.get("assignee") if "assignee" in values else (existing or {}).get("assignee") or ""
    can_write = viewer().can_write
    ro = "" if can_write else " disabled"
    overdue = existing and note_is_overdue(existing.get("due_date") or "", status, _today_iso())
    head = (f"<div><b>{_esc(title)}</b><div class='rdom break'>{_esc(res.get('domain') or url)}</div>"
            + ("<span class='tag tag-warn'>overdue</span>" if overdue else "") + "</div>")
    err = f"<div class='hint hint-error' role='alert'>{_esc(error)}</div>" if error else ""
    form = (
        f"<form id='note-form' class='stack' action='/notes/save' method='post' data-sm-submit>{err}"
        f"<input type='hidden' name='url' value='{_esc(url)}'>"
        "<div class='field'><label for='nn'>Note</label>"
        f"<textarea id='nn' name='note' rows='4' maxlength='4000'{ro}"
        f" placeholder='What are you doing about this result?'>{_esc(text)}</textarea></div>"
        "<div class='row'>"
        f"<div class='field'><label for='ns'>Status</label><select id='ns' name='status'{ro}>"
        f"{_status_options(status)}</select></div>"
        f"<div class='field'><label for='ndue'>Due date</label><input id='ndue' type='date' name='due_date'"
        f" value='{_esc(due)}'{ro}></div></div>"
        f"<div class='field'><label for='nas'>Assigned to (optional)</label><input id='nas' name='assignee'"
        f" maxlength='80' value='{_esc(assignee)}'{ro}></div>"
        "<div class='field'><label for='ncm'>Comment on the status change (optional)</label>"
        f"<input id='ncm' name='comment' maxlength='300'{ro}></div>"
        "</form>"
    )
    hist = (f"<div><h3 class='sub-h'>History</h3>{_history_html(existing['id'])}</div>" if existing else "")
    foot = ("<a class='btn ghost' href='/ui/notes' data-sm-nav>All notes</a>"
            + ("<button type='submit' form='note-form' class='btn primary'>Save note</button>" if can_write else ""))
    return ui.overlay("Note", head + form + hist, foot)


def notes_list_html(overdue_only: bool = False) -> str:
    today = _today_iso()
    notes = db.list_notes()
    if overdue_only:
        notes = [n for n in notes if note_is_overdue(n.get("due_date") or "", n.get("status"), today)]
    n_over = sum(1 for n in db.list_notes()
                 if note_is_overdue(n.get("due_date") or "", n.get("status"), today))
    filt = ""
    if n_over or overdue_only:
        filt = ("<div class='tabs-row'>"
                f"<a class='chip' href='/ui/notes' data-sm-nav aria-pressed='{'false' if overdue_only else 'true'}'>All</a>"
                f"<a class='chip' href='/ui/notes?overdue=1' data-sm-nav aria-pressed='{'true' if overdue_only else 'false'}'>"
                f"Overdue ({n_over})</a></div>")
    by_status = {s: [] for s in NOTE_STATUSES}
    for n in notes:
        by_status[normalize_note_status(n.get("status"))].append(n)
    sections = []
    for s in NOTE_STATUSES:
        rows = by_status[s]
        if not rows:
            continue
        items = []
        for n in rows:
            over = note_is_overdue(n.get("due_date") or "", n.get("status"), today)
            res = _result_for(n["url"])
            due = (f"<span class='tag{' tag-warn' if over else ''}'>due {_esc(fmt_day(n['due_date']))}"
                   f"{' · overdue' if over else ''}</span>" if n.get("due_date") else "")
            items.append(
                "<div class='li'><div class='li-top'>"
                f"<a class='rtitle' href='/ui/note?{_esc(urlencode({'url': n['url']}))}' data-sm-nav>"
                f"{_esc(res.get('domain') or n['url'])}</a>{due}"
                f"<span class='rt muted num'>{_esc(fmt_day(n.get('updated_at'), year=False))}</span></div>"
                f"<div class='li-sub'>{_esc((n.get('note') or '')[:160]) or '<i>No text</i>'}</div></div>")
        sections.append(f"<h3 class='sub-h'>{_esc(NOTE_STATUS_LABELS[s])} ({len(rows)})</h3>"
                        f"<div class='list'>{''.join(items)}</div>")
    body = (filt + ("".join(sections) or "<div class='empty'><b>No notes yet.</b><br>Add one from a "
                    "result's ⋯ menu in Results.</div>"))
    intro = f"<p class='note'>{plural(len(notes), 'note')}, grouped by status.</p>" if notes else ""
    return ui.overlay("Notes", intro + body)


@router.get("/ui/note", response_class=HTMLResponse, tags=["ui"])
async def ui_note(url: str = ""):
    return fragment_or_page("Note", note_panel_html(url), active="/results")


@router.get("/ui/notes", response_class=HTMLResponse, tags=["ui"])
async def ui_notes(overdue: str = ""):
    return fragment_or_page("Notes", notes_list_html(bool(overdue)), active="/results")


@router.get("/notes")
async def notes_page(url: str = "", overdue: str = ""):
    """Old board: the Notes panel (or one note) opens over Results."""
    if url:
        return RedirectResponse(overlay_url(f"/ui/note?url={quote(url, safe='')}", "panel"), status_code=303)
    return RedirectResponse(overlay_url("/ui/notes" + ("?overdue=1" if overdue else ""), "panel"),
                            status_code=303)


def _answer(url: str, message: str, kind: str = "ok", status: int = 200, error: str = "",
            values: dict | None = None):
    if is_fragment_request():
        inner = note_panel_html(url, error=error, values=values)
        if status == 200:
            inner = "<span hidden data-sm-reload-on-close></span>" + inner
            return HTMLResponse(inner, headers=toast_header(message))
        return HTMLResponse(inner, status_code=status)
    return redirect_with_flash(overlay_url(f"/ui/note?url={quote(url, safe='')}", "panel"), message, kind)


@router.post("/notes/save", response_class=HTMLResponse)
async def notes_save(request: Request, url: str = Form(..., max_length=2048), note: str = Form("", max_length=10000),
                     assignee: str = Form("", max_length=200), due_date: str = Form("", max_length=64), status: str = Form("", max_length=64),
                     comment: str = Form("", max_length=10000)):
    url = url.strip()
    note = note.strip()
    assignee = assignee.strip()
    due_date = due_date.strip()
    values = {"note": note, "assignee": assignee, "due_date": due_date, "status": status}
    if http_url(url) is None:
        # Notes belong to result pages: only an absolute http(s) URL (never
        # javascript:/data: or a relative path that would render as a link).
        return _answer("", "Not saved: that isn't a web page address.", "error", 400,
                       "Not saved: that isn't a web page address.", values)
    if due_date:
        try:
            date.fromisoformat(due_date)
        except ValueError:
            return _answer(url, "Not saved: pick a valid due date.", "error", 400,
                           "Not saved: pick a valid due date.", values)
    before = next((n for n in db.list_notes() if n["url"] == url), None)
    db.set_note(url, note, assignee=assignee, due_date=due_date)
    audit(request, "note_save", resource_type="url", resource_id=url,
          note=note, assignee=assignee, due_date=due_date)
    saved = next((n for n in db.list_notes() if n["url"] == url), None)
    changed = before is None or any((before.get(k) or "") != v for k, v in
                                    (("note", note), ("assignee", assignee), ("due_date", due_date)))
    status = status.strip()
    if (saved and status in NOTE_STATUSES and normalize_note_status(saved.get("status")) != status
            and db.set_note_status(saved["id"], status, by=_who(request), comment=comment.strip())):
        audit(request, "note_status_change", resource_type="url_note", resource_id=saved["id"],
              url=url, to_status=status, comment=comment.strip())
        changed = True
    if saved is None and before is not None:
        return _answer(url, "Note removed.")
    if saved is None:
        return _answer(url, "Nothing to save: the note is empty.", "info")
    return _answer(url, "Note saved." if changed else "No changes.")


@router.post("/notes/status", response_class=HTMLResponse)
async def notes_status(request: Request, note_id: str = Form("", max_length=64), to_status: str = Form("", max_length=64),
                       comment: str = Form("", max_length=10000)):
    # Defensive note_id (H5): a non-numeric value is ignored without writing.
    try:
        note_id_n = int((note_id or "").strip())
    except ValueError:
        return redirect_with_flash(overlay_url("/ui/notes", "panel"), "Nothing changed.", "info")
    to_status = to_status.strip()
    comment = comment.strip()
    ok = db.set_note_status(note_id_n, to_status, by=_who(request), comment=comment)
    note = next((n for n in db.list_notes() if n.get("id") == note_id_n), None)
    if ok:
        # Status changes are a management action: audited (C24) only when the
        # note actually transitioned.
        audit(request, "note_status_change", resource_type="url_note", resource_id=note_id_n,
              url=(note or {}).get("url", ""), to_status=to_status, comment=comment)
    url = (note or {}).get("url", "")
    if not url:
        return redirect_with_flash(overlay_url("/ui/notes", "panel"), "That note doesn't exist.", "error")
    return _answer(url, f"Status set to {NOTE_STATUS_LABELS.get(to_status, to_status)}." if ok
                   else "No changes.")


__all__ = ["note_panel_html", "notes_list_html"]
