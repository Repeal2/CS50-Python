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


def test_hwnd_to_watch_for_auto_stop_finds_a_whole_window_targets_handle():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    from meeting_scribe.screen.window_picker import WindowTarget

    target = WindowTarget(hwnd=4242, title="Microsoft Teams")
    assert gui_app._hwnd_to_watch_for_auto_stop(target) == 4242


def test_hwnd_to_watch_for_auto_stop_finds_a_window_pinned_areas_handle():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    from meeting_scribe.screen.region_picker import WindowRegionTarget

    target = WindowRegionTarget(
        hwnd=4242,
        window_title="Microsoft Teams",
        offset_left_frac=0.1,
        offset_top_frac=0.1,
        width_frac=0.5,
        height_frac=0.2,
        picked_width=300,
        picked_height=100,
    )
    assert gui_app._hwnd_to_watch_for_auto_stop(target) == 4242


def test_hwnd_to_watch_for_auto_stop_is_none_for_the_whole_screen():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    assert gui_app._hwnd_to_watch_for_auto_stop(None) is None


def test_hwnd_to_watch_for_auto_stop_is_none_for_a_fixed_position_area():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    from meeting_scribe.screen.region_picker import RegionTarget

    target = RegionTarget(left=10, top=10, width=200, height=150)
    assert gui_app._hwnd_to_watch_for_auto_stop(target) is None


def test_hotkey_label_shows_not_set_for_none():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    assert gui_app._hotkey_label(None) == "Not set"


def test_hotkey_label_shows_the_combo():
    gui_app = pytest.importorskip("meeting_scribe.gui.app")
    from meeting_scribe.hotkeys import MOD_CONTROL, MOD_SHIFT, HotkeyCombo

    combo = HotkeyCombo(modifiers=MOD_CONTROL | MOD_SHIFT, vk=0x53)
    assert gui_app._hotkey_label(combo) == "Ctrl+Shift+S"
