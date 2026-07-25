from meeting_scribe.storage.database import Database


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
