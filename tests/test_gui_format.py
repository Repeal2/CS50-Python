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


def test_notes_bullet_prefix_matches_a_dash_bullet_and_its_indentation():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    match = gui_app._NOTES_BULLET_PREFIX.match("    - Follow up with legal")
    assert match.group(0) == "    - "


def test_notes_bullet_prefix_matches_plain_lines_as_just_leading_whitespace():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    match = gui_app._NOTES_BULLET_PREFIX.match("  Just a plain note, no bullet")
    assert match.group(0) == "  "


def test_notes_bullet_prefix_matches_asterisk_and_dot_bullets():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    assert gui_app._NOTES_BULLET_PREFIX.match("* Something").group(0) == "* "
    assert gui_app._NOTES_BULLET_PREFIX.match("• Something").group(0) == "• "


def test_notes_bullet_prefix_matches_an_empty_line():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    assert gui_app._NOTES_BULLET_PREFIX.match("").group(0) == ""


def test_no_mic_signal_is_true_for_the_digital_silence_warning():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    problems = (
        "Microphone: no signal at all — every sample is digital silence. A working device always has "
        "some noise floor, so this is almost certainly the wrong input, an unplugged one, or one muted "
        "at the driver.",
    )
    assert gui_app._has_no_mic_signal(problems) is True


def test_no_mic_signal_is_false_with_no_problems():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    assert gui_app._has_no_mic_signal(()) is False


def test_no_mic_signal_ignores_other_kinds_of_mic_and_system_problems():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    # A quiet mic (still producing signal, just nothing above speech level) and clipping are real
    # problems, but neither one is "no input at all" — only digital silence is.
    problems = (
        "Microphone: nothing above speech level in 120s (peak -58 dBFS) while the other track was "
        "active — check the right device is selected and that it isn't muted.",
        "Microphone: clipping (peak -1 dBFS). Turn the input level down in Windows, or the recording "
        "will distort.",
        "System audio: no signal at all — every sample is digital silence. A working device always has "
        "some noise floor, so this is almost certainly the wrong input, an unplugged one, or one muted "
        "at the driver.",
    )
    assert gui_app._has_no_mic_signal(problems) is False


def test_hotkey_label_shows_not_set_for_none():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    assert gui_app._hotkey_label(None) == "Not set"


def test_hotkey_label_shows_the_combo():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    from meeting_scribe.hotkeys import MOD_CONTROL, MOD_SHIFT, HotkeyCombo

    combo = HotkeyCombo(modifiers=MOD_CONTROL | MOD_SHIFT, vk=0x53)
    assert gui_app._hotkey_label(combo) == "Ctrl+Shift+S"


def test_device_choices_lists_system_default_first_then_every_present_device():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    assert gui_app._device_choices(["Headset", "Speakers"], "Headset") == [
        gui_app.SYSTEM_DEFAULT_LABEL, "Headset", "Speakers",
    ]


def test_device_choices_keeps_a_selected_device_that_is_currently_unplugged():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    # The lists now refresh on their own as devices come and go. Resetting an unplugged selection to
    # "System default" would silently become the saved setting on the next Settings save.
    assert gui_app._device_choices(["Speakers"], "Jabra Evolve") == [
        gui_app.SYSTEM_DEFAULT_LABEL, "Speakers", "Jabra Evolve",
    ]


def test_device_choices_does_not_duplicate_system_default():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    assert gui_app._device_choices([], gui_app.SYSTEM_DEFAULT_LABEL) == [gui_app.SYSTEM_DEFAULT_LABEL]


def _segment_row(timestamp, source, text, *, speaker=None, engine=None):
    return {"timestamp_seconds": timestamp, "source": source, "text": text, "speaker": speaker, "engine": engine}


def test_meeting_transcripts_keeps_this_pcs_and_the_clouds_apart():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    rows = [
        _segment_row(0.0, "mic", "let's start", engine="local"),
        _segment_row(0.1, "mic", "let's start", engine="runpod"),
        _segment_row(2.0, "system", "sounds could", engine="local"),
        _segment_row(2.0, "system", "sounds good", speaker="SPEAKER_00", engine="runpod"),
        _segment_row(3.0, "screen_ocr", "Slide: Agenda"),
    ]

    screen, local, cloud = gui_app._meeting_transcripts(rows)

    assert screen == "[00:03] Screen: Slide: Agenda"
    assert local == "[00:00] You: let's start\n[00:02] Others: sounds could"
    assert cloud == "[00:00] You: let's start\n[00:02] SPEAKER_00: sounds good"


