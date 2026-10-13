"""Tests for item C21b (deep dive §C21, second of three parts): per-subject
scoping at the `Database` level -- `Database.for_subject(subject_id)`, the
`SCOPING_COMPLETE` lock on `add_subject`, and the real filtering of the 5
core tables (search_results, runs, alerts, goals, url_notes). The rest of
the tables with subject_id are deliberately left UNFILTERED in this
delivery (declared debt -- see tests/test_scoping_guard.py, reason
"deferred C21b-2").

TDD, direct `Database` (`from conftest import make_db as _db`) -- no
TestClient or routers: C21b does NOT wire an HTTP request's active
subject to `for_subject()` (that's C21c, client_viewer's white-label
portal). This tests the mechanism itself.

The subject_id values used (1 and 2) don't need a real row in `subjects`
-- subject_id is a LOGICAL FK with no declared constraint."""
import sqlite3

import pytest
from conftest import make_db as _db

from database import models
from database.models import Database
from database.models import url_hash as url_hash_fn


def _raw(db: Database) -> sqlite3.Connection:
    return db._connect()


# --------------------------------------------------------------------- #
# for_subject(): lightweight instance, NOT mutable state on the singleton
# --------------------------------------------------------------------- #
def test_for_subject_returns_a_new_instance_sharing_db_path(tmp_path):
    db = _db(tmp_path)
    scoped = db.for_subject(2)
    assert scoped is not db
    assert isinstance(scoped, Database)
    assert scoped.db_path == db.db_path
    assert scoped._subject_id == 2


def test_for_subject_does_not_mutate_the_original_instance(tmp_path):
    """The `db` singleton (imported by ~30 routers via dashboard.deps) must
    NOT acquire an active subject just because SOMEONE called
    for_subject() -- if it did, it would be the shared mutable state the
    brief explicitly forbids (a race between concurrent requests with
    different subjects)."""
    db = _db(tmp_path)
    assert db._subject_id is None
    db.for_subject(2)
    assert db._subject_id is None  # still has no subject after the call


def test_two_for_subject_calls_on_the_same_db_do_not_interfere(tmp_path):
    db = _db(tmp_path)
    scoped_a = db.for_subject(1)
    scoped_b = db.for_subject(2)
    assert scoped_a._subject_id == 1
    assert scoped_b._subject_id == 2  # creating B didn't overwrite A's subject


# --------------------------------------------------------------------- #
# The lock (brief arbitration, point 1): SCOPING_COMPLETE
# --------------------------------------------------------------------- #
def test_add_subject_is_rejected_while_scoping_is_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "SCOPING_COMPLETE", False)
    db = _db(tmp_path)
    with pytest.raises(RuntimeError):
        db.add_subject("Second Subject")


def test_add_subject_rejection_does_not_insert_any_row(tmp_path, monkeypatch):
    """The lock isn't decorative -- the rejection must happen BEFORE
    touching the table, not a rollback after a half-done INSERT."""
    monkeypatch.setattr(models, "SCOPING_COMPLETE", False)
    db = _db(tmp_path)
    with pytest.raises(RuntimeError):
        db.add_subject("Second Subject")
    conn = _raw(db)
    count = conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0]
    conn.close()
    assert count == 1  # only subject 1, seeded by _ensure_tenancy_seed


def test_add_subject_succeeds_once_scoping_is_flagged_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "SCOPING_COMPLETE", True)
    db = _db(tmp_path)
    subject_id = db.add_subject("Second Subject")
    assert subject_id is not None
    conn = _raw(db)
    row = conn.execute("SELECT name FROM subjects WHERE id = ?", (subject_id,)).fetchone()
    conn.close()
    assert row["name"] == "Second Subject"


# --------------------------------------------------------------------- #
# Compatibility (mandatory 4): Database() without a subject == the usual
# behavior. The 2466 existing tests are the real proof of this; these
# tests pin down the explicit CONTRACT that makes it possible.
# --------------------------------------------------------------------- #
def test_database_without_subject_has_subject_id_none(tmp_path):
    db = _db(tmp_path)
    assert db._subject_id is None


def test_writes_without_active_subject_still_default_to_subject_1(tmp_path):
    """save_run/save_results/add_alert/set_note without an
    active subject (plain Database()) still write subject_id=1 -- IDENTICAL
    to pre-C21b behavior (the column's DEFAULT), never NULL."""
    db = _db(tmp_path)
    db.save_run(apify_run_id="r1", queries=["q"], country_code="us", status="completed",
                summary={"total": 0, "positive": 0, "negative": 0, "neutral": 0})
    db.save_results([{"url": "https://x.example/1", "query": "q", "country_code": "us",
                       "classification": "positive"}], run_id="r1")
    db.add_alert("manual", "warning", "us", "msg", "dedupe-compat")
    db.set_note("https://x.example/1", "note")

    conn = _raw(db)
    assert conn.execute("SELECT subject_id FROM runs WHERE apify_run_id='r1'"
                         ).fetchone()["subject_id"] == 1
    assert conn.execute("SELECT subject_id FROM search_results WHERE run_id='r1'"
                         ).fetchone()["subject_id"] == 1
    assert conn.execute("SELECT subject_id FROM alerts WHERE dedupe_key='dedupe-compat'"
                         ).fetchone()["subject_id"] == 1
    assert conn.execute("SELECT subject_id FROM url_notes WHERE url='https://x.example/1'"
                         ).fetchone()["subject_id"] == 1
    conn.close()


