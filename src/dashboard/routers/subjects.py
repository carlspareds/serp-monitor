"""Projects and subjects (Settings → Projects and subjects).

A project is a `clients` row (the UI calls it "Project"); a subject is one
tracked person or company and belongs to one project. Each subject's domain
lists live here too (the single home of Domains): the active subject's row
has an expandable "Domains" area (<details id='domains'>, the target of every
/settings#domains link); another subject's "Domains" switches to it first.
Forms open as modals (fragments below) and post to plain endpoints that redirect back with a toast
(PRG). Writes need an operator or admin (role_middleware blocks viewers);
users tied to a project (client users) can only manage subjects of their own
project and can't create projects.
"""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from dashboard import nav, ui
from dashboard.context import (
    SUBJECT_COOKIE,
    allowed_subjects,
    is_fragment_request,
    redirect_with_flash,
    safe_next,
    viewer,
)
from dashboard.deps import db
from dashboard.session import audit
from dashboard.ui_helpers import (
    COUNTRIES,
    _esc,
    country_name,
    fragment_or_page,
    subject_label,
)
from database.subjects import SUBJECT_TYPES

router = APIRouter()

_BACK = "/settings#projects"
_TYPE_LABELS = {"person": "Person", "company": "Company", "brand": "Brand"}


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #
def _projects_for_viewer(include_archived: bool = False) -> list[dict]:
    v = viewer()
    projects = db.list_clients(include_inactive=include_archived)
    if v.restricted:
        projects = [p for p in projects if p["id"] == v.client_id]
    return projects


def _can_touch_subject(subject_id: int) -> bool:
    return any(s["id"] == subject_id for s in allowed_subjects()) or (
        not viewer().restricted and db.get_subject(subject_id) is not None)


def _form_error(title: str, form_html: str, message: str, next_url: str,
                submit_label: str = "Save", form_id: str = "subject-form"):
    """Validation error: re-render the fragment with the message (overlay)
    or redirect back with an error toast (plain form)."""
    if is_fragment_request():
        body = f"<div class='flash flash-error' role='alert'>{_esc(message)}</div>{form_html}"
        foot = ("<button type='button' class='btn ghost' data-sm-close>Cancel</button>"
                f"<button type='submit' class='btn primary' form='{form_id}'>{_esc(submit_label)}</button>")
        return HTMLResponse(ui.overlay(title, body, foot), status_code=400)
    return redirect_with_flash(safe_next(next_url, _BACK), f"Not saved: {message}", "error")


def _country_checks(selected: list[str]) -> str:
    return "".join(
        f"<label><input type='checkbox' name='countries' value='{c}'"
        f"{' checked' if c in selected else ''}> {_esc(country_name(c))}</label>"
        for c in COUNTRIES
    )


