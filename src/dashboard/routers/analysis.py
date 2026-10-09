"""/analysis folded into the Overview (/): trend, share of voice, Wins and
"what changed" now live there. Old links and bookmarks redirect, keeping the
selected country."""
from urllib.parse import urlencode

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/analysis")
async def analysis_page(country: str = ""):
    query = f"?{urlencode({'country': country})}" if country else ""
    return RedirectResponse(f"/{query}", status_code=307)
