"""
One-page executive PDF
================================
Generates a clean PDF with: header (optionally with brand logo),
human-edited executive narrative (5.1), KPIs, executive summary
(optional AI, on-request), per-country table, (4.5) timeline actions with
their measured impact, and a brand footer.
Meant to be attached to the email or printed.
"""

import html
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, SimpleDocTemplate, Spacer, TableStyle
from reportlab.platypus import Paragraph as _RLParagraph
from reportlab.platypus import Table as _RLTable

from export.excel_exporter import report_label, subject_title
from processing.event_impact import DEFAULT_PDF_WINDOW, impactable_events, impacts_at

# The PDF uses ReportLab's built-in Helvetica (WinAnsi / cp1252 encoding): a
# character outside it prints as a black box or nothing (live test: the
# "Project ▸ Subject" header lost its separator). Every text that reaches the
# PDF goes through pdf_safe: known symbols get a supported look-alike (› is in
# cp1252, like – — … · •), anything else is decomposed (é stays é) or replaced.
_PDF_GLYPHS = {"▸": "›", "▶": "›", "►": "›", "→": "›", "⟶": "›", "◂": "‹", "◀": "‹", "←": "‹",
               "≠": "!=", "≤": "<=", "≥": ">=", "✓": "OK", "✔": "OK", "✗": "x", "✕": "x",
               "−": "-", "‐": "-", "‑": "-", "\u00a0": " ", "\u202f": " ", "\u2009": " "}


def pdf_safe(text) -> str:
    """`text` with only characters Helvetica (cp1252) can draw."""
    if not isinstance(text, str):
        return text
    out = []
    for ch in text:
        ch = _PDF_GLYPHS.get(ch, ch)
        try:
            ch.encode("cp1252")
            out.append(ch)
            continue
        except UnicodeEncodeError:
            pass
        plain = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        try:
            plain.encode("cp1252")
            out.append(plain)
        except UnicodeEncodeError:
            out.append("?")
    return "".join(out)


def Paragraph(text, style, *args, **kwargs):
    return _RLParagraph(pdf_safe(text), style, *args, **kwargs)


def Table(data, *args, **kwargs):
    rows = [[pdf_safe(c) if isinstance(c, str) else c for c in row] for row in data]
    return _RLTable(rows, *args, **kwargs)

# 4.5 (brief §4.5, "the executive PDF gains an 'Actions & impact' section"):
# how many recent events are listed -- list_events() already comes sorted
# DESC by event_date/id, so [:_PDF_EVENTS_LIMIT] are the last N.
# Only DEFAULT_PDF_WINDOW (processing.event_impact, 30 days) -- the section
# is a one-line executive summary per event, not the 3 full windows
# (that already lives in /timeline).
_PDF_EVENTS_LIMIT = 5

# Defaults IDENTICAL to Database._BRANDING_DEFAULTS (5.1) -- without a
# `branding` row (first boot), the PDF comes out the same as before this
# feature.
_DARK = colors.HexColor("#14233F")
_BLUE = colors.HexColor("#1F4E9C")
_RED = colors.HexColor("#C62828")

# Fixed brand logo height (5.1) -- the width scales keeping the original
# aspect ratio (see _logo_flowable). Fits comfortably in the header
# without pushing the title too far down.
_LOGO_HEIGHT = 14 * mm
# Repo root, to resolve logo_path (relative, e.g.
# "data/branding/logo.png" -- see Database.set_branding) to an absolute path.
_project_root = Path(__file__).resolve().parent.parent.parent


def _logo_flowable(logo_path: str):
    """Image flowable of the logo scaled to _LOGO_HEIGHT, or None if the
    file does not exist / is not a readable image (never breaks the PDF --
    degrades to "no logo", same as today with no branding configured)."""
    if not logo_path:
        return None
    full = _project_root / logo_path
    if not full.is_file():
        return None
    try:
        iw, ih = ImageReader(str(full)).getSize()
        if not iw or not ih:
            return None
        width = _LOGO_HEIGHT * iw / ih
        return Image(str(full), width=width, height=_LOGO_HEIGHT)
    except Exception:  # noqa: BLE001 -- corrupt file / unsupported by PIL
        return None


