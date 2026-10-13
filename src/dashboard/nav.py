"""App shell navigation: top bar, left rail (bottom tab bar on mobile) and the
account menu.

Three destinations only -- Overview, Results, Settings. Everything else
(search, review, compare, reports, domains, timeline, evidence...) opens as a
modal or side panel over the current page (static/app.js, `SM.*`), so the rail
never grows past three items. Older full pages that still exist (/runs,
/notes, /diff...) highlight the destination they belong to (`resolve`).

This module never touches the DB or the request: `dashboard.ui_helpers.shell()`
computes the counts and the active project/subject and passes them in, so the
markup can be tested in isolation.
"""
from __future__ import annotations

import html
from dataclasses import dataclass


@dataclass(frozen=True)
class NavItem:
    key: str
    label: str
    path: str
    icon: str
    children: tuple[str, ...] = ()   # path prefixes that highlight this item


NAV: tuple[NavItem, ...] = (
    NavItem("overview", "Overview", "/", "overview", ("/analysis",)),
    NavItem("results", "Results", "/results",
            "results", ("/search_page", "/search_multi", "/search-text", "/job/", "/run/",
                        "/runs", "/diff", "/review", "/domains", "/notes", "/timeline",
                        "/evidence", "/export")),
    NavItem("settings", "Settings", "/settings", "settings",
            ("/settings/", "/users", "/audit", "/setup")),
)

HELP_PATH = "/onboarding"

# Stroke icons on a 24x24 grid, drawn with currentColor (sized by CSS).
# Keys used by the onboarding page (alerts, reports, search, settings, theme...)
# must stay.
ICONS: dict[str, str] = {
    "home": "<path d='M3.5 10.5 12 3.5l8.5 7'/><path d='M5.5 9v10.5a1 1 0 0 0 1 1H10v-6h4v6h3.5a1 1 0 0 0 1-1V9'/>",
    "overview": "<path d='M4 20V10M10 20V4M16 20v-7M22 20H2'/>",
    "results": "<path d='M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01'/>",
    "serp": "<path d='M4 6h11M4 11h6M4 16h5'/><circle cx='15.5' cy='14.5' r='3.5'/><path d='m18 17 2.5 2.5'/>",
    "workspace": "<rect x='4.5' y='3.5' width='15' height='17' rx='2'/><path d='M8.5 8h7M8.5 12h7M8.5 16h4'/>",
    "alerts": "<path d='M6 8a6 6 0 1 1 12 0c0 7 3 8 3 8H3s3-1 3-8'/><path d='M10.3 21a1.9 1.9 0 0 0 3.4 0'/>",
    "reports": "<path d='M14 3.5H7.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8z'/><path d='M14 3.5V8h4.5'/>"
               "<path d='M9 17v-2.5M12 17v-5M15 17v-3.5'/>",
    "settings": "<path d='M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12'/><circle cx='16' cy='6' r='2'/>"
                "<circle cx='10' cy='12' r='2'/><circle cx='18' cy='18' r='2'/>",
    "help": "<circle cx='12' cy='12' r='8.5'/><path d='M9.6 9.4a2.5 2.5 0 1 1 3.4 2.4c-.6.3-1 .8-1 1.5v.4'/>"
            "<path d='M12 16.8v.2'/>",
    "theme": "<circle cx='12' cy='12' r='8.5'/><path d='M12 3.5a8.5 8.5 0 0 1 0 17z' fill='currentColor' stroke='none'/>",
    "logout": "<path d='M14 4.5h3.5a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H14'/><path d='m9.5 8-4 4 4 4'/><path d='M5.5 12H15'/>",
    "menu": "<path d='M4 7h16M4 12h16M4 17h16'/>",
    "search": "<circle cx='11' cy='11' r='7'/><path d='M20 20l-3.5-3.5'/>",
    "user": "<circle cx='12' cy='8' r='4'/><path d='M4 21c0-4 4-6 8-6s8 2 8 6'/>",
    "spark": "<path d='M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6'/>",
    "chev": "<path d='M6 9l6 6 6-6'/>",
    "x": "<path d='M6 6l12 12M18 6L6 18'/>",
    "plus": "<path d='M12 5v14M5 12h14'/>",
    "check": "<path d='m5 12.5 4.5 4.5L19 7.5'/>",
    "dots": "<circle cx='5' cy='12' r='1.2'/><circle cx='12' cy='12' r='1.2'/><circle cx='19' cy='12' r='1.2'/>",
}

_LOGO = ("<svg viewBox='0 0 24 24' width='16' height='16' fill='none' stroke='currentColor'"
         " stroke-width='2.2' stroke-linecap='round' aria-hidden='true' focusable='false'>"
         "<path d='M3 17l6-6 4 4 8-8'/><path d='M15 7h6v6'/></svg>")


def icon(name: str, cls: str = "ico") -> str:
    """Inline stroke SVG, decorative (the text label next to it carries meaning)."""
    return (f"<svg class='{cls}' viewBox='0 0 24 24' fill='none' stroke='currentColor'"
            " stroke-width='1.8' stroke-linecap='round' stroke-linejoin='round'"
            f" aria-hidden='true' focusable='false'>{ICONS[name]}</svg>")


