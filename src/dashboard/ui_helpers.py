"""UI templates, navigation shell, formatting, and layout helpers."""
import html
import os
from dataclasses import dataclass
from pathlib import Path

from apify.actors import ACTORS, CONFIGURED_ENGINES, VALID_DEVICES
from config.positive_domains import merged_with
from dashboard import nav
from dashboard.deps import db
from dashboard.session import auth_enabled, is_demo_mode
from database.models import url_hash as _url_hash

project_root = Path(__file__).resolve().parent.parent.parent

# Canonical country order
COUNTRIES = ["global", "ca", "us", "hk", "gb", "fr", "ch", "de", "it", "nl"]
COUNTRY_NAMES = {"global": "Global (no country)", "ca": "Canada", "us": "United States",
                 "hk": "Hong Kong", "gb": "United Kingdom", "fr": "France", "ch": "Switzerland",
                 "de": "Germany", "it": "Italy", "nl": "Netherlands"}


def country_name(code: str) -> str:
    return COUNTRY_NAMES.get((code or "").lower(), (code or "").upper())


def ordered_countries(available) -> list:
    """Orders the available countries according to the canonical COUNTRIES order."""
    av = set(available)
    ordered = [c for c in COUNTRIES if c in av]
    ordered += [c for c in available if c not in COUNTRIES]
    return ordered


# Fallbacks of the tracked query / countries (the subject row is the source of truth)
DEFAULT_QUERY = ""
DEFAULT_COUNTRIES = "us,ca,gb"

# Spend / input clamps
_MIN_PAGES = 1
_MAX_PAGES = 3


def _clamp_pages(pages) -> int:
    try:
        p = int(pages)
    except (TypeError, ValueError):
        p = _MIN_PAGES
    return max(_MIN_PAGES, min(_MAX_PAGES, p))


def _split_valid_engines(engines: list) -> tuple[list[str], list[str]]:
    valid = [e for e in (engines or []) if e in CONFIGURED_ENGINES]
    invalid = [e for e in (engines or []) if e not in CONFIGURED_ENGINES]
    return valid, invalid


def _split_valid_countries(countries: list) -> tuple[list[str], list[str]]:
    valid = [c for c in (countries or []) if c in COUNTRIES]
    invalid = [c for c in (countries or []) if c not in COUNTRIES]
    return valid, invalid


_RECLASSIFY_BATCH_LIMIT_SETTING = "reclassify_batch_limit"
_DEFAULT_RECLASSIFY_BATCH_LIMIT = 200


def _reclassify_batch_limit() -> int:
    try:
        n = int(db.get_setting(_RECLASSIFY_BATCH_LIMIT_SETTING,
                                str(_DEFAULT_RECLASSIFY_BATCH_LIMIT)))
    except (TypeError, ValueError):
        return _DEFAULT_RECLASSIFY_BATCH_LIMIT
    return n if n > 0 else _DEFAULT_RECLASSIFY_BATCH_LIMIT


def merged_positive_domains() -> list:
    return merged_with(db.list_positive_domains())


def report_defaults():
    """(tracked query, countries) of the ACTIVE subject (multi-subject phase 2:
    the subject row is the source of truth, not the report_query/report_countries
    settings keys). Falls back to DEFAULT_COUNTRIES when the subject has none."""
    q = db.get_tracked_query() or DEFAULT_QUERY
    countries = list(db.get_tracked_countries() or [])
    if not countries:
        countries = [x.strip().lower() for x in DEFAULT_COUNTRIES.split(",") if x.strip()]
    return q, countries


# Kept for pages that still use the native <dialog> confirmation (.js-del)
# and as the mount point of the SM overlay layer (static/app.js).
SHELL_CHROME = """
<dialog id='confirm-modal'><div class='modal-card confirm-card'>
  <h3>Confirm</h3>
  <p id='confirm-msg'>Confirm?</p>
  <div class='actions'>
    <button type='button' id='confirm-cancel' class='btn-ghost'>Cancel</button>
    <button type='button' id='confirm-ok' class='danger'>Delete</button>
  </div>
</div></dialog>
<div id='sm-layer'></div>
<aside class='assistant-dock' id='assistant-panel' role='complementary' aria-label='Assistant' hidden></aside>
"""


# Applies the saved theme before first paint (no flash). app.js owns the toggle.
THEME_BOOT = "<script src='/static/theme-boot.js'></script>"