def test_unscoped_database_reads_only_the_default_subject(tmp_path):
    """A Database() without a subject sees ONLY the primary subject, not
    everything.

    This test used to assert the opposite ("without for_subject you see
    EVERYTHING") on the premise that only one subject exists today. That
    premise was false: A11 has been writing with other subject_id values
    for a while -- `add_peer` assigns 1000+id to each comparable peer, and
    its monthly cycle saves runs and results with that id. With the
    default set to "see everything", registering a single peer was enough
    for ANOTHER person's results to appear mixed into /results and into
    the watched subject's statistics.

    Whoever genuinely wants to see all subjects asks for it explicitly
    with for_all_subjects() -- see the next test."""
    db = _db(tmp_path)
    db.for_subject(1).save_run(
        apify_run_id="r1", queries=["q"], country_code="us", status="completed",
        summary={"total": 0, "positive": 0, "negative": 0, "neutral": 0})
    db.for_subject(1001).save_run(  # an A11 peer, exactly like in production
        apify_run_id="r2", queries=["q"], country_code="es", status="completed",
        summary={"total": 0, "positive": 0, "negative": 0, "neutral": 0})

    assert {r["apify_run_id"] for r in db.list_runs()} == {"r1"}


def test_for_all_subjects_is_the_explicit_way_to_see_everything(tmp_path):
    db = _db(tmp_path)
    db.for_subject(1).save_run(
        apify_run_id="r1", queries=["q"], country_code="us", status="completed",
        summary={"total": 0, "positive": 0, "negative": 0, "neutral": 0})
    db.for_subject(1001).save_run(
        apify_run_id="r2", queries=["q"], country_code="es", status="completed",
        summary={"total": 0, "positive": 0, "negative": 0, "neutral": 0})

    assert {r["apify_run_id"] for r in db.for_all_subjects().list_runs()} == {"r1", "r2"}


def test_for_subject_none_is_rejected_instead_of_showing_everything(tmp_path):
    """`None` is what arrives when someone forgot to resolve the subject.
    Degrading that to "see everything" would be failing open in the one
    mechanism whose job is to close."""
    db = _db(tmp_path)
    with pytest.raises(ValueError, match="for_all_subjects"):
        db.for_subject(None)