def _e(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def all_paths() -> list[str]:
    return [item.path for item in NAV]


def resolve(path: str) -> NavItem | None:
    """The rail item for `path`: exact match first, then the longest child
    prefix. Unknown paths (the wizard, help, error pages) highlight nothing."""
    for item in NAV:
        if item.path == path:
            return item
    best: tuple[int, NavItem] | None = None
    for item in NAV:
        for prefix in item.children:
            if path.startswith(prefix) and (best is None or len(prefix) > best[0]):
                best = (len(prefix), item)
    return best[1] if best else None


def render_rail(active_path: str) -> str:
    """Left rail on desktop, bottom tab bar on phones (same markup, CSS)."""
    current = resolve(active_path)
    links = []
    for item in NAV:
        aria = " aria-current='page'" if current is not None and current.key == item.key else ""
        links.append(f"<a href='{item.path}'{aria}>{icon(item.icon)}<span>{_e(item.label)}</span></a>")
    return f"<nav class='rail' id='rail' aria-label='Main'>{''.join(links)}</nav>"


def render_topbar(project: str = "", subject: str = "", alerts: int = 0,
                  username: str = "", auth: bool = False, switcher: bool = True) -> str:
    """Brand, Project ▸ Subject switcher, stored-results search, Assistant,
    background-searches chip (render_jobs_chip), alerts bell and account
    menu. The popovers' contents are server-rendered
    fragments loaded on demand (/ui/switcher, /ui/alerts); the account menu is
    a <template> in the page (no request needed)."""
    bell_badge = f"<span class='dotbadge' data-alerts-count>{alerts}</span>" if alerts else ""
    bell_label = f"Alerts, {alerts} open" if alerts else "Alerts"
    subject_label = subject or "Unnamed subject"
    sw = ""
    if switcher:
        proj = f"<span class='proj'>{_e(project)} ▸</span>" if project else ""
        sw = ("<button type='button' class='switcher' id='sw' aria-haspopup='menu'"
              " aria-expanded='false' data-popover='/ui/switcher'"
              f" aria-label='Switch subject (current: {_e(subject_label)})'>"
              f"{proj}<span class='subj'><b>{_e(subject_label)}</b></span>{icon('chev', 'ico chev')}</button>")
    return (
        "<header class='top' id='top'>"
        f"<a class='brand' href='/' aria-label='SERP Monitor, Overview'><span class='logo'>{_LOGO}</span>"
        "<span class='name'>SERP Monitor</span></a>"
        f"{sw}"
        "<form class='tsearch' action='/search-text' method='get' role='search'>"
        f"{icon('search')}<input type='search' name='q' placeholder='Find in stored results'"
        " aria-label='Find in stored results'></form>"
        "<button type='button' class='ai-btn' id='aiBtn' data-assistant-toggle data-panel='/assistant/panel'"
        " data-panel-target='assistant-panel' aria-controls='assistant-panel' aria-expanded='false'"
        " title='Assistant (/ or Ctrl+K)'>"
        f"{icon('spark')}<span class='lbl'>Assistant</span></button>"
        f"{render_jobs_chip()}"
        "<button type='button' class='iconbtn' id='bell' aria-haspopup='dialog' aria-expanded='false'"
        f" data-popover='/ui/alerts' data-popover-class='alerts-pop' aria-label='{bell_label}'>"
        f"{icon('alerts')}{bell_badge}</button>"
        "<button type='button' class='iconbtn' id='acct' aria-haspopup='menu' aria-expanded='false'"
        f" data-popover='#acct-menu' aria-label='Account'>{icon('user')}</button>"
        "</header>"
        f"{render_account_menu(username, auth)}"
    )


def render_jobs_chip() -> str:
    """Background searches chip, next to the bell. Hidden until app.js
    (SM.jobs) finds running or finished-unseen searches in GET /jobs/active:
    "Searching… 0:42" (spinner) / "2 searches running" / "Search finished"
    (dot) / "Search failed" (danger). One job: opens its panel (/ui/job/{id});
    several: a popover listing them. The visible text ticks every second, so
    state changes are announced by the separate polite live region."""
    return ("<button type='button' class='jobchip' id='jobchip' data-jobs-chip hidden"
            " aria-haspopup='dialog' aria-expanded='false' aria-describedby='jobchip-live'>"
            "<span class='jobchip-spin' aria-hidden='true'></span>"
            "<span class='jobchip-dot' aria-hidden='true'></span>"
            "<span class='jobchip-text' data-jobs-chip-text>Searching…</span>"
            "<span class='jobchip-short' aria-hidden='true' data-jobs-chip-short></span></button>"
            "<span class='sr-only' id='jobchip-live' role='status' aria-live='polite'"
            " data-jobs-chip-live></span>")


def render_account_menu(username: str = "", auth: bool = False) -> str:
    """<template> cloned by SM.popover for the account button."""
    head = f"<div class='hd'>{_e(username)}</div>" if username else ""
    sign_out = (f"<hr><a class='item' role='menuitem' href='/logout'>{icon('logout')}Sign out</a>"
                if auth else "")
    return (
        "<template id='acct-menu'>"
        f"{head}"
        f"<a class='item' role='menuitem' href='/settings'>{icon('settings')}Settings</a>"
        "<button type='button' class='item' role='menuitem' data-theme-toggle>"
        f"{icon('theme')}Switch theme</button>"
        f"<a class='item' role='menuitem' href='{HELP_PATH}'>{icon('help')}Help</a>"
        f"{sign_out}"
        "</template>"
    )


__all__ = [
    "HELP_PATH",
    "ICONS",
    "NAV",
    "NavItem",
    "all_paths",
    "icon",
    "render_account_menu",
    "render_jobs_chip",
    "render_rail",
    "render_topbar",
    "resolve",
]
