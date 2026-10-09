"""Users: the Settings → Users card and its three POSTs (create,
activate/deactivate, change password) -- C21a.

Admin role only (see `_require_admin`): whoever can create accounts can
grant themselves access to everything, so this card's gate is the same
privilege boundary as the entire system's. A non-admin posting here gets the
SAME redirect to "/" as someone with no session at all -- not an explanatory
403: confirming "this exists but isn't for you" only helps someone probing.

_require_admin() treats "auth disabled" (a fresh, unconfigured install with
no credentials at all) as implicitly admin, otherwise the one place that
creates the first real user would be unreachable before a user exists.
Creating that FIRST account also signs its creator in as it: without that,
the very next request would bounce to /login (auth just became enabled).
The first account must therefore be an admin.

GET /users is kept for old links and redirects to /settings#users.

The password NEVER travels back nor gets logged: `Database.list_users`
returns rows without `password_hash`, and the audit trail (C24) logs whose
password was changed, never the value it was changed to.
"""
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from dashboard.context import redirect_with_flash, viewer
from dashboard.deps import db
from dashboard.main import (
    audit,
    auth_enabled,
    create_session,
    is_admin,
    revoke_sessions,
    session_user,
)
from dashboard.ui_helpers import _esc
from database.models import USER_ROLES

router = APIRouter()

# How each stored role value is shown in the UI. The DB keeps the original
# values (client_viewer stays client_viewer): only the label changes.
ROLE_LABELS = {"admin": "Admin", "operator": "Operator", "client_viewer": "Viewer (read-only)"}
_BACK = "/settings#users"


def _require_admin(request: Request) -> bool:
    return is_admin(request)


def _deny():
    return RedirectResponse("/", status_code=303)


_ROLE_HELP = {
    "admin": "Manages users, integrations and every setting.",
    "operator": "Uses the dashboard day to day: searches, reviews, notes, alerts.",
    "client_viewer": "Can look but not change anything; only the default project's subjects.",
}


def users_card_html() -> str:
    from dashboard import integrations as integ
    from dashboard import nav
    from dashboard.ui_helpers import fmt_utc

    me = viewer().username
    projects = {c["id"]: c["name"] for c in db.list_clients()}
    users = db.list_users()
    rows = []
    for u in users:
        you = " <span class='muted'>(you)</span>" if u["username"] == me else ""
        project = "All projects"
        if u.get("client_id") and u["role"] != "admin":
            project = projects.get(u["client_id"], "project")
        state = ("<span class='pill positive'><span class='dot' aria-hidden='true'></span>Active</span>"
                 if u["active"] else
                 "<span class='pill neutral'><span class='dot' aria-hidden='true'></span>Inactive</span>")
        name = _esc(u["username"])
        if u["active"]:
            toggle_btn = (f"<button type='submit' class='btn ghost small danger-text' "
                          f"data-sm-confirm='{name} will be signed out and can no longer sign in. "
                          "The account stays in the audit log; you can activate it again later.' "
                          f"data-confirm-title='Deactivate {name}?' data-confirm-label='Deactivate'>"
                          f"Deactivate<span class='sr-only'> {name}</span></button>")
        else:
            toggle_btn = (f"<button type='submit' class='btn ghost small'>Activate"
                          f"<span class='sr-only'> {name}</span></button>")
        toggle = ("<form class='inline-form' action='/users/toggle' method='post'>"
                  f"<input type='hidden' name='user_id' value='{u['id']}'>"
                  f"<input type='hidden' name='active' value='{0 if u['active'] else 1}'>"
                  f"{toggle_btn}</form>")
        pw = (f"<a class='btn ghost small' href='/ui/settings/user/{u['id']}/password' data-modal "
              f"data-title='Set a new password'>Set password<span class='sr-only'> for {name}</span></a>")
        last = _esc(fmt_utc(u.get("last_login_at"))) if u.get("last_login_at") else "never"
        rows.append(
            f"<tr><td><b>{name}</b>{you}</td>"
            f"<td>{_esc(ROLE_LABELS.get(u['role'], u['role']))}</td><td>{_esc(project)}</td>"
            f"<td>{state}</td><td class='nowrap'>{last}</td>"
            f"<td class='rt'><span class='row-actions'>{pw}{toggle}</span></td></tr>")
    first = not auth_enabled()
    if users:
        table = ("<div class='table-wrap'><table class='stable'><tr><th>User</th><th>Role</th>"
                 "<th>Project</th><th>Status</th><th>Last sign-in</th><th></th></tr>"
                 f"{''.join(rows)}</table></div>")
    else:
        table = ("<div class='empty-row'><b>No accounts yet.</b> Sign-in is off: anyone who can "
                 "open this page can change everything. Add an admin account to turn sign-in on."
                 "</div>")
    first_note = ("<p class='note'>The first account must be an admin; creating it turns on "
                  "sign-in and signs you in as that account.</p>" if first and users else "")
    pill = (integ.pill("ok", f"{sum(1 for u in users if u['active'])} active") if not first
            else integ.pill("warn", "Sign-in is off"))
    action = (f"<a class='btn primary small' href='/ui/settings/user/new' data-modal "
              f"data-title='Add a user'>{nav.icon('plus')}Add user</a>")
    roles = "".join(f"<li><b>{_esc(ROLE_LABELS.get(r, r))}</b>: {_esc(_ROLE_HELP.get(r, ''))}</li>"
                    for r in USER_ROLES)
    more = (f"<ul class='note role-list'>{roles}</ul>"
            "<p class='note'>Deactivating keeps the account in the audit log; there is no delete "
            "on purpose. Changing <code>AUTH_PASS</code> in <code>.env</code> doesn't change the "
            "password of the account it created: the <code>.env</code> pair stays valid as a "
            "rescue sign-in.</p>")
    return integ.section_card("Users", "People who can sign in, and what each one may do.",
                              first_note + table, pill, action, more, "About accounts")


