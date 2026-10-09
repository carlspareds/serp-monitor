"""Results page (/results): the stored results of the active subject's latest
run (or a chosen run date), as in the approved mockup.

Header (subject · N results), toolbar (New search, Review, Compare, Runs,
More ▾ -> Timeline / Evidence / Notes / Reports / AI checks), a one-row filter
bar (country, run date, label chips, "More filters" popover) and the results
table with a row menu per result. Results is the ONE home of New search,
Review, Compare, Reports and the AI checks; other pages only carry a single
shortcut that opens the same dialog. Domains live in Settings → Projects and
subjects (the row menu's "Mark this domain as mine / negative" writes to the
same lists in one click). Every secondary tool is a modal or side
panel (/ui/...) opened over this page; deep links use ?panel= / ?modal=
(ui_helpers.overlay_url, read by static/app.js).

Everything is scoped to the active subject (the db singleton reads the
request's subject ContextVar).
"""
from urllib.parse import quote, urlencode

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from apify.actors import ACTORS, VALID_DEVICES
from dashboard import nav
from dashboard.context import viewer
from dashboard.deps import db
from dashboard.ui_helpers import (
    _esc,
    _pill,
    active_project_and_subject,
    ai_note_hint,
    fmt_day,
    ordered_countries,
    page,
    plural,
    subject_label,
)
from database.models import NOTE_STATUS_LABELS, normalize_note_status
from database.models import url_hash as _url_hash

router = APIRouter()

_LABELS = (("", "All"), ("positive", "Positive"), ("negative", "Negative"), ("neutral", "Neutral"))
_SORTS = ("position", "classification", "severity")
_LABEL_ORDER = {"negative": 0, "neutral": 1, "positive": 2}
_LIMIT = 500


def _flag(value: str) -> bool:
    return (value or "").strip().lower() not in ("", "0", "false", "off", "no")


def _severity_of(r: dict) -> int:
    """Severity used by the filter/sort: a negative not scored yet counts as 3
    (same fallback as Database.get_results_filtered and the alerts)."""
    sev = r.get("severity")
    if isinstance(sev, int):
        return sev
    return 3 if r.get("classification") == "negative" else 0


def _sorted(rows: list[dict], sort_by: str, order: str) -> list[dict]:
    reverse = order == "desc"
    if sort_by == "classification":
        def key(r):
            return (_LABEL_ORDER.get(r.get("classification") or "neutral", 1), r.get("position") or 999)
    elif sort_by == "severity":
        def key(r):
            sev = r.get("severity") if isinstance(r.get("severity"), int) else -1
            return (sev, -(r.get("position") or 999)) if reverse else (sev, r.get("position") or 999)
    else:
        def key(r):
            return (r.get("position") if isinstance(r.get("position"), int) else 999, r.get("id") or 0)
    return sorted(rows, key=key, reverse=reverse)


def _previous_hashes(country: str, date: str, dates: list[str]) -> set[str] | None:
    """url_hashes of the run date before `date` (None when there is none)."""
    if date not in dates:
        return None
    i = dates.index(date)
    if i + 1 >= len(dates):
        return None
    prev = set()
    for rid in db.latest_run_ids(country, dates[i + 1]):
        for r in db.get_results_by_run(rid):
            if r.get("url"):
                prev.add(_url_hash(r["url"]))
    return prev


class _State:
    """The filter state of the page, and URLs that change one piece of it."""

    def __init__(self, **values):
        self.values = values

    def get(self, key):
        return self.values.get(key)

    def url(self, **changes) -> str:
        merged = {**self.values, **changes}
        params = {k: v for k, v in merged.items() if v not in ("", None, False, 0)}
        params = {k: ("1" if v is True else v) for k, v in params.items()}
        return "/results" + (f"?{urlencode(params)}" if params else "")

    def hidden(self, *skip: str) -> str:
        out = []
        for k, v in self.values.items():
            if k in skip or v in ("", None, False, 0):
                continue
            out.append(f"<input type='hidden' name='{k}' value='{_esc('1' if v is True else v)}'>")
        return "".join(out)


