import sys

import pytest

from meeting_scribe.screen import meeting_detector
from meeting_scribe.screen.meeting_detector import (
    DetectedMeeting,
    MeetingEndTracker,
    MeetingPromptTracker,
    _is_teams_app_key,
    _mic_in_use,
    detect_teams_meeting,
    prompt_position,
    teams_is_using_microphone,
)
from meeting_scribe.screen.window_picker import TeamsMeetingWindow

NEW_TEAMS = "MSTeams_8wekyb3d8bbwe"
CLASSIC_TEAMS = "C:#Users#me#AppData#Local#Microsoft#Teams#current#Teams.exe"


class FakeKey:
    def __init__(self, subkeys=None, values=None):
        self.subkeys = subkeys or {}
        self.values = values or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def Close(self):
        pass


class FakeWinreg:
    """Just enough of the winreg module for teams_is_using_microphone: OpenKey/EnumKey/QueryValueEx over
    a tree of FakeKeys rooted at the ConsentStore\\microphone key (None = that key doesn't exist)."""

    HKEY_CURRENT_USER = object()

    def __init__(self, consent_store: FakeKey | None):
        self._consent_store = consent_store

    def OpenKey(self, key, sub_key):
        if key is self.HKEY_CURRENT_USER:
            if self._consent_store is None:
                raise OSError("not found")
            return self._consent_store
        try:
            return key.subkeys[sub_key]
        except KeyError:
            raise OSError("not found") from None

    def EnumKey(self, key, index):
        names = list(key.subkeys)
        if index >= len(names):
            raise OSError("no more items")
        return names[index]

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise OSError("not found")
        return key.values[name], 11  # REG_QWORD


def _app(start, stop):
    values = {}
    if start is not None:
        values["LastUsedTimeStart"] = start
    if stop is not None:
        values["LastUsedTimeStop"] = stop
    return FakeKey(values=values)


def test_is_teams_app_key_matches_both_teams_clients():
    assert _is_teams_app_key(NEW_TEAMS)
    assert _is_teams_app_key(CLASSIC_TEAMS)


def test_is_teams_app_key_checks_only_the_exe_name_of_a_desktop_app_path():
    # "Teams" in a folder name shouldn't make some other app look like Teams.
    assert not _is_teams_app_key("C:#Tools#Teams-helpers#MeetingScribe.exe")
    assert not _is_teams_app_key("Microsoft.WindowsSoundRecorder_8wekyb3d8bbwe")


def test_mic_in_use_means_started_and_not_yet_stopped():
    assert _mic_in_use(133_000_000_000, 0)
    assert not _mic_in_use(133_000_000_000, 133_000_000_500)
    assert not _mic_in_use(None, 0)
    assert not _mic_in_use(0, 0)
    assert not _mic_in_use(133_000_000_000, None)


def test_new_teams_holding_the_mic_counts_as_in_a_call():
    registry = FakeWinreg(FakeKey(subkeys={NEW_TEAMS: _app(133_000_000_000, 0)}))
    assert teams_is_using_microphone(registry)


def test_classic_teams_holding_the_mic_counts_as_in_a_call():
    non_packaged = FakeKey(subkeys={CLASSIC_TEAMS: _app(133_000_000_000, 0)})
    registry = FakeWinreg(FakeKey(subkeys={"NonPackaged": non_packaged}))
    assert teams_is_using_microphone(registry)


def test_teams_that_has_released_the_mic_is_not_in_a_call():
    registry = FakeWinreg(FakeKey(subkeys={NEW_TEAMS: _app(133_000_000_000, 133_000_000_900)}))
    assert not teams_is_using_microphone(registry)


def test_another_app_holding_the_mic_is_not_a_teams_call():
    registry = FakeWinreg(
        FakeKey(
            subkeys={
                "Microsoft.WindowsSoundRecorder_8wekyb3d8bbwe": _app(133_000_000_000, 0),
                NEW_TEAMS: _app(133_000_000_000, 133_000_000_900),
            }
        )
    )
    assert not teams_is_using_microphone(registry)


def test_missing_consent_store_or_values_are_not_a_call():
    assert not teams_is_using_microphone(FakeWinreg(None))
    assert not teams_is_using_microphone(FakeWinreg(FakeKey(subkeys={NEW_TEAMS: _app(None, None)})))


@pytest.mark.skipif(sys.platform == "win32", reason="the platform guard only triggers off Windows")
def test_teams_is_using_microphone_requires_windows_without_an_injected_registry():
    with pytest.raises(RuntimeError):
        teams_is_using_microphone()


def test_detect_teams_meeting_is_none_when_teams_is_not_in_a_call(monkeypatch):
    monkeypatch.setattr(meeting_detector, "teams_is_using_microphone", lambda: False)
    monkeypatch.setattr(
        meeting_detector, "find_teams_meeting_window", lambda: TeamsMeetingWindow(1, "Chat with Alice")
    )
    assert detect_teams_meeting() is None


