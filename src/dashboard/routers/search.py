"""Global text search router: GET /search-text.

Item 3.3 (FTS5). The global search box in the topbar (see dashboard.nav /
dashboard.main.shell) submits here; groups results by classification
(positive/negative/neutral), same colors/pills as the rest of the
dashboard. See brief 3.3.
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from dashboard.deps import db, templates
from dashboard.main import shell

router = APIRouter()

_GROUPS = ["positive", "negative", "neutral"]


@router.get("/search-text", response_class=HTMLResponse)
async def search_text_page(request: Request, q: str = ""):
    out = db.search_text(q.strip())
    grouped = {g: [] for g in _GROUPS}
    for r in out["results"]:
        grouped.setdefault(r.get("classification") or "neutral", [])
        grouped[r.get("classification") or "neutral"].append(r)

    return templates.TemplateResponse(request, "search.html", {
        "title": "Find in stored results",
        "shell": shell("/search-text"),
        "q": q,
        "error": out["error"],
        "groups": [(g, grouped[g]) for g in _GROUPS],
        "total": len(out["results"]),
    })