def _subject_form(action: str, subject: dict | None = None, project_id: int | None = None,
                  next_url: str = _BACK) -> str:
    s = subject or {}
    projects = _projects_for_viewer()
    current_project = s.get("client_id") or project_id or (projects[0]["id"] if projects else 1)
    proj_opts = "".join(
        f"<option value='{p['id']}'{' selected' if p['id'] == current_project else ''}>{_esc(p['name'])}</option>"
        for p in projects
    )
    types = [t for t in SUBJECT_TYPES if t != "brand" or s.get("type") == "brand"]
    current_type = s.get("type") or "person"
    type_radios = "".join(
        f"<label><input type='radio' name='type' value='{t}'{' checked' if t == current_type else ''}>"
        f" {_TYPE_LABELS[t]}</label>" for t in types)
    query = (s.get("queries") or [""])[0] if s else ""
    countries = s.get("countries") or ([] if s else ["us"])
    switch = ("" if subject else
              "<label class='checks'><input type='checkbox' name='switch' value='1' checked> "
              "Switch to this subject now</label>")
    project_field = (
        "<div class='field'><label for='sf-project'>Project</label>"
        f"<select id='sf-project' name='project_id'>{proj_opts}</select>"
        "<span class='help'>The group it belongs to, e.g. a client.</span></div>")
    return (
        f"<form method='post' action='{_esc(action)}' class='form-grid' id='subject-form' data-sm-submit>"
        f"<input type='hidden' name='next' value='{_esc(next_url)}'>"
        "<div class='field'><label for='sf-name'>Name</label>"
        f"<input id='sf-name' type='text' name='name' value='{_esc(s.get('name'))}' required"
        " placeholder='e.g. Jane Doe or Acme Corp' autocomplete='off'>"
        "<span class='help'>The person or company you want to track.</span></div>"
        "<fieldset class='field'><legend class='label'>This is a</legend>"
        f"<div class='checks'>{type_radios}</div></fieldset>"
        "<div class='field'><label for='sf-query'>Search query</label>"
        f"<input id='sf-query' type='text' name='query' value='{_esc(query)}'"
        " placeholder='Same as the name'><span class='help'>What is typed into Google and Bing. "
        "Leave empty to search the name. Example: \"Jane Doe\" Vancouver.</span></div>"
        "<div class='field'><span class='label'>Countries to search</span>"
        f"<div class='checks country-checks'>{_country_checks(countries)}</div>"
        "<span class='help'>Each country is a separate search with its own results.</span></div>"
        "<div class='field'><label for='sf-aliases'>Other names (optional)</label>"
        f"<input id='sf-aliases' type='text' name='aliases' value='{_esc(', '.join(s.get('aliases') or []))}'"
        " placeholder='e.g. J. Doe, Jane A. Doe'><span class='help'>Other spellings or names, "
        "separated by commas.</span></div>"
        "<div class='field'><label for='sf-context'>Short description for the AI (optional)</label>"
        f"<textarea id='sf-context' name='context' rows='2'"
        " placeholder='e.g. Vancouver-based investor, founder of Ashworth Group'>"
        f"{_esc(s.get('context'))}</textarea>"
        "<span class='help'>One line about who this is. The AI uses it to tell this subject apart "
        "from people or companies with the same name.</span></div>"
        f"{project_field}{switch}</form>"
    )


def _subject_overlay(title: str, form: str, submit_label: str, extra_foot: str = "") -> str:
    foot = (f"{extra_foot}<button type='button' class='btn ghost' data-sm-close>Cancel</button>"
            f"<button type='submit' class='btn primary' form='subject-form'>{_esc(submit_label)}</button>")
    return ui.overlay(title, form, foot)


def _parse_subject_form(name: str, type_: str, query: str, aliases: str, countries: list,
                        context: str) -> dict:
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("a name is required.")
    type_ = (type_ or "person").strip().lower()
    if type_ not in SUBJECT_TYPES:
        raise ValueError("pick a person or a company.")
    valid = [c for c in countries if c in COUNTRIES]
    if not valid:
        raise ValueError("pick at least one country.")
    query = " ".join((query or "").split()) or name
    alias_list = [a.strip() for a in (aliases or "").split(",") if a.strip()]
    return {"name": name, "type": type_, "queries": [query], "aliases": alias_list,
            "countries": valid, "context": (context or "").strip()}


def _project_id(raw: str) -> int | None:
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        return None
    return pid if any(p["id"] == pid for p in _projects_for_viewer()) else None


# --------------------------------------------------------------------- #
# Settings card
# --------------------------------------------------------------------- #
def _domains_area() -> str:
    """The active subject's domain lists (the one home of Domains)."""
    from dashboard.routers.domains import domains_manager_html

    mine, neg = len(db.list_positive_domains()), len(db.list_negative_domains())
    return ("<details class='subj-domains' id='domains'>"
            f"<summary><b>Domains</b> <span class='muted'>{mine} mine · {neg} negative</span></summary>"
            f"<div class='subj-domains-body'>{domains_manager_html('/settings#domains')}</div></details>")


