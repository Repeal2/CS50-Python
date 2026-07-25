import uuid
from unittest.mock import patch

import pytest

from meeting_scribe.ai.copilot_bridge import CopilotResponseTimeout
from meeting_scribe.ai.notes import generate_notes


def test_generate_notes_rejects_empty_transcript(tmp_path):
    with pytest.raises(ValueError):
        generate_notes(
            "   \n  ",
            inbox_dir=tmp_path / "Inbox",
            outbox_dir=tmp_path / "Outbox",
            poll_interval_seconds=0.01,
            timeout_seconds=1,
        )


def test_generate_notes_returns_the_outbox_response(tmp_path):
    inbox_dir = tmp_path / "Inbox"
    outbox_dir = tmp_path / "Outbox"
    outbox_dir.mkdir(parents=True)
    fixed_id = uuid.UUID(int=0)
    (outbox_dir / f"{fixed_id.hex}__notes.response.txt").write_text("Generated notes.", encoding="utf-8")

    with patch("meeting_scribe.ai.copilot_bridge.uuid.uuid4", return_value=fixed_id):
        result = generate_notes(
            "hello world",
            inbox_dir=inbox_dir,
            outbox_dir=outbox_dir,
            poll_interval_seconds=0.01,
            timeout_seconds=1,
        )

    assert result == "Generated notes."


def test_generate_notes_writes_the_system_prompt_and_transcript_to_the_inbox(tmp_path):
    inbox_dir = tmp_path / "Inbox"
    outbox_dir = tmp_path / "Outbox"
    outbox_dir.mkdir(parents=True)
    fixed_id = uuid.UUID(int=1)
    (outbox_dir / f"{fixed_id.hex}__notes.response.txt").write_text("Notes.", encoding="utf-8")

    with patch("meeting_scribe.ai.copilot_bridge.uuid.uuid4", return_value=fixed_id):
        generate_notes(
            "the transcript body",
            inbox_dir=inbox_dir,
            outbox_dir=outbox_dir,
            poll_interval_seconds=0.01,
            timeout_seconds=1,
            system_prompt="My custom instructions",
        )

    [request_path] = list(inbox_dir.iterdir())
    content = request_path.read_text(encoding="utf-8")
    assert "My custom instructions" in content
    assert "the transcript body" in content


def test_generate_notes_raises_on_timeout(tmp_path):
    with pytest.raises(CopilotResponseTimeout):
        generate_notes(
            "hello",
            inbox_dir=tmp_path / "Inbox",
            outbox_dir=tmp_path / "Outbox",
            poll_interval_seconds=0.01,
            timeout_seconds=0.03,
        )