def _role_cards(selected: str = "operator") -> str:
    from dashboard import integrations as integ
    first = not auth_enabled()
    options = [(r, ROLE_LABELS.get(r, r), _ROLE_HELP.get(r, "")) for r in USER_ROLES
               if not first or r == "admin"]
    return integ._radio_cards("role", options, "admin" if first else selected)


def _user_form(error: str = "", username: str = "", role: str = "operator") -> str:
    from dashboard import integrations as integ
    err = f"<div class='flash flash-error' role='alert'>{_esc(error)}</div>" if error else ""
    first = ("<p class='note'>This is the first account, so it must be an admin. Creating it "
             "turns on sign-in and signs you in as this user.</p>" if not auth_enabled() else "")
    return (err + first + "<form id='user-form' method='post' action='/users/add' class='form-grid' "
            "data-sm-submit>"
            + integ.field("Username", integ.text_input("nu-name", "username", username, "e.g. maria",
                                                       extra="required"),
                          "What they type to sign in.", "nu-name")
            + integ.field("Password", "<input id='nu-pass' type='password' name='password' required "
                          "autocomplete='new-password'>",
                          "At least 8 characters is a good idea. Share it with them privately.",
                          "nu-pass")
            + "<fieldset class='field'><legend class='label'>Role</legend>"
            + _role_cards(role) + "</fieldset></form>")


@router.get("/ui/settings/user/new", response_class=HTMLResponse)
async def ui_user_new(request: Request):
    if not _require_admin(request):
        return HTMLResponse("", status_code=403)
    from dashboard import ui
    from dashboard.ui_helpers import fragment_or_page
    foot = ("<button type='button' class='btn ghost' data-sm-close>Cancel</button>"
            "<button type='submit' class='btn primary' form='user-form' "
            "data-busy-label='Creating…'>Create user</button>")
    return fragment_or_page("Add a user", ui.overlay("Add a user", _user_form(), foot),
                            active="/settings")


@router.get("/ui/settings/user/{user_id}/password", response_class=HTMLResponse)
async def ui_user_password(request: Request, user_id: int):
    if not _require_admin(request):
        return HTMLResponse("", status_code=403)
    from dashboard import integrations as integ
    from dashboard import ui
    from dashboard.ui_helpers import fragment_or_page
    target = db.get_user(user_id)
    if target is None:
        return fragment_or_page("Set a new password", ui.overlay(
            "Set a new password", "<div class='empty'>This user doesn't exist.</div>"),
            active="/settings", status_code=404)
    body = (f"<p class='note'>{_esc(target['username'])} will be signed out everywhere and must "
            "use the new password next time.</p>"
            "<form id='pw-form' method='post' action='/users/password' class='form-grid' data-sm-submit>"
            f"<input type='hidden' name='user_id' value='{target['id']}'>"
            + integ.field("New password", "<input id='pw-new' type='password' name='password' "
                          "required autocomplete='new-password'>",
                          "Share it with them privately.", "pw-new") + "</form>")
    foot = ("<button type='button' class='btn ghost' data-sm-close>Cancel</button>"
            "<button type='submit' class='btn primary' form='pw-form' "
            "data-busy-label='Saving…'>Set password</button>")
    return fragment_or_page("Set a new password",
                            ui.overlay(f"New password for {target['username']}", body, foot),
                            active="/settings")