def projects_card_html() -> str:
    from dashboard import integrations as integ

    v = viewer()
    projects = _projects_for_viewer(include_archived=not v.restricted)
    visible = {s["id"] for s in allowed_subjects()}
    all_subjects = db.list_subjects(include_inactive=True)
    current_subject = db.current_subject()
    current = current_subject.get("id")
    blocks = []
    for p in projects:
        subs = [s for s in all_subjects if s["client_id"] == p["id"]
                and (s["id"] in visible or (not v.restricted and not s["active"]))]
        active_subs = [s for s in subs if s["active"]]
        archived = not p["active"]
        lines = []
        for s in subs:
            names = [country_name(c) for c in s["countries"]]
            countries = (", ".join(names[:4]) + (f" +{len(names) - 4}" if len(names) > 4 else "")
                         ) or "no countries"
            tags = ""
            is_current = s["id"] == current
            if is_current:
                tags += ("<span class='pill info'><span class='dot' aria-hidden='true'></span>"
                         "Active now</span>")
            if s.get("is_demo"):
                tags += "<span class='tag'>demo</span>"
            if not s["active"]:
                tags += "<span class='tag'>inactive</span>"
            actions = ""
            if s["active"] and not is_current and s["id"] in visible:
                actions += (f"<a class='btn ghost small' href='/subject/switch?id={s['id']}"
                            f"&amp;next=/settings%23projects'>Switch to<span class='sr-only'> "
                            f"{_esc(subject_label(s))}</span></a>")
            if v.can_write and s["active"]:
                actions += (f"<a class='btn ghost small' href='/ui/subject/{s['id']}/edit' data-modal"
                            f" data-title='Edit subject'>Edit<span class='sr-only'> {_esc(subject_label(s))}</span></a>")
            elif v.can_write and not s["active"] and not archived:
                actions += (f"<form class='inline-form' method='post' action='/subjects/{s['id']}/activate'>"
                            "<button type='submit' class='btn ghost small'>Reactivate</button></form>")
            domains = ""
            if is_current and s["active"]:
                domains = _domains_area()
            elif s["active"] and s["id"] in visible:
                actions = (f"<a class='btn ghost small' href='/subject/switch?id={s['id']}"
                           f"&amp;next=/settings%23domains'>Domains<span class='sr-only'> of "
                           f"{_esc(subject_label(s))}</span></a>") + actions
            lines.append(
                f"<div class='subj{' is-current' if is_current else ''}'>"
                f"<div class='subj-main'><div class='subj-name'><b>{_esc(subject_label(s))}</b>{tags}</div>"
                f"<span class='li-sub'>{_esc(_TYPE_LABELS.get(s['type'], s['type']))} · "
                f"{_esc(countries)}</span></div><div class='subj-actions'>{actions}</div>{domains}</div>")
        actions = ""
        if v.can_write and not archived:
            actions = (f"<a class='btn small' href='/ui/subject/new?project={p['id']}' data-modal"
                       f" data-title='Add a subject'>{nav.icon('plus')}Add subject"
                       f"<span class='sr-only'> to {_esc(p['name'])}</span></a>")
            if not v.restricted:
                actions += (f"<a class='btn ghost small' href='/ui/project/{p['id']}/edit' data-modal"
                            f" data-title='Edit project'>Rename<span class='sr-only'> project {_esc(p['name'])}</span></a>")
        elif v.can_write and archived and not v.restricted:
            actions = (f"<form class='inline-form' method='post' action='/projects/{p['id']}/restore'>"
                       "<button type='submit' class='btn ghost small'>Restore</button></form>")
        count = f"{len(active_subs)} subject{'s' if len(active_subs) != 1 else ''}"
        state = "<span class='tag'>archived</span>" if archived else ""
        body = "".join(lines) or ("<div class='empty-row'>No subjects yet. Add the person or "
                                  "company you want to track.</div>")
        blocks.append(f"<div class='proj'><div class='proj-head'><div class='proj-title'>"
                      f"<b>{_esc(p['name'])}</b><span class='tag'>{count}</span>{state}</div>"
                      f"<div class='proj-actions'>{actions}</div></div>{body}</div>")
    action = ""
    if v.can_write and not v.restricted:
        action = (f"<a class='btn primary small' href='/ui/project/new' data-modal data-title='New project'>"
                  f"{nav.icon('plus')}New project</a>")
    pill = integ.pill("info", f"Active: {subject_label(current_subject)}") if current else ""
    return integ.section_card(
        "Projects and subjects",
        "A project groups the people or companies you track. Each subject has its own searches, "
        "results, alerts and domain lists (open Domains under the active subject). Switch "
        "between them here or from the top bar.",
        f"<div class='projects'>{''.join(blocks)}</div>", pill, action)


