"""UI pieces shared by legacy pages (f-strings) and Jinja templates
(UX-2). HTML only: no DB, no request. Dialog ids are opened from
`static/app.js` with `data-dialog`, and the server can ask for one to
appear open on load with `data-open-dialog` in <body> (page() / base.html).

`overlay()` is the newer pattern: the content of a modal / side panel that a
fragment route returns and `SM.openModal` / `SM.openPanel` load (see
docs/ui-architecture.md).
"""
import html

_SVG = ("<svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.75'"
        " stroke-linecap='round' stroke-linejoin='round' aria-hidden='true' focusable='false'>")
_X_ICON = _SVG + "<path d='M6 6l12 12M18 6 6 18'/></svg>"
_PLUG_ICON = (_SVG + "<path d='M9 3.5v4M15 3.5v4'/><path d='M6.5 7.5h11v3a5.5 5.5 0 0 1-11 0z'/>"
              "<path d='M12 16v4.5'/></svg>")


def _e(text: str) -> str:
    return html.escape(str(text), quote=True)


def dialog(dialog_id: str, title: str, body_html: str) -> str:
    """<dialog> with header, close button, and body. `body_html` is
    already-safe HTML (the existing form, moved as-is)."""
    return (
        f"<dialog id='{_e(dialog_id)}'>"
        "<div class='modal-card'>"
        "<div class='modal-head'>"
        f"<h3>{_e(title)}</h3>"
        "<button type='button' class='modal-close' data-close aria-label='Close'>"
        f"{_X_ICON}</button>"
        "</div>"
        f"<div class='modal-body'>{body_html}</div>"
        "</div></dialog>"
    )


def overlay(title: str, body_html: str, foot_html: str = "", title_html: str | None = None) -> str:
    """Content of an SM overlay (modal / side panel) returned by a fragment
    route: header with the title and a close button, scrollable body, optional
    footer. `body_html`/`foot_html` are already-safe HTML; `title` is escaped
    unless `title_html` is given. static/app.js labels the dialog from the
    [data-sm-title] heading."""
    heading = title_html if title_html is not None else _e(title)
    foot = f"<div class='ov-foot'>{foot_html}</div>" if foot_html else ""
    return (
        "<div class='ov-head'>"
        f"<h2 data-sm-title>{heading}</h2>"
        "<button type='button' class='iconbtn' data-sm-close aria-label='Close'>"
        f"{_X_ICON}</button></div>"
        f"<div class='ov-body'>{body_html}</div>{foot}"
    )


def opener(dialog_id: str, label: str, ghost: bool = False) -> str:
    cls = "btn-sm btn-ghost" if ghost else "btn-sm"
    prefix = "" if ghost else "＋ "
    return (f"<button type='button' class='{cls}' data-dialog='{_e(dialog_id)}'>"
            f"{prefix}{_e(label)}</button>")


def empty_state(name: str, what: str, action_html: str) -> str:
    """Card for a panel whose integration isn't configured (spec §5.4)."""
    return (
        "<div class='empty-state'>"
        f"<div class='empty-icon' aria-hidden='true'>{_PLUG_ICON}</div>"
        f"<p>Connect {_e(name)} to see {_e(what)} here.</p>"
        f"<div class='empty-action'>{action_html}</div>"
        "</div>"
    )