# --------------------------------------------------------------------- #
# No-leak (mandatory 1): two subjects, own data, real isolation in BOTH
# directions -- that A doesn't see B, AND that A DOES see its own (a
# broken filter that returns nothing would pass a badly written no-leak
# test).
# --------------------------------------------------------------------- #
@pytest.fixture
def two_subjects(tmp_path):
    """Subject 1 with data in runs/search_results/alerts/url_notes,
    and subject 2 with ITS OWN set, completely different (countries, urls,
    dedupe_keys, metrics) so that no "doesn't see" assert can pass by
    coincidence of shared data."""
    db = _db(tmp_path)
    db1 = db.for_subject(1)
    db2 = db.for_subject(2)

    db1.save_run(apify_run_id="run-a1", queries=["Subject A"], country_code="us",
                 status="completed", engine="google", device="desktop",
                 summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db1.save_results(
        [{"url": "https://a.example/bad", "query": "Subject A", "country_code": "us",
          "classification": "negative", "domain": "a.example",
          "title": "Bad news about Subject A", "description": "coverage of subject a",
          "engine": "google", "device": "desktop"}],
        run_id="run-a1",
    )
    db1.add_alert("manual", "warning", "us", "subject 1 alert", "dedupe-subj1")
    db1.set_note("https://a.example/bad", "subject 1 note")

    db2.save_run(apify_run_id="run-b1", queries=["Subject B"], country_code="es",
                 status="completed", engine="bing", device="mobile",
                 summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db2.save_results(
        [{"url": "https://b.example/bad", "query": "Subject B", "country_code": "es",
          "classification": "negative", "domain": "b.example",
          "title": "Bad news about Subject B", "description": "coverage of subject b",
          "engine": "bing", "device": "mobile"}],
        run_id="run-b1",
    )
    db2.add_alert("manual", "warning", "es", "subject 2 alert", "dedupe-subj2")
    db2.set_note("https://b.example/bad", "subject 2 note")

    return db1, db2


def _ids(rows, key="id"):
    return {r[key] for r in rows}


class TestNoFugaSearchResultsYRuns:
    def test_get_results_filtered(self, two_subjects):
        db1, db2 = two_subjects
        urls1 = {r["url"] for r in db1.get_results_filtered()}
        urls2 = {r["url"] for r in db2.get_results_filtered()}
        assert urls1 == {"https://a.example/bad"}
        assert urls2 == {"https://b.example/bad"}

    def test_search_text(self, two_subjects):
        db1, db2 = two_subjects
        out1 = db1.search_text("Subject")
        out2 = db2.search_text("Subject")
        urls1 = {r["url"] for r in out1["results"]}
        urls2 = {r["url"] for r in out2["results"]}
        assert urls1 == {"https://a.example/bad"}
        assert urls2 == {"https://b.example/bad"}

    def test_dates_for_country_does_not_cross_subjects_sharing_no_country(self, two_subjects):
        # Different countries in the fixture (us/es) would already be
        # enough, but the method filters by country_code AND subject_id --
        # this checks that a subject doesn't see dates for a country that
        # isn't even theirs.
        db1, db2 = two_subjects
        assert db1.dates_for_country("es") == []
        assert db2.dates_for_country("us") == []

    def test_dates_for_country_same_country_different_subjects(self, tmp_path):
        db = _db(tmp_path)
        db1, db2 = db.for_subject(1), db.for_subject(2)
        db1.save_run(apify_run_id="ra", queries=["q"], country_code="us", status="completed",
                     summary={"total": 0, "positive": 0, "negative": 0, "neutral": 0})
        db1.save_results([{"url": "https://a.example/1", "query": "q", "country_code": "us",
                            "classification": "neutral"}], run_id="ra")
        db2.save_run(apify_run_id="rb", queries=["q"], country_code="us", status="completed",
                     summary={"total": 0, "positive": 0, "negative": 0, "neutral": 0})
        db2.save_results([{"url": "https://b.example/1", "query": "q", "country_code": "us",
                            "classification": "neutral"}], run_id="rb")
        dates1 = db1.dates_for_country("us")
        dates2 = db2.dates_for_country("us")
        # same country, same day -- but each subject ONLY sees its own date
        # (not an empty list for either: both have data today)
        assert len(dates1) == 1
        assert len(dates2) == 1

    def test_get_latest_run_id(self, two_subjects):
        db1, db2 = two_subjects
        assert db1.get_latest_run_id("us") == "run-a1"
        assert db1.get_latest_run_id("es") == ""  # not their country NOR their subject
        assert db2.get_latest_run_id("es", engine="bing") == "run-b1"
        assert db2.get_latest_run_id("us") == ""

    def test_latest_run_ids(self, two_subjects):
        db1, db2 = two_subjects
        assert db1.latest_run_ids("us") == ["run-a1"]
        assert db1.latest_run_ids("es") == []
        assert db2.latest_run_ids("es") == ["run-b1"]
        assert db2.latest_run_ids("us") == []

    def test_pending_review(self, tmp_path):
        db = _db(tmp_path)
        db1, db2 = db.for_subject(1), db.for_subject(2)
        db1.save_results([{"url": "https://a.example/p", "query": "q", "country_code": "us",
                            "classification": "negative", "confidence": 0.1}], run_id=None)
        db2.save_results([{"url": "https://b.example/p", "query": "q", "country_code": "es",
                            "classification": "negative", "confidence": 0.1}], run_id=None)
        urls1 = {r["url"] for r in db1.pending_review()}
        urls2 = {r["url"] for r in db2.pending_review()}
        assert urls1 == {"https://a.example/p"}
        assert urls2 == {"https://b.example/p"}

    def test_count_pending_review(self, tmp_path):
        db = _db(tmp_path)
        db1, db2 = db.for_subject(1), db.for_subject(2)
        db1.save_results([{"url": "https://a.example/p1", "query": "q", "country_code": "us",
                            "classification": "negative", "confidence": 0.1},
                           {"url": "https://a.example/p2", "query": "q", "country_code": "us",
                            "classification": "negative", "confidence": 0.1}], run_id=None)
        db2.save_results([{"url": "https://b.example/p1", "query": "q", "country_code": "es",
                            "classification": "negative", "confidence": 0.1}], run_id=None)
        assert db1.count_pending_review() == 2
        assert db2.count_pending_review() == 1

    def test_get_negatives_pending_severity(self, two_subjects):
        db1, db2 = two_subjects
        urls1 = {r["url"] for r in db1.get_negatives_pending_severity("run-a1")}
        urls2 = {r["url"] for r in db2.get_negatives_pending_severity("run-b1")}
        assert urls1 == {"https://a.example/bad"}
        assert urls2 == {"https://b.example/bad"}
        # b trying to read a's run (defense in depth, not just
        # "per country"/"per run_id" isolation -- subject_id also filters)
        assert db2.get_negatives_pending_severity("run-a1") == []

    def test_get_result_pending_severity(self, two_subjects):
        db1, db2 = two_subjects
        conn = db1._connect()
        result_id_a = conn.execute(
            "SELECT id FROM search_results WHERE url='https://a.example/bad'"
        ).fetchone()["id"]
        conn.close()
        assert db1.get_result_pending_severity(result_id_a) is not None
        assert db2.get_result_pending_severity(result_id_a) is None  # leak blocked

    def test_distinct_countries(self, two_subjects):
        db1, db2 = two_subjects
        assert db1.distinct_countries() == ["us"]
        assert db2.distinct_countries() == ["es"]

    def test_distinct_engines(self, two_subjects):
        db1, db2 = two_subjects
        assert db1.distinct_engines() == ["google"]
        assert db2.distinct_engines() == ["bing"]

    def test_distinct_devices(self, two_subjects):
        db1, db2 = two_subjects
        assert db1.distinct_devices() == ["desktop"]
        assert db2.distinct_devices() == ["mobile"]

    def test_get_stats(self, two_subjects):
        db1, db2 = two_subjects
        stats1 = db1.get_stats(engine="google")
        stats2 = db2.get_stats(engine="bing")
        assert stats1["total_runs"] == 1
        assert stats1["total_results"] == 1
        assert stats2["total_runs"] == 1
        assert stats2["total_results"] == 1
        # subject 1 asking about subject 2's engine sees nothing of 2's
        assert db1.get_stats(engine="bing")["total_runs"] == 0

    def test_get_results_by_run(self, two_subjects):
        db1, db2 = two_subjects
        assert {r["url"] for r in db1.get_results_by_run("run-a1")} == {"https://a.example/bad"}
        assert db1.get_results_by_run("run-b1") == []  # leak blocked by another subject's id
        assert {r["url"] for r in db2.get_results_by_run("run-b1")} == {"https://b.example/bad"}

    def test_delete_run_cannot_delete_another_subjects_run(self, two_subjects):
        db1, db2 = two_subjects
        db1.delete_run("run-b1")  # attempt to delete subject 2's run
        assert db2.get_run("run-b1") is not None  # still exists
        db2.delete_run("run-b1")  # the real owner really can
        assert db2.get_run("run-b1") is None

    def test_list_runs(self, two_subjects):
        db1, db2 = two_subjects
        assert {r["apify_run_id"] for r in db1.list_runs()} == {"run-a1"}
        assert {r["apify_run_id"] for r in db2.list_runs()} == {"run-b1"}

    def test_get_run(self, two_subjects):
        db1, db2 = two_subjects
        assert db1.get_run("run-a1") is not None
        assert db1.get_run("run-b1") is None  # leak blocked
        assert db2.get_run("run-b1") is not None
        assert db2.get_run("run-a1") is None

    def test_latest_run_per_country(self, two_subjects):
        db1, db2 = two_subjects
        countries1 = {r["country_code"] for r in db1.latest_run_per_country()}
        countries2 = {r["country_code"] for r in db2.latest_run_per_country()}
        assert countries1 == {"us"}
        assert countries2 == {"es"}

    def test_get_history_pivot(self, two_subjects):
        db1, db2 = two_subjects
        urls1 = {r["url"] for r in db1.get_history_pivot()["rows"]}
        urls2 = {r["url"] for r in db2.get_history_pivot()["rows"]}
        assert urls1 == {"https://a.example/bad"}
        assert urls2 == {"https://b.example/bad"}

    def test_series_stats(self, two_subjects):
        db1, db2 = two_subjects
        series1 = db1.series_stats(engine="google")
        series2 = db2.series_stats(engine="bing")
        assert sum(day["total"] for day in series1) == 1
        assert sum(day["total"] for day in series2) == 1
        # subject 1 asking about subject 2's engine sees nothing of 2's
        assert db1.series_stats(engine="bing") == []


class TestNoFugaAlerts:
    def test_list_alerts(self, two_subjects):
        db1, db2 = two_subjects
        msgs1 = {r["message"] for r in db1.list_alerts(status="all")}
        msgs2 = {r["message"] for r in db2.list_alerts(status="all")}
        assert msgs1 == {"subject 1 alert"}
        assert msgs2 == {"subject 2 alert"}

    def test_count_unseen_alerts(self, two_subjects):
        db1, db2 = two_subjects
        assert db1.count_unseen_alerts() == 1
        assert db2.count_unseen_alerts() == 1

    def test_open_alerts(self, two_subjects):
        db1, db2 = two_subjects
        msgs1 = {r["message"] for r in db1.open_alerts()}
        msgs2 = {r["message"] for r in db2.open_alerts()}
        assert msgs1 == {"subject 1 alert"}
        assert msgs2 == {"subject 2 alert"}

    def test_get_alert(self, two_subjects):
        db1, db2 = two_subjects
        alert_id_2 = db2.list_alerts(status="all")[0]["id"]
        assert db2.get_alert(alert_id_2) is not None
        assert db1.get_alert(alert_id_2) is None  # leak blocked


class TestNoLeakNotes:
    def test_get_notes_map(self, two_subjects):
        db1, db2 = two_subjects
        map1 = db1.get_notes_map()
        map2 = db2.get_notes_map()
        assert set(map1.keys()) == {"https://a.example/bad"}
        assert set(map2.keys()) == {"https://b.example/bad"}

    def test_list_notes(self, two_subjects):
        db1, db2 = two_subjects
        urls1 = {r["url"] for r in db1.list_notes()}
        urls2 = {r["url"] for r in db2.list_notes()}
        assert urls1 == {"https://a.example/bad"}
        assert urls2 == {"https://b.example/bad"}


# ======================================================================= #
# C21b-2: per-table no-leak, for the rest of _SUBJECT_TABLES (debt declared
# by C21b, see the brief's Scope). Same no-leak approach in BOTH
# directions as the classes above -- per table, not a test per
# method: at least one read and one mutation for each.
# ======================================================================= #
class TestNoFugaEvents:
    def test_list_events(self, tmp_path):
        db = _db(tmp_path)
        db1, db2 = db.for_subject(1), db.for_subject(2)
        db1.add_event("2026-01-01", "subject 1 event")
        db2.add_event("2026-01-02", "subject 2 event")
        descs1 = {r["description"] for r in db1.list_events()}
        descs2 = {r["description"] for r in db2.list_events()}
        assert descs1 == {"subject 1 event"}
        assert descs2 == {"subject 2 event"}

    def test_delete_event_cannot_delete_another_subjects_event(self, tmp_path):
        db = _db(tmp_path)
        db1, db2 = db.for_subject(1), db.for_subject(2)
        db2.add_event("2026-01-02", "subject 2 event")
        event_id_2 = db2.list_events()[0]["id"]
        db1.delete_event(event_id_2)  # cross-subject attempt
        assert len(db2.list_events()) == 1
        db2.delete_event(event_id_2)
        assert len(db2.list_events()) == 0


class TestNoFugaNoteHistory:
    def test_note_history_does_not_leak_across_subjects(self, tmp_path):
        db = _db(tmp_path)
        db1, db2 = db.for_subject(1), db.for_subject(2)
        db1.set_note("https://a.example/bad", "note 1")
        db2.set_note("https://b.example/bad", "note 2")
        conn = _raw(db)
        note_id_1 = conn.execute(
            "SELECT rowid FROM url_notes WHERE url = ?", ("https://a.example/bad",)
        ).fetchone()[0]
        note_id_2 = conn.execute(
            "SELECT rowid FROM url_notes WHERE url = ?", ("https://b.example/bad",)
        ).fetchone()[0]
        conn.close()
        db1.set_note_status(note_id_1, "in_progress", "op1")
        db2.set_note_status(note_id_2, "in_progress", "op2")
        by1 = {r["by"] for r in db1.note_history(note_id_1)}
        by2 = {r["by"] for r in db2.note_history(note_id_2)}
        assert by1 == {"op1"}
        assert by2 == {"op2"}
        assert db2.note_history(note_id_1) == []  # leak blocked


class TestNoFugaEvidence:
    def test_add_and_get_evidence(self, tmp_path):
        db = _db(tmp_path)
        db1, db2 = db.for_subject(1), db.for_subject(2)
        e1 = db1.add_evidence("https://a.example/bad", url_hash_fn("https://a.example/bad"),
                               "2026-01-01T00:00:00", "/tmp/a.html", "deadbeef", "manual")
        e2 = db2.add_evidence("https://b.example/bad", url_hash_fn("https://b.example/bad"),
                               "2026-01-01T00:00:00", "/tmp/b.html", "cafebabe", "manual")
        assert db1.get_evidence_by_id(e1) is not None
        assert db1.get_evidence_by_id(e2) is None  # leak blocked
        assert db2.get_evidence_by_id(e2) is not None

    def test_has_evidence_and_evidence_for_are_per_subject(self, tmp_path):
        db = _db(tmp_path)
        db1, db2 = db.for_subject(1), db.for_subject(2)
        db2.add_evidence("https://shared.example/bad", url_hash_fn("https://shared.example/bad"),
                          "2026-01-01T00:00:00", "/tmp/b.html", "cafebabe", "manual")
        assert db2.has_evidence(url_hash_fn("https://shared.example/bad")) is True
        assert db1.has_evidence(url_hash_fn("https://shared.example/bad")) is False
        assert db1.evidence_for(url_hash_fn("https://shared.example/bad")) == []
        assert len(db2.evidence_for(url_hash_fn("https://shared.example/bad"))) == 1


class TestNoFugaUrlLifecycle:
    """Rows inserted by hand to test list_victories in isolation; the real
    writer (processing.lifecycle) is covered per subject by
    TestLifecyclePerSubject further below."""
    def test_list_victories(self, tmp_path):
        db = _db(tmp_path)
        conn = db._connect()
        now = "2026-01-01T00:00:00"
        conn.execute(
            "INSERT INTO url_lifecycle (url_hash, url, country, engine, first_seen, last_seen, "
            "classification, status, gone_since, subject_id) VALUES "
            "('h1', 'https://a.example/gone', 'us', 'google', ?, ?, 'negative', 'gone', ?, 1)",
            (now, now, now),
        )
        conn.execute(
            "INSERT INTO url_lifecycle (url_hash, url, country, engine, first_seen, last_seen, "
            "classification, status, gone_since, subject_id) VALUES "
            "('h2', 'https://b.example/gone', 'es', 'google', ?, ?, 'negative', 'gone', ?, 2)",
            (now, now, now),
        )
        conn.commit()
        conn.close()
        db1, db2 = db.for_subject(1), db.for_subject(2)
        urls1 = {r["url"] for r in db1.list_victories(days=3650)}
        urls2 = {r["url"] for r in db2.list_victories(days=3650)}
        assert urls1 == {"https://a.example/gone"}
        assert urls2 == {"https://b.example/gone"}


def _alert_of(db, subject_id, kind="ssl_expiry", country="us"):
    scoped = db.for_subject(subject_id)
    scoped.add_alert(kind=kind, severity="warning", country=country,
                     message=f"m-{subject_id}", dedupe_key=f"{kind}-{subject_id}")
    return scoped.list_alerts(limit=10)[0]["id"]


def test_a_subject_cannot_acknowledge_another_subjects_alert(tmp_path):
    db = _db(tmp_path)
    victim = _alert_of(db, 2)

    db.for_subject(1).ack_alert(victim, assignee="intruso")

    row = next(a for a in db.for_subject(2).list_alerts(limit=10) if a["id"] == victim)
    assert row["status"] != "acknowledged"
    assert (row["assignee"] or "") != "intruso"


def test_a_subject_cannot_snooze_another_subjects_alert(tmp_path):
    """Snoozing someone else's alert silences it -- it's the same damage
    as resolving it, which WAS already covered."""
    db = _db(tmp_path)
    victim = _alert_of(db, 2)

    db.for_subject(1).snooze_alert(victim, "2099-01-01T00:00:00")

    row = next(a for a in db.for_subject(2).list_alerts(limit=10) if a["id"] == victim)
    assert not row["snoozed_until"]


def test_marking_alerts_seen_does_not_touch_other_subjects(tmp_path):
    """The worst of the three: it doesn't even take an id, it was a global
    UPDATE. Opening one subject's bell marked everyone's alerts as read,
    and a read alert stops notifying."""
    db = _db(tmp_path)
    _alert_of(db, 1, country="us")
    _alert_of(db, 2, country="es")

    db.for_subject(1).mark_alerts_seen()

    assert db.for_subject(2).count_unseen_alerts() == 1
    assert db.for_subject(1).count_unseen_alerts() == 0


def test_a_subject_cannot_overwrite_another_subjects_human_verdict(tmp_path):
    """set_review_verdict used to return the `url` of another subject's row
    (read leak) and recompute the run counts of ANOTHER subject (fan-out to
    another table). Its reason in the allowlist said exactly the opposite."""
    db = _db(tmp_path)
    db.for_subject(2).save_run(apify_run_id="r-2", queries=["q"], country_code="es",
                                status="completed",
                                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db.for_subject(2).save_results([{
        "query": "q", "url": "https://ajeno.example/x", "title": "t", "description": "d",
        "displayed_url": "ajeno.example", "position": 1, "country_code": "es",
        "domain": "ajeno.example", "classification": "negative"}], run_id="r-2")
    victim_id = db.for_subject(2).get_results_filtered(limit=5)[0]["id"]

    assert db.for_subject(1).set_review_verdict(victim_id, "positive") is None

    row = db.for_subject(2).get_results_filtered(limit=5)[0]
    assert row["classification"] == "negative"
    assert row["classified_by"] != "human"


def test_a_bulk_reclassify_never_touches_another_subjects_rows(tmp_path):
    """reclassify_all used to return only a count, and that's why its
    reason considered it harmless -- but it rewrote the classification of
    ALL subjects. The damage was in what it wrote, not in what it showed."""
    db = _db(tmp_path)
    for sid, run in ((1, "r-1"), (2, "r-2")):
        db.for_subject(sid).save_run(apify_run_id=run, queries=["q"], country_code="us",
                                      status="completed",
                                      summary={"total": 1, "positive": 0, "negative": 1,
                                               "neutral": 0})
        db.for_subject(sid).save_results([{
            "query": "q", "url": f"https://s{sid}.example/x", "title": "t", "description": "d",
            "displayed_url": f"s{sid}.example", "position": 1, "country_code": "us",
            "domain": f"s{sid}.example", "classification": "negative"}], run_id=run)

    db.for_subject(1).reclassify_all(lambda rec: {"classification": "positive"})

    assert db.for_subject(1).get_results_filtered(limit=5)[0]["classification"] == "positive"
    assert db.for_subject(2).get_results_filtered(limit=5)[0]["classification"] == "negative"


def test_a_subject_cannot_annotate_severity_on_another_subjects_result(tmp_path):
    db = _db(tmp_path)
    db.for_subject(2).save_run(apify_run_id="r-2", queries=["q"], country_code="es",
                                status="completed",
                                summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
    db.for_subject(2).save_results([{
        "query": "q", "url": "https://ajeno.example/y", "title": "t", "description": "d",
        "displayed_url": "ajeno.example", "position": 1, "country_code": "es",
        "domain": "ajeno.example", "classification": "negative"}], run_id="r-2")
    victim_id = db.for_subject(2).get_results_filtered(limit=5)[0]["id"]

    db.for_subject(1).set_result_severity(victim_id, 5, "intruso")

    row = db.for_subject(2).get_results_filtered(limit=5)[0]
    assert (row["topic"] or "") != "intruso"


# ======================================================================= #
# The three global-uniqueness traps, ALREADY RESOLVED (C21b-2). These
# tests were born asserting the broken behavior, meant to go red the day
# someone fixed it -- and that day arrived, so they now assert the
# opposite. They're kept because they're the only safety net preventing a
# future migration from reverting things to how they were.
# ======================================================================= #
def test_two_subjects_can_have_the_same_alert_dedupe_key(tmp_path):
    """`alerts.dedupe_key` used to be GLOBALLY UNIQUE and `add_alert` uses
    INSERT OR IGNORE, so the second subject's alert didn't error: it just
    DISAPPEARED -- the mute-alerts bug resurrected across tenants. Now the
    UNIQUE is (dedupe_key, subject_id): dedupe still works within each
    subject, and subjects don't overwrite each other."""
    db = _db(tmp_path)
    db.for_subject(1).add_alert(kind="ssl_expiry", severity="warning", country="us",
                                 message="from subject 1", dedupe_key="ssl_expiry-us")
    db.for_subject(2).add_alert(kind="ssl_expiry", severity="critical", country="us",
                                 message="from subject 2", dedupe_key="ssl_expiry-us")

    assert [a["message"] for a in db.for_subject(1).list_alerts(limit=10)] == ["from subject 1"]
    assert [a["message"] for a in db.for_subject(2).list_alerts(limit=10)] == ["from subject 2"]


def test_the_same_dedupe_key_is_still_deduped_within_one_subject(tmp_path):
    """Positive control: without this, "subjects no longer overwrite each
    other" could simply mean dedupe stopped working."""
    db = _db(tmp_path)
    scoped = db.for_subject(1)
    scoped.add_alert(kind="ssl_expiry", severity="warning", country="us",
                     message="first", dedupe_key="same-key")
    scoped.add_alert(kind="ssl_expiry", severity="warning", country="us",
                     message="repeat", dedupe_key="same-key")

    assert len(scoped.list_alerts(limit=10)) == 1


def test_two_subjects_can_annotate_the_same_url(tmp_path):
    """`url_notes` used to have the url as a global PRIMARY KEY -- the only
    one of the three that failed loudly (IntegrityError) instead of
    silently."""
    db = _db(tmp_path)
    db.for_subject(1).set_note("https://same.example/x", "open", "subject 1 note")
    db.for_subject(2).set_note("https://same.example/x", "open", "subject 2 note")

    notes_1 = [n for n in db.for_subject(1).list_notes() if "same.example" in n["url"]]
    notes_2 = [n for n in db.for_subject(2).list_notes() if "same.example" in n["url"]]
    assert len(notes_1) == 1
    assert len(notes_2) == 1
    assert notes_1[0]["url"] == notes_2[0]["url"]  # the SAME url, two different rows


def test_the_unique_migration_preserves_rows_and_note_history_links(tmp_path):
    """The migration recreates three tables (SQLite doesn't let you drop a
    constraint any other way). It starts from the truly OLD schema --
    with `url TEXT PRIMARY KEY` and the global UNIQUE on dedupe_key --
    because a DB created by the current code is already born migrated and
    wouldn't prove anything.

    What's verified is that not a single row is lost and that
    `url_notes`'s rowids survive: `note_history.note_id` hangs off them,
    so letting SQLite reassign them while copying would have silently
    rewritten each note's history, attributing transitions to the wrong
    note."""
    import sqlite3

    from database.models import Database

    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE url_notes (url TEXT PRIMARY KEY, status TEXT, note TEXT,
                                updated_at TIMESTAMP);
        CREATE TABLE note_history (id INTEGER PRIMARY KEY AUTOINCREMENT, note_id INTEGER,
                                   from_status TEXT, to_status TEXT, by TEXT, at TEXT,
                                   comment TEXT);
        CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TIMESTAMP,
                             kind TEXT, severity TEXT, country TEXT, message TEXT,
                             dedupe_key TEXT UNIQUE, seen INTEGER DEFAULT 0);
        CREATE TABLE goals (id INTEGER PRIMARY KEY AUTOINCREMENT, country TEXT DEFAULT '',
                            metric TEXT, target REAL, created_at TEXT,
                            subject_id INTEGER DEFAULT 1, UNIQUE(country, metric));
    """)
    conn.execute("INSERT INTO url_notes (url, status, note) VALUES (?, ?, ?)",
                  ("https://old.example/a", "open", "note A"))
    conn.execute("INSERT INTO url_notes (url, status, note) VALUES (?, ?, ?)",
                  ("https://old.example/b", "in_progress", "note B"))
    note_b_rowid = conn.execute(
        "SELECT rowid FROM url_notes WHERE url = ?", ("https://old.example/b",)
    ).fetchone()[0]
    conn.execute("INSERT INTO note_history (note_id, from_status, to_status, by, at) "
                  "VALUES (?, 'open', 'in_progress', 'tester', '2026-01-01T00:00:00')",
                  (note_b_rowid,))
    conn.execute("INSERT INTO alerts (kind, severity, country, message, dedupe_key) "
                  "VALUES ('ssl_expiry', 'warning', 'us', 'old', 'old-key')")
    conn.execute("INSERT INTO goals (country, metric, target) VALUES ('', 'pct_positive', 42.0)")
    conn.commit()
    conn.close()

    Database(db_path=path)  # the migration runs here

    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM url_notes").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 1
    # note B's rowid survives, so its history is still its own
    assert conn.execute("SELECT rowid FROM url_notes WHERE url = ?",
                         ("https://old.example/b",)).fetchone()[0] == note_b_rowid
    assert conn.execute("SELECT note_id FROM note_history").fetchone()[0] == note_b_rowid
    # and no temporary migration table remains
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '%_pre_c21b2'"
    ).fetchone()[0] == 0
    conn.close()


def test_the_unique_migration_is_idempotent(tmp_path):
    """Runs on every boot: a second pass must not recreate anything or
    duplicate rows."""
    from database.models import Database

    path = str(tmp_path / "idem.db")
    db = Database(db_path=path)
    db.for_subject(1).set_note("https://idem.example/a", "note")
    before = len(db.for_subject(1).get_notes_map())

    Database(db_path=path)
    Database(db_path=path)

    assert len(db.for_subject(1).get_notes_map()) == before


# ======================================================================= #
# Multi-subject phase 1: per-subject domains, lifecycle writer with the
# 4-column PK, and a sweep over EVERY subject table through the public API
# under the request ContextVar.
# ======================================================================= #
class TestNoLeakDomains:
    def test_positive_and_negative_domains_are_per_subject(self, tmp_path):
        db = _db(tmp_path)
        db1, db2 = db.for_subject(1), db.for_subject(2)
        db1.add_positive_domain("shared.example")
        db2.add_positive_domain("SHARED.example")  # same domain, own row (PK domain+subject)
        db2.add_positive_domain("two.example")
        db1.add_negative_domain("bad-one.example")
        db2.add_negative_domain("bad-two.example")
        assert db1.list_positive_domains() == ["shared.example"]
        assert db2.list_positive_domains() == ["shared.example", "two.example"]
        assert db1.list_negative_domains() == ["bad-one.example"]
        assert db2.list_negative_domains() == ["bad-two.example"]
        assert db.for_all_subjects().list_positive_domains() == ["shared.example",
                                                                 "two.example"]

    def test_removing_a_domain_only_touches_the_active_subject(self, tmp_path):
        db = _db(tmp_path)
        db1, db2 = db.for_subject(1), db.for_subject(2)
        db1.add_positive_domain("shared.example")
        db2.add_positive_domain("shared.example")
        db1.remove_positive_domain("shared.example")
        assert db1.list_positive_domains() == []
        assert db2.list_positive_domains() == ["shared.example"]
        db2.add_negative_domain("x.example")
        db1.remove_negative_domain("x.example")
        assert db2.list_negative_domains() == ["x.example"]


class TestLifecyclePerSubject:
    """The real writer (processing.lifecycle) with two subjects observing the SAME
    URL in the same country/engine: independent rows, independent trajectories."""

    @staticmethod
    def _run(db, run_id, urls, query):
        db.save_run(apify_run_id=run_id, queries=[query], country_code="us",
                    status="completed",
                    summary={"total": len(urls), "positive": 0, "negative": len(urls),
                             "neutral": 0})
        db.save_results([
            {"query": query, "url": u, "title": "t", "description": "d", "position": i + 1,
             "country_code": "us", "domain": u.split("/")[2], "classification": "negative",
             "engine": "google"}
            for i, u in enumerate(urls)
        ], run_id=run_id)

    def test_same_url_two_subjects_independent_rows(self, tmp_path):
        from processing.lifecycle import update_url_lifecycle

        db = _db(tmp_path)
        db.set_setting("gone_after_runs", "2")
        db1, db2 = db.for_subject(1), db.for_subject(2)
        shared = "https://shared.example/bad"
        self._run(db1, "a1", [shared, "https://filler1.example/x"], "Subject A")
        update_url_lifecycle(db1, "a1")
        self._run(db2, "b1", [shared, "https://filler2.example/x"], "Subject B")
        update_url_lifecycle(db2, "b1")

        conn = _raw(db)
        rows = conn.execute("SELECT subject_id, status FROM url_lifecycle WHERE url = ? "
                            "ORDER BY subject_id", (shared,)).fetchall()
        conn.close()
        assert [tuple(r) for r in rows] == [(1, "active"), (2, "active")]

        # Subject B stops seeing the URL for 2 runs -> gone for B only.
        for i in (2, 3):
            self._run(db2, f"b{i}", ["https://filler2.example/x"], "Subject B")
            update_url_lifecycle(db2, f"b{i}")
        conn = _raw(db)
        status = dict(conn.execute("SELECT subject_id, status FROM url_lifecycle "
                                   "WHERE url = ?", (shared,)).fetchall())
        conn.close()
        assert status == {1: "active", 2: "gone"}
        assert [w["url"] for w in db2.list_victories(days=3650)] == [shared]
        assert db1.list_victories(days=3650) == []
        # The url_gone alert is B's; A's next run (self-healing sweep) must not
        # re-emit it under A.
        self._run(db1, "a2", [shared, "https://filler1.example/x"], "Subject A")
        update_url_lifecycle(db1, "a2")
        assert [a["kind"] for a in db2.list_alerts(status="all")] == ["url_gone"]
        assert [a for a in db1.list_alerts(status="all") if a["kind"] == "url_gone"] == []


def test_every_subject_table_is_written_under_the_contextvar_subject(tmp_path):
    """Drive every _SUBJECT_TABLES table through its public write path with the
    request ContextVar set to subject 2 (an UNSCOPED instance, like the dashboard
    singleton): every row must land on subject 2, and subject 1 must see none of
    it. `goals` has no Database write API (only its schema) -- written raw."""
    from database.models import subject_scope
    from processing.lifecycle import update_url_lifecycle

    db = _db(tmp_path)
    url = "https://only-two.example/bad"
    with subject_scope(2):
        db.save_run(apify_run_id="r2", queries=["Subject B"], country_code="us",
                    status="completed",
                    summary={"total": 1, "positive": 0, "negative": 1, "neutral": 0})
        db.save_results([{"query": "Subject B", "url": url, "country_code": "us",
                          "classification": "negative", "domain": "only-two.example",
                          "position": 1, "engine": "google"}], run_id="r2")
        update_url_lifecycle(db, "r2")
        db.add_alert("manual", "warning", "us", "two", "dedupe-two")
        db.add_evidence(url, url_hash_fn(url), "2026-01-01T00:00:00", "/tmp/x.html",
                        "deadbeef", "manual")
        db.add_event("2026-01-01", "two event")
        db.set_note(url, "two note")
        note_id = db.list_notes()[0]["id"]
        db.set_note_status(note_id, "in_progress", by="op2")
        db.add_positive_domain("own-two.example")
        db.add_negative_domain("only-two.example")
    conn = _raw(db)
    conn.execute("INSERT INTO goals (country, metric, target, subject_id) "
                 "VALUES ('us', 'pct_positive', 50, 2)")
    conn.commit()
    per_table = {}
    for table in Database._SUBJECT_TABLES:
        per_table[table] = {r[0] for r in conn.execute(f"SELECT DISTINCT subject_id FROM {table}")}  # noqa: S608 -- table from Database._SUBJECT_TABLES
    conn.close()
    assert per_table == {t: {2} for t in Database._SUBJECT_TABLES}

    db1 = db.for_subject(1)
    assert db1.list_runs() == []
    assert db1.get_results_filtered() == []
    assert db1.list_alerts(status="all") == []
    assert db1.list_events() == []
    assert db1.list_notes() == []
    assert db1.list_positive_domains() == []
    assert db1.list_negative_domains() == []
    assert db1.evidence_for(url_hash_fn(url)) == []