# --------------------------------------------------------------------- #
# Fragments
# --------------------------------------------------------------------- #
@router.get("/ui/subject/new", response_class=HTMLResponse, tags=["subjects"])
async def ui_subject_new(project: str = "", next: str = _BACK):
    pid = _project_id(project)
    form = _subject_form("/subjects/add", None, pid, safe_next(next, _BACK))
    return fragment_or_page("Add a subject", _subject_overlay("Add a subject", form, "Add subject"),
                            active="/settings")


@router.get("/ui/subject/{subject_id}/edit", response_class=HTMLResponse, tags=["subjects"])
async def ui_subject_edit(subject_id: int):
    subject = db.get_subject(subject_id)
    if subject is None or not _can_touch_subject(subject_id):
        return fragment_or_page("Edit subject", ui.overlay("Edit subject",
                                "<div class='empty'>This subject isn't available.</div>"),
                                active="/settings", status_code=404)
    form = _subject_form(f"/subjects/{subject_id}/edit", subject)
    extra = ""
    if subject_id != 1:
        extra = (f"<form method='post' action='/subjects/{subject_id}/deactivate' class='inline-form'"
                 " style='margin-right:auto'><button type='submit' class='btn ghost danger-text'>"
                 "Deactivate</button></form>")
    return fragment_or_page("Edit subject",
                            _subject_overlay(f"Edit {subject_label(subject)}", form, "Save changes", extra),
                            active="/settings")


def _project_form(action: str, name: str = "") -> str:
    return (f"<form method='post' action='{_esc(action)}' class='form-grid' id='project-form' data-sm-submit>"
            "<div class='field'><label for='pf-name'>Project name</label>"
            f"<input id='pf-name' type='text' name='name' value='{_esc(name)}' required"
            " placeholder='e.g. Client: Acme' autocomplete='off'></div></form>")


@router.get("/ui/project/new", response_class=HTMLResponse, tags=["subjects"])
async def ui_project_new():
    foot = ("<button type='button' class='btn ghost' data-sm-close>Cancel</button>"
            "<button type='submit' class='btn primary' form='project-form'>Create project</button>")
    return fragment_or_page("New project", ui.overlay("New project", _project_form("/projects/add"), foot),
                            active="/settings")


@router.get("/ui/project/{project_id}/edit", response_class=HTMLResponse, tags=["subjects"])
async def ui_project_edit(project_id: int):
    project = db.get_client(project_id)
    if project is None or viewer().restricted:
        return fragment_or_page("Edit project", ui.overlay("Edit project",
                                "<div class='empty'>This project isn't available.</div>"),
                                active="/settings", status_code=404)
    archive = ""
    if project_id != 1:
        archive = (f"<form method='post' action='/projects/{project_id}/archive' class='inline-form'"
                   " style='margin-right:auto'><button type='submit' class='btn ghost danger-text'>"
                   "Archive project</button></form>")
    foot = (f"{archive}<button type='button' class='btn ghost' data-sm-close>Cancel</button>"
            "<button type='submit' class='btn primary' form='project-form'>Save</button>")
    note = ("<p class='note'>Archiving hides the project and deactivates its subjects. Their data "
            "is kept and can be restored.</p>" if project_id != 1 else
            "<p class='note'>This is the default project; it can be renamed but not archived.</p>")
    body = _project_form(f"/projects/{project_id}/rename", project["name"]) + note
    return fragment_or_page("Edit project", ui.overlay(f"Edit {project['name']}", body, foot),
                            active="/settings")


