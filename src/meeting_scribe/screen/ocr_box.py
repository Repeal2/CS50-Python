"""The on-screen OCR box: a frame drawn around exactly the area on-screen text is read from, which the user
moves by dragging its edge, resizes by dragging its handles, and switches reading on and off with the
button attached to its top-left corner.

It replaces choosing a screen source from a dropdown (whole screen, a window, an area drawn once, an area
pinned to a window) — each of those left what was actually being read somewhere between hard to see and
invisible, and pinned areas drifted as windows re-laid themselves out. Here what's read is always exactly
what's inside the frame, and the frame is always on screen while a meeting records.

A meeting starts with the box showing but not reading, so it can be put in place first (over the Teams
captions, a shared slide, the chat) before anything is captured.

The box is one borderless, always-on-top window. Its interior is painted in a colour Windows is told to
treat as transparent (Tk's -transparentcolor), which on Windows also makes those pixels click-through —
so the call underneath stays fully usable through the box — while the frame, handles and button take
the mouse. Everything the box draws sits *outside* the area being read, so none of it ends up in the
OCR frame.

The geometry (layout, hit-testing, dragging) is plain functions over rect dicts, testable without a
display; only OcrAreaBox touches Tk, and only inside its methods, so this module imports anywhere.
"""

from __future__ import annotations

import typing
from dataclasses import dataclass

if typing.TYPE_CHECKING:
    import tkinter as tk

# Smallest area that can still hold a line of text worth reading.
MIN_AREA_WIDTH = 60
MIN_AREA_HEIGHT = 24

# The band around the area that holds the frame and handles — wide enough to grab easily.
MARGIN = 10
HANDLE = MARGIN  # a handle fills the frame band exactly, never overlapping the area being read
BAR_HEIGHT = 30
BAR_WIDTH = 190

# The handles, by the edge(s) each one moves.
HANDLE_NAMES = ("nw", "n", "ne", "e", "se", "s", "sw", "w")


@dataclass(frozen=True)
class BoxLayout:
    """Where everything goes, for the area `area` (an mss-style {left, top, width, height} screen rect).
    The window extends MARGIN beyond the area on every side, plus the button bar above it; `window` is
    that in screen coordinates, and everything else is in window coordinates as (x0, y0, x1, y1)."""

    area: dict

    @property
    def window(self) -> dict:
        width = max(self.area["width"] + 2 * MARGIN, BAR_WIDTH)
        return {
            "left": self.area["left"] - MARGIN,
            "top": self.area["top"] - MARGIN - BAR_HEIGHT,
            "width": width,
            "height": self.area["height"] + 2 * MARGIN + BAR_HEIGHT,
        }

    @property
    def inner(self) -> tuple[int, int, int, int]:
        """The area being read, in window coordinates."""
        top = BAR_HEIGHT + MARGIN
        return (MARGIN, top, MARGIN + self.area["width"], top + self.area["height"])

    @property
    def outer(self) -> tuple[int, int, int, int]:
        """The frame's outside edge, in window coordinates."""
        x0, y0, x1, y1 = self.inner
        return (x0 - MARGIN, y0 - MARGIN, x1 + MARGIN, y1 + MARGIN)

    @property
    def bar(self) -> tuple[int, int, int, int]:
        return (0, 0, BAR_WIDTH, BAR_HEIGHT)

    @property
    def toggle_button(self) -> tuple[int, int, int, int]:
        """The Start/Stop OCR button, in the bar right after the drag grip."""
        return (26, 3, BAR_WIDTH - 4, BAR_HEIGHT - 3)

    @property
    def grip(self) -> tuple[int, int, int, int]:
        return (0, 0, 24, BAR_HEIGHT)

    def handles(self) -> dict[str, tuple[int, int, int, int]]:
        """Each handle's square, centred in the frame band (outside the area being read) at the corners
        and edge midpoints."""
        x0, y0, x1, y1 = self.inner
        mid_x, mid_y = (x0 + x1) // 2, (y0 + y1) // 2
        left, right = x0 - MARGIN // 2, x1 + MARGIN // 2
        top, bottom = y0 - MARGIN // 2, y1 + MARGIN // 2
        centres = {
            "nw": (left, top), "n": (mid_x, top), "ne": (right, top), "e": (right, mid_y),
            "se": (right, bottom), "s": (mid_x, bottom), "sw": (left, bottom), "w": (left, mid_y),
        }
        half = HANDLE // 2
        return {name: (cx - half, cy - half, cx + half, cy + half) for name, (cx, cy) in centres.items()}


