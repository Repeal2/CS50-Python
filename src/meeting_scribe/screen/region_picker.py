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
    immediately when a Toplevel is constructed, but forcing a pending-event flush before reading
    winfo_id() is the safe way to depend on that rather than an implementation detail."""
    window.update_idletasks()
    import ctypes

    ctypes.windll.user32.MoveWindow(window.winfo_id(), left, top, width, height, True)


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


def _frame_rects(rect: dict, thickness: int) -> tuple[dict, dict, dict, dict]:
    """mss-style {left, top, width, height} rects for four thin strips forming a hollow frame just
    *outside* `rect`'s bounds (an mss-style dict — RegionTarget.mss_region or WindowRegionTarget.mss_region
    both produce one) — outside, not on top of it, so the border itself never ends up inside the captured
    region and doesn't contaminate the OCR frame. Order: top, bottom, left, right.

    Returned as plain rects rather than Tk geometry strings — a geometry string's offset portion doesn't
    mean "this coordinate", full stop, once it's negative (see _position_window, which is how
    RegionOutline.reposition actually places these), so there's no correct geometry string to build for
    a strip on a monitor above or left of the primary display in the first place."""
    left, top, width, height = rect["left"], rect["top"], rect["width"], rect["height"]
    outer_width = width + 2 * thickness
    return (
        {"left": left - thickness, "top": top - thickness, "width": outer_width, "height": thickness},
        {"left": left - thickness, "top": top + height, "width": outer_width, "height": thickness},
        {"left": left - thickness, "top": top, "width": thickness, "height": height},
        {"left": left + width, "top": top, "width": thickness, "height": height},
    )


def _outline_color(normal_color: str, alert_color: str, *, alerting: bool, flash_on: bool) -> str:
    """The border color for one tick of RegionOutline.set_alert's flash — split out as a pure function
    so the on/off decision is testable without a real Tk display (see set_alert/_flash)."""
    return alert_color if alerting and flash_on else normal_color


class RegionOutline:
    """A thin, always-on-top border drawn around a capture rectangle so the user can see — for as long
    as it's the selected screen source, not just at the moment they picked it — exactly what's being
    captured. Four separate borderless windows form a hollow frame rather than one covering the whole
    region, so nothing sits on top of (or is captured as part of) the content underneath.

    Takes a plain mss-style rect rather than a RegionTarget so it also works for a WindowRegionTarget,
    whose absolute position depends on the pinned window's current bounds rather than being fixed —
    `reposition()` lets the caller re-draw the frame around wherever that window has moved to.

    `set_alert(True)` flashes the border red — used when the mic has no detectable input at all, so
    "nothing is being picked up" is visible right at the OCR capture area a user is actually looking at,
    not just as another line in the activity log they may not have scrolled to. `set_alert(False)`
    (or letting the outline get destroyed by `close()`) returns it to its normal color and stops the
    flash timer."""

    THICKNESS = 3
    COLOR = "#00e5ff"
    ALERT_COLOR = "#ff3b30"
    # How often the border toggles between its normal color and ALERT_COLOR while alerting.
    ALERT_FLASH_MS = 400

    def __init__(self, parent: "tk.Misc", rect: dict):
        import tkinter as tk

        self._parent = parent
        self._parts = [tk.Toplevel(parent) for _ in range(4)]
        for part in self._parts:
            part.overrideredirect(True)
            part.attributes("-topmost", True)
            part.configure(bg=self.COLOR)
        self.reposition(rect)
        self._alerting = False
        self._flash_on = False
        self._flash_after_id: str | None = None

    def reposition(self, rect: dict) -> None:
        # Never .geometry() on these — see _position_window's docstring for why mixing it with this call
        # is what silently broke the outline in the first place (this runs on every poll tick to keep a
        # window-pinned area's outline glued to the window as it moves, so the two would keep fighting).
        for part, frame in zip(self._parts, _frame_rects(rect, self.THICKNESS)):
            _position_window(part, frame["left"], frame["top"], frame["width"], frame["height"])

    def set_alert(self, active: bool) -> None:
        """Starts or stops the red flash. Safe to call every poll tick with the current condition —
        it's a no-op once the state already matches, so it doesn't reset the flash's phase or restart
        its timer on every call while the alert continues."""
        if active == self._alerting:
            return
        self._alerting = active
        if active:
            self._flash_on = False
            self._flash()
        else:
            self._cancel_flash()
            self._paint(self.COLOR)

    def _flash(self) -> None:
        self._flash_on = not self._flash_on
        self._paint(_outline_color(self.COLOR, self.ALERT_COLOR, alerting=True, flash_on=self._flash_on))
        self._flash_after_id = self._parent.after(self.ALERT_FLASH_MS, self._flash)

    def _cancel_flash(self) -> None:
        if self._flash_after_id is not None:
            self._parent.after_cancel(self._flash_after_id)
            self._flash_after_id = None

    def _paint(self, color: str) -> None:
        for part in self._parts:
            part.configure(bg=color)

    def close(self) -> None:
        self._cancel_flash()
        for part in self._parts:
            part.destroy()
        self._parts = []
