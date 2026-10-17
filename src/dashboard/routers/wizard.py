"""First-run setup wizard: /setup.

Reviewer-facing problem this solves: a fresh clone of this repo drops a
visitor straight into a dashboard full of synthetic data, with no
explanation of what's real, what's demo, or how to configure anything --
and a real search fails silently without an Apify key. First run must
explain itself instead of assuming the visitor already knows the product.

Design: NO separate progress-tracking state machine. Each step's "is this
done?" check reads the REAL, already-persisted configuration (is
APIFY_API_KEY set? does an admin user exist? is a query configured?) --
so the wizard is naturally resumable (reopening /setup jumps straight to
whatever's still unconfigured) and naturally stays in sync with Settings
(which writes the exact same .env keys / settings rows through the exact
same admin.set_env/db.set_setting calls used here -- the wizard is a
guided front door to that config, not a separate copy of it). Every step
that isn't the required Apify key can be explicitly skipped, recorded in
its own `wizard_skipped_<step>` setting so a skip sticks instead of
re-prompting forever. The admin-account step is the exception: sign-in is
required (SECURITY.md, item 6), so it can't be skipped -- on a normal
install it is already done before the wizard opens (/setup/admin creates
the first admin), and it only shows up under the ALLOW_NO_AUTH demo opt-out.
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from apify.client import ApifyClient, ApifyError
from auth.passwords import password_problem
from dashboard import admin
from dashboard import integrations as integ
from dashboard.context import SUBJECT_COOKIE
from dashboard.deps import db
from dashboard.main import (
    COUNTRIES,
    _env_file,
    _esc,
    audit,
    is_admin,
    page,
)
from dashboard.session import credentials_exist, start_session
from dashboard.ui_helpers import country_name
from database.models import DEFAULT_CLIENT_ID
from notify import ROUTING_NOTE, channel_status
from processing import ai_classifier

router = APIRouter()

_STEPS = ("apify", "integrations", "auth", "first_search")


def wizard_completed() -> bool:
    """Whether first_run_middleware should stop forcing every request to
    /setup. True once the wizard itself finished (every step done or
    skipped) -- OR, just as importantly, if the database already has at
    least one real run in it. Without that second check, ANY
    already-populated instance that never happened to click through the
    wizard (scripts/seed_demo_data.py, an existing production DB from
    before this wizard existed, or simply an operator who configured
    everything by hand in Settings instead) would get force-redirected to
    /setup on every single page forever -- a "first run" flow has no
    business firing on an install that plainly isn't a first run."""
    if db.get_setting("wizard_completed", "") == "1":
        return True
    # Any subject's run counts (a freshly added subject has none of its own).
    return bool(db.for_all_subjects().list_runs(limit=1))


def _setup_write_allowed(request: Request) -> bool:
    """Once real auth exists (any admin account, or the .env AUTH_USER/
    AUTH_PASS pair), every /setup/* POST requires being logged in as that
    admin -- not just the step that happens to still be pending. Without
    this, an unauthenticated visitor who finds /setup/integrations or
    /setup/first-search while the operator hasn't gotten to them yet could
    still plant their own Gemini/SMTP/Telegram credentials or overwrite
    the tracked query, even on an instance where auth is otherwise
    already configured. Before any auth exists at all, /setup/* is
    necessarily open to whoever reaches it first -- that's the one
    legitimate unauthenticated path (see /setup/auth's own guard for the
    account-creation-specific risk)."""
    return is_admin(request)


def _integrations_configured() -> dict[str, bool]:
    """Same "configured" checks as Settings, onboarding and the senders."""
    return {"ai": ai_classifier.is_configured(), **channel_status(db)}


def _step_configured(step: str) -> bool:
    """The step's config really exists (as opposed to being skipped)."""
    if step == "apify":
        # Step "apify" (name kept for stored wizard_skipped_* rows): any SERP
        # provider key, Apify or Serper.dev (serp.registry).
        from serp.registry import active_provider_name
        return bool(active_provider_name())
    if step == "integrations":
        return (db.get_setting("wizard_saved_integrations", "") == "1"
                or any(_integrations_configured().values()))
    if step == "auth":
        return credentials_exist()
    if step == "first_search":
        return bool(db.get_tracked_query())
    return True