# --------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------- #
@router.post("/projects/add", tags=["subjects"])
async def project_add(request: Request, name: str = Form("")):
    if viewer().restricted:
        return redirect_with_flash(_BACK, "You can't create projects.", "error")
    name = " ".join(name.split())
    if not name:
        return _form_error("New project", _project_form("/projects/add"), "a name is required.", _BACK,
                           "Create project", "project-form")
    pid = db.add_client(name)
    audit(request, "project_add", resource_type="project", resource_id=pid, name=name)
    return redirect_with_flash(_BACK, f"Project {name} created. Add a subject to it next.")


@router.post("/projects/{project_id}/rename", tags=["subjects"])
async def project_rename(request: Request, project_id: int, name: str = Form("")):
    project = db.get_client(project_id)
    if project is None or viewer().restricted:
        return redirect_with_flash(_BACK, "That project isn't available.", "error")
    name = " ".join(name.split())
    if not name:
        return _form_error("Edit project", _project_form(f"/projects/{project_id}/rename"),
                           "a name is required.", _BACK, "Save", "project-form")
    if name == project["name"]:
        return redirect_with_flash(_BACK, "No changes to the project.", "info")
    db.rename_client(project_id, name)
    audit(request, "project_rename", resource_type="project", resource_id=project_id, name=name)
    return redirect_with_flash(_BACK, f"Project renamed to {name}.")


@router.post("/projects/{project_id}/archive", tags=["subjects"])
async def project_archive(request: Request, project_id: int):
    project = db.get_client(project_id)
    if project is None or viewer().restricted:
        return redirect_with_flash(_BACK, "That project isn't available.", "error")
    try:
        db.set_client_active(project_id, False)
    except ValueError as exc:
        return redirect_with_flash(_BACK, f"Not archived: {exc}.", "error")
    audit(request, "project_archive", resource_type="project", resource_id=project_id)
    return redirect_with_flash(_BACK, f"Project {project['name']} archived.")


@router.post("/projects/{project_id}/restore", tags=["subjects"])
async def project_restore(request: Request, project_id: int):
    project = db.get_client(project_id)
    if project is None or viewer().restricted:
        return redirect_with_flash(_BACK, "That project isn't available.", "error")
    db.set_client_active(project_id, True)
    audit(request, "project_restore", resource_type="project", resource_id=project_id)
    return redirect_with_flash(_BACK, f"Project {project['name']} restored. Reactivate its subjects "
                                      "to track them again.")


@router.post("/subjects/add", tags=["subjects"])
async def subject_add(request: Request, name: str = Form(""), type: str = Form("person"),
                      query: str = Form(""), aliases: str = Form(""),
                      countries: list[str] = Form(default=[]), context: str = Form(""),
                      project_id: str = Form(""), switch: str = Form(""), next: str = Form(_BACK)):
    back = safe_next(next, _BACK)
    form_again = _subject_form("/subjects/add", {"name": name, "type": type, "queries": [query],
                                                 "aliases": aliases.split(","), "countries": countries,
                                                 "context": context}, _project_id(project_id), back)
    pid = _project_id(project_id)
    if pid is None:
        return _form_error("New subject", form_again, "pick a project.", back, "Create subject")
    try:
        fields = _parse_subject_form(name, type, query, aliases, countries, context)
        sid = db.add_subject(fields["name"], aliases=fields["aliases"], type=fields["type"],
                             countries=fields["countries"], context=fields["context"],
                             client_id=pid, queries=fields["queries"])
    except ValueError as exc:
        msg = str(exc)
        if "already answers" in msg:
            msg = "another active subject already uses that name."
        return _form_error("New subject", form_again, msg, back, "Create subject")
    audit(request, "subject_add", resource_type="subject", resource_id=sid, name=fields["name"],
          project_id=pid)
    if switch:
        resp = redirect_with_flash("/", f"Created {fields['name']}. Run a first search to see results.")
        resp.set_cookie(SUBJECT_COOKIE, str(sid), max_age=365 * 24 * 3600, httponly=True,
                        samesite="lax", path="/")
        return resp
    # The New search dialog's "New subject…" comes back to itself with the new
    # subject preselected (next carries the placeholder).
    return redirect_with_flash(back.replace("__new_subject__", str(sid)), f"Created {fields['name']}.")