def _toolbar(pending: int, back: str = "/results") -> str:
    count = f"<span class='count' data-review-count>{pending}</span>" if pending else \
        "<span class='count' data-review-count hidden>0</span>"
    v = viewer()
    search = (f"<a class='btn primary' href='/ui/search' data-modal data-title='New search'>"
              f"{nav.icon('plus')}New search</a>" if v.can_write else "")
    ai_checks = "/ui/ai-checks?" + urlencode({"next": back})
    return (
        f"{search}"
        f"<a class='btn' href='/ui/review' data-modal data-title='Review'>Review {count}</a>"
        "<a class='btn' href='/ui/compare' data-modal data-modal-wide data-title='Compare runs'>Compare</a>"
        "<a class='btn' href='/ui/runs' data-panel data-title='Runs'>Runs</a>"
        "<button type='button' class='btn' id='moreTools' aria-haspopup='menu' aria-expanded='false'"
        f" data-popover='#results-more'>More {nav.icon('chev', 'ico chev')}</button>"
        "<template id='results-more'>"
        "<a class='item' role='menuitem' href='/ui/timeline' data-panel data-title='Timeline'>Timeline</a>"
        "<a class='item' role='menuitem' href='/ui/evidence' data-panel data-title='Evidence'>Evidence</a>"
        "<a class='item' role='menuitem' href='/ui/notes' data-panel data-title='Notes'>Notes</a>"
        "<a class='item' role='menuitem' href='/ui/reports' data-modal data-modal-wide"
        " data-title='Reports'>Reports</a>"
        f"<hr><a class='item' role='menuitem' href='{_esc(ai_checks)}' data-modal"
        " data-title='AI checks'>AI checks…</a>"
        "</template>"
    )


def _filter_bar(st: _State, countries: list[str], dates: list[str], engines: list[str],
                devices: list[str]) -> str:
    country, date, label = st.get("country"), st.get("date"), st.get("classification") or ""
    country_opts = "".join(
        f"<option value='{_esc(c)}'{' selected' if c == country else ''}>{_esc(c.upper())}</option>"
        for c in countries)
    date_opts = "".join(
        f"<option value='{_esc(d)}'{' selected' if d == date else ''}>{_esc(fmt_day(d))}</option>"
        for d in dates) or "<option value=''>No runs</option>"
    selects = (
        "<form class='filter-form' method='get' action='/results' data-autosubmit>"
        f"{st.hidden('country', 'date')}"
        f"<label class='sr-only' for='f-country'>Country</label>"
        f"<select id='f-country' name='country' aria-label='Country'>{country_opts}</select>"
        f"<label class='sr-only' for='f-date'>Run date</label>"
        f"<select id='f-date' name='date' aria-label='Run date'>{date_opts}</select>"
        "<noscript><button type='submit' class='btn small'>Show</button></noscript></form>"
    )
    chips = "".join(
        f"<a class='chip' href='{_esc(st.url(classification=value))}'"
        f" aria-pressed='{'true' if value == label else 'false'}'>{_esc(text)}</a>"
        for value, text in _LABELS)
    active_more = sum(1 for k in ("include_homonyms", "min_severity", "has_note", "new_only",
                                  "engine", "device") if st.get(k))
    more_label = "More filters" + (f" ({active_more})" if active_more else "")

    def box(name, text, on):
        return (f"<label class='item check-item'><input type='checkbox' name='{name}' value='1'"
                f"{' checked' if on else ''}> {_esc(text)}</label>")

    extra = ""
    if len(engines) > 1:
        opts = "<option value=''>All engines</option>" + "".join(
            f"<option value='{_esc(e)}'{' selected' if e == st.get('engine') else ''}>{_esc(e.title())}</option>"
            for e in engines)
        extra += (f"<div class='item'><label for='f-engine'>Engine</label>"
                  f"<select id='f-engine' name='engine' data-filter='engine'>{opts}</select></div>")
    if len(devices) > 1:
        opts = "<option value=''>All devices</option>" + "".join(
            f"<option value='{_esc(d)}'{' selected' if d == st.get('device') else ''}>{_esc(d.title())}</option>"
            for d in devices)
        extra += (f"<div class='item'><label for='f-device'>Device</label>"
                  f"<select id='f-device' name='device' data-filter='device'>{opts}</select></div>")
    more = (
        "<template id='more-filters'>"
        "<form method='get' action='/results' data-autosubmit class='more-filters'>"
        f"{st.hidden('include_homonyms', 'min_severity', 'has_note', 'new_only', 'engine', 'device')}"
        "<div class='hd'>More filters</div>"
        + box("include_homonyms", "Include same-name results", st.get("include_homonyms"))
        + (f"<label class='item check-item'><input type='checkbox' name='min_severity' value='3'"
           f"{' checked' if st.get('min_severity') else ''}> Severity 3 or higher</label>")
        + box("has_note", "Has a note", st.get("has_note"))
        + box("new_only", "Only new since last run", st.get("new_only"))
        + extra
        + "<noscript><button type='submit' class='btn small'>Apply</button></noscript>"
        + (f"<hr><a class='item' href='{_esc(st.url(include_homonyms='', min_severity='', has_note='', new_only='', engine='', device=''))}'>"
           "Clear these filters</a>" if active_more else "")
        + "</form></template>"
    )
    return (
        f"<div class='filterbar'>{selects}<span class='sep' aria-hidden='true'></span>"
        f"<div class='chips' role='group' aria-label='Label'>{chips}</div>"
        "<button type='button' class='btn ghost small more-filters-btn' id='moreFilters'"
        " aria-haspopup='dialog' aria-expanded='false' data-popover='#more-filters'"
        f" data-popover-class='filters-pop'>{_esc(more_label)} {nav.icon('chev', 'ico chev')}</button>"
        f"{more}</div>"
    )


