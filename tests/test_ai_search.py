from meeting_scribe.ai.search import _render_hits, ask
from meeting_scribe.storage.database import Database, SearchHit


def test_render_hits_includes_label_and_meeting_id():
    hits = [SearchHit(kind="segment", meeting_id=3, label="transcript (mic)", snippet="hello", rank=0.1)]
    rendered = _render_hits(hits)
    assert "meeting_id=3" in rendered
    assert "transcript (mic)" in rendered
    assert "hello" in rendered


def test_ask_short_circuits_on_no_hits_without_calling_claude(tmp_path):
    with Database(tmp_path / "test.db") as db:
        project = db.create_project("Empty Project")
        answer = ask(db, project.id, "anything", api_key="fake-key", model="claude-opus-5")
        assert "couldn't find anything" in answer
