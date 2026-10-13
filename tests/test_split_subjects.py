"""scripts/maintenance/split_subjects.py: splits a subject 1 that accumulated searches for
other people into one Subject per tracked name. Fixture mirrors the real demo
DB: 4 "Jordan Ashworth" runs (the demo seed) + 1 "Alex Rivera" run in the
same country, all on subject 1."""
import importlib.util
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import _ROOT

from database.models import Database, url_hash


def _load(name):
    path = _ROOT / "scripts" / f"{name}.py"
    if not path.exists():
        path = _ROOT / "scripts" / "maintenance" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SHARED = "https://jordanashworth.example/"  # seen by BOTH searches
ALEX_ONLY = "https://bad.example/alex-fraud"


@pytest.fixture
def mixed_db(tmp_path, monkeypatch):
    from notify import dispatcher
    from processing.alerts import generate_alerts
    from processing.lifecycle import update_url_lifecycle

    path = str(tmp_path / "mixed.db")
    monkeypatch.setenv("DB_PATH", path)
    _load("seed_demo_data").main()  # 4 Jordan runs on subject 1 (fresh DB)
    monkeypatch.setattr(dispatcher, "notify_alert", lambda *_a, **_k: None)

    db = Database(db_path=path)  # unscoped = subject 1, like the pre-split app
    db.save_run(apify_run_id="alex-1", queries=["Alex Rivera"], country_code="us",
                status="completed",
                summary={"total": 4, "positive": 0, "negative": 2, "neutral": 2})
    rows = [
        (ALEX_ONLY, "Alex Rivera fraud", "negative"),
        ("https://worse.example/alex-scam", "Alex Rivera scam", "negative"),
        ("https://en.wikipedia.org/wiki/Alex_Rivera", "Alex Rivera", "neutral"),
        (SHARED, "Jordan Ashworth", "neutral"),
    ]
    db.save_results([
        {"query": "Alex Rivera", "url": u, "title": t, "description": "",
         "position": i + 1, "country_code": "us", "domain": u.split("/")[2],
         "classification": c, "engine": "google"}
        for i, (u, t, c) in enumerate(rows)
    ], run_id="alex-1")
    # A later DATE than the demo's last run, so the alerts below are computed with
    # Alex's run as "latest" and Jordan's as the previous-date baseline.
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).replace(tzinfo=None)
    conn = db._connect()
    conn.execute("UPDATE search_results SET scraped_at = ? WHERE run_id = 'alex-1'",
                 (tomorrow.isoformat(timespec="seconds"),))
    conn.commit()
    conn.close()
    update_url_lifecycle(db, "alex-1")
    generate_alerts(db)
    db.set_note(ALEX_ONLY, "alex note")
    note_id = next(n["id"] for n in db.list_notes() if n["url"] == ALEX_ONLY)
    db.set_note_status(note_id, "in_progress", by="tester")
    db.set_note(SHARED, "shared note")
    db.add_evidence(ALEX_ONLY, url_hash(ALEX_ONLY), "2026-01-01T00:00:00",
                    "/tmp/c.html", "cafe", "manual")
    return path


def _counts(path):
    return _load("split_subjects").counts_per_subject(path)


def _dump(path):
    """Logical content (schema + rows). Not the raw bytes: closing the last
    connection to a WAL database checkpoints the -wal file into the main file,
    which changes the bytes without changing any data."""
    conn = sqlite3.connect(path)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def test_dry_run_changes_nothing(mixed_db, capsys):
    before = _dump(mixed_db)
    assert _load("split_subjects").main(["--dry-run", "--db", mixed_db]) == 0
    out = capsys.readouterr().out
    assert "'Alex Rivera' -> NEW subject: 1 run(s), 4 result(s)" in out
    assert "--dry-run: nothing was changed." in out
    assert _dump(mixed_db) == before
    assert not list(Path(mixed_db).parent.glob("*.bak"))


