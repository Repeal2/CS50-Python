from meeting_scribe.ai.copilot_push import format_meeting_for_push, push_meeting


def test_format_meeting_for_push_includes_project_title_and_transcript():
    content = format_meeting_for_push(
        project_name="Acme Renewal",
        title="Kickoff",
        transcript_text="let's get started",
        manual_notes=None,
    )

    assert "Acme Renewal" in content
    assert "Kickoff" in content
    assert "let's get started" in content
    assert "MANUAL NOTES" not in content


def test_format_meeting_for_push_includes_manual_notes_when_present():
    content = format_meeting_for_push(
        project_name="Acme Renewal",
        title="Kickoff",
        transcript_text="let's get started",
        manual_notes="Follow up with legal.",
    )

    assert "MANUAL NOTES" in content
    assert "Follow up with legal." in content


def test_format_meeting_for_push_handles_an_empty_transcript():
    content = format_meeting_for_push(
        project_name="Acme Renewal", title="Kickoff", transcript_text="", manual_notes=None
    )

    assert "no transcript captured" in content


def test_push_meeting_writes_a_uniquely_named_file(tmp_path):
    inbox_dir = tmp_path / "Inbox"

    first = push_meeting("first meeting content", inbox_dir=inbox_dir)
    second = push_meeting("second meeting content", inbox_dir=inbox_dir)

    assert first != second
    assert first.read_text(encoding="utf-8") == "first meeting content"
    assert second.read_text(encoding="utf-8") == "second meeting content"
    assert len(list(inbox_dir.iterdir())) == 2


def test_push_meeting_creates_the_inbox_directory(tmp_path):
    inbox_dir = tmp_path / "nested" / "Inbox"

    push_meeting("content", inbox_dir=inbox_dir)

    assert inbox_dir.is_dir()