def _step_skipped(step: str) -> bool:
    if step == "auth":   # sign-in can't be skipped (an old skip no longer counts)
        return False
    return db.get_setting(f"wizard_skipped_{step}", "") == "1"


def _step_done(step: str) -> bool:
    return _step_configured(step) or _step_skipped(step)


def _demo_data_exists() -> bool:
    """A demo subject with runs is loaded (scripts/seed_demo_data.py)."""
    try:
        return any(s.get("is_demo") and db.for_subject(s["id"]).list_runs(limit=1)
                   for s in db.list_subjects())
    except Exception:  # noqa: BLE001
        return False


def current_step() -> str | None:
    """First not-yet-done step, or None if the wizard is complete."""
    for step in _STEPS:
        if not _step_done(step):
            return step
    return None


def _mark_complete_if_done() -> None:
    if current_step() is None:
        db.set_setting("wizard_completed", "1")


def _progress_html(step: str | None) -> str:
    steps = [
        ("apify", "1. Search"),
        ("integrations", "2. Integrations"),
        ("auth", "3. Admin account"),
        ("first_search", "4. First subject"),
    ]
    items = []
    for idx, (s_key, s_label) in enumerate(steps):
        # Only steps whose config really exists get the check mark; a
        # skipped step says so instead of pretending to be done.
        if s_key == step:
            cls, dot, note = "active", str(idx + 1), ""
        elif _step_configured(s_key):
            cls, dot, note = "done", "✓", ""
        elif _step_skipped(s_key):
            cls, dot, note = "skipped", str(idx + 1), " <span class='muted'>(skipped)</span>"
        else:
            cls, dot, note = "", str(idx + 1), ""
        current = " aria-current='step'" if s_key == step else ""
        items.append(f"<div class='wizard-step-item {cls}'{current}><span class='wizard-step-dot'>{dot}</span>"
                     f" {s_label}{note}</div>")
    return f"<div class='wizard-steps'>{''.join(items)}</div>"


def _wrap(inner: str, title: str, step: str | None = None) -> HTMLResponse:
    progress = _progress_html(step)
    guide_notice = (
        "<div style='margin-bottom:18px;font-size:13px;color:var(--muted);background:var(--accent-soft);"
        "padding:9px 14px;border-radius:6px;border-left:3px solid var(--accent);display:flex;justify-content:space-between;align-items:center;'>"
        "<span>Need an overview first?</span>"
        "<span><a href='/onboarding' style='font-weight:600;'>Getting started guide →</a></span></div>"
    )
    body = f"<div class='card' style='max-width:640px'>{progress}{guide_notice}{inner}</div>"
    return HTMLResponse(page(title, body, active="/setup"))


def _pill(ok: bool, yes: str = "Configured", no: str = "Not configured") -> str:
    return f"<span class='pill {'ok' if ok else 'neutral'}'>{yes if ok else no}</span>"


_INTEGRATIONS_NEXT = "/setup?step=integrations"


def _summary_html() -> str:
    """Final page. Says plainly what is still missing -- skipping the search
    provider step must not end on "all set" while the instance is still in
    demo mode. Same integration rows (and dialogs) as Settings."""
    apify_card = ""
    if not _step_configured("apify"):
        until = ("Until then the dashboard shows the demo data only." if _demo_data_exists()
                 else "Until then, searches can't run.")
        apify_card = (
            "<div class='hint' id='apify-missing'>"
            f"<b>Search provider key not set (Apify or Serper.dev). Add one to run real "
            f"searches.</b> {until}</div>"
            + _apify_form("/setup")
        )
    rows = "".join(integ.integration_row(n, "/setup", show_purpose=False)
                   for n in ("ai", *integ.CHANNELS))
    title = ("Setup complete" if _step_configured("apify")
             else "Setup complete (no search provider key yet)")
    return (
        f"<h2>{title}</h2>{apify_card}"
        f"<p class='muted'>Optional integrations. {ROUTING_NOTE}</p>"
        f"<div class='integ-list'>{rows}</div>"
        "<p style='margin-top:16px'><a class='btn primary' href='/'>Go to the dashboard</a></p>"
    )


