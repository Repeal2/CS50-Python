import uuid
from unittest.mock import patch

from meeting_scribe.ai.search import _render_hits, ask
from meeting_scribe.storage.database import Database, SearchHit


def test_render_hits_includes_label_and_meeting_id():
    hits = [SearchHit(kind="segment", meeting_id=3, label="transcript (mic)", snippet="hello", rank=0.1)]
    rendered = _render_hits(hits)
    assert "meeting_id=3" in rendered
    assert "transcript (mic)" in rendered
    assert "hello" in rendered


def test_ask_short_circuits_on_no_hits_without_touching_the_bridge(tmp_path):
    with Database(tmp_path / "test.db") as db:
        project = db.create_project("Empty Project")
        answer = ask(
            db,
            project.id,
            "anything",
            inbox_dir=tmp_path / "Inbox",
            outbox_dir=tmp_path / "Outbox",
            poll_interval_seconds=0.01,
            timeout_seconds=1,
        )
        assert "couldn't find anything" in answer
        assert not (tmp_path / "Inbox").exists()


def test_ask_submits_retrieved_passages_and_question_to_the_bridge(tmp_path):
    inbox_dir = tmp_path / "Inbox"
    outbox_dir = tmp_path / "Outbox"
    outbox_dir.mkdir(parents=True)
    fixed_id = uuid.UUID(int=2)
    (outbox_dir / f"{fixed_id.hex}__search.response.txt").write_text("The answer.", encoding="utf-8")

    with Database(tmp_path / "test.db") as db:
        project = db.create_project("Test Project")
        meeting_id = db.create_meeting(project.id, "Kickoff")
        db.add_transcript_segment(meeting_id, "mic", 1.0, "we decided to ship in October")
        db.finish_meeting(meeting_id, transcript_text="we decided to ship in October", notes_markdown=None)

        with patch("meeting_scribe.ai.copilot_bridge.uuid.uuid4", return_value=fixed_id):
            answer = ask(
                db,
                project.id,
                "when are we shipping?",
                inbox_dir=inbox_dir,
                outbox_dir=outbox_dir,
                poll_interval_seconds=0.01,
                timeout_seconds=1,
            )

    assert answer == "The answer."
    [request_path] = list(inbox_dir.iterdir())
    content = request_path.read_text(encoding="utf-8")
    assert "when are we shipping?" in content
    assert "ship in October" in content
