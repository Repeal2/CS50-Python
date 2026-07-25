import threading

from meeting_scribe.storage.database import Database


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


def test_search_finds_segments_documents_and_notes(tmp_path):
    with Database(tmp_path / "test.db") as db:
        project = db.create_project("Search Test")
        meeting_id = db.create_meeting(project.id, "Budget review")
        db.add_transcript_segment(meeting_id, "mic", 0.0, "We need to finalize the budget by Friday.")
        db.finish_meeting(
            meeting_id,
            transcript_text="We need to finalize the budget by Friday.",
            notes_markdown="Action item: finalize budget by Friday.",
        )
        db.add_document(project.id, "invite.png", "Budget Review Meeting - Friday 3pm")

        hits = db.search_project(project.id, "budget Friday")
        kinds = {hit.kind for hit in hits}
        assert kinds == {"segment", "document", "meeting"}


def test_search_empty_query_returns_nothing(tmp_path):
    with Database(tmp_path / "test.db") as db:
        project = db.create_project("Empty")
        assert db.search_project(project.id, "!!!") == []


def test_project_isolation(tmp_path):
    with Database(tmp_path / "test.db") as db:
        p1 = db.create_project("Project One")
        p2 = db.create_project("Project Two")
        m1 = db.create_meeting(p1.id, "Meeting")
        db.add_transcript_segment(m1, "mic", 0.0, "unique keyword zzyzx here")
        db.finish_meeting(m1, transcript_text="unique keyword zzyzx here")

        assert len(db.search_project(p1.id, "zzyzx")) == 1
        assert len(db.search_project(p2.id, "zzyzx")) == 0


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
