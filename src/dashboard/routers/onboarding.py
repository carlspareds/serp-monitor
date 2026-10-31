"""Public Getting started guide (/onboarding).

Guided setup for new users and operators: Demo Mode vs Live Scraping path
selection, a system checklist, and a workspace feature guide.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from markupsafe import Markup

from dashboard import nav
from dashboard.deps import db, templates
from dashboard.main import (
    COUNTRIES,
    DEFAULT_COUNTRIES,
    is_demo_mode,
)
from dashboard.session import has_live_access
from notify import channel_status, routed_severities
from processing import ai_classifier

_PROVIDER_LABELS = {"gemini": "Gemini", "openai": "OpenAI-compatible"}

# Page icons for /onboarding: the nav.py stroke set plus a few
# page-specific ones, drawn the same way (24x24 grid, currentColor stroke).
_PAGE_ICONS = {
    **nav.ICONS,
    "compass": "<circle cx='12' cy='12' r='8.5'/><path d='M15.5 8.5 13.5 13.5 8.5 15.5 10.5 10.5z'/>",
    "check": "<circle cx='12' cy='12' r='8.5'/><path d='m8.5 12.2 2.4 2.4 4.8-5'/>",
    "target": "<circle cx='12' cy='12' r='8.5'/><circle cx='12' cy='12' r='5'/><circle cx='12' cy='12' r='1.5'/>",
    "globe": "<circle cx='12' cy='12' r='8.5'/><path d='M3.5 12h17'/>"
             "<path d='M12 3.5c2.4 2.6 3.5 5.4 3.5 8.5s-1.1 5.9-3.5 8.5c-2.4-2.6-3.5-5.4-3.5-8.5s1.1-5.9 3.5-8.5z'/>",
    "sparkle": "<path d='M12 4l1.8 4.7 4.7 1.8-4.7 1.8L12 17l-1.8-4.7-4.7-1.8 4.7-1.8z'/><path d='M18.5 15.5v4M16.5 17.5h4'/>",
    "chart": "<path d='M4 20h16'/><path d='M7 16v-4M12 16V7M17 16v-6'/>",
    "trend": "<path d='m4 17 5-5 3.5 3.5L20 8'/><path d='M15 8h5v5'/>",
    "compare": "<path d='M7.5 4v13'/><path d='m4.5 14 3 3 3-3'/><path d='M16.5 20V7'/><path d='m13.5 10 3-3 3 3'/>",
    "scale": "<path d='M12 4v16M8 20h8M5 7.5h14'/><path d='m5 7.5-2.5 6a2.5 2.5 0 0 0 5 0z'/>"
             "<path d='m19 7.5-2.5 6a2.5 2.5 0 0 0 5 0z'/>",
    "camera": "<rect x='3.5' y='7' width='17' height='12.5' rx='2'/><path d='m9 7 1.5-2.5h3L15 7'/>"
              "<circle cx='12' cy='13' r='3'/>",
    "calendar": "<rect x='4' y='5.5' width='16' height='14.5' rx='2'/><path d='M4 10h16M8.5 3.5v4M15.5 3.5v4'/>",
    "flask": "<path d='M9.5 3.5h5M10.5 3.5v6L5 18.5a1.4 1.4 0 0 0 1.2 2h11.6a1.4 1.4 0 0 0 1.2-2l-5.5-9v-6'/>"
             "<path d='M7.5 15h9'/>",
    "bolt": "<path d='M13 3.5 5.5 13.5H12l-1 7 7.5-10H12z'/>",
}


def _page_icon(name: str, size: int = 18) -> Markup:
    """Same markup pattern as nav.icon() (decorative, aria-hidden), with an
    explicit size because these pages have no per-context .ico CSS rule."""
    return Markup(
        f"<svg class='ico' width='{size}' height='{size}' viewBox='0 0 24 24' fill='none'"
        " stroke='currentColor' stroke-width='1.75' stroke-linecap='round' stroke-linejoin='round'"
        " aria-hidden='true' focusable='false' style='vertical-align:-0.2em;flex-shrink:0'>"
        f"{_PAGE_ICONS[name]}</svg>")


templates.env.globals["page_icon"] = _page_icon

router = APIRouter()

# What an anonymous visitor sees when auth is on: the same page, with
# generic placeholders instead of the workspace's live numbers.
_PUBLIC_STATS = {"total_results": 0, "total_runs": 0, "countries": 0, "last_run": None,
                 "by_class": {"positive": 0, "negative": 0, "neutral": 0}}
_PLACEHOLDER_QUERY = "Your Brand or Name"


@router.get("/onboarding", response_class=HTMLResponse)
async def onboarding_page(request: Request):
    if not has_live_access(request):
        return templates.TemplateResponse(request, "onboarding.html", {
            "title": "Getting started guide · SERP Monitor",
            "stats": dict(_PUBLIC_STATS),
            "tracked_query": _PLACEHOLDER_QUERY,
            "tracked_countries": DEFAULT_COUNTRIES.upper(),
            "has_apify": False,
            "has_ai": False,
            "ai_provider": None,
            "has_alerts": False,
            "channels_configured": [],
            "routed": [],
            "has_runs": False,
            "runs_count": 0,
            "is_demo": True,
            "countries": COUNTRIES,
            "live_data": False,
        })
    stats = db.get_stats()
    # The ACTIVE subject's tracking config (multi-subject phase 2), not the
    # legacy report_query/report_countries settings keys.
    tracked_query = db.get_tracked_query() or _PLACEHOLDER_QUERY
    tracked_countries = ",".join(db.get_tracked_countries()) or DEFAULT_COUNTRIES
    from serp.registry import active_provider_name
    has_apify = bool(active_provider_name())    # any SERP provider (issue #3)
    # Same "configured" checks as Settings, the setup wizard and the senders.
    has_ai = ai_classifier.is_configured()
    info = ai_classifier.provider_info()
    ai_provider = ({"label": _PROVIDER_LABELS.get(info["provider"], info["provider"]),
                    "model": info["model"], "host": info["base_url"]} if has_ai else None)
    channels = [name for name, ok in channel_status(db).items() if ok]
    routed = routed_severities(db)
    # "Configured" = at least one severity is routed to a channel that can deliver.
    has_alerts = bool(routed)
    has_runs = bool(db.list_runs(limit=1))
    runs_count = stats.get("total_runs", 0)

    return templates.TemplateResponse(request, "onboarding.html", {
        "title": "Getting started guide · SERP Monitor",
        "stats": stats,
        "tracked_query": tracked_query,
        "tracked_countries": tracked_countries.upper(),
        "has_apify": has_apify,
        "has_ai": has_ai,
        "ai_provider": ai_provider,
        "has_alerts": has_alerts,
        "channels_configured": channels,
        "routed": routed,
        "has_runs": has_runs,
        "runs_count": runs_count,
        "is_demo": is_demo_mode(),
        "countries": COUNTRIES,
        "live_data": True,
    })