def _apify_form(next_url: str, skip_label: str = "") -> str:
    """Step 1: the SAME provider rows and guided dialogs as Settings → Search
    (/ui/integration/apify and /ui/integration/serper, saved by
    /settings/apikey and /settings/serper-key with next=/setup) -- no second
    copy of the forms. Only the wizard's Skip lives here. POST /setup/apify
    still accepts an Apify key from older bookmarks/scripts."""
    skip = ("<form method='post' action='/setup/apify?skip=1' class='actions-row'>"
            f"<button type='submit' class='btn ghost'>{_esc(skip_label)}</button></form>"
            if skip_label else "")
    rows = "".join(integ.integration_row(n, next_url) for n in integ.SERP_PROVIDERS)
    return (f"<div class='integ-list'>{rows}</div>"
            "<p class='muted'>Either provider is enough. Set up opens a short guided dialog: get "
            f"the key, paste it, test it and save. The wizard moves on once one is saved.</p>{skip}")


@router.get("/setup", response_class=HTMLResponse)
async def setup(request: Request, error: str = "", notice: str = ""):
    if not _setup_write_allowed(request):
        return RedirectResponse("/login", status_code=303)
    step = request.query_params.get("step") or current_step()
    err_html = f"<p class='hint' style='color:var(--neg-text)'>{_esc(error)}</p>" if error else ""
    if notice:
        err_html = f"<p class='hint'>{_esc(notice)}</p>" + err_html

    if step is None:
        return _wrap(err_html + _summary_html(), "Setup", step=None)

    if step == "apify":
        demo = _demo_data_exists()
        without = ("Without one, you'll only see the synthetic demo data that is already loaded."
                   if demo else "Without one, searches can't run yet.")
        skip_label = "Skip, use demo data only" if demo else "Skip for now"
        return _wrap(
            "<h2>1. Welcome to SERP Monitor</h2>"
            "<p class='muted'>This tool tracks how a name or business ranks in Google and Bing "
            "search results over time, and alerts you when something negative appears. "
            "It needs a SERP provider account (Apify, or Serper.dev for Google only) to run "
            f"real searches. {without}</p>"
            f"{err_html}{_apify_form('/setup', skip_label)}",
            "Setup · Search provider",
            step=step,
        )

    if step == "integrations":
        rows = "".join(integ.integration_row(n, _INTEGRATIONS_NEXT) for n in ("ai", *integ.CHANNELS))
        return _wrap(
            "<h2>2. Optional integrations</h2>"
            "<p class='muted'>All optional. Set up the ones you want now (each opens a short guided "
            "dialog), or later in <a href='/settings#ai'>Settings → AI provider</a> and "
            "<a href='/settings#notifications'>Settings → Notifications</a>: it's the same "
            "dialog.</p>"
            f"{err_html}<div class='integ-list'>{rows}</div>"
            f"<p class='muted'>{ROUTING_NOTE}</p>"
            "<form method='post' action='/setup/integrations' class='actions-row'>"
            "<button type='submit' class='btn primary'>Continue</button>"
            "<button type='submit' formaction='/setup/integrations?skip=1' formnovalidate "
            "class='btn ghost'>Skip for now</button>"
            "</form>",
            "Setup · Integrations",
            step=step,
        )

    if step == "auth":
        return _wrap(
            "<h2>3. Create the admin account</h2>"
            "<p class='muted'>Sign-in is required. This first account manages users, settings "
            "and API keys; you can add operators and read-only viewers later in "
            "<a href='/settings#users'>Settings → Users</a>.</p>"
            f"{err_html}"
            "<form method='post' action='/setup/auth'>"
            "<div class='form-grid wizard-form'>"
            + integ.field("Username", integ.text_input("wz-user", "username", "", "e.g. admin",
                                                       extra="required maxlength='150'"), "", "wz-user")
            + integ.field("Password", "<input id='wz-pass' type='password' name='password' "
                          "required minlength='12' maxlength='1024' autocomplete='new-password'>",
                          "At least 12 characters. A passphrase of a few words works well.",
                          "wz-pass")
            + "<div class='actions-row'><button type='submit' class='btn primary'>Create admin "
              "account and continue</button></div></div>"
            "</form>",
            "Setup · Admin account",
            step=step,
        )

    # first_search
    country_opts = "".join(f"<option value='{c}'{' selected' if c == 'us' else ''}>"
                           f"{_esc(country_name(c))}</option>" for c in COUNTRIES)
    return _wrap(
        "<h2>4. Track your first name or business</h2>"
        "<p class='muted'>What do you want to monitor? This becomes your first subject, "
        "searched by the weekly run. You can rename it, add countries or add more subjects "
        "later in <a href='/settings#projects'>Settings → Projects and subjects</a>.</p>"
        f"{err_html}"
        "<form method='post' action='/setup/first-search'>"
        "<div class='form-grid wizard-form'>"
        + integ.field("Name to track", integ.text_input("wz-query", "query", "",
                                                        "e.g. Jane Doe, or Acme Corp",
                                                        extra="required"),
                      "The person or company you want to monitor.", "wz-query")
        + integ.field("Country", f"<select id='wz-country' name='country'>{country_opts}</select>",
                      "Where to search first. You can add more countries later.", "wz-country")
        + "<div class='actions-row'><button type='submit' class='btn primary'>Save and go run "
          "your first search</button>"
        "<button type='submit' formaction='/setup/first-search?skip=1' formnovalidate "
        "class='btn ghost'>Skip for now</button></div></div>"
        "</form>",
        "Setup · First search",
        step=step,
    )