def _styles(branding: dict) -> dict:
    """reportlab styles for build_pdf (8.3) -- a single place that knows how to read
    branding['primary_color']/['secondary_color'] and produce the derived
    ParagraphStyle/color objects."""
    base = getSampleStyleSheet()
    dark = colors.HexColor(branding["primary_color"])
    return {
        "dark": dark,
        "h1": ParagraphStyle("h1", parent=base["Title"], textColor=dark, fontSize=18, spaceAfter=2),
        "sub": ParagraphStyle("sub", parent=base["Normal"], textColor=colors.grey, fontSize=9),
        # secondary_color with a real consumer (review 5.1 S3).
        "h2": ParagraphStyle("h2", parent=base["Heading2"],
                             textColor=colors.HexColor(branding["secondary_color"]), fontSize=12),
        "body": ParagraphStyle("body", parent=base["Normal"], fontSize=10, leading=14),
        "footer": ParagraphStyle("footer", parent=base["Normal"], textColor=colors.grey,
                                 fontSize=8, alignment=1),
        "cell": ParagraphStyle("cell", parent=base["Normal"], fontSize=9),
    }


def _country_latest_and_totals(db, countries=None) -> tuple[list[dict], dict, int]:
    """(latest, tot, pct): the most recent run of each country (fixed
    order, 'global' first) + the aggregated totals + global % positive --
    the "current state" snapshot that feeds the cover KPI and the
    "Detail by country" table of build_pdf."""
    latest = db.latest_run_per_country()
    if countries:
        wanted = {c.lower() for c in countries}
        latest = [r for r in latest if (r.get("country_code") or "").lower() in wanted]
    order = ["global", "ca", "us", "hk", "gb", "fr", "ch", "de", "it", "nl"]
    latest.sort(key=lambda r: order.index((r.get("country_code") or "").lower())
                if (r.get("country_code") or "").lower() in order else 99)

    tot = {"positive": 0, "negative": 0, "neutral": 0, "total": 0}
    for r in latest:
        tot["positive"] += r.get("positive") or 0
        tot["negative"] += r.get("negative") or 0
        tot["neutral"] += r.get("neutral") or 0
        tot["total"] += r.get("total_results") or 0
    pct = round(100 * tot["positive"] / tot["total"]) if tot["total"] else 0
    return latest, tot, pct


def _kpi_table(tot: dict, pct: int, n_countries: int, dark) -> Table:
    """Aggregated KPI table for the cover (positive/negative/neutral/
    %positive/countries)."""
    kpi = [["Positive", "Negative", "Neutral", "% Positive", "Countries"],
           [tot["positive"], tot["negative"], tot["neutral"], f"{pct}%", n_countries]]
    kt = Table(kpi, colWidths=[34 * mm] * 5)
    kt.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), dark),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 1), (-1, 1), 16),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 1), (0, 1), _BLUE),
        ("TEXTCOLOR", (1, 1), (1, 1), _RED),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 1), (-1, 1), 8),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E3E7EE")),
    ]))
    return kt


def basic_table(rows: list, col_widths: list, dark, *, bold_last_row: bool = False) -> Table:
    """TableStyle shared by the content tables of the PDF (build_pdf:
    "Detail by country", "Actions & impact") -- dark header + light grid +
    alternating rows + columns (except the 1st, usually the label)
    centered.

    `bold_last_row`: bolds the last row (e.g. a totals/delta row)."""
    t = Table(rows, colWidths=col_widths)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), dark),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (1, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E3E7EE")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FAFBFD")]),
    ]
    if bold_last_row:
        style.append(("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"))
    t.setStyle(TableStyle(style))
    return t



def _cover_elements(db, branding: dict, sty: dict, *, title: str, subtitle: str,
                    narrative_override: str | None, countries=None) -> tuple[list, list[dict]]:
    """Cover of build_pdf (8.3): brand logo (5.1) + title + narrative ALREADY
    hand-edited (5.1) + KPIs from the MOST RECENT snapshot ("today's
    current state").

    Returns (elements, latest) -- latest is used by build_pdf for the
    "Detail by country" table."""
    el = []
    logo = _logo_flowable(branding["logo_path"])
    if logo is not None:
        el.append(logo)
        el.append(Spacer(1, 4 * mm))
    el.append(Paragraph(title, sty["h1"]))
    el.append(Paragraph(subtitle, sty["sub"]))
    el.append(Spacer(1, 8 * mm))

    narrative = (db.get_setting("report_narrative", "") if narrative_override is None
                else narrative_override)
    if narrative:
        el.append(Paragraph("Executive narrative", sty["h2"]))
        el.append(Paragraph(html.escape(narrative).replace("\n", "<br/>"), sty["body"]))
        saved_at = db.get_setting("report_narrative_saved_at", "")
        if narrative_override is None and saved_at:
            el.append(Paragraph(f"Narrative figures as of {html.escape(saved_at)} UTC", sty["sub"]))
        el.append(Spacer(1, 6 * mm))

    latest, tot, pct = _country_latest_and_totals(db, countries)
    el.append(_kpi_table(tot, pct, len(latest), sty["dark"]))
    el.append(Spacer(1, 8 * mm))
    return el, latest