def _sort_th(label: str, key: str, st: _State, cls: str = "") -> str:
    current, order = st.get("sort_by") or "position", st.get("order") or "asc"
    active = current == key
    new_order = "desc" if active and order == "asc" else "asc"
    arrow = ""
    aria = ""
    if active:
        arrow = f" <span class='arrow' aria-hidden='true'>{'▲' if order == 'asc' else '▼'}</span>"
        aria = f" aria-sort='{'ascending' if order == 'asc' else 'descending'}'"
    href = st.url(sort_by=key if key != "position" else "", order=new_order if new_order != "asc" else "")
    cls_attr = f" class='sort {cls}'" if cls else " class='sort'"
    return f"<th{cls_attr}{aria}><a href='{_esc(href)}'>{_esc(label)}{arrow}</a></th>"


def _ask_prompt(r: dict) -> str:
    title = " ".join(str(r.get("title") or r.get("url") or "this result").split())[:120]
    label = r.get("classification") or "neutral"
    return f"Why is “{title}” ({r.get('domain') or 'this result'}) labelled {label}? What should I do about it?"


def _row_menu(i: int, r: dict, back: str) -> str:
    v = viewer()
    url = r.get("url") or ""
    rid = r.get("id")
    domain = r.get("domain") or ""
    items = [f"<div class='hd'>{_esc(domain or 'Result')}</div>"]
    by = r.get("classified_by") or "rule"
    reason = " ".join(str(r.get("ai_reason") or "").split())
    if by == "ai":
        why = f"Labelled by your AI provider{': ' + reason if reason else '.'}"
        items.append(f"<div class='rm-why' data-label-source='ai'>{_esc(why)}</div>")
    elif by == "human":
        items.append("<div class='rm-why' data-label-source='human'>Labelled by a person.</div>")
    note_label = "Add or edit note" if v.can_write else "View note"
    items.append(f"<a class='item' role='menuitem' href='/ui/note?url={quote(url, safe='')}'"
                 f" data-panel data-title='Note'>{_esc(note_label)}</a>")
    if v.can_write and r.get("classification") == "negative" and url:
        items.append(
            "<form class='inline-form' action='/evidence/capture' method='post' data-sm-submit>"
            f"<input type='hidden' name='url' value='{_esc(url)}'>"
            f"<input type='hidden' name='result_id' value='{_esc(rid)}'>"
            f"<input type='hidden' name='next' value='{_esc(back)}'>"
            "<button type='submit' class='item' role='menuitem'>Save evidence snapshot</button></form>")
    if v.can_write and rid is not None:
        items.append(f"<a class='item' role='menuitem' href='/ui/review?id={_esc(rid)}&amp;back={quote(back, safe='')}' data-modal"
                     " data-title='Change label'>Change label…</a>")
    if v.can_write and domain:
        for which, text in (("positive", "Mark this domain as mine"), ("negative", "Mark this domain as negative")):
            items.append(
                "<form class='inline-form' action='/domains/add-any' method='post'>"
                f"<input type='hidden' name='domain' value='{_esc(domain)}'>"
                f"<input type='hidden' name='list' value='{which}'>"
                f"<input type='hidden' name='next' value='{_esc(back)}'>"
                f"<button type='submit' class='item' role='menuitem' data-domain-mark='{which}'>{text}</button></form>")
    items.append(f"<a class='item' role='menuitem' href='/ui/timeline?domain={quote(domain, safe='')}'"
                 " data-panel data-title='Timeline'>Show on timeline</a>")
    items.append(f"<button type='button' class='item' role='menuitem' data-assistant-ask='{_esc(_ask_prompt(r))}'"
                 f" data-result-id='{_esc(rid)}'>Ask the assistant about this</button>")
    if url.startswith(("http://", "https://")):
        items.append(f"<hr><a class='item' role='menuitem' href='{_esc(url)}' target='_blank'"
                     " rel='noopener noreferrer'>Open page in new tab</a>")
    return f"<template id='rm-{i}'>{''.join(items)}</template>"


