"""The small always-on-top "Teams meeting detected — start recording?" window shown below a Teams call
when one starts (see screen.meeting_detector for the detection and the when-to-show rules)."""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable

from meeting_scribe.screen.meeting_detector import prompt_position
from meeting_scribe.screen.region_picker import _position_window


class MeetingPrompt:
    """Borderless, topmost, and without a taskbar entry, so it sits next to the call without pulling
    focus to this app's main window. Positioned only through region_picker._position_window, never
    .geometry() — see that function for why the two can't be mixed, and for why Tk's geometry strings
    get multi-monitor positions wrong."""

    def __init__(
        self,
        parent: tk.Misc,
        meeting_name: str | None,
        on_start: Callable[[], None],
        on_dismiss: Callable[[], None],
    ):
        self._window = tk.Toplevel(parent)
        # Hidden until the first place(), so it never shows for a moment at Tk's default position.
        self._window.withdraw()
        self._shown = False
        self._window.overrideredirect(True)
        self._window.attributes("-topmost", True)

        frame = ttk.Frame(self._window, padding=10, relief="solid", borderwidth=1)
        frame.pack(fill="both", expand=True)

        heading_font = tkfont.nametofont("TkDefaultFont").copy()
        heading_font.configure(weight="bold")
        ttk.Label(frame, text="Teams meeting detected", font=heading_font).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        if meeting_name:
            ttk.Label(frame, text=meeting_name, wraplength=300).grid(
                row=1, column=0, columnspan=2, sticky="w", pady=(2, 0)
            )
        ttk.Button(frame, text="Start recording", command=on_start).grid(
            row=2, column=0, sticky="we", pady=(8, 0), padx=(0, 4)
        )
        ttk.Button(frame, text="Dismiss", command=on_dismiss).grid(row=2, column=1, sticky="we", pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)

        self._window.update_idletasks()
        self._width = self._window.winfo_reqwidth()
        self._height = self._window.winfo_reqheight()

    def place(self, window_rect: dict | None, work_area: dict) -> None:
        """Moves the prompt to sit below `window_rect` (the meeting window, or None if it can't be
        measured) within `work_area` — called on every detection poll so it follows the call window if
        it's moved."""
        x, y = prompt_position(window_rect, work_area, self._width, self._height)
        if not self._shown:
            # Shown and moved in the same callback, so Windows never paints it in between.
            self._window.deiconify()
            self._shown = True
        _position_window(self._window, x, y, self._width, self._height)

    def close(self) -> None:
        self._window.destroy()
