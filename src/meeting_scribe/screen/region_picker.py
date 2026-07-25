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


def _region_from_drag(x1: int, y1: int, x2: int, y2: int) -> RegionTarget | None:
    """Turns two dragged corner points into a RegionTarget, or None if the drag was too small to be a
    deliberate selection (e.g. a stray click)."""
    left, right = sorted((x1, x2))
    top, bottom = sorted((y1, y2))
    if right - left < MIN_REGION_SIZE or bottom - top < MIN_REGION_SIZE:
        return None
    return RegionTarget(left=left, top=top, width=right - left, height=bottom - top)


def pick_region_interactively(parent: "tk.Misc") -> RegionTarget | None:
    """Opens a fullscreen, mostly-transparent overlay the user drags a rectangle onto, showing the
    selection live as they drag. Blocks (via wait_window) until they release the mouse or press Escape
    to cancel. Returns None on cancel or too-small a selection."""
    import tkinter as tk

    overlay = tk.Toplevel(parent)
    overlay.attributes("-fullscreen", True)
    overlay.attributes("-alpha", 0.25)
    overlay.attributes("-topmost", True)
    overlay.configure(bg="black", cursor="crosshair")
    overlay.grab_set()
    overlay.focus_force()

    canvas = tk.Canvas(overlay, bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    canvas.create_text(
        overlay.winfo_screenwidth() // 2,
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
            # Canvas coords are overlay-relative; the overlay is fullscreen at (0, 0), so they're also
            # screen-absolute, which is what mss (and RegionOutline) expect.
            state["result"] = _region_from_drag(start_x, start_y, event.x, event.y)
        overlay.destroy()

    def on_cancel(_event: tk.Event | None = None) -> None:
        overlay.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    overlay.bind("<Escape>", on_cancel)

    overlay.wait_window()
    return state["result"]


def _frame_geometries(target: RegionTarget, thickness: int) -> tuple[str, str, str, str]:
    """Tk geometry strings ("WxH+X+Y") for four thin strips forming a hollow frame just *outside*
    `target`'s bounds — outside, not on top of it, so the border itself never ends up inside the
    captured region and doesn't contaminate the OCR frame. Order: top, bottom, left, right."""
    outer_width = target.width + 2 * thickness
    top = f"{outer_width}x{thickness}+{target.left - thickness}+{target.top - thickness}"
    bottom = f"{outer_width}x{thickness}+{target.left - thickness}+{target.top + target.height}"
    left = f"{thickness}x{target.height}+{target.left - thickness}+{target.top}"
    right = f"{thickness}x{target.height}+{target.left + target.width}+{target.top}"
    return top, bottom, left, right


class RegionOutline:
    """A thin, always-on-top border drawn around a RegionTarget so the user can see — for as long as
    it's the selected screen source, not just at the moment they picked it — exactly what's being
    captured. Four separate borderless windows form a hollow frame rather than one covering the whole
    region, so nothing sits on top of (or is captured as part of) the content underneath."""

    THICKNESS = 3
    COLOR = "#00e5ff"

    def __init__(self, parent: "tk.Misc", target: RegionTarget):
        import tkinter as tk

        self._parts = [tk.Toplevel(parent) for _ in range(4)]
        for part in self._parts:
            part.overrideredirect(True)
            part.attributes("-topmost", True)
            part.configure(bg=self.COLOR)
        for part, geometry in zip(self._parts, _frame_geometries(target, self.THICKNESS)):
            part.geometry(geometry)

    def close(self) -> None:
        for part in self._parts:
            part.destroy()
        self._parts = []