@router.post("/setup/apify", response_class=HTMLResponse)
async def setup_apify(request: Request, apikey: str = Form("", max_length=1024)):
    if not _setup_write_allowed(request):
        return RedirectResponse("/login", status_code=303)
    apikey = apikey.strip()
    # A submitted key is always verified and saved -- also after the step
    # was skipped ("use demo data") or done, e.g. from the final summary.
    # Skip only applies when nothing was submitted.
    if not apikey:
        if request.query_params.get("skip") and not _step_done("apify"):
            db.set_setting("wizard_skipped_apify", "1")
            audit(request, "wizard_skip", step="apify")
            _mark_complete_if_done()
            return RedirectResponse("/setup", status_code=303)
        if _step_done("apify"):
            return RedirectResponse("/setup", status_code=303)
        return await setup(request, error="Paste your Apify API key, or choose Skip to set it "
                                          "up later.")
    try:
        admin.check_env_values([("APIFY_API_KEY", apikey)])
    except ValueError as exc:
        return await setup(request, error=str(exc))

    try:
        result = await run_in_threadpool(ApifyClient(api_key=apikey).check_account)
    except ApifyError as exc:
        result = {"ok": False, "detail": str(exc)}

    # Saved whether or not the check passes -- same as Settings → General
    # (Apify can be briefly unreachable); a failed check is reported, in
    # plain words, and the wizard moves on.
    admin.set_env(_env_file(), "APIFY_API_KEY", apikey)
    # Secret: audited as "which field changed", never the value (same rule
    # as /settings/apikey).
    audit(request, "wizard_apify_save", field="APIFY_API_KEY", verified=bool(result["ok"]))
    _mark_complete_if_done()
    if not result["ok"]:
        reason = admin.apify_check_reason(result.get("detail") or "")
        return await setup(request, notice=f"Saved, but {reason}. "
                                           "Check it in Settings → Search.")
    return RedirectResponse("/setup", status_code=303)


@router.post("/setup/integrations", response_class=HTMLResponse)
async def setup_integrations(request: Request):
    """Continue / Skip on the integrations step. Each integration is saved by
    its own dialog (the Settings endpoints), so this only records the step:
    done when something is set up, skipped otherwise."""
    if not _setup_write_allowed(request):
        return RedirectResponse("/login", status_code=303)
    configured = [k for k, v in _integrations_configured().items() if v]
    if configured:
        if db.get_setting("wizard_saved_integrations", "") != "1":
            db.set_setting("wizard_saved_integrations", "1")
            audit(request, "wizard_integrations_save", fields=",".join(configured))
    elif not _step_done("integrations"):
        db.set_setting("wizard_skipped_integrations", "1")
        audit(request, "wizard_skip", step="integrations")
    _mark_complete_if_done()
    return RedirectResponse("/setup", status_code=303)