def test_split_moves_alex_into_its_own_subject(mixed_db, capsys):
    pre = Database(db_path=mixed_db).for_all_subjects()
    newneg_before = [a for a in pre.list_alerts(status="all", limit=1000)
                     if a["kind"] == "negative_new"]
    thr_before = [a for a in pre.list_alerts(status="all", limit=1000)
                  if a["kind"] == "threshold"]

    assert _load("split_subjects").main(["--db", mixed_db]) == 0
    assert len(list(Path(mixed_db).parent.glob("mixed.db.pre-split-*.bak"))) == 1

    db = Database(db_path=mixed_db)
    subjects = {s["name"]: s for s in db.list_subjects()}
    assert set(subjects) == {"Jordan Ashworth (demo)", "Alex Rivera"}
    alex = subjects["Alex Rivera"]
    assert alex["countries"] == ["us"] and alex["query"] == "Alex Rivera"
    c = db.for_subject(alex["id"])
    j = db.for_subject(1)

    assert [r["apify_run_id"] for r in c.list_runs()] == ["alex-1"]
    assert len(j.list_runs()) == 4
    assert {r["query"] for r in j.get_results_filtered(limit=1000)} == {"Jordan Ashworth"}
    assert len(c.get_results_filtered(limit=1000)) == 4

    conn = sqlite3.connect(mixed_db)
    lc = {(r[0], r[1]) for r in conn.execute("SELECT url, subject_id FROM url_lifecycle")}
    conn.close()
    # Alex-only rows moved; the SHARED url keeps subject 1's row and Alex gets
    # his own rebuilt one.
    assert (ALEX_ONLY, alex["id"]) in lc and (ALEX_ONLY, 1) not in lc
    assert (SHARED, 1) in lc and (SHARED, alex["id"]) in lc

    assert [n["url"] for n in c.list_notes()] == [ALEX_ONLY]
    assert [h["to_status"] for h in c.note_history(c.list_notes()[0]["id"])] == ["in_progress"]
    assert SHARED in {n["url"] for n in j.list_notes()}  # ambiguous -> stays
    assert [e["url"] for e in c.evidence_for(url_hash(ALEX_ONLY))] == [ALEX_ONLY]
    assert j.evidence_for(url_hash(ALEX_ONLY)) == []

    # negative_new computed with Alex's run vs Jordan's baseline: deleted.
    alex_day = c.dates_for_country("us")[0]
    assert newneg_before and any(f":{alex_day}:" in a["dedupe_key"] for a in newneg_before)
    remaining = db.for_all_subjects().list_alerts(status="all", limit=1000)
    assert not [a for a in remaining
                if a["kind"] == "negative_new" and f":{alex_day}:" in a["dedupe_key"]]
    # threshold over Alex's own run: moved to him.
    assert any(a["dedupe_key"] == f"thr:us:{alex_day}" for a in thr_before)
    assert [a["dedupe_key"] for a in c.list_alerts(status="all", limit=1000)] == [
        f"thr:us:{alex_day}"]
    # Jordan keeps his own alerts (the demo's url_gone win included).
    assert "url_gone" in {a["kind"] for a in j.list_alerts(status="all", limit=1000)}
    assert j.list_victories()

    out = capsys.readouterr().out
    assert "AMBIGUOUS notes (left on subject 1): 1" in out


def test_split_is_idempotent(mixed_db, capsys):
    split = _load("split_subjects")
    split.main(["--db", mixed_db])
    after_first = _counts(mixed_db)
    capsys.readouterr()
    assert split.main(["--db", mixed_db]) == 0
    assert "Nothing to do" in capsys.readouterr().out
    assert _counts(mixed_db) == after_first
    assert len(list(Path(mixed_db).parent.glob("*.bak"))) == 1  # no-op: no new backup
    assert len(Database(db_path=mixed_db).list_subjects()) == 2


def test_split_reuses_an_existing_subject_with_that_name(mixed_db):
    db = Database(db_path=mixed_db)
    sid = db.add_subject("alex  rivera", countries=["pt"])
    _load("split_subjects").main(["--db", mixed_db])
    assert [r["apify_run_id"] for r in db.for_subject(sid).list_runs()] == ["alex-1"]
    assert len(db.list_subjects()) == 2


def test_split_refuses_when_subject_1_has_no_name(tmp_path):
    path = str(tmp_path / "anon.db")
    db = Database(db_path=path)
    db.save_run(apify_run_id="x", queries=["Someone"], country_code="us",
                status="completed", summary={"total": 0, "positive": 0, "negative": 0,
                                             "neutral": 0})
    with pytest.raises(SystemExit, match="no name"):
        _load("split_subjects").main(["--db", path])
