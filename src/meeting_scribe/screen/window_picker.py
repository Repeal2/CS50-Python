"""Lets the user pick a specific window to OCR instead of the whole screen — e.g. just the Teams/Zoom
window, so captions/chat from that app are captured without also picking up unrelated desktop content.

Windows-only (wraps win32gui). Import is deferred so this module can be imported anywhere; calling
either function off Windows raises RuntimeError, matching audio.recorder's pattern.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


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
    """Returns an mss-compatible capture region for the window, or None if it's been closed or
    minimized since it was selected (the caller should skip that capture cycle)."""
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
