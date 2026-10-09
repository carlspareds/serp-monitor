"""Report files of a subject: create (PDF / Excel / CSV), list, find.

Files live in ``<exports_dir>/<subject_id>/`` with a JSON sidecar per file
(``<name>.json``: kind, title, countries, period, created_at, created_by,
source, emailed). The Reports dialog (dashboard.routers.export), the
assistant's ``create_report`` / ``list_reports`` tools and the weekly job all
go through here, so "Previous reports" shows every file of the subject.

Period (last week / last 4 weeks / last 3 months):
  * CSV: every stored result scraped within the period.
  * PDF: the latest run of each country, plus a "This period" section (new
    negative results, Wins and open alerts within the period).
  * Excel: the latest run of each country and the full run history; the
    period doesn't change the workbook (the preview says so).
"""
from __future__ import annotations

import csv
import json
import re
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path

KINDS = {
    "pdf": {"title": "Share with a client or manager", "format": "PDF", "ext": "pdf",
            "button": "Create PDF", "name": "Executive summary"},
    "xlsx": {"title": "Analyse in a spreadsheet", "format": "Excel workbook", "ext": "xlsx",
             "button": "Create workbook", "name": "Workbook"},
    "csv": {"title": "Use in scripts or other tools", "format": "CSV file", "ext": "csv",
            "button": "Create CSV", "name": "Results export"},
}
PERIODS = {"week": ("Last week", 7), "4weeks": ("Last 4 weeks", 28), "3months": ("Last 3 months", 91)}
DEFAULT_PERIOD = "4weeks"
MEDIA = {"pdf": "application/pdf", "csv": "text/csv",
         "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,80}\.(pdf|xlsx|csv)$")
_CSV_COLS = ["run_id", "country_code", "engine", "query", "position", "url", "domain", "title",
             "description", "classification", "confidence", "classified_by", "severity",
             "same_person", "scraped_at"]


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def subject_dir(base: Path | str, subject_id: int) -> Path:
    return Path(base) / str(int(subject_id))


def safe_name(name: str) -> str:
    """The file name if it is one of ours (no paths, known extension), else ''."""
    name = (name or "").strip()
    return name if _NAME_RE.match(name) else ""


def _slug(text: str, limit: int = 28) -> str:
    """Lowercase ASCII words joined by '-' (accents dropped), at most `limit` chars."""
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:limit].strip("-")


def csv_name_stem(db, subject_id: int) -> str:
    """'<project>-<subject>' slug for a results CSV's file name (safe_name-proof:
    starts with a letter, only [a-z0-9-]). Falls back to 'project'/'subject-<id>'."""
    subject = None
    project = None
    try:
        subject = db.get_subject(int(subject_id))
        if subject is not None:
            project = db.get_client(subject.get("client_id") or 1)
    except Exception:  # noqa: BLE001,S110 -- a file name must never fail the export
        pass
    subject_slug = _slug((subject or {}).get("name") or "") or f"subject-{int(subject_id)}"
    project_slug = _slug((project or {}).get("name") or "") or "project"
    stem = f"{project_slug}-{subject_slug}"
    return stem if stem[0].isalpha() else f"p-{stem}"


_KIND_WORD = {"csv": "results", "pdf": "summary", "xlsx": "workbook"}


