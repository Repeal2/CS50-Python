"""Lets the user drag out a custom rectangular capture area, and keep a lightweight always-visible
outline around it while it's selected — the freeform counterpart to window_picker's whole-window
selection, for when the thing to capture isn't cleanly one window (e.g. a corner of a shared screen, a
captions bar that isn't its own top-level window).

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

    from meeting_scribe.screen.window_picker import WindowTarget

MIN_REGION_SIZE = 8


@dataclass(frozen=True)
class RegionTarget:
    left: int
    top: int
    width: int
    height: int

    @property
    def label(self) -> str:
        return f"Custom area ({self.width}x{self.height} at {self.left},{self.top})"

    @property
    def mss_region(self) -> dict:
        return {"left": self.left, "top": self.top, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class WindowRegionTarget:
    """A custom rectangle pinned to a window instead of to fixed screen coordinates: the offset and size
    are stored as *fractions* of the window's own width/height (both 0..1) rather than fixed pixels, so
    wherever the window is and whatever size it's at when a capture runs — moved to another monitor,
    resized, or re-laid-out at a different DPI scale since this was picked — the captured rectangle
    scales and moves with it, landing in roughly the same relative spot on the window. This is a
    best-effort approximation, not exact layout tracking: it assumes whatever's inside the pinned area
    (e.g. a captions bar) moves/resizes proportionally with the window, which holds for a simple corner
    or edge crop but not for UI that Teams recenters or clamps to a fixed size regardless of window size.
    The window-relative counterpart to RegionTarget (a fixed screen rectangle) for when the wanted area
    is "a piece of a specific window" rather than either the whole window (WindowTarget) or a fixed spot
    on the screen."""

    hwnd: int
    window_title: str
    offset_left_frac: float
    offset_top_frac: float
    width_frac: float
    height_frac: float
    # The pixel size at the moment this was picked, kept only for the dropdown label — display purposes,
    # not used to resolve a capture region (mss_region always derives the actual pixel size from the
    # window's *current* dimensions, which is the whole point of storing fractions instead of pixels).
    picked_width: int
    picked_height: int

    @property
    def label(self) -> str:
        return f"{self.window_title} — custom area ({self.picked_width}x{self.picked_height})"

    def mss_region(self, window_rect: dict) -> dict:
        """Resolves to an mss-style region given the window's *current* bounds (as returned by
        window_picker.get_window_region) — call this fresh every capture cycle, not once, so a moved,
        resized, or rescaled window is reflected immediately."""
        return {
            "left": round(window_rect["left"] + self.offset_left_frac * window_rect["width"]),
            "top": round(window_rect["top"] + self.offset_top_frac * window_rect["height"]),
            "width": round(self.width_frac * window_rect["width"]),
            "height": round(self.height_frac * window_rect["height"]),
        }


def pin_region_to_window(
    region: RegionTarget, window: "WindowTarget", window_rect: dict
) -> WindowRegionTarget:
    """Converts an absolute, drag-selected RegionTarget into one pinned to `window`, using the window's
    bounds at the moment of picking (`window_rect`, from window_picker.get_window_region) to express the
    offset and size as fractions of the window's dimensions rather than fixed pixels — see
    WindowRegionTarget. The region is expected to have been drawn somewhere on/near that window, but
    nothing enforces that — an offset that ends up outside the window's current bounds just means the
    pinned area doesn't overlap the window, same as picking any other area that isn't there. Requires a
    non-empty window_rect (get_window_region already only returns one with positive width/height)."""
    return WindowRegionTarget(
        hwnd=window.hwnd,
        window_title=window.title,
        offset_left_frac=(region.left - window_rect["left"]) / window_rect["width"],
        offset_top_frac=(region.top - window_rect["top"]) / window_rect["height"],
        width_frac=region.width / window_rect["width"],
        height_frac=region.height / window_rect["height"],
        picked_width=region.width,
        picked_height=region.height,
    )


def _region_from_drag(x1: int, y1: int, x2: int, y2: int) -> RegionTarget | None:
    """Turns two dragged corner points into a RegionTarget, or None if the drag was too small to be a
    deliberate selection (e.g. a stray click)."""
    left, right = sorted((x1, x2))
    top, bottom = sorted((y1, y2))
    if right - left < MIN_REGION_SIZE or bottom - top < MIN_REGION_SIZE:
        return None
    return RegionTarget(left=left, top=top, width=right - left, height=bottom - top)


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
    positioned and sized from that instead of relying on Tk's single-monitor notion of "the screen".
    """
    import mss
    import tkinter as tk

    with mss.mss() as sct:
        virtual_screen = sct.monitors[0]

    overlay = tk.Toplevel(parent)
    # overrideredirect (no title bar/borders) rather than "-fullscreen", which on Windows only ever
    # covers the primary monitor regardless of the geometry given below.
    overlay.overrideredirect(True)
    overlay.geometry(
        f"{virtual_screen['width']}x{virtual_screen['height']}"
        f"+{virtual_screen['left']}+{virtual_screen['top']}"
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
            # added back in to get real screen-absolute coordinates for mss (and RegionOutline).
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


def _frame_geometries(rect: dict, thickness: int) -> tuple[str, str, str, str]:
    """Tk geometry strings ("WxH+X+Y") for four thin strips forming a hollow frame just *outside*
    `rect`'s bounds (an mss-style {left, top, width, height} dict — RegionTarget.mss_region or
    WindowRegionTarget.mss_region both produce one) — outside, not on top of it, so the border itself
    never ends up inside the captured region and doesn't contaminate the OCR frame. Order: top, bottom,
    left, right."""
    left, top, width, height = rect["left"], rect["top"], rect["width"], rect["height"]
    outer_width = width + 2 * thickness
    frame_top = f"{outer_width}x{thickness}+{left - thickness}+{top - thickness}"
    bottom = f"{outer_width}x{thickness}+{left - thickness}+{top + height}"
    frame_left = f"{thickness}x{height}+{left - thickness}+{top}"
    right = f"{thickness}x{height}+{left + width}+{top}"
    return frame_top, bottom, frame_left, right


class RegionOutline:
    """A thin, always-on-top border drawn around a capture rectangle so the user can see — for as long
    as it's the selected screen source, not just at the moment they picked it — exactly what's being
    captured. Four separate borderless windows form a hollow frame rather than one covering the whole
    region, so nothing sits on top of (or is captured as part of) the content underneath.

    Takes a plain mss-style rect rather than a RegionTarget so it also works for a WindowRegionTarget,
    whose absolute position depends on the pinned window's current bounds rather than being fixed —
    `reposition()` lets the caller re-draw the frame around wherever that window has moved to."""

    THICKNESS = 3
    COLOR = "#00e5ff"

    def __init__(self, parent: "tk.Misc", rect: dict):
        import tkinter as tk

        self._parts = [tk.Toplevel(parent) for _ in range(4)]
        for part in self._parts:
            part.overrideredirect(True)
            part.attributes("-topmost", True)
            part.configure(bg=self.COLOR)
        self.reposition(rect)

    def reposition(self, rect: dict) -> None:
        for part, geometry in zip(self._parts, _frame_geometries(rect, self.THICKNESS)):
            part.geometry(geometry)

    def close(self) -> None:
        for part in self._parts:
            part.destroy()
        self._parts = []
