"""Notices when a Teams call starts, so the app can offer to record it (see gui.meeting_prompt) instead
of relying on the user remembering to press Start — and when a recorded call ends, to offer to stop.

"In a call" is read from Windows' own record of which apps are using the microphone right now — the
same data behind the mic icon in the taskbar and Settings > Privacy > Microphone — rather than from
window titles. Titles alone can't tell a call from the main Teams window sitting on a chat with someone
(both read as "<some name> | Microsoft Teams"), which would be a constant false alarm for something that
pops up on its own. The window side (window_picker.find_teams_meeting_window) is only used for where to
put the prompt and what to call the meeting.

Covers the Teams desktop apps (the new packaged client and the classic one). Teams running in a web
browser shows up as the browser using the mic, not Teams, and isn't detected.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from enum import Enum

from meeting_scribe.screen.window_picker import TeamsMeetingWindow, find_teams_meeting_window

_MIC_CONSENT_STORE = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"


def _is_teams_app_key(key_name: str) -> bool:
    """Whether a ConsentStore\\microphone subkey belongs to Teams. Packaged apps are keyed by package
    family name ("MSTeams_8wekyb3d8bbwe" for the new client); desktop apps live under NonPackaged, keyed by
    their exe path with "#" for "\\" — so only the exe's own name is checked there, not the whole path,
    or anything installed under a folder with "teams" in it would match."""
    return "teams" in key_name.rsplit("#", 1)[-1].lower()


def _mic_in_use(last_used_start: int | None, last_used_stop: int | None) -> bool:
    """Windows stamps LastUsedTimeStart when an app opens the mic and LastUsedTimeStop when it lets go,
    so a start with a stop of 0 means it's holding the mic right now. Teams keeps the mic open while
    muted, so this still reads as in-use for a muted call."""
    return bool(last_used_start) and last_used_stop == 0


def _query_int(winreg, key, value_name: str) -> int | None:
    try:
        value, _type = winreg.QueryValueEx(key, value_name)
    except OSError:
        return None
    return value if isinstance(value, int) else None


def _subkey_names(winreg, key) -> list[str]:
    names = []
    index = 0
    while True:
        try:
            names.append(winreg.EnumKey(key, index))
        except OSError:
            return names
        index += 1


def teams_is_using_microphone(winreg=None) -> bool:
    """Whether a Teams desktop app currently has the microphone open — i.e. is in a call. `winreg` is
    injectable for tests; defaults to the real module. False rather than an error for anything that
    can't be read (no such registry key on an older Windows, access denied)."""
    if winreg is None:
        if sys.platform != "win32":
            raise RuntimeError("Detecting an active Teams call requires Windows (winreg)")
        import winreg

    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _MIC_CONSENT_STORE)
    except OSError:
        return False
    with root:
        parents = [root]
        try:
            parents.append(winreg.OpenKey(root, "NonPackaged"))
        except OSError:
            pass
        try:
            for parent in parents:
                for name in _subkey_names(winreg, parent):
                    if not _is_teams_app_key(name):
                        continue
                    try:
                        app_key = winreg.OpenKey(parent, name)
                    except OSError:
                        continue
                    with app_key:
                        if _mic_in_use(
                            _query_int(winreg, app_key, "LastUsedTimeStart"),
                            _query_int(winreg, app_key, "LastUsedTimeStop"),
                        ):
                            return True
        finally:
            for parent in parents[1:]:
                parent.Close()
    return False


@dataclass(frozen=True)
class DetectedMeeting:
    # None if Teams is in a call but has no visible window to anchor the prompt to (e.g. minimized to
    # the tray) — the prompt then goes to the primary monitor instead.
    window: TeamsMeetingWindow | None

    @property
    def name(self) -> str | None:
        return self.window.meeting_name if self.window is not None else None


def detect_teams_meeting() -> DetectedMeeting | None:
    """The Teams call in progress right now, or None if there isn't one. Windows-only; runs a full
    window enumeration, so call it off the GUI thread."""
    if not teams_is_using_microphone():
        return None
    return DetectedMeeting(window=find_teams_meeting_window())


class _PromptState(Enum):
    NO_CALL = "no_call"
    PROMPTING = "prompting"
    HANDLED = "handled"


class MeetingPromptTracker:
    """Decides, one detection poll at a time, whether the "start recording?" prompt should be showing:
    once per call, and never while already recording. A call the user dismissed the prompt for, or
    recorded (even if they stop recording partway through), isn't prompted for again — only a new call,
    after this one ends, is."""

    def __init__(self) -> None:
        self._state = _PromptState.NO_CALL

    def update(self, *, in_call: bool, recording: bool) -> bool:
        if not in_call:
            self._state = _PromptState.NO_CALL
        elif recording:
            self._state = _PromptState.HANDLED
        elif self._state is _PromptState.NO_CALL:
            self._state = _PromptState.PROMPTING
        return self._state is _PromptState.PROMPTING

    def dismiss(self) -> None:
        if self._state is _PromptState.PROMPTING:
            self._state = _PromptState.HANDLED


class MeetingEndTracker:
    """The counterpart to MeetingPromptTracker for the other end of a call: decides whether the "meeting
    ended — stop recording?" prompt should be showing. Only a recording that has actually seen a Teams
    call counts, so recording something else (a Zoom call, an in-person meeting) never gets asked. Once
    asked, a recording isn't asked again unless a call is seen again (rejoining, say) and then ends.

    The end has to hold for END_CONFIRM_POLLS consecutive polls, not just one, so a momentary gap in
    Teams holding the mic (e.g. switching audio devices mid-call) doesn't pop up the prompt."""

    END_CONFIRM_POLLS = 2

    def __init__(self) -> None:
        self._call_seen = False
        self._polls_without_call = 0
        self._prompting = False

    def update(self, *, in_call: bool, recording: bool) -> bool:
        if not recording:
            self._call_seen = False
            self._polls_without_call = 0
            self._prompting = False
        elif in_call:
            self._call_seen = True
            self._polls_without_call = 0
            self._prompting = False
        elif self._call_seen:
            self._polls_without_call += 1
            if self._polls_without_call >= self.END_CONFIRM_POLLS:
                self._call_seen = False
                self._prompting = True
        return self._prompting

    def dismiss(self) -> None:
        self._prompting = False


def prompt_position(
    window_rect: dict | None, work_area: dict, width: int, height: int, gap: int = 8
) -> tuple[int, int]:
    """Top-left corner for a width x height prompt: horizontally centered just below the meeting window
    (`window_rect`, mss-style), kept inside the monitor's `work_area`. A window with no room beneath it —
    maximized, or touching the taskbar — gets the prompt tucked just inside its bottom edge instead. With
    no window rect (minimized, or no window at all), it sits at the bottom-center of the work area."""
    work_left, work_top = work_area["left"], work_area["top"]
    work_right = work_left + work_area["width"]
    work_bottom = work_top + work_area["height"]

    if window_rect is None:
        x = work_left + (work_area["width"] - width) // 2
        y = work_bottom - height - gap
    else:
        x = window_rect["left"] + (window_rect["width"] - width) // 2
        window_bottom = window_rect["top"] + window_rect["height"]
        y = window_bottom + gap
        if y + height > work_bottom:
            y = min(window_bottom, work_bottom) - height - gap

    x = max(work_left, min(x, work_right - width))
    y = max(work_top, min(y, work_bottom - height))
    return x, y

