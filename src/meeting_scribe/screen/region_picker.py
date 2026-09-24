"""A screen rectangle to capture (RegionTarget), a drag-to-select overlay for picking one on the spot
(used by Capture Attendees), and the Win32 window-positioning helpers the app's borderless overlays share.

Built on Tkinter, which is already a hard dependency of the whole GUI, so unlike audio/window enumeration
there's no Windows-only platform guard here — this just needs a display, which the app already requires.
Tkinter itself is only imported inside the functions that actually touch it (not at module level), so
RegionTarget and the pure geometry helpers stay importable in environments without a display, e.g. tests.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass

if typing.TYPE_CHECKING:
    import tkinter as tk

MIN_REGION_SIZE = 8


@dataclass(frozen=True)
class RegionTarget:
    left: int
    top: int
    width: int
    height: int

    @property
    def mss_region(self) -> dict:
        return {"left": self.left, "top": self.top, "width": self.width, "height": self.height}


def _region_from_drag(x1: int, y1: int, x2: int, y2: int) -> RegionTarget | None:
    """Turns two dragged corner points into a RegionTarget, or None if the drag was too small to be a
    deliberate selection (e.g. a stray click)."""
    left, right = sorted((x1, x2))
    top, bottom = sorted((y1, y2))
    if right - left < MIN_REGION_SIZE or bottom - top < MIN_REGION_SIZE:
        return None
    return RegionTarget(left=left, top=top, width=right - left, height=bottom - top)


def _position_window(window: "tk.Misc", left: int, top: int, width: int, height: int) -> None:
    """Moves a Tk window to an absolute virtual-desktop pixel position via a direct Win32 call, instead
    of Tk's own geometry-string offset syntax ("WxH+X+Y"/"WxH-X-Y") — that syntax's leading "-" doesn't
    mean "this coordinate is negative". Inherited from X11's XParseGeometry, it means "measured from the
    opposite (right/bottom) edge of the screen", and on Windows "the screen" Tk measures that against is
    the *primary* monitor (the same single-monitor blind spot this module's other multi-monitor
    workarounds already exist for — see pick_region_interactively). So a geometry string built with a
    correctly-signed but negative offset doesn't crash (a bare "+{negative}" does, with "bad geometry
    specifier" — the bug this replaced), but it does silently land the window somewhere relative to the
    primary monitor's edge instead of at the intended negative coordinate — exactly the multi-monitor
    case (a display above or left of the primary) this exists to get right. Going through MoveWindow
    instead sidesteps that parser entirely: the coordinates it takes are always literal.

    Sets *both* size and position, and is meant to be the only thing that ever does either for a window
    it's used on — never call .geometry() on the same window afterward. Tk keeps its own notion of where
    a window is, independent of the OS; mixing the two means Tk doesn't know this call happened, and the
    next .geometry() call (even a size-only "WxH" one, which is supposed to leave position alone) can
    reassert Tk's stale idea of the position, or otherwise disagree with the OS about the window's actual
    geometry. That's what silently broke the always-visible capture-area outline, which recomputes and
    reapplies its position on every poll tick.

    update_idletasks() first guarantees the window has a real platform handle to move — Tk creates one
    immediately when a Toplevel is constructed, but forcing a pending-event flush before reading its
    handle (see _toplevel_hwnd) is the safe way to depend on that rather than an implementation detail."""
    window.update_idletasks()
    import ctypes
    from ctypes import wintypes

    move_window = ctypes.windll.user32.MoveWindow
    move_window.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL]
    move_window.restype = wintypes.BOOL
    move_window(_toplevel_hwnd(window), left, top, width, height, True)


def _toplevel_hwnd(window: "tk.Misc") -> int:
    """The HWND of the actual top-level OS window for a Tk Toplevel. On Windows, winfo_id() is *not*
    that: Tk nests every toplevel's contents in a child window inside a separate "wrapper" frame window,
    and winfo_id() returns the child. Moving the child with MoveWindow shifts it around inside the wrapper
    (clipped to it) while the wrapper — the thing actually on screen — stays wherever Tk first put it.
    `wm frame` returns the wrapper."""
    return int(window.wm_frame(), 16)


def pick_region_interactively(parent: "tk.Misc") -> RegionTarget | None:
    """Opens a mostly-transparent overlay spanning every connected monitor (not just the primary one)
    the user drags a rectangle onto, showing the selection live as they drag. Blocks (via wait_window)
    until they release the mouse or press Escape to cancel. Returns None on cancel or too-small a
    selection.

    Tkinter's own `-fullscreen` attribute and `winfo_screenwidth()`/`winfo_screenheight()` only cover the
    *primary* monitor on Windows, not the full virtual desktop — on a multi-monitor setup that made the
    greyed-out selection area smaller than the actual screen, and unusable on secondary monitors
    entirely. mss's monitor 0 is the real union of every display, negative coordinates and all (a monitor
    positioned above/left of the primary has a negative left/top), so the overlay is explicitly
    positioned and sized from that instead of relying on Tk's single-monitor notion of "the screen" — see
    _position_window for why that positioning goes through Win32 rather than Tk's own geometry string.
    """
    import mss
    import tkinter as tk

    with mss.mss() as sct:
        virtual_screen = sct.monitors[0]

    overlay = tk.Toplevel(parent)
    # overrideredirect (no title bar/borders) rather than "-fullscreen", which on Windows only ever
    # covers the primary monitor regardless of the geometry given below. Sized and positioned entirely
    # through _position_window (never .geometry()) — see its docstring for why mixing the two is unsafe.
    overlay.overrideredirect(True)
    _position_window(
        overlay, virtual_screen["left"], virtual_screen["top"],
        virtual_screen["width"], virtual_screen["height"],
    )
    overlay.attributes("-alpha", 0.25)
    overlay.attributes("-topmost", True)
    overlay.configure(bg="black", cursor="crosshair")
    overlay.grab_set()
    overlay.focus_force()

    canvas = tk.Canvas(overlay, bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_text(
        virtual_screen["width"] // 2,
        24,
        text="Drag to select the area to capture — Esc to cancel",
        fill="white",
        font=("Segoe UI", 12),
    )

    state: dict = {"start": None, "rect": None, "result": None}

    def on_press(event: tk.Event) -> None:
        state["start"] = (event.x, event.y)
        state["rect"] = canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#00e5ff", width=2
        )

    def on_drag(event: tk.Event) -> None:
        if state["rect"] is None:
            return
        start_x, start_y = state["start"]
        canvas.coords(state["rect"], start_x, start_y, event.x, event.y)

    def on_release(event: tk.Event) -> None:
        if state["start"] is not None:
            start_x, start_y = state["start"]
            # Canvas coords are overlay-relative, and the overlay's own top-left is the virtual
            # desktop's top-left (which can be negative) rather than (0, 0), so that offset has to be
            # added back in to get real screen-absolute coordinates for mss.
            state["result"] = _region_from_drag(
                start_x + virtual_screen["left"],
                start_y + virtual_screen["top"],
                event.x + virtual_screen["left"],
                event.y + virtual_screen["top"],
            )
        overlay.destroy()

    def on_cancel(_event: tk.Event | None = None) -> None:
        overlay.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    overlay.bind("<Escape>", on_cancel)

    overlay.wait_window()
    return state["result"]
