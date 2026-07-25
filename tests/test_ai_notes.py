import pytest

from meeting_scribe.ai.notes import generate_notes


def test_generate_notes_rejects_empty_transcript():
    with pytest.raises(ValueError):
        generate_notes("   \n  ", api_key="fake-key", model="claude-opus-5")
