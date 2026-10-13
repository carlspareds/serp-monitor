"""Review (2.1): the one-at-a-time Review dialog (/ui/review) and the verdict
endpoints POST /review/{result_id}/verdict and /review/{result_id}/not-this-person.

Results with confidence < settings.review_threshold land in the queue
(review_status='pending', see db.save_results/reclassify_all). A verdict sets
classified_by='human', confidence=1.0 and is IMMUTABLE against bulk
reclassifications (reclassify_all excludes 'human'); every verdict is audited
(C24).

The dialog shows ONE result at a time ("N of M" + progress bar), four big
buttons (keyboard 1-4, wired in static/app.js via [data-review-keys] /
[data-key]) and Skip. A verdict is a fetch POST that answers with the next
item's fragment (auto-advance) and an X-SM-Toast; the fragment reports the
remaining count ([data-sm-review-count]) so the Review badges update. Without
JS the same forms work with Post/Redirect/Get.

`?id=` opens the dialog for one specific result ("Change label…" in the
Results row menu); its verdict redirects back to the page it came from.
`mode=chat` is the assistant's review card (static/app.js): the same audited
verdict, answered with JSON {ok, message, pending} instead of a page.
"""
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from dashboard import ui
from dashboard.context import (
    is_fragment_request,
    redirect_with_flash,
    safe_next,
    viewer,
)
from dashboard.deps import db
from dashboard.main import audit
from dashboard.ui_helpers import (
    _esc,
    _pill,
    fragment_or_page,
    overlay_url,
    toast_header,
)
from processing.severity import annotate_severity_for_result

router = APIRouter()

_QUEUE_LIMIT = 500
_VERDICTS = (("positive", "Positive", "1"), ("negative", "Negative", "2"), ("neutral", "Neutral", "3"))
_DONE_TEXT = {"positive": "Marked positive.", "negative": "Marked negative.",
              "neutral": "Marked neutral.", "not_this_person": "Marked as a different person."}


def _ids(raw: str) -> list[int]:
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out[:200]