def _table(rows: list[dict], st: _State, notes: dict, evidence: dict, new_set: set | None,
           show_engine: bool, back: str, empty_html: str) -> str:
    head = (
        "<thead><tr>"
        + _sort_th("#", "position", st, "num")
        + "<th>Result</th>"
        + _sort_th("Label", "classification", st)
        + _sort_th("Severity", "severity", st, "hide-sm")
        + "<th class='hide-sm'>Notes</th><th><span class='sr-only'>Actions</span></th></tr></thead>"
    )
    if not rows:
        return (f"<div class='table-wrap'><table class='results-table'>{head}<tbody><tr><td colspan='6'>"
                f"{empty_html}</td></tr></tbody></table></div>")
    body = []
    templates = []
    for i, r in enumerate(rows):
        url = r.get("url") or ""
        h = _url_hash(url) if url else ""
        cls = r.get("classification") or "neutral"
        title = _esc(r.get("title") or url or "Untitled")
        title_html = (f"<a class='rtitle' href='{_esc(url)}' target='_blank' rel='noopener noreferrer'>{title}</a>"
                      if url.startswith(("http://", "https://")) else f"<span class='rtitle'>{title}</span>")
        tags = []
        if new_set is not None and h and h not in new_set:
            tags.append("<span class='tag tag-new'>new</span>")
        if show_engine and r.get("engine"):
            tags.append(f"<span class='tag'>{_esc((r.get('engine') or '').title())}</span>")
        if r.get("device") and r.get("device") != "desktop":
            tags.append(f"<span class='tag'>{_esc(r['device'])}</span>")
        label = _pill(cls)
        if r.get("review_status") == "pending":
            label += " <span class='tag tag-review'>needs review</span>"
        if r.get("same_person") == "no":
            label += (" <span class='tag' title='Marked as same-name (not this person)'>same-name</span>")
        sev = r.get("severity")
        topic = f" title='{_esc(r.get('topic'))}'" if r.get("topic") else ""
        sev_html = (f"<span class='num sev'{topic}>{sev} / 5</span>" if isinstance(sev, int)
                    else "<span class='muted'>—</span>")
        note = notes.get(url)
        note_bits = []
        if note:
            st_label = NOTE_STATUS_LABELS.get(normalize_note_status(note.get("status")), "Note")
            note_bits.append(f"<span class='tag' title='{_esc(note.get('note') or '')}'>{_esc(st_label)}</span>")
        n_ev = evidence.get(h, 0)
        if n_ev:
            note_bits.append(f"<a class='tag' href='/ui/evidence?url_hash={_esc(h)}' data-panel"
                             f" data-title='Evidence'>{_esc(plural(n_ev, 'snapshot'))}</a>")
        notes_html = " ".join(note_bits) or "<span class='muted'>—</span>"
        domain = _esc(r.get("domain") or "")
        body.append(
            f"<tr id='r-{_esc(r.get('id'))}' class='{_esc(cls)}' data-result-id='{_esc(r.get('id'))}'"
            f" data-result-url='{_esc(url)}' data-domain='{domain}' data-label='{_esc(cls)}'>"
            f"<td class='num muted'>{_esc(r.get('position'))}</td>"
            f"<td class='rcell'>{title_html} {' '.join(tags)}<div class='rdom dom' title='{domain}'>{domain}</div></td>"
            f"<td>{label}</td>"
            f"<td class='hide-sm'>{sev_html}</td>"
            f"<td class='hide-sm'>{notes_html}</td>"
            f"<td class='rowmenu'><button type='button' class='iconbtn' aria-haspopup='menu'"
            f" aria-expanded='false' data-popover='#rm-{i}'"
            f" aria-label='Actions for {title}'>{nav.icon('dots')}</button></td></tr>"
        )
        templates.append(_row_menu(i, r, back))
    return (f"<div class='table-wrap'><table class='results-table'>{head}<tbody>{''.join(body)}"
            f"</tbody></table></div>{''.join(templates)}")