def report_path(db, base, subject_id: int, kind: str) -> Path:
    """A new, unused path for a report file under subject_dir():
    '<project>-<subject>-<results|summary|workbook>-<YYYY-MM-DD>.<ext>'
    (recognisable once downloaded), '-2', '-3'... appended when that name
    is already taken the same day."""
    ext = KINDS[kind]["ext"]
    out_dir = subject_dir(base, subject_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{csv_name_stem(db, subject_id)}-{_KIND_WORD[kind]}-{_now():%Y-%m-%d}"
    path = out_dir / f"{stem}.{ext}"
    n = 1
    while path.exists():
        n += 1
        path = out_dir / f"{stem}-{n}.{ext}"
    return path


def period_range(period: str, now: datetime | None = None) -> tuple[str, str, str]:
    """(label, since ISO date, until ISO date) of a period key."""
    now = now or _now()
    label, days = PERIODS.get(period, PERIODS[DEFAULT_PERIOD])
    since = (now - timedelta(days=days)).date().isoformat()
    return label, since, now.date().isoformat()


def _fmt_day(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso[:10])
    except ValueError:
        return iso
    return f"{d.day} {d.strftime('%b %Y')}"


# --------------------------------------------------------------------- #
# What's in a report (the preview and the PDF's "This period" section)
# --------------------------------------------------------------------- #
def _new_negatives(db, since: str, countries: list[str]) -> list[dict]:
    clause, params = db._subject_clause()
    conn = db._connect()
    try:
        rows = conn.execute(
            "SELECT url, country, first_seen FROM url_lifecycle WHERE classification = 'negative' "
            f"AND (same_person IS NULL OR same_person != 'no') AND first_seen >= ? AND {clause} "
            "ORDER BY first_seen DESC",
            (since, *params),
        ).fetchall()
    finally:
        conn.close()
    out, seen = [], set()
    for r in rows:
        country = (r["country"] or "").lower()
        if countries and country not in countries:
            continue
        from urllib.parse import urlsplit
        domain = (urlsplit(r["url"] or "").netloc or r["url"] or "").removeprefix("www.")
        if (domain, country) in seen:
            continue
        seen.add((domain, country))
        out.append({"domain": domain, "country": country, "first_seen": r["first_seen"] or ""})
    return out


def _wins(db, since: str, until: str, countries: list[str]) -> list[dict]:
    from urllib.parse import urlsplit
    out = []
    for v in db.list_victories(since=since, until=until + "T23:59:59"):
        country = (v.get("country") or "").lower()
        if countries and country not in countries:
            continue
        out.append({"domain": (urlsplit(v.get("url") or "").netloc or v.get("url") or "").removeprefix("www."),
                    "country": country, "gone_since": v.get("gone_since") or ""})
    return out


def summarize(db, kind: str, countries: list[str], period: str) -> dict:
    """Real figures for the preview ("What you'll get") and the PDF section."""
    from processing import ai_classifier

    countries = [c.lower() for c in countries or []]
    label, since, until = period_range(period)
    latest = db.latest_run_per_country()
    if countries:
        latest = [r for r in latest if (r.get("country_code") or "").lower() in countries]
    alerts = [a for a in db.list_alerts(limit=500, status="active")
              if not countries or (a.get("country") or "").lower() in countries or not a.get("country")]
    # Open = not acknowledged (the bell's definition); acknowledged are counted apart.
    acked_alerts = [a for a in alerts if a.get("status") == "acknowledged"]
    alerts = [a for a in alerts if a.get("status") != "acknowledged"]
    csv_rows = 0
    if kind == "csv":
        csv_rows = len(_csv_rows(db, countries, since))
    latest_day = max(((r.get("created_at") or "")[:10] for r in latest), default="")
    return {
        "period_label": label, "since": since, "until": until,
        "range_text": f"{_fmt_day(since)} to {_fmt_day(until)}",
        "latest_runs": len(latest),
        "latest_results": sum(int(r.get("total_results") or 0) for r in latest),
        "latest_day": latest_day,
        "countries": sorted({(r.get("country_code") or "").lower() for r in latest}),
        "new_negatives": _new_negatives(db, since, countries),
        "wins": _wins(db, since, until, countries),
        "open_alerts": len(alerts),
        "acknowledged_alerts": len(acked_alerts),
        "ai_summary": bool(ai_classifier.is_configured()),
        "history_runs": len(db.list_runs(limit=10000)),
        "csv_rows": csv_rows,
    }


# --------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------- #
def _csv_rows(db, countries: list[str], since: str) -> list[dict]:
    rows: list[dict] = []
    for c in countries or [""]:
        rows.extend(db.get_results_filtered(country_code=c, limit=100_000, sort_by="recent"))
    return [r for r in rows if str(r.get("scraped_at") or "")[:10] >= since]


def _meta_path(path: Path) -> Path:
    return path.with_name(path.name + ".json")


def _write_meta(path: Path, meta: dict) -> dict:
    meta = {**meta, "file": path.name, "size": path.stat().st_size if path.is_file() else 0}
    _meta_path(path).write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta


def register_file(base, subject_id: int, path: Path, kind: str, *, title: str = "",
                  source: str = "manual", emailed: bool = False, countries=None, period: str = "",
                  created_by: str = "", subject: str = "") -> dict:
    """Records metadata for a file already written under subject_dir().
    `subject`: the "Project › Subject" label the file was built for."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return _write_meta(Path(path), {
        "kind": kind, "title": title or KINDS.get(kind, {}).get("name", "Report"),
        "subject_id": int(subject_id), "subject": subject, "countries": list(countries or []),
        "period": period,
        "created_at": _now().isoformat(timespec="seconds"), "created_by": created_by,
        "source": source, "emailed": bool(emailed),
    })


def create_report(db, base, subject_id: int, kind: str, countries=None, period: str = DEFAULT_PERIOD,
                  created_by: str = "", source: str = "manual") -> dict:
    """Builds the file with the existing exporters and returns its metadata.
    The title is always "Project › Subject" (excel_exporter.subject_title), the
    same on every route -- callers can't pass a name-only label any more."""
    from export.excel_exporter import build_workbook, neutralize_formula, subject_title
    from export.pdf_report import build_pdf
    from processing import ai_classifier

    if kind not in KINDS:
        raise ValueError(f"unknown report type {kind!r}")
    period = period if period in PERIODS else DEFAULT_PERIOD
    countries = [c.lower() for c in countries or []]
    path = report_path(db, base, subject_id, kind)
    _label, since, _until = period_range(period)
    subject_name = subject_title(db, subject_id)
    if kind == "pdf":
        info = summarize(db, "pdf", countries, period)
        text = _summary_text(db, info)
        ai, note = "", ""
        if ai_classifier.is_configured():
            ok, out = ai_classifier.executive_summary_result(text)
            ai, note = (out, "") if ok else ("", out)
        build_pdf(db, str(path), ai_summary=ai, ai_summary_note=note,
                  subject_name=subject_name or None,
                  countries=countries or None, period={
            "label": info["range_text"], "new_negatives": info["new_negatives"],
            "wins": info["wins"], "open_alerts": info["open_alerts"],
            "acknowledged_alerts": info.get("acknowledged_alerts", 0)})
    elif kind == "xlsx":
        build_workbook(db, str(path), countries=countries or None,
                       subject_name=subject_name or None)
    else:
        rows = _csv_rows(db, countries, since)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=_CSV_COLS, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: neutralize_formula(r.get(k)) for k in _CSV_COLS})
    return register_file(base, subject_id, path, kind, countries=countries, period=period,
                         created_by=created_by, source=source, subject=subject_name)


