"""Shell overlays: the Project ▸ Subject switcher and subject switching.

The Results-area dialogs and panels live with their features: /ui/search and
/ui/job/{id} (search_runner), /ui/review (review), /ui/compare (diff),
/ui/runs and /ui/run/{id} (runs), /ui/timeline (timeline), /ui/evidence
(evidence), /ui/ai-checks (domains), /ui/reports (export), /ui/note and
/ui/notes (notes); the assistant panel is /assistant/panel (assistant.router).
Domains have no dialog: they live in Settings → Projects and subjects.
Fragment routes return the bare overlay content when loaded by static/app.js
(header X-SM-Fragment: 1) and a normal page otherwise
(ui_helpers.fragment_or_page), so every overlay URL also works in a new tab.
"""
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from dashboard import nav
from dashboard.context import (
    SUBJECT_COOKIE,
    allowed_subjects,
    redirect_with_flash,
    safe_next,
    viewer,
)
from dashboard.deps import db
from dashboard.ui_helpers import (
    _esc,
    subject_label,
)

router = APIRouter()

_SUBJECT_COOKIE_AGE = 365 * 24 * 3600


# --------------------------------------------------------------------- #
# Subject switcher
# --------------------------------------------------------------------- #
def _referer_path(request: Request) -> str:
    ref = request.headers.get("referer") or ""
    parts = urlsplit(ref)
    if parts.netloc and parts.netloc != request.headers.get("host"):
        return "/"
    path = parts.path or "/"
    return safe_next(path + (f"?{parts.query}" if parts.query else ""), "/")


def switcher_html(next_url: str = "/") -> str:
    v = viewer()
    current = db.current_subject().get("id")
    subjects = allowed_subjects()
    projects = {c["id"]: c for c in db.list_clients()}
    groups: dict[int, list[dict]] = {}
    for s in subjects:
        groups.setdefault(s["client_id"], []).append(s)
    parts = []
    for pid in sorted(groups, key=lambda i: (projects.get(i, {}).get("name") or "").lower()):
        parts.append(f"<div class='hd'>{_esc(projects.get(pid, {}).get('name') or 'Project')}</div>")
        for s in groups[pid]:
            is_current = s["id"] == current
            check = f"<span class='check'>{nav.icon('check')}</span>" if is_current else ""
            href = "/subject/switch?" + urlencode({"id": s["id"], "next": next_url})
            aria = " aria-current='true'" if is_current else ""
            demo = " <span class='tag'>demo</span>" if s.get("is_demo") else ""
            parts.append(f"<a class='item' role='menuitem' href='{_esc(href)}'{aria}>"
                         f"<span>{_esc(subject_label(s))}{demo}</span>{check}</a>")
    if not subjects:
        parts.append("<div class='empty'>No subjects available to you.</div>")
    parts.append("<hr>")
    if v.can_write:
        parts.append("<a class='item' role='menuitem' href='/ui/subject/new' data-modal"
                     f" data-title='New subject'>{nav.icon('plus')}New subject</a>")
    parts.append(f"<a class='item' role='menuitem' href='/settings#projects'>{nav.icon('settings')}"
                 "Manage projects</a>")
    return "".join(parts)


@router.get("/ui/switcher", response_class=HTMLResponse, tags=["ui"])
async def ui_switcher(request: Request):
    return HTMLResponse(switcher_html(_referer_path(request)),
                        headers={"Cache-Control": "no-store"})


@router.get("/subject/switch", tags=["ui"])
async def subject_switch(id: str = "", next: str = "/"):
    target = safe_next(next, "/")
    try:
        sid = int(id)
    except ValueError:
        sid = None
    matches = [s for s in allowed_subjects() if s["id"] == sid]
    subject = matches[0] if matches else None
    if subject is None:
        return redirect_with_flash(target, "That subject isn't available.", "error")
    resp = redirect_with_flash(target, f"Showing {subject_label(subject)}.", "info")
    resp.set_cookie(SUBJECT_COOKIE, str(sid), max_age=_SUBJECT_COOKIE_AGE, httponly=True,
                    samesite="lax", path="/")
    return resp
