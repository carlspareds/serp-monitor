"""Evidence vault (3.2/1.3): the Evidence side panel (/ui/evidence, every
snapshot or those of one URL with ?url_hash=), POST /evidence/capture (manual
capture, audited C24, PRG + toast with a friendly error) and GET
/evidence/{id}/download. The old /evidence page redirects to /results with the
panel open.

See processing.evidence for the automatic chokepoint (capture_new_negatives,
called after update_url_lifecycle when a run is saved)."""
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from common.validate import http_url
from dashboard import ui
from dashboard.context import redirect_with_flash, safe_next
from dashboard.deps import db
from dashboard.main import audit
from dashboard.ui_helpers import (
    _esc,
    fmt_day,
    fmt_utc,
    fragment_or_page,
    overlay_url,
    plural,
)
from processing.evidence import capture_url, evidence_dir

router = APIRouter()


# Reserved/non-resolving TLDs (RFC 2606/6761): the demo data lives under .example.
_UNRESOLVABLE_TLDS = (".example", ".test", ".invalid", ".localhost")
_TRIGGER_LABELS = {"auto_negative": "automatic", "manual": "saved by hand"}


def _short_url(url: str) -> str:
    parts = urlsplit(url or "")
    text = (parts.netloc + parts.path).removeprefix("www.") if parts.netloc else (url or "")
    return text.rstrip("/") or url


def evidence_panel_html(url_hash: str = "") -> str:
    rows = db.evidence_for(url_hash) if url_hash else db.list_evidence()
    urls = {}
    for r in db.list_evidence(limit=2000):
        urls.setdefault(r["url_hash"], r["url"])
    items = []
    for r in rows:
        path = Path(r.get("html_path") or "")
        exists = path.is_file()
        size = f"{path.stat().st_size / 1024:.0f} KB" if exists else "file missing"
        sha = (r.get("sha256_html") or "")
        dl = (f"<a class='btn small rt' href='/evidence/{r['id']}/download' download>Download</a>"
              if exists else "<span class='rt tag tag-warn'>file missing</span>")
        items.append(
            "<div class='li'><div class='li-top'>"
            f"<b class='break'>{_esc(_short_url(r.get('url')))}</b>"
            f"<span class='rt muted num' title='{_esc(fmt_utc(r.get('captured_at')))}'>"
            f"{_esc(fmt_day(r.get('captured_at')))}</span></div>"
            f"<div class='li-top'><span class='mono muted' title='SHA-256 {_esc(sha)}'>{_esc(sha[:12])}…</span>"
            f"<span class='tag'>{_esc(_TRIGGER_LABELS.get(r.get('trigger'), r.get('trigger') or ''))}</span>"
            f"<span class='muted li-sub'>{_esc(size)}</span>{dl}</div></div>"
        )
    opts = "<option value=''>All pages</option>" + "".join(
        f"<option value='{_esc(h)}'{' selected' if h == url_hash else ''}>{_esc(_short_url(u))}</option>"
        for h, u in urls.items())
    picker = ("<form class='row' method='get' action='/ui/evidence' data-sm-submit data-autosubmit>"
              "<div class='field'><label for='ev-url'>Page</label>"
              f"<select id='ev-url' name='url_hash'>{opts}</select></div>"
              "<noscript><button type='submit' class='btn'>Show</button></noscript></form>"
              if len(urls) > 1 else "")
    intro = ("<p class='note'>Saved copies of negative pages, with a SHA-256 fingerprint taken at "
             "capture time. New negative results are saved automatically; use “Save evidence "
             "snapshot” in a result's menu to save one by hand.</p>")
    if rows:
        listing = (f"<p class='note'>{plural(len(rows), 'snapshot')}, newest first.</p>"
                   f"<div class='list'>{''.join(items)}</div>")
    elif url_hash:
        listing = "<div class='empty'>No snapshots of this page yet.</div>"
    else:
        listing = "<div class='empty'><b>No snapshots yet.</b><br>They appear here when a negative result is saved.</div>"
    return ui.overlay("Evidence", intro + picker + listing)


@router.get("/ui/evidence", response_class=HTMLResponse, tags=["ui"])
async def ui_evidence(url_hash: str = ""):
    return fragment_or_page("Evidence", evidence_panel_html(url_hash.strip()), active="/results")


@router.get("/evidence")
async def evidence_page(url_hash: str = ""):
    """Old page: the Evidence panel opens over Results."""
    path = "/ui/evidence" + (f"?{urlencode({'url_hash': url_hash})}" if url_hash else "")
    return RedirectResponse(overlay_url(path, "panel"), status_code=303)


def _capture_failed_message(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith(_UNRESOLVABLE_TLDS):
        return ("Couldn't save a snapshot: demo addresses (.example) don't exist on the internet. "
                "Snapshots work for real results.")
    return ("Couldn't save a snapshot: the site didn't answer or refused the request. "
            "Try again later.")


@router.post("/evidence/capture", response_class=HTMLResponse)
def evidence_capture(request: Request, url: str = Form(..., max_length=2048), result_id: int | None = Form(None),
                     next: str = Form("/results", max_length=2048)):
    back = safe_next(next, "/results")
    if http_url(url) is None:
        return redirect_with_flash(back, "Couldn't save a snapshot: that isn't a web page "
                                         "address.", "error")
    row = capture_url(db, url, trigger="manual", result_id=result_id)
    if row is None:
        return redirect_with_flash(back, _capture_failed_message(url), "error")
    audit(request, "evidence_capture", resource_type="url", resource_id=url,
          trigger="manual", evidence_id=row["id"])
    target = overlay_url("/ui/evidence?" + urlencode({"url_hash": row["url_hash"]}), "panel", base=back)
    return redirect_with_flash(target, "Snapshot saved with its SHA-256 fingerprint.")


@router.get("/evidence/{evidence_id}/download")
async def evidence_download(evidence_id: int):
    row = db.get_evidence_by_id(evidence_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    # html_path is the only content source today (3.2 -- manual/automatic
    # capture). png_path is kept in case a historical row (from the
    # retired SERP snapshot/screenshot capture) remains, but it's ALWAYS
    # served the same way as HTML: it's third-party content (Google or
    # the captured site), never renderable under this origin.
    path = Path((row.get("html_path") or row.get("png_path")) or "")
    # Containment (review 3.2 H2): the path comes from the DB -- if a row
    # ever pointed outside the vault (corruption, future bug), never serve it.
    vault = evidence_dir(db).resolve()
    if not path.resolve().is_relative_to(vault):
        raise HTTPException(status_code=404, detail="Evidence not found")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Evidence file missing on disk")
    # octet-stream + attachment + nosniff (review 3.2 H1/H8): EVERYTHING this endpoint serves is third-party content (HTML
    # from a 3.2 site, or a 1.3 Google SERP HTML snapshot) --
    # never renderable under this origin.
    return FileResponse(str(path), media_type="application/octet-stream",
                        filename=path.name,
                        headers={"X-Content-Type-Options": "nosniff"})