@dataclass(frozen=True)
class Shell:
    """Chrome shared by every signed-in page (page() and templates/base.html)."""
    topbar: str
    rail: str
    banner: str = ""
    chrome: str = SHELL_CHROME

    # Older templates/tests read these names.
    @property
    def sidebar(self) -> str:
        return self.rail

    @property
    def tabbar(self) -> str:
        return ""


def active_project_and_subject() -> tuple[dict, dict]:
    """(project row, subject dict) of the request's active subject."""
    try:
        subject = db.current_subject()
    except Exception:  # noqa: BLE001
        subject = {"id": 0, "name": "", "client_id": 1, "is_demo": False, "countries": []}
    try:
        project = db.get_client(subject.get("client_id") or 1) or {}
    except Exception:  # noqa: BLE001
        project = {}
    return project, subject


def subject_label(subject: dict) -> str:
    return (subject.get("name") or subject.get("query") or "").strip() or "Unnamed subject"


def demo_banner_html() -> str:
    """Plain-text notice shown only while the ACTIVE subject is the demo one."""
    _project, subject = active_project_and_subject()
    if not subject.get("is_demo"):
        return ""
    return ("<div class='demo-banner' role='status'>Demo subject: synthetic sample data for a "
            "fictional person. Nothing shown for this subject is real.</div>")


def shell(active: str) -> Shell:
    """Chrome common to all authenticated pages."""
    from dashboard.context import viewer

    v = viewer()
    try:
        alerts = db.count_open_alerts()   # open = not acknowledged, not snoozed
    except Exception:  # noqa: BLE001
        alerts = 0
    project, subject = active_project_and_subject()
    topbar = nav.render_topbar(project=project.get("name") or "", subject=subject_label(subject),
                               alerts=alerts, username=v.username, auth=auth_enabled(),
                               switcher=bool(v.subjects))
    return Shell(topbar=topbar, rail=nav.render_rail(active),
                 banner=demo_banner_html() if active != "/setup" else "")


def flash_html() -> str:
    """The PRG message for this page (see dashboard.context), rendered inline so
    it works without JS; app.js turns it into a toast."""
    from dashboard.context import flash

    f = flash()
    if not f or not f.get("message"):
        return ""
    kind = f.get("kind") or "ok"
    role = "alert" if kind == "error" else "status"
    return (f"<div class='flash flash-{_esc(kind)}' role='{role}' data-sm-flash='{_esc(kind)}'>"
            f"{_esc(f['message'])}</div>")


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def _short(text, n: int = 110) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def fmt_utc(ts) -> str:
    """'2026-08-24T09:00:00Z' / '2026-08-24T09:00:00' -> '2026-08-24 09:00 UTC'
    ('—' when empty). Stored timestamps are naive UTC; a trailing Z/offset
    is dropped so the zone is never printed twice."""
    text = str(ts or "").strip()
    if not text:
        return "—"
    text = text.replace("T", " ").rstrip("Z")
    for sep in ("+", "."):
        if sep in text[10:]:
            text = text[: 10 + text[10:].index(sep)]
    return f"{text[:16]} UTC" if len(text) >= 16 else f"{text} UTC"


def plural(n: int, word: str, many: str | None = None) -> str:
    """'1 result', '3 results' (no "(s)")."""
    return f"{n} {word if n == 1 else (many or word + 's')}"


def fmt_day(ts, year: bool = True) -> str:
    """'2026-10-06T06:00:00' -> '6 Oct 2026' ('—' when empty, echoed back
    when unparseable)."""
    from datetime import datetime as _dt

    text = str(ts or "").strip()
    if not text:
        return "—"
    try:
        d = _dt.fromisoformat(text[:19].replace("Z", ""))
    except ValueError:
        return text
    return f"{d.day} {d.strftime('%b')}" + (f" {d.year}" if year else "")


def overlay_url(path: str, kind: str = "panel", base: str = "/results") -> str:
    """Deep link that opens an overlay over a page: /results?panel=/ui/runs.
    static/app.js reads ?panel= / ?modal= on load (same-site /ui/ paths only)."""
    from urllib.parse import quote

    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{'modal' if kind == 'modal' else 'panel'}={quote(path, safe='/')}"


