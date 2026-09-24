"""The small always-on-top prompts shown next to a Teams call: "start recording?" when one starts, and
"stop recording?" when a recorded one ends (see screen.meeting_detector for the detection and the
when-to-show rules)."""

from __future__ import annotations

import sys
import tkinter as tk
from dataclasses import dataclass
from typing import Callable

from meeting_scribe.screen.meeting_detector import prompt_position
from meeting_scribe.screen.region_picker import _position_window, _toplevel_hwnd


@dataclass(frozen=True)
class _Palette:
    background: str
    border: str
    title: str
    detail: str
    secondary: str
    secondary_hover: str
    secondary_text: str


# Neutral surfaces close to Windows 11 / Teams' own, so the card reads as part of the desktop rather
# than as a stock Tk dialog.
_LIGHT = _Palette("#FFFFFF", "#D1D1D1", "#242424", "#616161", "#FFFFFF", "#F0F0F0", "#242424")
_DARK = _Palette("#292929", "#474747", "#FFFFFF", "#ADADAD", "#333333", "#3D3D3D", "#FFFFFF")


@dataclass(frozen=True)
class _Style:
    accent: str
    accent_hover: str
    icon: str
    icon_color: str


_START_STYLE = _Style(accent="#5B5FC7", accent_hover="#4F52B2", icon="●", icon_color="#D13438")
_STOP_STYLE = _Style(accent="#C4314B", accent_hover="#A72C40", icon="■", icon_color="#C4314B")

_PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def windows_prefers_dark_apps(winreg=None) -> bool:
    """Whether Windows is set to dark mode for apps (Settings > Personalization > Colors), so the prompt
    matches the rest of the desktop. Light off Windows or if the setting can't be read. `winreg` is
    injectable for tests."""
    if winreg is None:
        if sys.platform != "win32":
            return False
        import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PERSONALIZE_KEY) as key:
            value, _type = winreg.QueryValueEx(key, "AppsUseLightTheme")
    except OSError:
        return False
    return value == 0


def _font(size: int, *, strong: bool = False) -> tuple:
    if sys.platform == "win32":
        return ("Segoe UI Semibold" if strong else "Segoe UI", size)
    return ("TkDefaultFont", size, "bold") if strong else ("TkDefaultFont", size)


def _round_corners(window: tk.Toplevel) -> None:
    """Asks Windows 11 to draw the borderless window with rounded corners, like its own flyouts. Does
    nothing on Windows 10 or earlier (the attribute doesn't exist there) or off Windows."""
    if sys.platform != "win32":
        return
    import ctypes

    dwmwa_window_corner_preference = 33
    dwmwcp_round = ctypes.c_int(2)
    try:
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            _toplevel_hwnd(window),
            dwmwa_window_corner_preference,
            ctypes.byref(dwmwcp_round),
            ctypes.sizeof(dwmwcp_round),
        )
    except (AttributeError, OSError):
        pass


class _FlatButton(tk.Label):
    """A flat, colorable button. ttk buttons take their look from the native theme and can't be recolored
    on Windows, and tk.Button ignores most styling there too, so this is a Label that acts like one:
    hover color, hand cursor, and a click that only counts if released over the button."""

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        background: str,
        hover: str,
        foreground: str,
        font: tuple,
        border: str | None = None,
    ):
        super().__init__(
            parent, text=text, bg=background, fg=foreground, font=font, padx=14, pady=5, cursor="hand2"
        )
        if border is not None:
            self.configure(highlightthickness=1, highlightbackground=border, highlightcolor=border)
        self._command = command
        self.bind("<Enter>", lambda _e: self.configure(bg=hover))
        self.bind("<Leave>", lambda _e: self.configure(bg=background))
        self.bind("<ButtonRelease-1>", self._on_release)

    def _on_release(self, event: tk.Event) -> None:
        if 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height():
            self._command()