@router.get("/users")
async def users_page(request: Request):
    if not _require_admin(request):
        return _deny()
    return RedirectResponse(_BACK, status_code=303)


@router.post("/users/add")
async def users_add(request: Request, username: str = Form(""), password: str = Form(""),
                    role: str = Form("operator")):
    """Defensive params: everything arrives as str and an invalid value
    redirects back with a readable reason, without writing anything."""
    if not _require_admin(request):
        return _deny()
    username = (username or "").strip()
    role = (role or "").strip()
    if not username or not password:
        return redirect_with_flash(_BACK, "Not created: username and password are required.", "error")
    if role not in USER_ROLES:
        return redirect_with_flash(_BACK, f"Not created: unknown role {role}.", "error")
    first_account = not auth_enabled()
    if first_account and role != "admin":
        return redirect_with_flash(_BACK, "Not created: the first account must be an admin, "
                                          "otherwise nobody could manage users.", "error")
    # There is no client picker: admins and operators are staff (no
    # client), and a viewer (role value `client_viewer`) is tied to the
    # install's default client -- the first row of `clients`.
    cid = None
    if role == "client_viewer":
        clients = db.list_clients()
        if not clients:
            return redirect_with_flash(_BACK, "Not created: no project exists to attach a viewer to.",
                                       "error")
        cid = clients[0]["id"]
    try:
        # PBKDF2 is deliberately slow: hash off the event loop.
        user_id = await run_in_threadpool(db.add_user, username, password, role=role, client_id=cid)
    except Exception:  # noqa: BLE001 -- username UNIQUE, and any other DB rejection
        return redirect_with_flash(_BACK, f"Not created: the username '{username}' may already be "
                                          "taken.", "error")
    audit(request, "user_add", resource_type="user", resource_id=user_id,
          username=username, role=role, client_id=cid)
    if first_account:
        # Auth just turned on: sign the creator in as the new admin.
        token = create_session(username)
        audit(request, "login_ok", actor_type="user", actor_id=username)
        resp = redirect_with_flash(_BACK, f"Created {username}. Sign-in is now required, and "
                                          "you're signed in as this account.")
        resp.set_cookie("sid", token, httponly=True, samesite="lax", max_age=86400)
        return resp
    return redirect_with_flash(_BACK, f"Created {username}.")


@router.post("/users/toggle")
async def users_toggle(request: Request, user_id: str = Form(""), active: str = Form("")):
    """Deactivate instead of delete: audit_log references users by
    name, and deleting the row would leave that trail pointing at no one.
    An admin CANNOT deactivate themselves."""
    if not _require_admin(request):
        return _deny()
    try:
        uid = int(user_id)
    except ValueError:
        return redirect_with_flash(_BACK, "Invalid user.", "error")
    target = db.get_user(uid)
    if target is None:
        return redirect_with_flash(_BACK, "Unknown user.", "error")
    make_active = (active or "").strip() == "1"
    if not make_active and target["username"] == session_user(request):
        return redirect_with_flash(_BACK, "You cannot deactivate your own account.", "error")
    if bool(target["active"]) == make_active:
        return redirect_with_flash(_BACK, "No change.", "info")
    db.set_user_active(uid, make_active)
    # Deactivating also closes what was already open.
    revoked = revoke_sessions(target["username"]) if not make_active else 0
    audit(request, "user_set_active", resource_type="user", resource_id=uid,
          username=target["username"], active=make_active, sessions_revoked=revoked)
    verb = "activated" if make_active else "deactivated"
    return redirect_with_flash(_BACK, f"{target['username']} {verb}.")


@router.post("/users/password")
async def users_password(request: Request, user_id: str = Form(""), password: str = Form("")):
    if not _require_admin(request):
        return _deny()
    try:
        uid = int(user_id)
    except ValueError:
        return redirect_with_flash(_BACK, "Invalid user.", "error")
    if not password:
        return redirect_with_flash(_BACK, "Password cannot be empty.", "error")
    target = db.get_user(uid)
    if target is None:
        return redirect_with_flash(_BACK, "Unknown user.", "error")
    await run_in_threadpool(db.set_user_password, uid, password)
    # Changing the password also closes any open sessions.
    revoked = revoke_sessions(target["username"])
    # WHO changed WHOSE password is audited -- never the value.
    audit(request, "user_set_password", resource_type="user", resource_id=uid,
          username=target["username"], sessions_revoked=revoked)
    return redirect_with_flash(_BACK, f"Password changed for {target['username']}.")