def _inside(x: int, y: int, rect: tuple[int, int, int, int]) -> bool:
    x0, y0, x1, y1 = rect
    return x0 <= x < x1 and y0 <= y < y1


def hit_test(layout: BoxLayout, x: int, y: int) -> str | None:
    """What a mouse press at window coordinates (x, y) grabs: "toggle" (the Start/Stop button), a
    handle name (see HANDLE_NAMES), "move" (the frame band or the grip), or None — the area itself and
    the empty space beside the bar, which are see-through and click-through anyway."""
    if _inside(x, y, layout.toggle_button):
        return "toggle"
    if _inside(x, y, layout.grip):
        return "move"
    for name, rect in layout.handles().items():
        if _inside(x, y, rect):
            return name
    if _inside(x, y, layout.outer) and not _inside(x, y, layout.inner):
        return "move"
    return None


def drag_area(area: dict, part: str, dx: int, dy: int, bounds: dict | None = None) -> dict:
    """The area after dragging `part` ("move" or a handle name) by (dx, dy) screen pixels from where it
    was when the drag began — always from the starting area, not incrementally, so rounding never
    accumulates. Resizing stops at MIN_AREA_WIDTH/HEIGHT, holding the opposite edge still. With `bounds`
    (the whole virtual screen, as an mss-style rect), the box — button bar included — is kept on
    screen, so it can't be dragged somewhere it can't be dragged back from."""
    left, top = area["left"], area["top"]
    right, bottom = left + area["width"], top + area["height"]
    if part == "move":
        left, right, top, bottom = left + dx, right + dx, top + dy, bottom + dy
    else:
        if "w" in part:
            left = min(left + dx, right - MIN_AREA_WIDTH)
        if "e" in part:
            right = max(right + dx, left + MIN_AREA_WIDTH)
        if "n" in part:
            top = min(top + dy, bottom - MIN_AREA_HEIGHT)
        if "s" in part:
            bottom = max(bottom + dy, top + MIN_AREA_HEIGHT)
    result = {"left": left, "top": top, "width": right - left, "height": bottom - top}
    return keep_on_screen(result, bounds) if bounds is not None else result


def keep_on_screen(area: dict, bounds: dict) -> dict:
    """`area` moved (never resized, unless it's bigger than the screen) so the whole box, button bar
    included, is inside `bounds`."""
    min_left = bounds["left"] + MARGIN
    min_top = bounds["top"] + MARGIN + BAR_HEIGHT
    max_right = bounds["left"] + bounds["width"] - MARGIN
    max_bottom = bounds["top"] + bounds["height"] - MARGIN
    width = min(area["width"], max_right - min_left)
    height = min(area["height"], max_bottom - min_top)
    # The button bar can be wider than a narrow box, and has to fit too.
    reach = max(width, BAR_WIDTH - 2 * MARGIN)
    left = min(max(area["left"], min_left), max_right - reach)
    top = min(max(area["top"], min_top), max_bottom - height)
    return {"left": left, "top": top, "width": width, "height": height}