def _impact_text(imp: dict, window: int) -> str:
    """One-line text for the `window`-day delta of ONE event -- used by
    build_pdf ("Actions & impact").
    "insufficient data" if either half of the window has no data (see
    processing.event_impact); partial coverage noted as "(n/window d)" if
    the 'after' segment has not yet completed the window."""
    if imp["insufficient"]:
        return "insufficient data"

    def _f(v):
        return f"{v:+.1f}" if v is not None else "—"

    neg = imp["pct_negative"]["delta"]
    text = (
        f"%pos {_f(imp['pct_positive']['delta'])} · "
        f"%neg {_f(-neg if neg is not None else None)} · "
        f"avg pos {_f(imp['neg_avg_position']['delta'])}"
    )
    if imp["n_after"] < window:
        text += f" ({imp['n_after']}/{window}d)"
    return text


def _period_elements(period: dict, sty: dict) -> list:
    """"This period" section (Reports dialog): new negative results, Wins and
    open alerts within the chosen period. `period` = {"label", "new_negatives":
    [{domain, country, first_seen}], "wins": [{domain, country, gone_since}],
    "open_alerts": int}."""
    h2, body, cell, dark = sty["h2"], sty["body"], sty["cell"], sty["dark"]
    out = [Paragraph(f"This period: {html.escape(period.get('label') or '')}", h2)]
    negs, wins = period.get("new_negatives") or [], period.get("wins") or []
    n_alerts = int(period.get("open_alerts") or 0)
    n_acked = int(period.get("acknowledged_alerts") or 0)
    out.append(Paragraph(
        f"{len(negs)} new negative result{'s' if len(negs) != 1 else ''} · "
        f"{len(wins)} Win{'s' if len(wins) != 1 else ''} (negative results that dropped out) · "
        f"Alerts: {n_alerts} open, {n_acked} acknowledged.", body))
    rows = [["Change", "Result", "Country", "Date"]]
    for n in negs[:12]:
        rows.append(["New negative", Paragraph(html.escape(n.get("domain") or ""), cell),
                     (n.get("country") or "").upper(), (n.get("first_seen") or "")[:10]])
    for w in wins[:12]:
        rows.append(["Win", Paragraph(html.escape(w.get("domain") or ""), cell),
                     (w.get("country") or "").upper(), (w.get("gone_since") or "")[:10]])
    if len(rows) > 1:
        out.append(Spacer(1, 3 * mm))
        out.append(basic_table(rows, [30 * mm, 90 * mm, 22 * mm, 32 * mm], dark))
    out.append(Spacer(1, 6 * mm))
    return out


_TRUNCATION_MARKER = "_(truncated: hit the output token limit)_"


def clean_ai_summary(text: str) -> str:
    """The AI summary as printed: stripped, without the legacy truncation
    marker; '' when nothing real is left."""
    return (text or "").replace(_TRUNCATION_MARKER, "").strip()