def ai_note_hint(notes) -> str:
    """Notice for runs whose AI checks failed (runs.ai_note): one line per
    distinct note, with a link to the AI provider settings. '' if none."""
    distinct = list(dict.fromkeys(n for n in notes if n))
    if not distinct:
        return ""
    lines = "<br>".join(_esc(n) for n in distinct)
    return (f"<div class='hint hint-warn' role='status' data-ai-note>{lines} Same-name and severity "
            "checks are missing for this run; the rule-based labels are still shown. "
            "<a href='/settings#ai'>Check the AI provider</a></div>")


def toast_header(message: str) -> dict:
    """X-SM-Toast header for a fragment answer (app.js shows it as a toast)."""
    from urllib.parse import quote

    return {"X-SM-Toast": quote(message[:300]), "Cache-Control": "no-store"}


def _pill(classification: str) -> str:
    """Sentiment/status pill. The coloured dot is decorative (aria-hidden):
    the text label carries the meaning."""
    c = _esc(classification or "neutral")
    return f"<span class='pill {c}'><span class='dot' aria-hidden='true'></span>{c}</span>"


def _severity_pill(r: dict) -> str:
    """"Sev" cell of _results_table (2.2): pill 1-5 colored by range."""
    sev = r.get("severity")
    if not isinstance(sev, int):
        return "—"
    if sev >= 4:
        pill = f"<span class='pill negative'>{sev}</span>"
    elif sev == 3:
        pill = f"<span class='pill warning'>{sev}</span>"
    else:
        pill = f"<span class='pill sev-low'>{sev}</span>"
    topic = r.get("topic")
    return pill + (f"<br><small class='muted'>{_esc(topic)}</small>" if topic else "")


def _env_file() -> str:
    """Path of the .env that the settings endpoints write to."""
    return os.getenv("ENV_FILE") or str(project_root / ".env")


def page_head(title: str, sub: str = "", actions: str = "") -> str:
    """<h1> + muted subtitle line + right-aligned toolbar (mockup .page-head)."""
    sub_html = f"<div class='sub'>{sub}</div>" if sub else ""
    tools = f"<div class='toolbar'>{actions}</div>" if actions else ""
    return f"<div class='page-head'><div class='page-title'><h1>{_esc(title)}</h1>{sub_html}</div>{tools}</div>"


def document(title: str, inner: str, body_attr: str = "") -> str:
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1, viewport-fit=cover'>"
        f"<title>{_esc(title)} · SERP Monitor</title>"
        f"<link rel='stylesheet' href='/static/style.css'>{THEME_BOOT}</head><body{body_attr}>"
        f"{inner}<script src='/static/app.js'></script></body></html>"
    )


def page(title: str, body: str, active: str = "/", open_dialog: str | None = None, *,
         sub: str = "", actions: str = "", head: bool = True) -> str:
    """Full page in the app shell. `sub` (already-safe HTML) and `actions`
    (buttons) fill the header like the mockup; head=False lets the body render
    its own header."""
    sh = shell(active)
    body_attr = f" data-open-dialog='{_esc(open_dialog)}'" if open_dialog else ""
    header = page_head(title, sub, actions) if head else ""
    return document(title, (
        f"{sh.chrome}{sh.topbar}<div class='shell'>{sh.rail}"
        f"<main class='main' id='main'>{sh.banner}{flash_html()}{header}"
        f"<div class='page-body'>{body}</div></main></div>"
    ), body_attr)


def fragment_or_page(title: str, inner: str, active: str = "/", status_code: int = 200):
    """Response for an overlay route (/ui/...): the bare overlay content when
    the SM loader asks for it (X-SM-Fragment: 1), otherwise the same content
    inside a normal page so the URL also works without JS / opened in a tab.
    `inner` is usually dashboard.ui.overlay(...)."""
    from fastapi.responses import HTMLResponse

    from dashboard.context import is_fragment_request

    if is_fragment_request():
        return HTMLResponse(inner, status_code=status_code,
                            headers={"Cache-Control": "no-store", "X-SM-Fragment": "1"})
    body = f"<div class='card standalone-overlay'>{inner}</div>"
    return HTMLResponse(page(title, body, active=active, head=False), status_code=status_code)


def _sort_th(label, key, sortable, sort_by, order, base_qs) -> str:
    """Column header; if sortable, it's a link that toggles the sort order."""
    if not sortable:
        return f"<th>{label}</th>"
    active = (sort_by == key)
    new_order = "desc" if (active and order == "asc") else "asc"
    arrow = ""
    if active:
        arrow = " <span class='arrow'>" + ("▲" if order == "asc" else "▼") + "</span>"
    qs = (base_qs + "&" if base_qs else "") + f"sort_by={key}&order={new_order}"
    return f"<th class='sort'><a href='/results?{qs}'>{label}{arrow}</a></th>"