@router.post("/subjects/{subject_id}/edit", tags=["subjects"])
async def subject_edit(request: Request, subject_id: int, name: str = Form(""),
                       type: str = Form("person"), query: str = Form(""), aliases: str = Form(""),
                       countries: list[str] = Form(default=[]), context: str = Form(""),
                       project_id: str = Form(""), next: str = Form(_BACK)):
    back = safe_next(next, _BACK)
    subject = db.get_subject(subject_id)
    if subject is None or not _can_touch_subject(subject_id):
        return redirect_with_flash(back, "That subject isn't available.", "error")
    form_again = _subject_form(f"/subjects/{subject_id}/edit",
                               {**subject, "name": name, "type": type, "queries": [query],
                                "aliases": aliases.split(","), "countries": countries,
                                "context": context})
    try:
        fields = _parse_subject_form(name, type, query, aliases, countries, context)
    except ValueError as exc:
        return _form_error("Edit subject", form_again, str(exc), back, "Save changes")
    other = db.find_subject_by_name(fields["name"], include_inactive=False)
    if other is not None and other["id"] != subject_id:
        return _form_error("Edit subject", form_again, "another active subject already uses that name.",
                           back, "Save changes")
    pid = _project_id(project_id) or subject["client_id"]
    unchanged = (fields["name"] == subject["name"] and fields["type"] == subject["type"]
                 and fields["queries"] == (subject["queries"] or [subject["name"]])
                 and fields["aliases"] == subject["aliases"]
                 and fields["countries"] == subject["countries"]
                 and fields["context"] == subject["context"] and pid == subject["client_id"])
    if unchanged:
        return redirect_with_flash(back, "No changes to save.", "info")
    db.update_subject(subject_id, name=fields["name"], type=fields["type"], aliases=fields["aliases"],
                      countries=fields["countries"], context=fields["context"],
                      queries=fields["queries"])
    if pid != subject["client_id"]:
        db.set_subject_project(subject_id, pid)
    audit(request, "subject_edit", resource_type="subject", resource_id=subject_id,
          name=fields["name"], project_id=pid)
    return redirect_with_flash(back, f"Saved {fields['name']}.")


@router.post("/subjects/{subject_id}/deactivate", tags=["subjects"])
async def subject_deactivate(request: Request, subject_id: int):
    subject = db.get_subject(subject_id)
    if subject is None or not _can_touch_subject(subject_id):
        return redirect_with_flash(_BACK, "That subject isn't available.", "error")
    try:
        db.deactivate_subject(subject_id)
    except ValueError:
        return redirect_with_flash(_BACK, "The default subject can't be deactivated.", "error")
    audit(request, "subject_deactivate", resource_type="subject", resource_id=subject_id)
    return redirect_with_flash(_BACK, f"{subject_label(subject)} deactivated. Its data is kept.")


@router.post("/subjects/{subject_id}/activate", tags=["subjects"])
async def subject_activate(request: Request, subject_id: int):
    subject = db.get_subject(subject_id)
    if subject is None or viewer().restricted:
        return redirect_with_flash(_BACK, "That subject isn't available.", "error")
    project = db.get_client(subject["client_id"])
    if project is not None and not project["active"]:
        return redirect_with_flash(_BACK, "Restore its project first.", "error")
    clash = db.find_subject_by_name(subject["name"], include_inactive=False)
    if clash is not None and clash["id"] != subject_id:
        return redirect_with_flash(_BACK, "Another active subject already uses that name.", "error")
    db.update_subject(subject_id, active=True)
    audit(request, "subject_activate", resource_type="subject", resource_id=subject_id)
    return redirect_with_flash(_BACK, f"{subject_label(subject)} reactivated.")