def build_pdf(db, output_path: str, ai_summary: str = "", narrative_override: str | None = None,
              period: dict | None = None, subject_name: str | None = None,
              countries=None, ai_summary_note: str = "") -> str:
    """narrative_override (5.1): explicit text to render on the cover
    instead of the `report_narrative` setting -- used by the tests and by
    any caller that wants to preview a narrative without persisting it
    first. None (default) -> reads the persisted `report_narrative`
    setting (db.get_setting/set_setting) as-is, whatever last wrote it.

    design (intentional deviation from spec §5.1, review S8):
    narrative_override is deliberately NOT exposed in POST /export/pdf --
    a caller that wants a specific narrative on the PDF must persist it to
    the `report_narrative` setting first (principle 5, human review before
    it reaches an export), not pass arbitrary text through the export
    call."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    branding = db.get_branding()
    sty = _styles(branding)
    dark = sty["dark"]
    h2, body, footer_style = sty["h2"], sty["body"], sty["footer"]

    doc = SimpleDocTemplate(output_path, pagesize=A4,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm)
    # now(UTC) instead of utcnow() (DTZ003): no formatting risk -- strftime
    # with this pattern does not include an offset, so the displayed text
    # is identical.
    # The subject `db` is scoped to (multi-subject): "Project › Subject"
    # (subject_title), unless the caller passes `subject_name`.
    subject = html.escape(pdf_safe(report_label(subject_name) or subject_title(db)))
    subtitle = ((f"{subject} &middot; " if subject else "") + "Executive report &middot; "
                f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC")
    el, latest = _cover_elements(
        db, branding, sty, title="SERP Monitor", subtitle=subtitle,
        narrative_override=narrative_override, countries=countries,
    )

    ai_summary = clean_ai_summary(ai_summary)
    if ai_summary:
        el.append(Paragraph("Executive summary (AI)", h2))
        el.append(Paragraph(html.escape(ai_summary).replace("\n", "<br/>"), body))
        el.append(Spacer(1, 6 * mm))
    elif ai_summary_note:
        # A failed/empty AI summary is never printed; just one small plain line.
        el.append(Paragraph(html.escape(f"AI summary unavailable: {ai_summary_note}"),
                            footer_style))
        el.append(Spacer(1, 4 * mm))

    if period:
        el.extend(_period_elements(period, sty))

    el.append(Paragraph("Detail by country", h2))
    data = [["Country", "Positive", "Negative", "Neutral", "Total", "% Pos"]]
    for r in latest:
        total = r.get("total_results") or 0
        pos = r.get("positive") or 0
        data.append([
            (r.get("country_code") or "").upper(), pos, r.get("negative") or 0,
            r.get("neutral") or 0, total, f"{round(100*pos/total) if total else 0}%",
        ])
    t = basic_table(data, [24 * mm, 26 * mm, 26 * mm, 26 * mm, 22 * mm, 22 * mm], dark)
    el.append(t)

    # Actions & impact (4.5): last _PDF_EVENTS_LIMIT timeline events with
    # their DEFAULT_PDF_WINDOW-day delta (or "insufficient data" if either
    # of the two halves of the window has no data, see
    # processing.event_impact). Section omitted entirely if there are no
    # ELIGIBLE events (manual ones with a past date -- review 4.5 #3:
    # without the filter, today's automatic url_gone events filled the
    # executive section with "insufficient data"). ONE series query for
    # the 5 (review #7, processing.event_impact.impacts_at).
    today = datetime.now(UTC).replace(tzinfo=None).date().isoformat()
    events = impactable_events(db.list_events(), today, _PDF_EVENTS_LIMIT)
    impacts = impacts_at(db, events, DEFAULT_PDF_WINDOW)
    if impacts:
        cell = sty["cell"]
        el.append(Spacer(1, 8 * mm))
        el.append(Paragraph("Actions & impact", h2))
        rows_ai = [["Date", "Event", f"{DEFAULT_PDF_WINDOW}d impact"]]
        for row in impacts:
            e, imp = row["event"], row["impact"]
            impact_txt = _impact_text(imp, DEFAULT_PDF_WINDOW)
            # Paragraph wraps only (review #8: a plain 70-char-wide str
            # overflowed the column); escaped because it parses mini-HTML.
            desc = html.escape(e.get("description") or "")
            rows_ai.append([
                (e.get("event_date") or "")[:10],
                Paragraph(desc, cell),
                impact_txt,
            ])
        at = basic_table(rows_ai, [22 * mm, 90 * mm, 62 * mm], dark)
        el.append(at)

    # Brand footer (5.1): a final centered paragraph, NOT a real
    # per-page footer (# design: SimpleDocTemplate/canvas.onPage would add
    # complexity -- this PDF is a single long "cover", not a multi-page
    # report with folios that would need it). Empty -> no block.
    if branding["footer_text"]:
        el.append(Spacer(1, 10 * mm))
        el.append(Paragraph(html.escape(branding["footer_text"]), footer_style))

    doc.build(el)
    return output_path
