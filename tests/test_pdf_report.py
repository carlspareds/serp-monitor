"""Tests for src/export/pdf_report.py (4.5): new "Actions &
impact" section with the last 5 timeline events and their 30-day delta.

Smoke (brief 4.5 explicitly asks for "smoke -- generates to tmp_path"):
with no PDF text-extraction library in the environment (no network to
install one), these tests verify that build_pdf() doesn't break with/without
events and produces a valid PDF file of reasonable size -- they don't
inspect the PDF's internal text.
"""
from pathlib import Path

from export.pdf_report import build_pdf


def test_build_pdf_without_events_omits_actions_section_and_still_builds(seeded_db, tmp_path):
    """seeded_db has no events (conftest.seeded_db doesn't call add_event)
    -- the "Actions & impact" section is omitted (brief: "if there are no
    events, it's omitted"), and the PDF still builds without breaking."""
    output_path = str(tmp_path / "report_no_events.pdf")

    result_path = build_pdf(seeded_db, output_path)

    assert result_path == output_path
    assert Path(output_path).exists()
    assert Path(output_path).stat().st_size > 0


def test_build_pdf_only_uses_last_five_eligible_events(seeded_db, tmp_path, monkeypatch):
    """9 past manual events -- the section processes EXACTLY 5
    (review 4.5 P3: spying on the arithmetic, not just 'doesn't crash').

    F3-close top-5 #2: build_pdf no longer calls event_impact_from_rows
    directly -- it goes through processing.event_impact.impacts_at(), which
    DOES call it internally (module-level, resolved in processing.
    event_impact) -- the spy is installed there."""
    from processing import event_impact as event_impact_module

    for i in range(9):
        seeded_db.add_event(f"2023-0{(i % 9) + 1}-05", f"Event {i}", kind="manual")

    calls = []
    real = event_impact_module.event_impact_from_rows

    def spy(by_day, event_date, windows=(30,)):
        calls.append(event_date)
        return real(by_day, event_date, windows)

    monkeypatch.setattr(event_impact_module, "event_impact_from_rows", spy)
    build_pdf(seeded_db, str(tmp_path / "report_many_events.pdf"))

    assert len(calls) == 5


def test_build_pdf_ignores_automatic_url_gone_events(seeded_db, tmp_path, monkeypatch):
    """Review 4.5 #3: automatic url_gone events (lifecycle, dated today)
    do NOT enter 'Actions & impact' -- without the filter, the executive
    PDF's 5 slots filled up with 'insufficient data' system noise.

    F3-close top-5 #2: same reason as above -- the spy is installed in
    processing.event_impact, not in export.pdf_report."""
    from datetime import UTC, datetime, timedelta

    from processing import event_impact as event_impact_module

    today = datetime.now(UTC).replace(tzinfo=None).date()
    for i in range(6):
        seeded_db.add_event(today.isoformat(), f"gone {i}", kind="url_gone")
    manual_day = (today - timedelta(days=40)).isoformat()
    seeded_db.add_event(manual_day, "Real client action", kind="manual")

    calls = []
    real = event_impact_module.event_impact_from_rows

    def spy(by_day, event_date, windows=(30,)):
        calls.append(event_date)
        return real(by_day, event_date, windows)

    monkeypatch.setattr(event_impact_module, "event_impact_from_rows", spy)
    build_pdf(seeded_db, str(tmp_path / "report_url_gone.pdf"))

    assert calls == [manual_day]