def test_meeting_transcripts_without_the_cloud_has_no_cloud_transcript():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    # Includes a line saved before lines were tagged by engine, which counts as this PC's.
    rows = [_segment_row(0.0, "mic", "hello", engine="local"), _segment_row(1.0, "system", "hi")]

    _screen, local, cloud = gui_app._meeting_transcripts(rows)

    assert local == "[00:00] You: hello\n[00:01] Others: hi"
    assert cloud == ""


def test_meeting_export_text_heads_each_transcript_when_there_are_two():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    from meeting_scribe.storage.database import Meeting

    meeting = Meeting(
        id=1, project_id=1, title="Kickoff", started_at="2026-07-25T10:00:00+00:00",
        ended_at="2026-07-25T10:30:00+00:00", transcript_text=None, notes_markdown=None,
        manual_notes=None, attendees=None, meeting_code="20260725-1000",
    )

    both = gui_app._meeting_export_text(meeting, "Acme", "[00:01] You: hi", "", "[00:01] You: hi there")
    cloud_only = gui_app._meeting_export_text(meeting, "Acme", "", "", "[00:01] You: hi there")

    assert "Transcript (this PC)\n--------------------\n[00:01] You: hi\n" in both
    assert "Transcript (cloud)\n------------------\n[00:01] You: hi there" in both
    assert "Transcript\n----------\n[00:01] You: hi there" in cloud_only


def test_format_elapsed_shows_minutes_then_hours():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    assert gui_app._format_elapsed(0) == "00:00"
    assert gui_app._format_elapsed(249.9) == "04:09"
    assert gui_app._format_elapsed(3723) == "1:02:03"


def test_notes_timestamp_matches_the_transcript_clock():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    assert gui_app._notes_timestamp(754) == "[12:34] "
    assert gui_app._notes_timestamp(3725) == "[62:05] "  # the transcript's minutes don't roll into hours


def test_format_duration():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    start = "2026-07-25T10:00:00+00:00"
    assert gui_app._format_duration(start, "2026-07-25T10:42:10+00:00") == "42 min"
    assert gui_app._format_duration(start, "2026-07-25T11:05:00+00:00") == "1 h 05 min"
    assert gui_app._format_duration(start, None) == ""
    assert gui_app._format_duration(start, "garbage") == ""


def test_device_from_choice_maps_the_default_label_to_none():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    assert gui_app._device_from_choice(gui_app.SYSTEM_DEFAULT_LABEL) is None
    assert gui_app._device_from_choice("Jabra Evolve") == "Jabra Evolve"
    assert gui_app._device_from_choice(gui_app.AUTO_HEADSET_LABEL, gui_app.AUTO_HEADSET_LABEL) is None


def test_meeting_export_text_includes_every_recorded_section_and_skips_empty_ones():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    from meeting_scribe.storage.database import Meeting

    meeting = Meeting(
        id=1, project_id=1, title="Kickoff", started_at="2026-07-25T10:00:00+00:00",
        ended_at="2026-07-25T10:30:00+00:00", transcript_text=None, notes_markdown=None,
        manual_notes="- follow up", attendees=None, meeting_code="20260725-1000",
    )

    text = gui_app._meeting_export_text(meeting, "Acme", "[00:01] You: hi", "")

    assert text.startswith("Kickoff\nProject: Acme\nDate: ")
    assert "(30 min)" in text
    assert "Transcript\n----------\n[00:01] You: hi" in text
    assert "Notes\n-----\n- follow up" in text
    assert "On-screen text" not in text and "Attendees" not in text


def test_safe_filename_replaces_characters_windows_rejects():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    assert gui_app._safe_filename('Q3: plan/"final"?') == "Q3 plan final"
    assert gui_app._safe_filename("???") == "meeting"
