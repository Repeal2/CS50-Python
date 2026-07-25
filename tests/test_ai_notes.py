from unittest.mock import MagicMock, patch

import pytest

from meeting_scribe.ai.notes import generate_notes
from meeting_scribe.config import DEFAULT_NOTES_SYSTEM_PROMPT


def test_generate_notes_rejects_empty_transcript():
    with pytest.raises(ValueError):
        generate_notes("   \n  ", api_key="fake-key", model="claude-opus-5")


def _fake_response(text="Notes."):
    response = MagicMock()
    response.stop_reason = "end_turn"
    response.content = [MagicMock(type="text", text=text)]
    return response


def test_generate_notes_defaults_to_the_recommended_system_prompt():
    with patch("meeting_scribe.ai.notes.anthropic.Anthropic") as MockAnthropic:
        MockAnthropic.return_value.messages.create.return_value = _fake_response()

        generate_notes("hello", api_key="fake-key", model="claude-opus-5")

        _args, kwargs = MockAnthropic.return_value.messages.create.call_args
        assert kwargs["system"] == DEFAULT_NOTES_SYSTEM_PROMPT


def test_generate_notes_uses_a_custom_system_prompt_when_given():
    with patch("meeting_scribe.ai.notes.anthropic.Anthropic") as MockAnthropic:
        MockAnthropic.return_value.messages.create.return_value = _fake_response()

        generate_notes("hello", api_key="fake-key", model="claude-opus-5", system_prompt="Custom prompt.")

        _args, kwargs = MockAnthropic.return_value.messages.create.call_args
        assert kwargs["system"] == "Custom prompt."
