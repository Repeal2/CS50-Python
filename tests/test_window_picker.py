import sys

import pytest

from meeting_scribe.screen.window_picker import (
    TeamsMeetingWindow,
    _best_teams_meeting_window,
    _meeting_name_from_title,
    find_teams_meeting_name,
    find_teams_meeting_window,
    get_monitor_work_area,
    get_window_region,
)

# The functions guard on sys.platform, so what's testable differs by platform: off Windows we can only
# verify the guard raises; on Windows the guard is a no-op and we exercise the real win32gui calls.


@pytest.mark.skipif(sys.platform == "win32", reason="the platform guard only triggers off Windows")
def test_get_window_region_requires_windows():
    with pytest.raises(RuntimeError):
        get_window_region(12345)


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


def test_meeting_name_from_title_keeps_only_the_subject_of_a_new_teams_title():
    # A real title from the new Teams client's pre-join screen.
    title = "Meeting join | Test meeting | AMERESCO | mbrookes@ameresco.com"
    assert _meeting_name_from_title(title) == "Test meeting"
    assert _meeting_name_from_title(title + " | Microsoft Teams") == "Test meeting"


def test_meeting_name_from_title_drops_the_view_label_of_an_in_meeting_title():
    assert _meeting_name_from_title("Meeting | Weekly sync | Contoso | me@contoso.com") == "Weekly sync"


def test_meeting_name_from_title_keeps_a_subject_with_an_email_but_no_organization():
    assert _meeting_name_from_title("Weekly sync | me@contoso.com") == "Weekly sync"


def test_meeting_name_from_title_keeps_pipes_inside_the_subject_itself():
    title = "Meeting join | Q3 review | Finance | Contoso | me@contoso.com"
    assert _meeting_name_from_title(title) == "Q3 review | Finance"


def test_meeting_name_from_title_keeps_a_lone_remaining_segment_rather_than_nothing():
    # With only one segment left there's no telling an organization from a subject, so it's kept.
    assert _meeting_name_from_title("Meeting join | Contoso | me@contoso.com") == "Contoso"


def test_meeting_name_from_title_is_none_when_only_a_view_label_and_an_email_remain():
    assert _meeting_name_from_title("Calendar | me@contoso.com | Microsoft Teams") is None


@pytest.mark.skipif(sys.platform != "win32", reason="exercises real win32gui calls")
def test_get_window_region_returns_none_for_nonexistent_window_on_windows():
    # A window handle this large is essentially guaranteed not to exist.
    assert get_window_region(999_999_999) is None