def _evidence_cell(r: dict) -> str:
    """"Evidence" cell of an _results_table row (3.2): a link to the saved
    snapshots, or -- for negative results only, the ones worth preserving --
    a button to capture the page now."""
    url = r.get("url") or ""
    h = _url_hash(url) if url else ""
    if not h:
        return ""
    n = len(db.evidence_for(h))
    if n:
        plural = "s" if n != 1 else ""
        return f"<a class='btn-link btn-sm btn-ghost' href='/evidence?url_hash={h}'>{n} snapshot{plural}</a>"
    if r.get("classification") != "negative":
        return ""
    result_id = r.get("id")
    rid_input = (f"<input type='hidden' name='result_id' value='{_esc(result_id)}'>"
                 if result_id is not None else "")
    return (
        f"<form action='/evidence/capture' method='post' style='display:inline'>"
        f"<input type='hidden' name='url' value='{_esc(url)}'>{rid_input}"
        f"<button type='submit' class='btn-sm btn-ghost' title='Save a copy of this page now'>"
        f"Capture</button></form>"
    )


def _results_table(results, sortable=False, sort_by="", order="", base_qs="", notes=None,
                   show_identity=False, show_evidence=False, show_severity=False,
                   show_engine=False, show_device=False) -> str:
    if not results:
        return "<div class='card'><p class='muted'>No results.</p></div>"
    notes = notes or {}
    rows = ""
    for r in results:
        cls = r.get("classification", "neutral")
        url = r.get("url") or ""
        note = notes.get(url)
        note_html = ""
        if note:
            st = note.get("status") or "note"
            tip = _esc(note.get("note") or "")
            note_html = (f"<br><span class='pill note' title='{tip}'>"
                         f"<span class='dot' aria-hidden='true'></span>note: {_esc(st)}</span>")
        identity_html = (
            " <span class='pill neutral' title='Marked as same-name (not this person)'>same-name</span>"
            if show_identity and r.get("same_person") == "no" else ""
        )
        nlink = (f"<a class='note-link' href='/notes?url={_esc(url)}' title='Add/edit note' "
                 f"aria-label='Add/edit note'>✎</a>")
        evidence_td = f"<td>{_evidence_cell(r)}</td>" if show_evidence else ""
        severity_td = f"<td>{_severity_pill(r)}</td>" if show_severity else ""
        engine_td = f"<td>{_esc(r.get('engine') or 'google')}</td>" if show_engine else ""
        device_td = f"<td>{_esc(r.get('device') or 'desktop')}</td>" if show_device else ""
        # Only http(s) URLs become links (same rule as the diff table), so a
        # scraped javascript:/data: URL can never end up in an href.
        title_html = (f"<a href='{_esc(url)}' target='_blank' rel='noopener'>{_esc(r.get('title'))}</a>"
                      if url.startswith(("http://", "https://")) else _esc(r.get('title')))
        rows += (
            f"<tr class='{cls}'>"
            f"<td>{_esc(r.get('position'))}</td>"
            f"<td>{_esc(r.get('country_code'))}</td>"
            f"<td>{title_html} {nlink}"
            f"<br><small class='muted'>{_esc(r.get('domain'))}</small>{note_html}</td>"
            f"<td class='desc'>{_esc(_short(r.get('description')))}</td>"
            f"<td>{_pill(cls)}{identity_html}</td>{severity_td}{evidence_td}{engine_td}{device_td}</tr>"
        )
    th_pos = _sort_th("Pos", "position", sortable, sort_by, order, base_qs)
    th_country = _sort_th("Country", "country", sortable, sort_by, order, base_qs)
    th_cls = _sort_th("Class.", "classification", sortable, sort_by, order, base_qs)
    th_severity = _sort_th("Sev", "severity", sortable, sort_by, order, base_qs) if show_severity else ""
    th_evidence = "<th>Evidence</th>" if show_evidence else ""
    th_engine = "<th>Engine</th>" if show_engine else ""
    th_device = "<th>Device</th>" if show_device else ""
    return (
        f"<div class='card card-table'><div class='table-wrap'><table><tr>{th_pos}{th_country}"
        f"<th>Title / Domain</th><th>Description</th>{th_cls}{th_severity}{th_evidence}"
        f"{th_engine}{th_device}</tr>"
        f"{rows}</table></div></div>"
    )