@router.post("/setup/auth", response_class=HTMLResponse)
async def setup_auth(request: Request, username: str = Form("", max_length=150),
                     password: str = Form("", max_length=1024)):
    """Creates the first admin from the wizard. Only while NO credentials
    exist (in practice: the ALLOW_NO_AUTH demo opt-out, since otherwise
    /setup/admin already did it); a replay afterwards is a redirect. Not
    skippable: sign-in is required."""
    if credentials_exist():
        return RedirectResponse("/setup", status_code=303)
    username = username.strip()
    if not username or not password:
        return await setup(request, error="Username and password are required.")
    problem = password_problem(password, username)
    if problem:
        return await setup(request, error=problem)
    try:
        # PBKDF2 is deliberately slow: hash off the event loop.
        await run_in_threadpool(db.add_user, username, password, role="admin")
    except Exception:  # noqa: BLE001 -- username UNIQUE, or any other DB rejection
        return await setup(request, error="Could not create that account (name already taken?).")
    audit(request, "wizard_auth_create", username=username, role="admin")
    _mark_complete_if_done()
    # Creating this account just turned sign-in on: sign the creator in so
    # the very next request (continuing the wizard) isn't bounced to /login.
    resp = RedirectResponse("/setup", status_code=303)
    start_session(resp, request, username)
    audit(request, "login_ok", actor_type="user", actor_id=username)
    return resp


@router.post("/setup/first-search", response_class=HTMLResponse)
async def setup_first_search(request: Request, query: str = Form("", max_length=200), country: str = Form("us", max_length=64)):
    if not _setup_write_allowed(request):
        return RedirectResponse("/login", status_code=303)
    if _step_done("first_search"):
        return RedirectResponse("/setup", status_code=303)
    if request.query_params.get("skip"):
        db.set_setting("wizard_skipped_first_search", "1")
        audit(request, "wizard_skip", step="first_search")
        _mark_complete_if_done()
        return RedirectResponse("/setup", status_code=303)

    query = " ".join(query.split())
    if not query:
        return await setup(request, error="Enter a name or business to track.")
    countries = [country] if country in COUNTRIES else ["us"]
    sid = _save_first_subject(query, countries)
    audit(request, "wizard_first_search_save", query=query, country=countries[0], subject_id=sid)
    _mark_complete_if_done()
    resp = RedirectResponse("/search_page", status_code=303)
    resp.set_cookie(SUBJECT_COOKIE, str(sid), max_age=365 * 24 * 3600, httponly=True,
                    samesite="lax", path="/")
    return resp


def _save_first_subject(query: str, countries: list[str]) -> int:
    """"Track your first name": the ACTIVE subject becomes it when it's still
    an unused placeholder (no runs, not the demo, no name or the same name);
    otherwise a new subject is created in the same project -- except when the
    active subject is the demo one: real subjects never land in the "Demo"
    project, they go to the default project ("My project", id 1)."""
    subject = db.current_subject()
    sid = subject.get("id")
    row = db.get_subject(sid) if sid else None
    unused = (row is not None and not row["is_demo"] and not db.list_runs(limit=1)
              and (not row["name"] or row["name"] == query))
    if unused:
        db.update_subject(sid, name=query, queries=[query], countries=countries)
        return sid
    existing = db.find_subject_by_name(query, include_inactive=False)
    if existing is not None:
        db.update_subject(existing["id"], countries=countries)
        return existing["id"]
    row = row or {}
    client_id = DEFAULT_CLIENT_ID if row.get("is_demo") else (row.get("client_id")
                                                              or DEFAULT_CLIENT_ID)
    return db.add_subject(query, countries=countries, client_id=client_id, queries=[query])
