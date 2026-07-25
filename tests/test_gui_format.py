import pytest

# gui.app imports tkinter at module level, which isn't installed in this dev sandbox (no display) but
# is present on the Windows CI runner where the app actually runs — skip here, run for real there.


def test_format_meeting_timestamp_is_readable_and_localized():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    formatted = gui_app._format_meeting_timestamp("2026-07-25T12:17:32.123456+00:00")
    assert "T" not in formatted
    assert "2026" in formatted


def test_format_meeting_timestamp_falls_back_to_raw_string_on_bad_input():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    assert gui_app._format_meeting_timestamp("not-a-date") == "not-a-date"


NOTES_MARKDOWN = """## Summary
We discussed the Q3 roadmap.

## Key Discussion Points
- Timeline
- Budget

## Decisions
- Ship in October

## Action Items
| Owner | Action | Due date |
|---|---|---|
| Alex | Draft the spec | Friday |
"""


def test_extract_section_returns_just_that_sections_body():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    section = gui_app._extract_section(NOTES_MARKDOWN, "Action Items")
    assert section is not None
    assert "Alex" in section
    assert "Draft the spec" in section
    assert "## Decisions" not in section
    assert "## Action Items" not in section  # heading itself is stripped, only the body remains


def test_extract_section_returns_none_when_heading_absent():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    assert gui_app._extract_section(NOTES_MARKDOWN, "Risks") is None