def _int(raw, default: int = 0) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def _get_result(result_id: int) -> dict | None:
    subject_clause, subject_params = db._subject_clause()
    conn = db._connect()
    try:
        row = conn.execute(f"SELECT * FROM search_results WHERE id = ? AND {subject_clause}",  # noqa: S608 -- subject_clause is the constant 'subject_id = ?' (values bound)
                           (result_id, *subject_params)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _why(row: dict) -> str:
    from assistant.tools import explain_row

    text = explain_row(db, row)["why"]
    if row.get("same_person") == "unsure":
        text += " It may be about a different person with the same name."
    return text


def _card(row: dict) -> str:
    url = row.get("url") or ""
    title = _esc(row.get("title") or url or "Untitled")
    title_html = (f"<a class='rtitle' href='{_esc(url)}' target='_blank' rel='noopener noreferrer'>{title}</a>"
                  if url.startswith(("http://", "https://")) else f"<span class='rtitle'>{title}</span>")
    desc = f"<p class='rev-desc'>{_esc(row.get('description'))}</p>" if row.get("description") else ""
    country = (row.get("country_code") or "").upper()
    meta = " · ".join(x for x in (_esc(row.get("domain") or ""), _esc(country),
                                   f"position {_esc(row.get('position'))}" if row.get("position") else "") if x)
    return (
        "<div class='rev-card'>"
        f"{title_html}<div class='mono muted rev-url'>{_esc(url)}</div>{desc}"
        f"<div class='li-sub'>{meta} · now {_pill(row.get('classification') or 'neutral')}</div>"
        f"<div class='why'><b>Why it's here:</b> {_esc(_why(row))}</div></div>"
    )


def _buttons(row: dict, hidden: str, read_only: bool) -> str:
    rid = row["id"]
    dis = " disabled" if read_only else ""
    btns = "".join(
        f"<button type='submit' class='btn rev-btn' name='classification' value='{v}' data-key='{k}'{dis}"
        f"{' autofocus' if k == '1' and not read_only else ''}>"
        f"<span class='pill {v}'><span class='dot' aria-hidden='true'></span>{label}</span>"
        f"<span class='kbd' aria-hidden='true'>{k}</span></button>"
        for v, label, k in _VERDICTS)
    btns += (f"<button type='submit' class='btn rev-btn' formaction='/review/{rid}/not-this-person'"
             f" data-key='4'{dis}><b>Not this person</b><span class='kbd' aria-hidden='true'>4</span></button>")
    return (f"<form class='rev-form' action='/review/{rid}/verdict' method='post' data-sm-submit"
            f" data-review-keys>{hidden}<div class='rev-actions'>{btns}</div></form>")


def review_fragment(done: int = 0, total: int = 0, skip: list[int] | None = None,
                    reviewed: bool = False) -> str:
    """The queue dialog: the next pending result (skipped ones go last)."""
    skip = skip or []
    queue = db.pending_review(limit=_QUEUE_LIMIT)
    remaining = len(queue)
    count_marker = f"<span hidden data-sm-review-count='{db.count_pending_review()}'></span>"
    reload_marker = "<span hidden data-sm-reload-on-close></span>" if reviewed or done else ""
    if not queue:
        body = (f"{count_marker}{reload_marker}<div class='empty'><b>All caught up.</b><br>"
                "New uncertain results will appear here after the next search.</div>")
        foot = "<button type='button' class='btn primary' data-sm-close>Done</button>"
        return ui.overlay("Review", body, foot)
    queue.sort(key=lambda r: (r["id"] in skip, skip.index(r["id"]) if r["id"] in skip else 0))
    row = queue[0]
    total = max(total, done + remaining)
    n = done + 1
    pct = int(done * 100 / total) if total else 0
    state = {"done": done, "total": total, "skip": ",".join(str(i) for i in skip)}
    hidden = "".join(f"<input type='hidden' name='{k}' value='{_esc(v)}'>" for k, v in state.items())
    read_only = not viewer().can_write
    skip_ids = [i for i in skip if i != row["id"]] + [row["id"]]
    skip_url = "/ui/review?" + urlencode({"done": done, "total": total,
                                          "skip": ",".join(str(i) for i in skip_ids)})
    note = ("Your account is read-only: you can look but not label." if read_only else
            "Your choice is final and never overwritten by the rules or the AI.")
    body = (
        f"{count_marker}{reload_marker}"
        f"<div class='prog' role='progressbar' aria-label='Review progress' aria-valuemin='0'"
        f" aria-valuemax='{total}' aria-valuenow='{done}'><div style='width:{pct}%'></div></div>"
        f"{_card(row)}{_buttons(row, hidden, read_only)}"
    )
    skip_btn = (f"<a class='btn ghost' href='{_esc(skip_url)}' data-sm-nav>Skip</a>"
                if remaining > 1 else "")
    foot = f"<span class='note rev-note'>{note}</span>{skip_btn}"
    title = f"Review <span class='muted num rev-count'>{n} of {total}</span>"
    return ui.overlay("Review", body, foot, title_html=title)


def item_fragment(row: dict, back: str) -> str:
    """"Change label…" for one result (any review state)."""
    hidden = (f"<input type='hidden' name='mode' value='item'>"
              f"<input type='hidden' name='back' value='{_esc(back)}'>")
    read_only = not viewer().can_write
    who = {"human": "a person", "ai": "your AI provider"}.get(row.get("classified_by") or "", "the rules")
    body = (f"{_card(row)}<p class='note'>Currently labelled by {who}. Pick the right label:</p>"
            f"{_buttons(row, hidden, read_only)}")
    foot = ("<span class='note rev-note'>Your choice is final and never overwritten by the rules or "
            "the AI.</span><button type='button' class='btn ghost' data-sm-close>Cancel</button>")
    return ui.overlay("Change label", body, foot)


@router.get("/review")
async def review_page():
    """Old queue page: the Review dialog opens over Results."""
    return redirect_with_flash(overlay_url("/ui/review", "modal"))


@router.get("/ui/review", response_class=HTMLResponse, tags=["ui"])
async def ui_review(id: str = "", done: str = "", total: str = "", skip: str = "",
                    back: str = "/results"):
    if id.strip():
        row = _get_result(_int(id)) if id.strip().isdigit() else None
        if row is None:
            inner = ui.overlay("Change label", "<div class='empty'>That result isn't available.</div>")
            return fragment_or_page("Change label", inner, active="/results", status_code=404)
        return fragment_or_page("Change label", item_fragment(row, safe_next(back, "/results")),
                                active="/results")
    inner = review_fragment(_int(done), _int(total), _ids(skip))
    return fragment_or_page("Review", inner, active="/results")


def _answer(message: str, mode: str, back: str, done: int, total: int, skip: str):
    """After a verdict: item mode goes back to its page; the queue answers a
    fetch submit with the next item (auto-advance) and a plain form with PRG."""
    if mode == "chat":
        return JSONResponse({"ok": True, "message": message, "pending": db.count_pending_review()},
                            headers={"Cache-Control": "no-store"})
    if mode == "item":
        return redirect_with_flash(safe_next(back, "/results"), message)
    if is_fragment_request():
        inner = review_fragment(done + 1, total, _ids(skip), reviewed=True)
        return HTMLResponse(inner, headers=toast_header(message))
    return redirect_with_flash(overlay_url("/ui/review", "modal"), message)


def _missing(mode: str, back: str):
    msg = "That result isn't available any more."
    if mode == "chat":
        return JSONResponse({"ok": False, "message": msg}, status_code=404)
    if mode != "item" and is_fragment_request():
        return HTMLResponse(review_fragment(), status_code=404, headers=toast_header(msg))
    raise HTTPException(status_code=404, detail="Result not found")


@router.post("/review/{result_id}/verdict", response_class=HTMLResponse)
async def review_verdict(
    request: Request,
    result_id: int,
    classification: Literal["positive", "negative", "neutral"] = Form(...),
    done: str = Form("0", max_length=64), total: str = Form("0", max_length=64), skip: str = Form("", max_length=64),
    mode: str = Form("", max_length=64), back: str = Form("/results", max_length=2048),
):
    updated = db.set_review_verdict(result_id, classification)
    if updated is None:
        return _missing(mode, back)
    audit(request, "review_verdict", resource_type="url", resource_id=updated["url"],
          verdict=classification, previous=updated["previous_classification"],
          result=updated["review_status"])
    # F6 (review 2.2): a human correction to 'negative' doesn't go through any
    # run chokepoint -- score its severity now (best-effort; no AI -> NULL).
    if classification == "negative":
        annotate_severity_for_result(db, result_id)
    message = (f"Label changed to {classification}." if mode in ("item", "chat")
               else _DONE_TEXT[classification])
    return _answer(message, mode, back, _int(done), _int(total), skip)


@router.post("/review/{result_id}/not-this-person", response_class=HTMLResponse)
async def review_not_this_person(request: Request, result_id: int,
                                 done: str = Form("0", max_length=64), total: str = Form("0", max_length=64), skip: str = Form("", max_length=64),
                                 mode: str = Form("", max_length=64), back: str = Form("/results", max_length=2048)):
    """"Not this person" (2.3): same_person='no' -- a HUMAN identity verdict,
    immutable against automatic re-annotation (set_same_person_verdict)."""
    url = db.set_same_person_verdict(result_id)
    if url is None:
        return _missing(mode, back)
    audit(request, "review_same_person", resource_type="url", resource_id=url, verdict="no")
    return _answer(_DONE_TEXT["not_this_person"], mode, back, _int(done), _int(total), skip)