@router.get("/results", response_class=HTMLResponse, tags=["results"])
async def show_results(country: str = "", date: str = "", classification: str = "",
                       sort_by: str = "position", order: str = "asc",
                       include_homonyms: str = "", min_severity: str = "",
                       has_note: str = "", new_only: str = "",
                       engine: str = "", device: str = ""):
    _project, subject = active_project_and_subject()
    name = subject_label(subject)
    pending = db.count_pending_review()
    toolbar = _toolbar(pending)
    countries = ordered_countries(db.distinct_countries())
    if not countries:
        v = viewer()
        cta = ("<a class='btn primary' href='/ui/search' data-modal data-title='New search'>"
               f"{nav.icon('plus')}Run a first search</a>" if v.can_write else "")
        body = ("<div class='card'><div class='empty'><b>No results yet for "
                f"{_esc(name)}.</b><br>Run a search and the results of each run show up here."
                f"<div class='actions-row' style='justify-content:center;margin-top:12px'>{cta}</div>"
                "</div></div>")
        return page("Results", body, active="/results", sub=f"{_esc(name)} · no runs yet",
                    actions=toolbar)

    engine = engine if engine in ACTORS else ""
    device = device if device in VALID_DEVICES else ""
    if country not in countries:
        country = countries[0]
    dates = db.dates_for_country(country)
    if date not in dates:
        date = dates[0] if dates else ""
    classification = classification if classification in ("positive", "negative", "neutral") else ""
    sort_by = sort_by if sort_by in _SORTS else "position"
    order = "desc" if order == "desc" else "asc"
    show_homonyms = _flag(include_homonyms)
    try:
        min_sev = int(float(min_severity)) if (min_severity or "").strip() else 0
    except ValueError:
        min_sev = 0
    min_sev = min_sev if 1 <= min_sev <= 5 else 0
    note_only, new_flag = _flag(has_note), _flag(new_only)

    run_ids = db.latest_run_ids(country, date)
    base: list[dict] = []
    for rid in run_ids:
        base.extend(db.get_results_filtered(run_id=rid, sort_by="position", order="asc",
                                            limit=_LIMIT, include_homonyms=show_homonyms,
                                            engine=engine, device=device))
    total = len(base)
    notes = db.get_notes_map()
    evidence: dict[str, int] = {}
    for e in db.list_evidence(limit=2000):
        evidence[e.get("url_hash")] = evidence.get(e.get("url_hash"), 0) + 1
    prev = _previous_hashes(country, date, dates)

    rows = base
    if classification:
        rows = [r for r in rows if (r.get("classification") or "neutral") == classification]
    if min_sev:
        rows = [r for r in rows if _severity_of(r) >= min_sev]
    if note_only:
        rows = [r for r in rows if r.get("url") in notes]
    if new_flag and prev is not None:
        rows = [r for r in rows if r.get("url") and _url_hash(r["url"]) not in prev]
    rows = _sorted(rows, sort_by, order)

    st = _State(country=country, date=date if date != (dates[0] if dates else "") else "",
                classification=classification, sort_by=sort_by if sort_by != "position" else "",
                order=order if order != "asc" else "", include_homonyms=show_homonyms,
                min_severity=min_sev or "", has_note=note_only, new_only=new_flag,
                engine=engine, device=device)
    # The selects need the real date even when it's the default one.
    st_select = _State(**{**st.values, "date": date})
    engines_present = db.distinct_engines()
    devices_present = db.distinct_devices()
    back = st.url()

    filtered = rows is not base and len(rows) != total
    if total == 0:
        empty = "<div class='empty'><b>This run has no results.</b></div>"
    elif new_flag and prev is None:
        empty = ("<div class='empty'><b>Nothing to compare with yet.</b><br>"
                 "This is the first run for this country, so every result is new.</div>")
    else:
        empty = ("<div class='empty'><b>No results match these filters.</b><br>"
                 f"<a href='{_esc(_State(country=country, date=st.get('date')).url())}'>Show all results</a></div>")
    table = _table(rows, st, notes, evidence, prev, len(engines_present) > 1, back, empty)
    showing = (f"<p class='note results-count'>Showing {plural(len(rows), 'result')} of {total}.</p>"
               if filtered else "")
    ai_hint = ai_note_hint((db.get_run(rid) or {}).get("ai_note") for rid in run_ids)
    body = (f"{ai_hint}<div class='card results-card'>{_filter_bar(st_select, countries, dates, engines_present, devices_present)}"
            f"{table}</div>{showing}")
    latest = dates[0] if dates else ""
    when = "in the latest run" if date == latest else f"on {_esc(fmt_day(date))}"
    sub = f"{_esc(name)} · {_esc(country.upper())} · {plural(total, 'result')} {when}"
    return page("Results", body, active="/results", sub=sub, actions=_toolbar(pending, back))
