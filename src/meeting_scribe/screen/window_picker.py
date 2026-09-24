"""Finds the Teams call window — its meeting name, and where it is on screen (for placing the meeting
prompts and the OCR box next to it).

Windows-only (wraps win32gui). Import is deferred so this module can be imported anywhere; calling
these functions off Windows raises RuntimeError, matching audio.recorder's pattern.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path


def get_window_region(hwnd: int) -> dict | None:
    """Returns the window's bounds as an mss-style region, or None if it's been closed or minimized."""
    if sys.platform != "win32":
        raise RuntimeError("Window capture requires Windows (win32gui)")
    import win32gui

    if not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
        return None
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None
    return {"left": left, "top": top, "width": width, "height": height}


# Matches the "| Microsoft Teams" (or "- Microsoft Teams") suffix Teams appends to its main window's
# title — "<name> | Microsoft Teams" while in a call, but also "<tab name> | Microsoft Teams" on any
# ordinary tab (Chat, Calendar, ...), which is exactly why a suffix match alone isn't enough to tell a
# meeting name from a tab label; see _GENERIC_TEAMS_TITLES.
_TEAMS_TITLE_SUFFIX_RE = re.compile(r"\s*[|–-]\s*Microsoft Teams\s*$", re.IGNORECASE)

# Teams' own labels for its non-call views — a title that's just one of these (after stripping the
# "| Microsoft Teams" suffix, if present) is the main app sitting on that tab, not an active meeting.
# Inevitably incomplete (Teams' exact labels vary by version and locale), but a wrong guess here only
# ever means a pre-filled title field the user can still freely edit, never something acted on
# irreversibly — see gui.app.RecordPage._detect_meeting_title.
_GENERIC_TEAMS_TITLES = frozenset(
    {
        "teams", "microsoft teams", "chat", "chats", "calendar", "calls", "activity", "apps", "files",
        "meeting join", "meeting", "call",
    }
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _meeting_name_from_title(title: str) -> str | None:
    """Extracts a plausible meeting name from a Teams window's title, or None if it looks like the main
    app sitting on some non-call tab rather than an actual meeting.

    The new Teams client titles its windows "<view> | <subject> | <organization> | <account email>",
    plus the "| Microsoft Teams" suffix — e.g. "Meeting join | Weekly sync | Contoso | me@contoso.com" —
    and only the subject is wanted: the leading view label, the signed-in account's email address, and the
    organization name just before it are all dropped. The organization is only recognizable by sitting
    right before the email, so a title with no email keeps whatever's there."""
    stripped = _TEAMS_TITLE_SUFFIX_RE.sub("", title).strip()
    parts = [part.strip() for part in stripped.split("|") if part.strip()]
    email_at = next((i for i, part in enumerate(parts) if _EMAIL_RE.match(part)), None)
    before_email = parts[email_at - 1] if email_at else None
    parts = [part for part in parts if not _EMAIL_RE.match(part)]
    while parts and parts[0].lower() in _GENERIC_TEAMS_TITLES:
        parts.pop(0)
    # Only dropped when something else is left — "Weekly sync | me@contoso.com" is a subject and an
    # email with no organization in between, not an organization to throw away.
    if before_email is not None and len(parts) > 1 and parts[-1] == before_email:
        parts.pop()
    return " | ".join(parts) or None


def _is_teams_process(hwnd: int) -> bool:
    """Whether this window belongs to a process that looks like Microsoft Teams — matches both the "new"
    Teams client (ms-teams.exe) and the classic one (Teams.exe) by checking for "teams" anywhere in the
    owning process's executable name, rather than an exact match, so an enterprise-renamed or
    differently-versioned build still matches. Swallows every failure (a protected/elevated process this
    app can't query, a window whose process has already exited) as "not a match" rather than letting one
    uninspectable window take detection down entirely."""
    import win32api
    import win32con
    import win32process

    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid
        )
        try:
            exe_path = win32process.GetModuleFileNameEx(handle, 0)
        finally:
            win32api.CloseHandle(handle)
    except Exception:
        return False
    return "teams" in Path(exe_path).stem.lower()


