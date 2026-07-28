import re
import sqlite3
import threading
from datetime import datetime

from meeting_scribe.storage.database import Database, _generate_meeting_code


def test_database_usable_from_a_different_thread_than_it_was_created_on(tmp_path):
    # Regression test: the GUI creates one Database on the main thread but writes to it from
    # background worker threads (finishing a meeting, answering a question) so the UI doesn't freeze.
    # sqlite3 connections default to check_same_thread=True, which raises "SQLite objects created in a
    # thread can only be used in that same thread" the moment a worker thread touches them.
    with Database(tmp_path / "test.db") as db:
        project = db.create_project("Cross Thread")
        result = {}

        def worker():
            try:
                meeting_id = db.create_meeting(project.id, "From a worker thread")
                db.add_transcript_segment(meeting_id, "mic", 0.0, "hello from a worker thread")
                db.finish_meeting(meeting_id, transcript_text="hello from a worker thread")
                result["meeting_id"] = meeting_id
            except Exception as exc:  # noqa: BLE001 - want to assert on any exception, not just one type
                result["error"] = exc

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        assert "error" not in result, f"worker thread raised: {result.get('error')}"
        meeting = db.get_meeting(result["meeting_id"])
        assert meeting.transcript_text == "hello from a worker thread"


def test_create_project_and_meeting(tmp_path):
    with Database(tmp_path / "test.db") as db:
        project = db.create_project("Acme Renewal")
        assert project.slug == "acme-renewal"

        same = db.get_or_create_project("Acme Renewal")
        assert same.id == project.id

        meeting_id = db.create_meeting(project.id, "Kickoff call")
        db.add_transcript_segment(meeting_id, "mic", 0.0, "Let's get started.")
        db.add_transcript_segment(meeting_id, "system", 5.5, "Sure, sounds good.")
        db.add_transcript_segment(meeting_id, "screen_ocr", 6.0, "Slide: Q3 Roadmap")

        segments = db.get_segments(meeting_id)
        assert [s["text"] for s in segments] == [
            "Let's get started.",
            "Sure, sounds good.",
            "Slide: Q3 Roadmap",
        ]

        db.finish_meeting(
            meeting_id,
            transcript_text="Let's get started.\nSure, sounds good.\nSlide: Q3 Roadmap",
            notes_markdown="## Summary\nKicked off the Q3 roadmap discussion.",
        )
        meeting = db.get_meeting(meeting_id)
        assert meeting.notes_markdown.startswith("## Summary")
        assert meeting.ended_at is not None


def test_project_isolation(tmp_path):
    with Database(tmp_path / "test.db") as db:
        p1 = db.create_project("Project One")
        p2 = db.create_project("Project Two")
        m1 = db.create_meeting(p1.id, "Meeting")
        db.add_transcript_segment(m1, "mic", 0.0, "unique keyword zzyzx here")
        db.finish_meeting(m1, transcript_text="unique keyword zzyzx here")

        db.add_document(p1.id, "p1-doc.txt", "belongs to project one")

        assert [m.id for m in db.list_meetings(p1.id)] == [m1]
        assert db.list_meetings(p2.id) == []
        assert len(db.list_documents(p1.id)) == 1
        assert db.list_documents(p2.id) == []


def test_set_manual_notes_persists_and_can_be_overwritten(tmp_path):
    with Database(tmp_path / "test.db") as db:
        project = db.create_project("Manual Notes")
        meeting_id = db.create_meeting(project.id, "Kickoff")

        db.set_manual_notes(meeting_id, "First note.")
        assert db.get_meeting(meeting_id).manual_notes == "First note."

        db.set_manual_notes(meeting_id, "First note.\nSecond note.")
        assert db.get_meeting(meeting_id).manual_notes == "First note.\nSecond note."


