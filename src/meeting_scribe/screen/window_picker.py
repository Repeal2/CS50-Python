"""Lets the user pick a specific window to OCR instead of the whole screen — e.g. just the Teams/Zoom
window, so captions/chat from that app are captured without also picking up unrelated desktop content.

Windows-only (wraps win32gui). Import is deferred so this module can be imported anywhere; calling
either function off Windows raises RuntimeError, matching audio.recorder's pattern.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WindowTarget:
    hwnd: int
    title: str


def list_capturable_windows() -> list[WindowTarget]:
    """Lists visible, titled, non-empty top-level windows the user could pick as an OCR target."""
    if sys.platform != "win32":
        raise RuntimeError("Window selection requires Windows (win32gui)")
    import win32gui

    windows: list[WindowTarget] = []

    def _on_window(hwnd: int, _extra: None) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd).strip()
        if not title:
            return True
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right - left <= 0 or bottom - top <= 0:
            return True
        windows.append(WindowTarget(hwnd=hwnd, title=title))
        return True

    win32gui.EnumWindows(_on_window, None)
    return windows


def get_window_region(hwnd: int) -> dict | None:
    """Returns an mss-compatible capture region for the window, or None if it's been closed *or*
    minimized since it was selected (the caller should skip that capture cycle either way — there's
    nothing to grab a screenshot of). That conflation is fine for screen capture, but wrong for anything
    that needs to tell "closed" apart from "temporarily minimized" — see window_exists for that."""
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


def window_exists(hwnd: int) -> bool:
    """Whether this window handle still refers to a real window at all — true even while it's minimized
    or hidden, unlike get_window_region's None (which also covers "temporarily not capturable" and so
    can't be used to tell a closed window from a merely minimized one). This is the right check for "has
    the window actually closed" — see gui.app's "stop recording when the screen-source window closes",
    where mistaking a minimized Teams call for an ended one would stop a meeting still in progress."""
    if sys.platform != "win32":
        raise RuntimeError("Window state requires Windows (win32gui)")
    import win32gui

    return bool(win32gui.IsWindow(hwnd))


# Matches the "| Microsoft Teams" (or "- Microsoft Teams") suffix Teams appends to its main window's
# title — "<name> | Microsoft Teams" while in a call, but also "<tab name> | Microsoft Teams" on any
# ordinary tab (Chat, Calendar, ...), which is exactly why a suffix match alone isn't enough to tell a
# meeting name from a tab label; see _GENERIC_TEAMS_TITLES.
_TEAMS_TITLE_SUFFIX_RE = re.compile(r"\s*[|–-]\s*Microsoft Teams\s*$", re.IGNORECASE)

# Teams' own labels for its non-call views — a title that's just one of these (after stripping the
# "| Microsoft Teams" suffix, if present) is the main app sitting on that tab, not an active meeting.
# Inevitably incomplete (Teams' exact labels vary by version and locale), but a wrong guess here only
# ever means a pre-filled title field the user can still freely edit, never something acted on
# irreversibly — see gui.app.RecordTab._detect_meeting_title.
_GENERIC_TEAMS_TITLES = frozenset(
    {"teams", "microsoft teams", "chat", "chats", "calendar", "calls", "activity", "apps", "files"}
)


def _meeting_name_from_title(title: str) -> str | None:
    """Extracts a plausible meeting name from a Teams window's title, or None if it looks like the main
    app sitting on some non-call tab rather than an actual meeting."""
    stripped = _TEAMS_TITLE_SUFFIX_RE.sub("", title).strip()
    if not stripped or stripped.lower() in _GENERIC_TEAMS_TITLES:
        return None
    return stripped


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


def find_teams_meeting_name() -> str | None:
    """Best-effort detection of an active Teams meeting's name, for auto-filling the Record tab's title
    field when it's still at its default (see gui.app.RecordTab._detect_meeting_title) — so starting a
    meeting, including via the Start hotkey from inside Teams itself, doesn't leave the transcript filed
    under "Untitled meeting" when Teams already knows its real name.

    Returns None if Teams isn't running, isn't in a call, or every Teams window found looks like the
    main app on some non-call tab rather than a meeting — never raises for "nothing found", only for
    being run off Windows. A bare title with no "| Microsoft Teams" suffix is preferred over a suffixed
    one when both are candidates: some Teams versions/configurations open a window dedicated to the
    active call, separate from the main app, titled with nothing else — a much less ambiguous signal
    than the main window's title, which carries that suffix on every tab, not just while in a call."""
    if sys.platform != "win32":
        raise RuntimeError("Detecting the active Teams meeting requires Windows (win32gui, win32process)")
    import win32gui

    # (is_bare, name) pairs — sorted so a bare (no-suffix) title wins over a suffixed one, with
    # EnumWindows' own (roughly z-order) order as the tiebreak among candidates of the same kind.
    candidates: list[tuple[bool, str]] = []

    def _on_window(hwnd: int, _extra: None) -> bool:
        if win32gui.IsWindowVisible(hwnd) and _is_teams_process(hwnd):
            title = win32gui.GetWindowText(hwnd).strip()
            name = _meeting_name_from_title(title)
            if name:
                candidates.append((_TEAMS_TITLE_SUFFIX_RE.search(title) is None, name))
        return True

    win32gui.EnumWindows(_on_window, None)
    if not candidates:
        return None
    candidates.sort(key=lambda pair: not pair[0])
    return candidates[0][1]