def test_detect_teams_meeting_pairs_the_call_with_its_window(monkeypatch):
    monkeypatch.setattr(meeting_detector, "teams_is_using_microphone", lambda: True)
    monkeypatch.setattr(
        meeting_detector, "find_teams_meeting_window", lambda: TeamsMeetingWindow(42, "Weekly sync")
    )
    meeting = detect_teams_meeting()
    assert meeting == DetectedMeeting(window=TeamsMeetingWindow(42, "Weekly sync"))
    assert meeting.name == "Weekly sync"


def test_detected_meeting_with_no_window_has_no_name():
    assert DetectedMeeting(window=None).name is None


def test_tracker_prompts_once_when_a_call_starts():
    tracker = MeetingPromptTracker()
    assert not tracker.update(in_call=False, recording=False)
    assert tracker.update(in_call=True, recording=False)
    assert tracker.update(in_call=True, recording=False)  # stays up until acted on


def test_tracker_does_not_reprompt_a_dismissed_call_but_does_prompt_the_next_one():
    tracker = MeetingPromptTracker()
    tracker.update(in_call=True, recording=False)
    tracker.dismiss()
    assert not tracker.update(in_call=True, recording=False)
    assert not tracker.update(in_call=False, recording=False)  # call ended
    assert tracker.update(in_call=True, recording=False)  # a new call


def test_tracker_hides_the_prompt_once_recording_and_not_again_if_recording_stops_mid_call():
    tracker = MeetingPromptTracker()
    tracker.update(in_call=True, recording=False)
    assert not tracker.update(in_call=True, recording=True)
    assert not tracker.update(in_call=True, recording=False)


def test_tracker_never_prompts_a_call_joined_while_already_recording():
    tracker = MeetingPromptTracker()
    assert not tracker.update(in_call=True, recording=True)
    assert not tracker.update(in_call=True, recording=False)


def test_tracker_hides_the_prompt_when_the_call_ends_unanswered():
    tracker = MeetingPromptTracker()
    tracker.update(in_call=True, recording=False)
    assert not tracker.update(in_call=False, recording=False)


def _end_tracker_after(polls):
    """A MeetingEndTracker fed (in_call, recording) pairs; returns it and what the last update said."""
    tracker = MeetingEndTracker()
    shown = False
    for in_call, recording in polls:
        shown = tracker.update(in_call=in_call, recording=recording)
    return tracker, shown


def test_end_prompt_needs_the_call_gone_for_two_checks_in_a_row():
    _, shown = _end_tracker_after([(True, True), (False, True)])
    assert not shown
    _, shown = _end_tracker_after([(True, True), (False, True), (False, True)])
    assert shown


def test_end_prompt_ignores_a_momentary_gap_in_the_call():
    _, shown = _end_tracker_after([(True, True), (False, True), (True, True), (False, True)])
    assert not shown


def test_end_prompt_never_asks_about_a_recording_that_never_saw_a_teams_call():
    _, shown = _end_tracker_after([(False, True)] * 5)
    assert not shown


def test_end_prompt_stays_up_until_answered_then_not_again_for_that_call():
    tracker, shown = _end_tracker_after([(True, True), (False, True), (False, True), (False, True)])
    assert shown
    tracker.dismiss()
    assert not tracker.update(in_call=False, recording=True)
    assert not tracker.update(in_call=False, recording=True)


def test_end_prompt_asks_again_if_the_call_is_rejoined_and_ends_again():
    tracker, _ = _end_tracker_after([(True, True), (False, True), (False, True)])
    tracker.dismiss()
    for in_call in (True, False, False):
        shown = tracker.update(in_call=in_call, recording=True)
    assert shown


def test_end_prompt_hides_when_the_call_is_rejoined_or_recording_stops():
    tracker, _ = _end_tracker_after([(True, True), (False, True), (False, True)])
    assert not tracker.update(in_call=True, recording=True)

    tracker, _ = _end_tracker_after([(True, True), (False, True), (False, True)])
    assert not tracker.update(in_call=False, recording=False)


WORK_AREA = {"left": 0, "top": 0, "width": 1920, "height": 1040}


def test_prompt_sits_centered_just_below_a_window_with_room_beneath_it():
    window = {"left": 400, "top": 100, "width": 800, "height": 600}
    assert prompt_position(window, WORK_AREA, width=300, height=90) == (650, 708)


def test_prompt_tucks_inside_the_bottom_edge_of_a_maximized_window():
    window = {"left": 0, "top": 0, "width": 1920, "height": 1040}
    assert prompt_position(window, WORK_AREA, width=300, height=90) == (810, 942)


def test_prompt_stays_inside_the_work_area_for_a_window_hanging_off_its_edge():
    window = {"left": 1800, "top": 100, "width": 800, "height": 600}
    x, y = prompt_position(window, WORK_AREA, width=300, height=90)
    assert x == 1920 - 300
    assert y == 708


def test_prompt_follows_a_window_on_a_monitor_left_of_the_primary():
    work_area = {"left": -1920, "top": 0, "width": 1920, "height": 1040}
    window = {"left": -1500, "top": 200, "width": 800, "height": 500}
    assert prompt_position(window, work_area, width=300, height=90) == (-1250, 708)


def test_prompt_with_no_window_goes_bottom_center_of_the_work_area():
    assert prompt_position(None, WORK_AREA, width=300, height=90) == (810, 942)
