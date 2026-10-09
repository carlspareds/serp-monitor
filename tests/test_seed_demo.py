"""scripts/seed_demo_data.py: the demo must show Wins and Notes, built
through the same code paths as a real search (update_url_lifecycle) and the
/notes board (set_note/set_note_status), and stay idempotent."""
import importlib.util

from conftest import _ROOT

from database.models import Database


def _load_seed():
    spec = importlib.util.spec_from_file_location(
        "seed_demo_data", _ROOT / "scripts" / "seed_demo_data.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_runs_lifecycle_so_wins_has_news_example(tmp_path, monkeypatch):
    db_path = str(tmp_path / "demo.db")
    monkeypatch.setenv("DB_PATH", db_path)
    _load_seed().main()
    db = Database(db_path=db_path)

    runs = db.list_runs()
    assert runs and all(db.get_run(r["apify_run_id"])["lifecycle_done"] for r in runs)
    wins = db.list_victories()
    assert [w["url"] for w in wins] == ["https://news.example/court/ashworth-dismissed"]
    kinds = [a["kind"] for a in db.list_alerts(status="all")]
    assert "url_gone" in kinds


def test_seed_creates_notes_through_db_api(tmp_path, monkeypatch):
    db_path = str(tmp_path / "demo.db")
    monkeypatch.setenv("DB_PATH", db_path)
    _load_seed().main()
    db = Database(db_path=db_path)

    notes = {n["url"]: n for n in db.list_notes()}
    scam = notes["https://scam.example/jordan-ashworth-fraud"]
    assert scam["status"] == "in_progress"
    assert scam["due_date"]
    assert "remove the article" in scam["note"]
    assert "resolved" in {n["status"] for n in notes.values()}
    # Status changes went through set_note_status, so the history exists.
    assert [h["to_status"] for h in db.note_history(scam["id"])] == ["in_progress"]


def test_seed_is_idempotent(tmp_path, monkeypatch):
    db_path = str(tmp_path / "demo.db")
    monkeypatch.setenv("DB_PATH", db_path)
    seed = _load_seed()
    seed.main()
    seed.main()
    db = Database(db_path=db_path)
    assert len(db.list_runs()) == 4
    assert len(db.list_notes()) == 2
    assert len(db.list_victories()) == 1


def test_fresh_db_turns_subject_1_into_the_demo_subject(tmp_path, monkeypatch):
    """A fresh install's subject 1 is an empty placeholder: the demo takes it over
    instead of leaving an empty subject next to the demo one."""
    db_path = str(tmp_path / "demo.db")
    monkeypatch.setenv("DB_PATH", db_path)
    seed = _load_seed()
    seed.main()
    db = Database(db_path=db_path)

    subjects = db.list_subjects()
    assert [s["id"] for s in subjects] == [1]
    demo = subjects[0]
    assert demo["name"] == "Jordan Ashworth (demo)"
    assert demo["is_demo"] is True
    assert demo["query"] == "Jordan Ashworth"
    assert demo["countries"] == ["us"]
    assert demo["context"]
    # Its domains are its own DB rows (the global config list is empty).
    assert set(seed.POSITIVE_DOMAINS) <= set(db.for_subject(1).list_positive_domains())
    assert db.for_subject(1).list_negative_domains() == ["scam.example"]
    # Legacy settings mirror subject 1 (phase-1 bridge for the unmigrated routers).
    assert db.get_setting("report_query") == "Jordan Ashworth"
    assert db.get_setting("report_countries") == "us"
    # It lives in a "Demo" project; project 1 keeps the default name for the
    # operator's own subjects.
    assert db.get_client(demo["client_id"])["name"] == "Demo"
    assert db.get_client(1)["name"] == "My project"
    assert db.subject_label(1) == "Demo ▸ Jordan Ashworth (demo)"


def test_demo_project_is_reused_and_never_renamed(tmp_path, monkeypatch):
    """Re-running the seed reuses the "Demo" project (no duplicates); a project
    holding real subjects is never renamed, and an existing demo subject left in
    project 1 by an older seed is moved to "Demo"."""
    db_path = str(tmp_path / "demo.db")
    monkeypatch.setenv("DB_PATH", db_path)
    seed = _load_seed()
    seed.main()
    db = Database(db_path=db_path)
    db.set_subject_project(1, 1)  # what an older seed left behind
    real = db.add_subject("Jane Real", queries=["Jane Real"], countries=["us"])
    seed.main()
    seed.main()

    names = [c["name"] for c in db.list_clients()]
    assert names.count("Demo") == 1
    assert db.get_client(1)["name"] == "My project"
    assert db.get_client(db.get_subject(1)["client_id"])["name"] == "Demo"
    assert db.get_subject(real)["client_id"] == 1


def test_db_with_real_data_gets_a_separate_demo_subject(tmp_path, monkeypatch):
    """Subject 1 already tracks someone real: the demo goes into a NEW subject and
    subject 1 (data, domains, legacy settings) is left untouched."""
    db_path = str(tmp_path / "real.db")
    monkeypatch.setenv("DB_PATH", db_path)
    db = Database(db_path=db_path)
    db.set_setting("report_query", "Jane Real")
    db.set_setting("report_countries", "es")
    db.save_run(apify_run_id="real-1", queries=["Jane Real"], country_code="es",
                status="completed", summary={"total": 0, "positive": 0, "negative": 0,
                                             "neutral": 0})
    db.add_positive_domain("janereal.example")

    seed = _load_seed()
    seed.main()
    seed.main()  # idempotent: reuses the demo subject, no second copy

    subjects = {s["id"]: s for s in db.list_subjects()}
    assert len(subjects) == 2
    demo_id = next(sid for sid, s in subjects.items() if s["is_demo"])
    assert demo_id != 1
    assert subjects[1]["name"] == "Jane Real"
    assert subjects[1]["is_demo"] is False
    assert subjects[1]["client_id"] == 1
    assert db.get_client(subjects[demo_id]["client_id"])["name"] == "Demo"
    assert db.get_setting("report_query") == "Jane Real"
    assert db.get_setting("report_countries") == "es"

    assert [r["apify_run_id"] for r in db.list_runs()] == ["real-1"]
    assert db.list_positive_domains() == ["janereal.example"]
    demo = db.for_subject(demo_id)
    assert len(demo.list_runs()) == 4
    assert "jordanashworth.example" in demo.list_positive_domains()
    assert [w["url"] for w in demo.list_victories()] == [
        "https://news.example/court/ashworth-dismissed"]
    assert len(demo.list_notes()) == 2
    # Nothing of the demo leaked into subject 1.
    assert db.list_victories() == []
    assert db.list_notes() == []
    assert db.list_alerts(status="all") == []