@dataclass(frozen=True)
class TeamsMeetingWindow:
    hwnd: int
    # None when no Teams window's title reads as a meeting name — the window is still worth knowing
    # about as somewhere to anchor UI next to (see gui.meeting_prompt), just not as a source of a name.
    meeting_name: str | None


def _best_teams_meeting_window(windows: list[tuple[int, str]]) -> TeamsMeetingWindow | None:
    """Picks the Teams window most likely to be the active call from (hwnd, title) pairs of visible
    Teams windows, in EnumWindows' (roughly z-order) order. A bare title with no "| Microsoft Teams"
    suffix wins over a suffixed one: some Teams versions/configurations open a window dedicated to the
    active call, separate from the main app, titled with nothing else — a much less ambiguous signal than
    the main window's title, which carries that suffix on every tab, not just while in a call. Falls back
    to the first Teams window at all (with no name) when none of them reads as a meeting."""
    named: list[tuple[bool, TeamsMeetingWindow]] = []
    for hwnd, title in windows:
        name = _meeting_name_from_title(title)
        if name:
            named.append((_TEAMS_TITLE_SUFFIX_RE.search(title) is None, TeamsMeetingWindow(hwnd, name)))
    if named:
        named.sort(key=lambda pair: not pair[0])
        return named[0][1]
    if windows:
        return TeamsMeetingWindow(windows[0][0], None)
    return None


def _visible_teams_windows() -> list[tuple[int, str]]:
    import win32gui

    windows: list[tuple[int, str]] = []

    def _on_window(hwnd: int, _extra: None) -> bool:
        if win32gui.IsWindowVisible(hwnd) and _is_teams_process(hwnd):
            title = win32gui.GetWindowText(hwnd).strip()
            if title:
                windows.append((hwnd, title))
        return True

    win32gui.EnumWindows(_on_window, None)
    return windows


def find_teams_meeting_window() -> TeamsMeetingWindow | None:
    """The Teams window most likely to be the active call (see _best_teams_meeting_window), or None if
    no Teams window is open at all. Doesn't by itself mean a call is in progress — every tab of the main
    Teams window has a title that can read as a meeting name — so callers that need to know that combine
    it with a stronger signal (see screen.meeting_detector)."""
    if sys.platform != "win32":
        raise RuntimeError("Detecting the active Teams meeting requires Windows (win32gui, win32process)")
    return _best_teams_meeting_window(_visible_teams_windows())


def find_teams_meeting_name() -> str | None:
    """Best-effort detection of an active Teams meeting's name, for auto-filling the Record page's title
    field when it's still at its default (see gui.app.RecordPage._detect_meeting_title) — so starting a
    meeting, including via the Start hotkey from inside Teams itself, doesn't leave the transcript filed
    under "Untitled meeting" when Teams already knows its real name.

    Returns None if Teams isn't running, isn't in a call, or every Teams window found looks like the
    main app on some non-call tab rather than a meeting — never raises for "nothing found", only for
    being run off Windows."""
    window = find_teams_meeting_window()
    return window.meeting_name if window is not None else None


def get_monitor_work_area(hwnd: int | None) -> dict:
    """The work area (the monitor minus the taskbar) of the monitor `hwnd` is on — or nearest to, if
    it's minimized or off-screen — as an mss-style {left, top, width, height} dict. None means the
    primary monitor."""
    if sys.platform != "win32":
        raise RuntimeError("Monitor lookup requires Windows (win32api)")
    import win32api
    import win32con

    if hwnd is None:
        monitor = win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY)
    else:
        monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
    left, top, right, bottom = win32api.GetMonitorInfo(monitor)["Work"]
    return {"left": left, "top": top, "width": right - left, "height": bottom - top}