def _summary_text(db, info: dict) -> str:
    """Context handed to the AI for the PDF's written summary."""
    s = db.get_stats()
    lines = [(f"Totals: {s['by_class']['positive']} positive, {s['by_class']['negative']} negative, "
              f"{s['by_class']['neutral']} neutral. Last run: {s.get('last_run')}."),
             f"Period: {info['range_text']}."]
    for r in sorted(db.latest_run_per_country(), key=lambda r: -(r.get("negative") or 0)):
        lines.append(f"- {(r.get('country_code') or '').upper()}: {r.get('positive') or 0} positive, "
                     f"{r.get('negative') or 0} negative, {r.get('neutral') or 0} neutral.")
    for n in info["new_negatives"][:20]:
        lines.append(f"- new negative in the period: {n['domain']} ({n['country'].upper()})")
    for w in info["wins"][:20]:
        lines.append(f"- win (negative dropped out): {w['domain']} ({w['country'].upper()})")
    lines.append(f"Alerts: {info['open_alerts']} open, {info.get('acknowledged_alerts', 0)} acknowledged.")
    return "\n".join(lines)


def _guess_meta(path: Path, subject_id: int) -> dict:
    kind = path.suffix.lstrip(".")
    weekly = path.name.startswith("weekly_")
    return {"kind": kind, "title": "Weekly report" if weekly else KINDS.get(kind, {}).get("name", "Report"),
            "subject_id": subject_id, "countries": [], "period": "",
            "created_at": datetime.fromtimestamp(path.stat().st_mtime, UTC).replace(tzinfo=None)
            .isoformat(timespec="seconds"),
            "created_by": "", "source": "weekly" if weekly else "manual", "emailed": False,
            "file": path.name, "size": path.stat().st_size}


def adopt_legacy_files(base, subject_id: int = 1) -> int:
    """Moves report files left FLAT in ``<exports_dir>/`` (weekly_*.xlsx and
    the old Export page's files, written before reports were stored per
    subject) into ``<exports_dir>/<subject_id>/`` -- subject 1, the only
    subject that existed then -- so they show under its Previous reports.
    Idempotent and best-effort: a file that already exists in the target is
    left where it is. Returns how many files moved."""
    base = Path(base)
    if not base.is_dir():
        return 0
    moved = 0
    target = subject_dir(base, subject_id)
    for p in base.iterdir():
        if not p.is_file() or not safe_name(p.name):
            continue
        dest = target / p.name
        if dest.exists():
            continue
        try:
            target.mkdir(parents=True, exist_ok=True)
            p.replace(dest)
            side = _meta_path(p)
            if side.is_file():
                side.replace(_meta_path(dest))
            moved += 1
        except OSError:
            continue
    return moved


def list_reports(base, subject_id: int, limit: int = 50) -> list[dict]:
    """The subject's report files, newest first."""
    d = subject_dir(base, subject_id)
    if not d.is_dir():
        return []
    out = []
    for p in d.iterdir():
        if not p.is_file() or not safe_name(p.name):
            continue
        meta = None
        mp = _meta_path(p)
        if mp.is_file():
            try:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = None
        meta = {**_guess_meta(p, subject_id), **(meta or {}), "file": p.name, "size": p.stat().st_size}
        out.append(meta)
    out.sort(key=lambda m: (m.get("created_at") or "", m["file"]), reverse=True)
    return out[:limit]


def find_report(base, subject_id: int, name: str) -> Path | None:
    """The file `name` of this subject, or None (never another subject's)."""
    safe = safe_name(name)
    if not safe:
        return None
    p = subject_dir(base, subject_id) / safe
    return p if p.is_file() else None


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB"