class PromptCard:
    """Borderless, topmost, and without a taskbar entry, so it sits next to the call without pulling
    focus to this app's main window. Positioned only through region_picker._position_window, never
    .geometry() — see that function for why the two can't be mixed, and for why Tk's geometry strings
    get multi-monitor positions wrong."""

    MIN_WIDTH = 360
    _FADE_STEPS = 8
    _FADE_STEP_MS = 15

    def __init__(
        self,
        parent: tk.Misc,
        *,
        title: str,
        detail: str,
        primary_text: str,
        on_primary: Callable[[], None],
        secondary_text: str,
        on_secondary: Callable[[], None],
        style: _Style,
    ):
        palette = _DARK if windows_prefers_dark_apps() else _LIGHT
        self._window = tk.Toplevel(parent)
        # Hidden until the first place(), so it never shows for a moment at Tk's default position.
        self._window.withdraw()
        self._shown = False
        self._window.overrideredirect(True)
        self._window.attributes("-topmost", True)
        # The window's own background shows through a 1px gap around the body as the card's border.
        self._window.configure(bg=palette.border)

        body = tk.Frame(self._window, bg=palette.background)
        body.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Frame(body, bg=style.accent, width=4).pack(side="left", fill="y")
        content = tk.Frame(body, bg=palette.background, padx=16, pady=12)
        content.pack(side="left", fill="both", expand=True)

        header = tk.Frame(content, bg=palette.background)
        header.pack(fill="x")
        tk.Label(
            header, text=style.icon, fg=style.icon_color, bg=palette.background, font=_font(10)
        ).pack(side="left")
        tk.Label(
            header, text=title, fg=palette.title, bg=palette.background, font=_font(11, strong=True)
        ).pack(side="left", padx=(6, 0))
        close = tk.Label(
            header, text="✕", fg=palette.detail, bg=palette.background, font=_font(9), cursor="hand2"
        )
        close.pack(side="right")
        close.bind("<ButtonRelease-1>", lambda _e: on_secondary())

        tk.Label(
            content,
            text=detail,
            fg=palette.detail,
            bg=palette.background,
            font=_font(9),
            wraplength=self.MIN_WIDTH - 40,
            justify="left",
            anchor="w",
        ).pack(fill="x", pady=(4, 12))

        buttons = tk.Frame(content, bg=palette.background)
        buttons.pack(fill="x")
        _FlatButton(
            buttons,
            primary_text,
            on_primary,
            background=style.accent,
            hover=style.accent_hover,
            foreground="#FFFFFF",
            font=_font(9, strong=True),
        ).pack(side="right")
        _FlatButton(
            buttons,
            secondary_text,
            on_secondary,
            background=palette.secondary,
            hover=palette.secondary_hover,
            foreground=palette.secondary_text,
            font=_font(9),
            border=palette.border,
        ).pack(side="right", padx=(0, 8))

        self._window.update_idletasks()
        self._width = max(self._window.winfo_reqwidth(), self.MIN_WIDTH)
        self._height = self._window.winfo_reqheight()
        _round_corners(self._window)

    def place(self, window_rect: dict | None, work_area: dict) -> None:
        """Moves the prompt to sit below `window_rect` (the meeting window, or None to go bottom-center
        of `work_area`) — called on every detection poll so it follows the call window if it's moved."""
        x, y = prompt_position(window_rect, work_area, self._width, self._height)
        first_show = not self._shown
        if first_show:
            # Shown and moved in the same callback, so Windows never paints it in between.
            self._window.attributes("-alpha", 0.0)
            self._window.deiconify()
            self._shown = True
        _position_window(self._window, x, y, self._width, self._height)
        if first_show:
            self._fade_in(1)

    def _fade_in(self, step: int) -> None:
        if not self._window.winfo_exists():
            return
        self._window.attributes("-alpha", step / self._FADE_STEPS)
        if step < self._FADE_STEPS:
            self._window.after(self._FADE_STEP_MS, self._fade_in, step + 1)

    def close(self) -> None:
        self._window.destroy()


def start_recording_prompt(
    parent: tk.Misc, meeting_name: str | None, on_start: Callable[[], None], on_dismiss: Callable[[], None]
) -> PromptCard:
    detail = f"Record “{meeting_name}”?" if meeting_name else "Record this call with Meeting Scribe?"
    return PromptCard(
        parent,
        title="Teams meeting detected",
        detail=detail,
        primary_text="Start recording",
        on_primary=on_start,
        secondary_text="Not now",
        on_secondary=on_dismiss,
        style=_START_STYLE,
    )


def stop_recording_prompt(
    parent: tk.Misc, recording_title: str, on_stop: Callable[[], None], on_keep: Callable[[], None]
) -> PromptCard:
    return PromptCard(
        parent,
        title="Meeting ended",
        detail=f"“{recording_title}” is still recording. Stop now to transcribe and save it.",
        primary_text="Stop recording",
        on_primary=on_stop,
        secondary_text="Keep recording",
        on_secondary=on_keep,
        style=_STOP_STYLE,
    )