def test_meetings_created_before_manual_notes_existed_get_the_column_via_migration(tmp_path):
    # Simulates a database from before manual_notes was added to SCHEMA: create the meetings table
    # without that column, then open it with Database and confirm the ALTER TABLE migration catches it
    # up rather than crashing on "no such column" the first time a Meeting row is read.
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE meetings (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            transcript_text TEXT,
            notes_markdown TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    with Database(db_path) as db:
        project = db.create_project("Legacy")
        meeting_id = db.create_meeting(project.id, "Pre-existing schema")

        meeting = db.get_meeting(meeting_id)
        assert meeting.manual_notes is None

        db.set_manual_notes(meeting_id, "Works after migration.")
        assert db.get_meeting(meeting_id).manual_notes == "Works after migration."


def test_set_attendees_persists_and_can_be_overwritten(tmp_path):
    with Database(tmp_path / "test.db") as db:
        project = db.create_project("Attendees")
        meeting_id = db.create_meeting(project.id, "Kickoff")

        assert db.get_meeting(meeting_id).attendees is None

        db.set_attendees(meeting_id, "John Smith\nJane Doe")
        assert db.get_meeting(meeting_id).attendees == "John Smith\nJane Doe"

        db.set_attendees(meeting_id, "John Smith\nJane Doe\nAlex Lee")
        assert db.get_meeting(meeting_id).attendees == "John Smith\nJane Doe\nAlex Lee"


def test_meetings_created_before_attendees_existed_get_the_column_via_migration(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE meetings (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            transcript_text TEXT,
            notes_markdown TEXT,
            manual_notes TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    with Database(db_path) as db:
        project = db.create_project("Legacy")
        meeting_id = db.create_meeting(project.id, "Pre-existing schema")

        assert db.get_meeting(meeting_id).attendees is None

        db.set_attendees(meeting_id, "Works after migration.")
        assert db.get_meeting(meeting_id).attendees == "Works after migration."


def test_update_meeting_title_renames_in_place(tmp_path):
    with Database(tmp_path / "test.db") as db:
        project = db.create_project("Renaming")
        meeting_id = db.create_meeting(project.id, "Draft title")

        db.update_meeting_title(meeting_id, "Weekly Client Meeting")

        assert db.get_meeting(meeting_id).title == "Weekly Client Meeting"


def test_list_recent_meeting_titles_is_distinct_and_most_recent_first(tmp_path):
    with Database(tmp_path / "test.db") as db:
        project = db.create_project("Repeats")
        other_project = db.create_project("Other")

        db.create_meeting(project.id, "Weekly Client Meeting", started_at="2026-01-01T00:00:00+00:00")
        db.create_meeting(project.id, "Kickoff", started_at="2026-01-02T00:00:00+00:00")
        db.create_meeting(project.id, "Weekly Client Meeting", started_at="2026-01-03T00:00:00+00:00")
        db.create_meeting(other_project.id, "Should not appear", started_at="2026-01-04T00:00:00+00:00")

        assert db.list_recent_meeting_titles(project.id) == ["Weekly Client Meeting", "Kickoff"]


def test_create_meeting_assigns_a_meeting_code_in_the_yyyymmdd_hhmm_format(tmp_path):
    with Database(tmp_path / "test.db") as db:
        project = db.create_project("Codes")
        meeting_id = db.create_meeting(project.id, "Kickoff")

        assert re.fullmatch(r"\d{8}-\d{4}", db.get_meeting(meeting_id).meeting_code)


def test_generate_meeting_code_appends_a_random_suffix_when_the_base_code_is_taken():
    base = datetime.now().strftime("%Y%m%d-%H%M")

    class FakeConn:
        def execute(self, _query):
            return [{"meeting_code": base}]

    code = _generate_meeting_code(FakeConn())

    assert code != base
    assert re.fullmatch(rf"{re.escape(base)}-[A-Z0-9]{{3}}", code)


def test_meetings_created_before_meeting_code_existed_get_the_column_via_migration(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE meetings (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            transcript_text TEXT,
            notes_markdown TEXT,
            manual_notes TEXT,
            attendees TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    with Database(db_path) as db:
        project = db.create_project("Legacy")
        meeting_id = db.create_meeting(project.id, "Pre-existing schema")

        assert re.fullmatch(r"\d{8}-\d{4}", db.get_meeting(meeting_id).meeting_code)


def test_add_document_stores_and_returns_source_path(tmp_path):
    with Database(tmp_path / "test.db") as db:
        project = db.create_project("Source Paths")

        db.add_document(project.id, "invite.pdf", "invite text", source_path="/vault/abc123.pdf")

        [document] = db.list_documents(project.id)
        assert document["source_path"] == "/vault/abc123.pdf"


def test_add_document_defaults_source_path_to_none(tmp_path):
    with Database(tmp_path / "test.db") as db:
        project = db.create_project("No Source Path")

        db.add_document(project.id, "invite.pdf", "invite text")

        [document] = db.list_documents(project.id)
        assert document["source_path"] is None


def test_documents_created_before_source_path_existed_get_the_column_via_migration(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL,
            meeting_id INTEGER,
            filename TEXT NOT NULL,
            content_text TEXT NOT NULL,
            added_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    with Database(db_path) as db:
        project = db.create_project("Legacy")
        db.add_document(project.id, "invite.pdf", "invite text", source_path="/vault/abc123.pdf")

        [document] = db.list_documents(project.id)
        assert document["source_path"] == "/vault/abc123.pdf"


def test_list_documents_for_meeting_scopes_to_that_meeting(tmp_path):
    with Database(tmp_path / "test.db") as db:
        project = db.create_project("Doc Scoping")
        meeting_a = db.create_meeting(project.id, "Meeting A")
        meeting_b = db.create_meeting(project.id, "Meeting B")

        db.add_document(project.id, "invite.png", "invite text", meeting_id=meeting_a)
        db.add_document(project.id, "spec.pdf", "project-wide spec, no meeting")

        meeting_a_docs = db.list_documents_for_meeting(meeting_a)
        meeting_b_docs = db.list_documents_for_meeting(meeting_b)

        assert [d["filename"] for d in meeting_a_docs] == ["invite.png"]
        assert meeting_b_docs == []