def _engine_checkboxes() -> str:
    labels = ""
    for engine, entry in ACTORS.items():
        configured = bool(entry.get("id"))
        checked = " checked" if engine == "google" else ""
        disabled = "" if configured else " disabled"
        suffix = "" if configured else " (coming soon)"
        labels += (
            f"<label style='margin-right:14px'><input type='checkbox' name='engines' "
            f"value='{engine}'{checked}{disabled}> {engine.title()}{suffix}</label>"
        )
    return labels


def _device_radios() -> str:
    labels = ""
    for d in VALID_DEVICES:
        checked = " checked" if d == "desktop" else ""
        labels += (
            f"<label style='margin-right:14px'><input type='radio' name='device' "
            f"value='{d}'{checked}> {d.title()}</label>"
        )
    return labels


def _search_forms_html() -> str:
    default_query, default_countries = report_defaults()
    first = default_countries[0] if default_countries else ""
    country_opts = "".join(f"<option value='{c}'{' selected' if c == first else ''}>{c.upper()}</option>"
                           for c in COUNTRIES)
    checkboxes = "".join(
        f"<label style='margin-right:14px'><input type='checkbox' name='countries' value='{c}'"
        f"{' checked' if c in default_countries else ''}> {c.upper()}</label>"
        for c in COUNTRIES
    )
    engine_checkboxes = _engine_checkboxes()
    device_radios = _device_radios()
    key_notice = (
        "<div class='hint'><b>Needs an Apify key.</b> Add it in the "
        "<a href='/setup'>setup wizard</a> or <a href='/settings#search'>Settings → Search</a>; "
        "until then searches will fail.</div>"
        if is_demo_mode() else ""
    )
    return f"""
    {key_notice}
    <h3>Single country</h3>
    <form action='/search' method='post' class='run-form'>
      <label>Tracked name: <input type='text' name='query' value='{_esc(default_query)}' size='40' required></label><br><br>
      <label>Country: <select name='country'>{country_opts}</select></label>
      <label style='margin-left:14px'>Pages: <input type='number' name='pages' value='1' min='1' max='3'></label><br><br>
      <b>Engines:</b><br>{engine_checkboxes}<br><br>
      <b>Device:</b><br>{device_radios}<br><br>
      <button type='submit'>Run search</button>
    </form>
    <h3>Multi-country report</h3>
    <form action='/search_multi' method='post' class='run-form'>
      <label>Tracked name: <input type='text' name='query' value='{_esc(default_query)}' size='40' required></label><br><br>
      <b>Countries:</b><br>{checkboxes}<br><br>
      <label>Pages: <input type='number' name='pages' value='1' min='1' max='3'></label><br><br>
      <b>Engines:</b><br>{engine_checkboxes}<br><br>
      <b>Device:</b><br>{device_radios}<br><br>
      <button type='submit'>Run multi-country report</button>
    </form>
    <p class='muted'>Launches one run per country and engine.</p>
    """


__all__ = [
    "COUNTRIES",
    "COUNTRY_NAMES",
    "DEFAULT_COUNTRIES",
    "DEFAULT_QUERY",
    "SHELL_CHROME",
    "THEME_BOOT",
    "_DEFAULT_RECLASSIFY_BATCH_LIMIT",
    "_MAX_PAGES",
    "_MIN_PAGES",
    "_RECLASSIFY_BATCH_LIMIT_SETTING",
    "Shell",
    "_clamp_pages",
    "_device_radios",
    "_engine_checkboxes",
    "_env_file",
    "_esc",
    "_evidence_cell",
    "_pill",
    "_reclassify_batch_limit",
    "_results_table",
    "_search_forms_html",
    "_severity_pill",
    "_short",
    "_sort_th",
    "_split_valid_countries",
    "_split_valid_engines",
    "active_project_and_subject",
    "country_name",
    "demo_banner_html",
    "document",
    "flash_html",
    "fmt_day",
    "fmt_utc",
    "fragment_or_page",
    "merged_positive_domains",
    "ordered_countries",
    "overlay_url",
    "page",
    "page_head",
    "plural",
    "project_root",
    "report_defaults",
    "shell",
    "subject_label",
    "toast_header",
]
