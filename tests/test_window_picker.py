import sys

import pytest

from meeting_scribe.screen.window_picker import (
    TeamsMeetingWindow,
    WindowTarget,
    _best_teams_meeting_window,
    _meeting_name_from_title,
    find_teams_meeting_name,
    find_teams_meeting_window,
    get_monitor_work_area,
    get_window_region,
    list_capturable_windows,
    window_exists,
)

# The functions guard on sys.platform, so what's testable differs by platform: off Windows we can only
# verify the guard raises; on Windows the guard is a no-op and we exercise the real win32gui calls.


@pytest.mark.skipif(sys.platform == "win32", reason="the platform guard only triggers off Windows")
def test_list_capturable_windows_requires_windows():
    with pytest.raises(RuntimeError):
        list_capturable_windows()


@pytest.mark.skipif(sys.platform == "win32", reason="the platform guard only triggers off Windows")
def test_get_window_region_requires_windows():
    with pytest.raises(RuntimeError):
        get_window_region(12345)


@pytest.mark.skipif(sys.platform == "win32", reason="the platform guard only triggers off Windows")
def test_window_exists_requires_windows():
    with pytest.raises(RuntimeError):
        window_exists(12345)


@pytest.mark.skipif(sys.platform == "win32", reason="the platform guard only triggers off Windows")
def test_find_teams_meeting_name_requires_windows():
    with pytest.raises(RuntimeError):
        find_teams_meeting_name()


@pytest.mark.skipif(sys.platform == "win32", reason="the platform guard only triggers off Windows")
def test_find_teams_meeting_window_requires_windows():
    with pytest.raises(RuntimeError):
        find_teams_meeting_window()


@pytest.mark.skipif(sys.platform == "win32", reason="the platform guard only triggers off Windows")
def test_get_monitor_work_area_requires_windows():
    with pytest.raises(RuntimeError):
        get_monitor_work_area(None)


def test_best_teams_meeting_window_prefers_a_bare_call_window_title():
    windows = [(1, "Chat | Alice Smith | Microsoft Teams"), (2, "Weekly sync")]
    assert _best_teams_meeting_window(windows) == TeamsMeetingWindow(2, "Weekly sync")


def test_best_teams_meeting_window_keeps_z_order_among_suffixed_titles():
    windows = [(1, "Weekly sync | Microsoft Teams"), (2, "Design review | Microsoft Teams")]
    assert _best_teams_meeting_window(windows) == TeamsMeetingWindow(1, "Weekly sync")


def test_best_teams_meeting_window_falls_back_to_an_unnamed_teams_window():
    windows = [(7, "Chat | Microsoft Teams"), (8, "Microsoft Teams")]
    assert _best_teams_meeting_window(windows) == TeamsMeetingWindow(7, None)


def test_best_teams_meeting_window_is_none_with_no_teams_windows():
    assert _best_teams_meeting_window([]) is None


def test_meeting_name_from_title_strips_the_pipe_suffix():
    assert _meeting_name_from_title("Weekly Sync | Microsoft Teams") == "Weekly Sync"


def test_meeting_name_from_title_strips_a_dash_suffix():
    assert _meeting_name_from_title("Weekly Sync - Microsoft Teams") == "Weekly Sync"
    assert _meeting_name_from_title("Weekly Sync – Microsoft Teams") == "Weekly Sync"


def test_meeting_name_from_title_accepts_a_bare_title_with_no_suffix():
    # Some Teams versions/configurations open a window dedicated to the active call, titled with
    # nothing else — no "| Microsoft Teams" suffix at all.
    assert _meeting_name_from_title("Weekly Sync") == "Weekly Sync"


def test_meeting_name_from_title_rejects_the_bare_app_name():
    assert _meeting_name_from_title("Microsoft Teams") is None
    assert _meeting_name_from_title("Teams") is None


def test_meeting_name_from_title_rejects_generic_non_call_tabs():
    for title in ("Chat | Microsoft Teams", "Calendar | Microsoft Teams", "Activity | Microsoft Teams"):
        assert _meeting_name_from_title(title) is None


def test_meeting_name_from_title_rejects_a_blank_title():
    assert _meeting_name_from_title("") is None
    assert _meeting_name_from_title("   | Microsoft Teams") is None


def test_meeting_name_from_title_is_case_insensitive_for_the_generic_check():
    assert _meeting_name_from_title("chat | microsoft teams") is None


@pytest.mark.skipif(sys.platform != "win32", reason="exercises real win32gui calls")
def test_list_capturable_windows_returns_window_targets_on_windows():
    windows = list_capturable_windows()
    assert isinstance(windows, list)
    assert all(isinstance(w, WindowTarget) for w in windows)


@pytest.mark.skipif(sys.platform != "win32", reason="exercises real win32gui calls")
def test_get_window_region_returns_none_for_nonexistent_window_on_windows():
    # A window handle this large is essentially guaranteed not to exist.
    assert get_window_region(999_999_999) is None


@pytest.mark.skipif(sys.platform != "win32", reason="exercises real win32gui calls")
def test_window_exists_is_false_for_a_nonexistent_window_on_windows():
    assert window_exists(999_999_999) is False
