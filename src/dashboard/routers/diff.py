"""Compare runs (4.2): the Compare dialog GET /ui/compare?a=&b=&country= --
what changed between two runs of a country. A is always the OLDER run and B
the newer one (swapped automatically when reversed). The old /diff page
redirects to /results with the dialog open.

Diff consistent with the rest of the system: uses processing.diff.run_diff
(namesakes same_person='no' excluded, 2.3 consistency; identity by url_hash,
2.5). run_a = old, run_b = new.
"""
from urllib.parse import urlencode

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

from dashboard import ui
from dashboard.deps import db
from dashboard.ui_helpers import (
    _esc,
    _pill,
    _short,
    fmt_utc,
    fragment_or_page,
    ordered_countries,
    overlay_url,
    plural,
)
from processing.diff import run_diff

router = APIRouter()


def _runs_for_country(country_code: str) -> list[dict]:
    """Runs for a country, most recent first. Direct column query
    (C12): list_runs() also brings the filtered positive/negative/
    neutral/total_results counts (batched since 4.1, see Database.
    _filtered_run_counts_batch), which aren't needed here -- only
    apify_run_id/created_at/country_code are used, so it's not even
    worth the extra query that builds those counts.

    Note (C4): selects by run ID (order of `runs`), unlike
    report.weekly._latest_run_pair, which selects by DATE -- both
    converge in practice (1 run per date); they aren't unified without
    a real bug that justifies it."""
    # C21b: `runs` is a core table with per-subject scoping. This query
    # lives outside models.py, so the models guardian couldn't see it --
    # and without the filter, /diff would offer, for comparison, the runs
    # of the A11 pairs (subject_id 1000+) alongside those of the monitored
    # subject.
    subject_clause, subject_params = db._subject_clause()
    conn = db._connect()
    cur = conn.cursor()
    cur.execute(
        f"SELECT apify_run_id, created_at, country_code FROM runs "
        f"WHERE country_code = ? AND {subject_clause} ORDER BY id DESC",
        (country_code, *subject_params),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def _previous_run_id(country_code: str, run_id: str) -> str:
    """apify_run_id immediately before run_id in that country, or "" if
    there isn't one."""
    runs = _runs_for_country(country_code)
    for i, r in enumerate(runs):
        if r["apify_run_id"] == run_id:
            return runs[i + 1]["apify_run_id"] if i + 1 < len(runs) else ""
    return ""


def _resolve_run_ids(a: str, b: str) -> tuple[str, str, str]:
    """Resolves (run_a, run_b, error) -- error="" if everything is ok.

    Defaults (4.2 brief): only `b` -> a = previous run in the same
    country; neither one -> last two runs of the most recent country.
    # ponytail: "only `a`" (not in the brief) is resolved symmetrically
    # (b = another run in the same country) without returning a 500.
    """
    if not a and not b:
        latest = db.list_runs(limit=1)
        if not latest:
            return "", "", "No runs yet."
        country = latest[0]["country_code"]
        b = latest[0]["apify_run_id"]
        a = _previous_run_id(country, b)
        if not a:
            return "", "", (f"There's only one run for {(country or '').upper()}; "
                            f"a second run is needed to compare.")
        return a, b, ""

    if b and not a:
        meta_b = db.get_run(b)
        if not meta_b:
            return "", "", "That run doesn't exist (it may have been deleted)."
        a = _previous_run_id(meta_b["country_code"], b)
        if not a:
            return "", "", (f"This is the first {(meta_b.get('country_code') or '').upper()} run, "
                            "so there's nothing earlier to compare it with.")
        return a, b, ""

    if a and not b:
        meta_a = db.get_run(a)
        if not meta_a:
            return "", "", "That run doesn't exist (it may have been deleted)."
        candidates = [r["apify_run_id"] for r in _runs_for_country(meta_a["country_code"])
                     if r["apify_run_id"] != a]
        if not candidates:
            return "", "", "There's no other run of that country to compare it with."
        return a, candidates[0], ""

    # both given
    if not db.get_run(a) or not db.get_run(b):
        return "", "", "One of those runs doesn't exist (it may have been deleted)."
    return a, b, ""


_ICONS = {"entered": ("+", "new", "Entered"), "left": ("−", "gone", "Left"),
          "up": ("↑", "mv", "Moved up"), "down": ("↓", "mv", "Moved down"),
          "relabel": ("≠", "mv", "Label changed")}


def _domain(r: dict) -> str:
    url = r.get("url") or ""
    domain = _esc(r.get("domain") or url)
    link = (f"<a href='{_esc(url)}' target='_blank' rel='noopener noreferrer'>{domain}</a>"
            if url.startswith(("http://", "https://")) else domain)
    return f"<b>{link}</b><div class='rdom'>{_esc(_short(r.get('title'), 80))}</div>"


def _change_row(kind: str, r: dict, position: str, label_html: str) -> str:
    ico, cls, text = _ICONS[kind]
    return (f"<tr><td><span class='chg-ico {cls}' title='{text}' aria-label='{text}'>{ico}</span></td>"
            f"<td>{_domain(r)}</td><td class='num'>{position}</td><td>{label_html}</td></tr>")


def _run_label(r: dict) -> str:
    eng = (r.get("engine") or "google")
    return fmt_utc(r.get("created_at")) + ("" if eng == "google" else f" · {eng.title()}")


def _runs_with_engine(country: str) -> list[dict]:
    rows = _runs_for_country(country) if country else []
    meta = {r["apify_run_id"]: r for r in db.list_runs(limit=500)}
    return [{**r, "engine": (meta.get(r["apify_run_id"]) or {}).get("engine") or "google"} for r in rows]


def _form(country: str, run_a: str, run_b: str) -> str:
    countries = ordered_countries(db.distinct_countries())
    runs = _runs_with_engine(country)
    c_opts = "".join(f"<option value='{_esc(c)}'{' selected' if c == country else ''}>{_esc(c.upper())}</option>"
                     for c in countries)

    def opts(sel):
        return "".join(f"<option value='{_esc(r['apify_run_id'])}'{' selected' if r['apify_run_id'] == sel else ''}>"
                       f"{_esc(_run_label(r))}</option>" for r in runs) or "<option value=''>No runs</option>"
    return (
        "<form class='row compare-form' method='get' action='/ui/compare' data-sm-submit data-autosubmit>"
        f"<div class='field' style='flex:0 0 110px'><label for='cmp-country'>Country</label>"
        f"<select id='cmp-country' name='country'>{c_opts}</select></div>"
        f"<div class='field'><label for='cmp-a'>Older run (A)</label><select id='cmp-a' name='a'>{opts(run_a)}</select></div>"
        f"<div class='field'><label for='cmp-b'>Newer run (B)</label><select id='cmp-b' name='b'>{opts(run_b)}</select></div>"
        "<noscript><button type='submit' class='btn'>Compare</button></noscript></form>"
    )


def compare_html(a: str = "", b: str = "", country: str = "") -> str:
    """The Compare dialog body for runs a (older) and b (newer)."""
    note = ""
    meta_b = db.get_run(b) if b else None
    if country and (not meta_b or (meta_b.get("country_code") or "").lower() != country.lower()):
        # The country picker changed: that country's two latest runs.
        runs = _runs_for_country(country)
        if len(runs) >= 2:
            a, b = runs[1]["apify_run_id"], runs[0]["apify_run_id"]
        else:
            msg = (f"There's only one {country.upper()} run so far; a second one is needed to compare."
                   if runs else f"No {country.upper()} runs yet.")
            return _form(country, "", runs[0]["apify_run_id"] if runs else "") + \
                f"<div class='empty'>{_esc(msg)}</div>"
    run_a, run_b, error = _resolve_run_ids(a, b)
    if error:
        fallback = country or ((db.list_runs(limit=1) or [{}])[0].get("country_code") or "")
        return _form(fallback, "", "") + f"<div class='empty'>{_esc(error)}</div>"
    meta_a, meta_b = db.get_run(run_a), db.get_run(run_b)
    country_a = (meta_a or {}).get("country_code") or ""
    country_b = (meta_b or {}).get("country_code") or ""
    if country_a.lower() != country_b.lower():
        return _form(country_b, "", run_b) + (
            f"<div class='empty'>These runs are from different countries ({country_a.upper() or '?'} "
            f"and {country_b.upper() or '?'}). Pick two runs of the same country.</div>")
    # C10: A must be the OLD run; compare by id (two runs can share a second).
    if (meta_a.get("id") or 0) > (meta_b.get("id") or 0):
        run_a, run_b = run_b, run_a
        meta_a, meta_b = meta_b, meta_a
        note = "<p class='note'>The older run is always A, so the two were swapped.</p>"

    diff = run_diff(db, run_a, run_b)
    entered, left = diff["entered"], diff["exited"]
    moved, relabelled = diff["moved"], diff["reclassified"]
    kpis = (
        "<div class='kpis kpis-3'>"
        f"<div class='card kpi'><div class='label'>Entered</div><div class='value num'>{len(entered)}</div></div>"
        f"<div class='card kpi'><div class='label'>Left</div><div class='value num'>{len(left)}</div></div>"
        f"<div class='card kpi'><div class='label'>Moved</div><div class='value num'>{len(moved)}</div></div>"
        "</div>"
    )
    rows = []
    for r in entered:
        rows.append(_change_row("entered", r, f"entered at {_esc(r.get('position'))}",
                                _pill(r.get("classification") or "neutral")))
    for r in left:
        rows.append(_change_row("left", r, f"left (was {_esc(r.get('position'))})",
                                _pill(r.get("classification") or "neutral")))
    for r, d in moved:
        before = (r.get("position") or 0) + d
        rows.append(_change_row("up" if d > 0 else "down", r, f"{before} → {_esc(r.get('position'))}",
                                _pill(r.get("classification") or "neutral")))
    for r, old, new in relabelled:
        rows.append(_change_row("relabel", r, _esc(r.get("position")),
                                f"{_pill(old or 'neutral')} → {_pill(new)}"))
    if rows:
        table = ("<div class='table-wrap card'><table><thead><tr><th><span class='sr-only'>Change</span></th>"
                 "<th>Domain</th><th>Position</th><th>Label</th></tr></thead>"
                 f"<tbody>{''.join(rows)}</tbody></table></div>")
    else:
        table = "<div class='empty'><b>Nothing changed.</b><br>The same results, in the same order.</div>"
    neg_in = sum(1 for r in entered if r.get("classification") == "negative")
    neg_out = sum(1 for r in left if r.get("classification") == "negative")
    summary_bits = []
    if neg_in:
        summary_bits.append(f"{plural(neg_in, 'new negative result')}")
    if neg_out:
        summary_bits.append(f"{plural(neg_out, 'negative result')} left")
    if relabelled:
        summary_bits.append(f"{plural(len(relabelled), 'label')} changed")
    summary = f"<p class='note'>{_esc(' · '.join(summary_bits))}.</p>" if summary_bits else ""
    hidden = diff.get("hidden_same_name") or 0
    if hidden:
        summary += (f"<p class='note' data-hidden-same-name>{plural(hidden, 'same-name result')} hidden "
                    "(marked as not this person in either run).</p>")
    return _form(country_b, run_a, run_b) + note + kpis + summary + table


@router.get("/ui/compare", response_class=HTMLResponse, tags=["ui"])
async def ui_compare(a: str = "", b: str = "", country: str = ""):
    body = "<span hidden data-sm-wide></span>" + compare_html(a.strip(), b.strip(), country.strip().lower())
    inner = ui.overlay("Compare runs", body)
    return fragment_or_page("Compare runs", inner, active="/results")


@router.get("/diff")
async def diff_page(a: str = "", b: str = "", country: str = ""):
    """Old page: the Compare dialog opens over Results with the same runs."""
    params = {k: v for k, v in (("a", a), ("b", b), ("country", country)) if v}
    path = "/ui/compare" + (f"?{urlencode(params)}" if params else "")
    return RedirectResponse(overlay_url(path, "modal"), status_code=303)