def default_area(work_area: dict, window_rect: dict | None = None) -> dict:
    """Where a box that has never been placed starts: across the bottom quarter of the call window if
    there is one (where Teams puts live captions), otherwise a wide strip in the middle of the screen."""
    if window_rect is not None and window_rect["width"] >= MIN_AREA_WIDTH * 2:
        height = max(MIN_AREA_HEIGHT, window_rect["height"] // 4)
        return {
            "left": window_rect["left"] + window_rect["width"] // 10,
            "top": window_rect["top"] + window_rect["height"] - height - window_rect["height"] // 12,
            "width": window_rect["width"] * 8 // 10,
            "height": height,
        }
    width = max(MIN_AREA_WIDTH, work_area["width"] * 6 // 10)
    height = max(MIN_AREA_HEIGHT, work_area["height"] // 5)
    return {
        "left": work_area["left"] + (work_area["width"] - width) // 2,
        "top": work_area["top"] + (work_area["height"] - height) // 2,
        "width": width,
        "height": height,
    }


def overlaps(area: dict, bounds: dict) -> bool:
    """Whether any of `area` is inside `bounds` — a saved box on a monitor that's since been unplugged
    isn't, and gets put back at its default place instead."""
    return (
        area["left"] < bounds["left"] + bounds["width"]
        and bounds["left"] < area["left"] + area["width"]
        and area["top"] < bounds["top"] + bounds["height"]
        and bounds["top"] < area["top"] + area["height"]
    )


_CURSORS = {
    "move": "fleur", "toggle": "hand2",
    "nw": "size_nw_se", "se": "size_nw_se", "ne": "size_ne_sw", "sw": "size_ne_sw",
    "n": "sb_v_double_arrow", "s": "sb_v_double_arrow", "e": "sb_h_double_arrow", "w": "sb_h_double_arrow",
}


class OcrAreaBox:
    """The box itself — see the module docstring. `on_area_changed(area)` is called when a move or resize
    is let go (not on every mouse movement); `on_toggle()` when the Start/Stop button is clicked — the
    caller decides what that means and reports back with set_reading()."""

    # Painted into the interior and told to Windows as the see-through, click-through colour. Deliberately
    # a colour nothing in the frame uses.
    _SEE_THROUGH = "#ff00fe"
    _FRAME_BG = "#1f2328"
    _IDLE = "#f5a623"  # not reading yet: amber, "set me up"
    _READING = "#34c759"
    _ALERT = "#ff3b30"
    _BUTTON_BG = "#2d333b"
    _BUTTON_TEXT = "#ffffff"
    ALERT_FLASH_MS = 400

    def __init__(
        self,
        parent: "tk.Misc",
        area: dict,
        *,
        on_area_changed: typing.Callable[[dict], None],
        on_toggle: typing.Callable[[], None],
        bounds: dict | None = None,
    ):
        import tkinter as tk

        self._parent = parent
        self._area = dict(area)
        self._bounds = bounds
        self._on_area_changed = on_area_changed
        self._on_toggle = on_toggle
        self._reading = False
        self._alerting = False
        self._flash_on = False
        self._flash_after_id: str | None = None
        self._drag: tuple[str, int, int, dict] | None = None
        self._pressed: str | None = None

        self._window = tk.Toplevel(parent)
        self._window.overrideredirect(True)
        self._window.attributes("-topmost", True)
        self._window.configure(bg=self._SEE_THROUGH)
        try:
            self._window.attributes("-transparentcolor", self._SEE_THROUGH)
        except tk.TclError:
            pass  # not Windows: the interior just isn't see-through (only matters off the target platform)
        # The window's size and position are only ever set through region_picker._position_window
        # (Win32), never by Tk: without this, the canvas's own default size request would have Tk resize
        # the window behind that call's back — the Tk/OS disagreement _position_window documents.
        self._window.pack_propagate(False)
        self._canvas = tk.Canvas(self._window, bg=self._SEE_THROUGH, highlightthickness=0, bd=0)
        self._canvas.pack(fill="both", expand=True)
        self._canvas.bind("<Motion>", self._on_motion)
        self._canvas.bind("<ButtonPress-1>", self._on_press)
        self._canvas.bind("<B1-Motion>", self._on_drag)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)
        self._place()

    @property
    def area(self) -> dict:
        return dict(self._area)

    def set_reading(self, reading: bool) -> None:
        self._reading = reading
        self._draw()

    def set_area(self, area: dict) -> None:
        self._area = dict(area)
        self._place()

    def set_alert(self, active: bool) -> None:
        """Flashes the frame red — used while the microphone is hearing digital silence, so it's visible
        right where the user is looking. Safe to call every poll with the current condition."""
        if active == self._alerting:
            return
        self._alerting = active
        if active:
            self._flash_on = False
            self._flash()
        else:
            self._cancel_flash()
            self._draw()

    def close(self) -> None:
        self._cancel_flash()
        try:
            self._window.destroy()
        except Exception:
            pass

    # --- drawing -----------------------------------------------------------------------------------

    def _place(self) -> None:
        from meeting_scribe.screen.region_picker import _position_window

        window = BoxLayout(self._area).window
        _position_window(self._window, window["left"], window["top"], window["width"], window["height"])
        self._draw()

    def _accent(self) -> str:
        if self._alerting and self._flash_on:
            return self._ALERT
        return self._READING if self._reading else self._IDLE

    def _draw(self) -> None:
        layout = BoxLayout(self._area)
        canvas = self._canvas
        canvas.delete("all")
        accent = self._accent()
        x0, y0, x1, y1 = layout.outer
        ix0, iy0, ix1, iy1 = layout.inner
        # The frame band: four solid strips, leaving the area itself see-through.
        for rect in ((x0, y0, x1, iy0), (x0, iy1, x1, y1), (x0, iy0, ix0, iy1), (ix1, iy0, x1, iy1)):
            canvas.create_rectangle(*rect, fill=self._FRAME_BG, outline="")
        # A bright line hugging the area, so exactly what's read is unmistakable.
        canvas.create_rectangle(ix0 - 2, iy0 - 2, ix1 + 1, iy1 + 1, outline=accent, width=2)
        for hx0, hy0, hx1, hy1 in layout.handles().values():
            canvas.create_rectangle(hx0, hy0, hx1, hy1, fill="#ffffff", outline=accent, width=2)

        bx0, by0, bx1, by1 = layout.bar
        canvas.create_rectangle(bx0, by0, bx1, by1, fill=self._FRAME_BG, outline="")
        gx0, gy0, gx1, gy1 = layout.grip
        canvas.create_text((gx0 + gx1) // 2, (gy0 + gy1) // 2, text="⠿", fill="#c9d1d9", font=("Segoe UI", 12))
        tx0, ty0, tx1, ty1 = layout.toggle_button
        pressed = self._pressed == "toggle"
        canvas.create_rectangle(
            tx0, ty0, tx1, ty1, fill=accent if pressed else self._BUTTON_BG, outline=accent, width=2
        )
        label = "■  Stop OCR" if self._reading else "▶  Start OCR"
        canvas.create_text(
            (tx0 + tx1) // 2, (ty0 + ty1) // 2, text=label, fill=self._BUTTON_TEXT,
            font=("Segoe UI", 10, "bold"),
        )

    def _flash(self) -> None:
        self._flash_on = not self._flash_on
        self._draw()
        self._flash_after_id = self._parent.after(self.ALERT_FLASH_MS, self._flash)

    def _cancel_flash(self) -> None:
        if self._flash_after_id is not None:
            self._parent.after_cancel(self._flash_after_id)
            self._flash_after_id = None
        self._flash_on = False

    # --- mouse ---------------------------------------------------------------------------------------

    def _on_motion(self, event) -> None:
        part = hit_test(BoxLayout(self._area), event.x, event.y)
        self._canvas.configure(cursor=_CURSORS.get(part, ""))

    def _on_press(self, event) -> None:
        part = hit_test(BoxLayout(self._area), event.x, event.y)
        if part == "toggle":
            self._pressed = "toggle"
            self._draw()
        elif part is not None:
            self._drag = (part, event.x_root, event.y_root, dict(self._area))

    def _on_drag(self, event) -> None:
        if self._drag is None:
            return
        part, start_x, start_y, start_area = self._drag
        self._area = drag_area(start_area, part, event.x_root - start_x, event.y_root - start_y, self._bounds)
        self._place()

    def _on_release(self, event) -> None:
        if self._pressed == "toggle":
            self._pressed = None
            self._draw()
            # Only a release still over the button counts, like any button.
            if hit_test(BoxLayout(self._area), event.x, event.y) == "toggle":
                self._on_toggle()
            return
        if self._drag is not None:
            self._drag = None
            self._on_area_changed(dict(self._area))
